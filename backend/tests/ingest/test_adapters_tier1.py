"""Tier 1 adapters (Workable, Recruitee, Teamtailor, BambooHR) against saved
payloads, plus network tests that list real boards.

The fixtures under backend/tests/fixtures/ingest are real responses cut down
(whole postings dropped, long bodies cut at a closing tag, fields no adapter
reads emptied). Jobvite has no adapter: its career sites publish no JSON, XML
or RSS feed, only HTML pages (see the Task 3 report).

Requests go through backend.ingest.core.http like production, with the
pacing sleep stubbed so a test never waits out a RATE_LIMIT.
"""

import datetime
import json
import re
from pathlib import Path

import httpx
import pytest

# ats_scraper first: finishing its import loads the adapter registry, and an
# adapter module still importing at that moment would be left out of it.
from backend.services.ats_scraper import ATSScraper  # isort: skip
from backend.ingest import adapters  # isort: skip
from backend.ingest.adapters import bamboohr, recruitee, teamtailor, workable
from backend.ingest.core import http
from backend.services.platform_liveness import LivenessResult
from backend.services.structured_extraction import detect_employment_type, parse_salary

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ingest"
UTC = datetime.timezone.utc


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json(name: str):
    return json.loads(load(name))


@pytest.fixture(autouse=True)
def no_pacing(request, monkeypatch):
    """Offline tests never wait for the per-host slot. Network tests keep the
    real pacing: they are the ones that talk to a live host."""
    if request.node.get_closest_marker("network"):
        return

    async def instant(seconds):
        pass

    monkeypatch.setattr(http, "_sleep", instant)
    monkeypatch.setattr(http, "_next_slot", {})
    monkeypatch.setattr(http, "_robots", {})


def served(routes: dict):
    """A client whose transport answers from ``routes`` (URL prefix -> an
    httpx.Response, or a function of the request) and the requests it saw."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for prefix, answer in routes.items():
            if str(request.url).startswith(prefix):
                return answer(request) if callable(answer) else answer
        return httpx.Response(404, text="Not Found")

    return http.make_client(transport=httpx.MockTransport(handler)), seen


def json_response(payload, status: int = 200) -> httpx.Response:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(status, text=body, headers={"content-type": "application/json"})


# --- The registry sees all of them ------------------------------------------------------

TIER_1 = ("workable", "recruitee", "teamtailor", "bamboohr")


@pytest.mark.parametrize("platform", TIER_1)
def test_the_registry_discovers_each_adapter_and_its_contract(platform):
    assert platform in adapters.adapter_platforms()
    module = adapters.get_adapter(platform)
    assert module.PLATFORM == platform
    assert module.TIER == 1
    assert module.COUNTRIES == ("CA", "US")
    assert module.RATE_LIMIT >= 1.0
    assert module.REQUIRED_ENV == ()
    assert adapters.adapter_enabled(platform)
    assert callable(module.list_jobs)


@pytest.mark.parametrize("url, platform", [
    ("https://apply.workable.com/financeit/j/DC11C09247/", "workable"),
    ("https://apply.workable.com/financeit/j/D3CF97088A/apply/", "workable"),
    ("https://petalmd.recruitee.com/o/medical-billing-specialist-duncan", "recruitee"),
    ("https://owlco.na.teamtailor.com/jobs/719055-part-time-data-quality-analyst", "teamtailor"),
    ("https://nordastelo.teamtailor.com/en-GB/jobs/8558815-intermediate-senior-structural-engineer",
     "teamtailor"),
    ("https://urban.bamboohr.com/careers/113", "bamboohr"),
])
def test_a_posting_url_belongs_to_its_adapter(url, platform):
    assert adapters.adapter_for_url(url).PLATFORM == platform


@pytest.mark.parametrize("url", [
    "https://apply.workable.com/financeit/",
    "https://apply.workable.com/api/v1/widget/accounts/financeit",
    "https://petalmd.recruitee.com/",
    "https://petalmd.recruitee.com/api/offers/",
    "https://owlco.na.teamtailor.com/jobs",
    "https://owlco.na.teamtailor.com/jobs.rss",
    "https://urban.bamboohr.com/careers",
    "https://urban.bamboohr.com/careers/list",
    "https://www.bamboohr.com/careers/human-resources-team",
])
def test_a_page_that_is_not_a_posting_is_nobody_s(url):
    assert adapters.adapter_for_url(url) is None


# --- Workable ---------------------------------------------------------------------------

WORKABLE_API = "https://apply.workable.com/api/v1/widget/accounts/financeit"


def workable_widget(**overrides):
    """Financeit's fixture with ``overrides`` applied to its first posting."""
    payload = load_json("workable_widget_financeit.json")
    payload["jobs"][0].update(overrides)
    return payload


@pytest.mark.asyncio
async def test_workable_parses_every_field_of_the_widget_payload():
    client, seen = served({WORKABLE_API: json_response(load("workable_widget_financeit.json"))})
    async with client:
        jobs, complete, total = await workable.list_jobs(client, "financeit", "Financeit", config={})

    assert (complete, total, len(jobs)) == (True, 5, 5)
    # One request, descriptions included, our identity on it.
    assert len(seen) == 1
    assert seen[0].url.params["details"] == "true"
    assert seen[0].headers["user-agent"] == http.USER_AGENT
    assert seen[0].headers["accept"] == "application/json"

    first = jobs[0]
    assert first.title == "Account Manager, British Columbia"
    assert first.company == "Financeit"
    assert first.url == "https://apply.workable.com/financeit/j/DC11C09247/"
    assert first.external_id == "workable:financeit:DC11C09247"
    assert first.location == "Vancouver, British Columbia, Canada"
    assert first.location_hint == ""
    assert first.posted_date == datetime.datetime(2026, 10, 23, tzinfo=UTC)
    assert first.department == "Sales"
    assert first.employment_type == "Full-time"
    assert first.work_type == "On Site"
    assert first.description.startswith("Who we are:")
    assert "<" not in first.description

    french = next(j for j in jobs if j.external_id.endswith(":2055703416"))
    assert french.title.startswith("Directeur(trice) régional(e) des ventes")
    assert french.location == "Laval, Quebec, Canada"
    assert french.description.startswith("Financeit est un fournisseur")


@pytest.mark.asyncio
async def test_workable_marks_a_telecommuting_posting_remote():
    client, _ = served({WORKABLE_API: json_response(load("workable_widget_financeit.json"))})
    async with client:
        jobs, _, _ = await workable.list_jobs(client, "financeit", "Financeit", config={})
    remote = next(j for j in jobs if j.external_id.endswith(":E6605625B2"))
    assert remote.work_type == "Remote"
    assert remote.employment_type == "Other"
    assert remote.location == "Toronto, Ontario, Canada"


@pytest.mark.asyncio
async def test_workable_reads_hybrid_from_the_title():
    payload = workable_widget(title="Account Manager (Hybrid)")
    client, _ = served({WORKABLE_API: json_response(payload)})
    async with client:
        jobs, _, _ = await workable.list_jobs(client, "financeit", "Financeit", config={})
    assert jobs[0].work_type == "Hybrid"


@pytest.mark.asyncio
async def test_workable_keeps_every_place_of_a_multi_location_posting_in_one_job():
    places = [
        {"country": "France", "countryCode": "FR", "city": "Paris", "region": "Île-de-France",
         "hidden": False},
        {"country": "Canada", "countryCode": "CA", "city": "Toronto", "region": "Ontario",
         "hidden": False},
        {"country": "Canada", "countryCode": "CA", "city": "Toronto", "region": "Ontario",
         "hidden": False},
    ]
    client, _ = served({WORKABLE_API: json_response(workable_widget(locations=places))})
    async with client:
        jobs, _, _ = await workable.list_jobs(client, "financeit", "Financeit", config={})
    assert len(jobs) == 5
    assert jobs[0].location == "Paris, Île-de-France, France; Toronto, Ontario, Canada"
    assert jobs[0].location_hint == "Toronto, Ontario, Canada"


@pytest.mark.asyncio
async def test_workable_uses_a_hidden_place_only_when_it_is_the_only_one():
    hidden = {"country": "Canada", "countryCode": "CA", "city": "Calgary", "region": "Alberta",
              "hidden": True}
    shown = {"country": "Canada", "countryCode": "CA", "city": "Toronto", "region": "Ontario",
             "hidden": False}
    client, _ = served({WORKABLE_API: json_response(workable_widget(locations=[hidden]))})
    async with client:
        only, _, _ = await workable.list_jobs(client, "financeit", "Financeit", config={})
    client, _ = served({WORKABLE_API: json_response(workable_widget(locations=[hidden, shown]))})
    async with client:
        both, _, _ = await workable.list_jobs(client, "financeit", "Financeit", config={})
    assert only[0].location == "Calgary, Alberta, Canada"
    assert both[0].location == "Toronto, Ontario, Canada"


@pytest.mark.asyncio
async def test_workable_falls_back_to_the_posting_s_own_city_fields():
    payload = workable_widget(locations=[], city="Halifax", state="Nova Scotia", country="Canada")
    client, _ = served({WORKABLE_API: json_response(payload)})
    async with client:
        jobs, _, _ = await workable.list_jobs(client, "financeit", "Financeit", config={})
    assert jobs[0].location == "Halifax, Nova Scotia, Canada"


@pytest.mark.asyncio
async def test_workable_does_not_call_a_board_complete_when_a_posting_could_not_be_read():
    payload = load_json("workable_widget_financeit.json")
    payload["jobs"].append({"title": "No shortcode", "shortcode": ""})
    client, _ = served({WORKABLE_API: json_response(payload)})
    async with client:
        jobs, complete, total = await workable.list_jobs(client, "financeit", "Financeit", config={})
    assert (len(jobs), complete, total) == (5, False, 6)


@pytest.mark.asyncio
async def test_workable_reads_an_account_with_no_postings_as_an_empty_complete_board():
    client, _ = served({WORKABLE_API: json_response({"name": "Financeit", "jobs": []})})
    async with client:
        assert await workable.list_jobs(client, "financeit", "Financeit", config={}) == ([], True, 0)


@pytest.mark.asyncio
async def test_workable_raises_for_a_missing_account_and_for_a_foreign_payload():
    client, _ = served({})  # every URL is a 404
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await workable.list_jobs(client, "nobody", "Nobody", config={})
    client, _ = served({"https://apply.workable.com/": json_response({"unexpected": True})})
    async with client:
        with pytest.raises(ValueError):
            await workable.list_jobs(client, "financeit", "Financeit", config={})


JOB_API = "https://apply.workable.com/api/v2/accounts/financeit/jobs/"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://apply.workable.com/financeit/j/DC11C09247/",
    "https://apply.workable.com/financeit/j/DC11C09247/apply/",
])
async def test_workable_check_alive_asks_the_job_api(url):
    client, seen = served({JOB_API + "DC11C09247": json_response(
        {"id": 6221088, "shortcode": "DC11C09247", "state": "published"})})
    async with client:
        result = await workable.check_alive(client, url)
    assert result == LivenessResult("alive", "workable_api_200", True)
    assert str(seen[0].url) == JOB_API + "DC11C09247"
    assert seen[0].headers["user-agent"] == http.USER_AGENT


@pytest.mark.asyncio
async def test_workable_check_alive_reads_a_404_as_gone():
    client, _ = served({JOB_API: httpx.Response(404, text="Job not found")})
    async with client:
        result = await workable.check_alive(client, "https://apply.workable.com/financeit/j/ZZZZZZZZZZ/")
    assert result == LivenessResult("dead", "workable_job_404", True)


@pytest.mark.asyncio
async def test_workable_check_alive_never_calls_a_bot_wall_or_a_closed_state_dead():
    client, _ = served({JOB_API + "DC11C09247": httpx.Response(429, text="slow down")})
    async with client:
        limited = await workable.check_alive(client, "https://apply.workable.com/financeit/j/DC11C09247/")
    client, _ = served({JOB_API: json_response({"shortcode": "DC11C09247", "state": "closed"})})
    async with client:
        closed = await workable.check_alive(client, "https://apply.workable.com/financeit/j/DC11C09247/")
    client, _ = served({JOB_API: httpx.Response(200, text="<html>not json</html>")})
    async with client:
        garbled = await workable.check_alive(client, "https://apply.workable.com/financeit/j/DC11C09247/")
    assert limited == LivenessResult("unknown", "bot_wall_429")
    assert closed == LivenessResult("unknown", "workable_state_closed")
    assert garbled == LivenessResult("unknown", "workable_api_unreadable")


@pytest.mark.asyncio
async def test_workable_check_alive_leaves_a_url_it_cannot_read_unknown():
    client, seen = served({})
    async with client:
        result = await workable.check_alive(client, "https://apply.workable.com/financeit/")
    assert result == LivenessResult("unknown", "unsupported_url")
    assert seen == []


# --- Recruitee --------------------------------------------------------------------------

RECRUITEE_API = "https://petalmd.recruitee.com/api/offers/"


def recruitee_offers(index: int = 0, **overrides):
    """Petal's fixture with ``overrides`` applied to one of its offers."""
    payload = load_json("recruitee_offers_petalmd.json")
    payload["offers"][index].update(overrides)
    return payload


async def list_recruitee(payload):
    client, _ = served({RECRUITEE_API: json_response(payload)})
    async with client:
        return await recruitee.list_jobs(client, "petalmd", "Petal", config={})


@pytest.mark.asyncio
async def test_recruitee_parses_every_field_of_the_offers_payload():
    client, seen = served({RECRUITEE_API: json_response(load("recruitee_offers_petalmd.json"))})
    async with client:
        jobs, complete, total = await recruitee.list_jobs(client, "petalmd", "Petal", config={})

    assert (complete, total, len(jobs)) == (True, 5, 5)
    assert [str(r.url) for r in seen] == [RECRUITEE_API]
    assert seen[0].headers["user-agent"] == http.USER_AGENT

    first = jobs[0]
    assert first.title == "Medical Billing Specialist (Duncan)"
    assert first.company == "Petal"
    assert first.url == "https://petalmd.recruitee.com/o/medical-billing-specialist-duncan"
    assert first.external_id == "recruitee:petalmd:2787721"
    assert first.location == "Duncan, British Columbia, Canada"
    assert first.posted_date == datetime.datetime(2026, 10, 16, 19, 29, 54, tzinfo=UTC)
    assert first.department == "Support & Billing Services"
    assert first.employment_type == "Full-time"
    assert first.work_type == "Hybrid"
    assert first.salary_text == "CAD 20 per hour"
    # The description and the requirements, as plain text.
    assert first.description.startswith("Petal is a leading Canadian healthcare orchestration")
    assert "Your profile" in first.description
    assert "<" not in first.description


@pytest.mark.asyncio
async def test_recruitee_keeps_a_remote_offer_s_places_not_its_remote_job_label():
    jobs, _, _ = await list_recruitee(load_json("recruitee_offers_petalmd.json"))
    remote = next(j for j in jobs if j.external_id.endswith(":2762047"))
    assert remote.work_type == "Remote"
    # Two places, one job; the second is North American, so it is the hint.
    assert remote.location == "Montréal, Québec, Canada; Quebec, Québec, Canada"
    assert remote.location_hint == "Quebec, Québec, Canada"
    assert remote.title == "Account Executive, SMB | SaaS Healthtech"


@pytest.mark.asyncio
async def test_recruitee_salary_text_reads_back_through_parse_salary():
    jobs, _, _ = await list_recruitee(load_json("recruitee_offers_petalmd.json"))
    by_id = {j.external_id.rsplit(":", 1)[1]: j for j in jobs}
    assert parse_salary(by_id["2787721"].salary_text) == (20, 20, "CAD", "hour")
    assert by_id["2421394"].salary_text == "25-26 CAD per hour"
    assert parse_salary(by_id["2421394"].salary_text) == (25, 26, "CAD", "hour")
    assert by_id["2762047"].salary_text == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("code, label, detected", [
    ("fulltime_permanent", "Full-time", "full_time"),
    ("parttime_permanent", "Part-time", "part_time"),
    ("internship", "Internship", "internship"),
    ("freelance", "Contract", "contract"),
    ("fulltime_temporary", "Contract", "contract"),
    ("volunteer", "Volunteer", ""),
    (None, "", ""),
])
async def test_recruitee_employment_type_uses_words_ingest_understands(code, label, detected):
    jobs, _, _ = await list_recruitee(recruitee_offers(employment_type_code=code))
    assert jobs[0].employment_type == label
    assert detect_employment_type("Analyst", "", jobs[0].employment_type) == detected


@pytest.mark.asyncio
async def test_recruitee_falls_back_to_the_offer_s_own_place_then_to_remote():
    own_place = dict(locations=[], city="Halifax", state_name="Nova Scotia", country="Canada")
    jobs, _, _ = await list_recruitee(recruitee_offers(**own_place))
    assert jobs[0].location == "Halifax, Nova Scotia, Canada"
    nowhere = dict(locations=[], city="", state_name="", country="", remote=True)
    jobs, _, _ = await list_recruitee(recruitee_offers(**nowhere))
    assert (jobs[0].location, jobs[0].work_type) == ("Remote", "Remote")


@pytest.mark.asyncio
async def test_recruitee_skips_unpublished_offers_and_flags_unreadable_ones():
    payload = load_json("recruitee_offers_petalmd.json")
    draft = dict(payload["offers"][0], id=1, status="draft")
    broken = {"title": "No id", "status": "published"}
    payload["offers"] += [draft, broken]
    jobs, complete, total = await list_recruitee(payload)
    assert len(jobs) == 5
    assert all(not j.external_id.endswith(":1") for j in jobs)
    assert (complete, total) == (False, 6)


@pytest.mark.asyncio
async def test_recruitee_strips_tracking_parameters_from_the_offer_url():
    url = "https://petalmd.recruitee.com/o/medical-billing-specialist-duncan?source=LinkedIn#apply"
    jobs, _, _ = await list_recruitee(recruitee_offers(careers_url=url))
    assert jobs[0].url == "https://petalmd.recruitee.com/o/medical-billing-specialist-duncan"


@pytest.mark.asyncio
async def test_recruitee_empty_board_missing_site_and_foreign_payload():
    assert await list_recruitee({"offers": []}) == ([], True, 0)
    client, _ = served({})
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await recruitee.list_jobs(client, "petalmd", "Petal", config={})
    with pytest.raises(ValueError):
        await list_recruitee({"unexpected": True})


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["evil.com/x#", "a b", "", "-lead"])
async def test_recruitee_refuses_a_slug_that_is_not_a_subdomain(slug):
    client, seen = served({})
    async with client:
        with pytest.raises(ValueError):
            await recruitee.list_jobs(client, slug, "Evil", config={})
    assert seen == []


OFFER_PAGE = "https://petalmd.recruitee.com/o/medical-billing-specialist-duncan"
OFFER_API = "https://petalmd.recruitee.com/api/offers/medical-billing-specialist-duncan"


@pytest.mark.asyncio
async def test_recruitee_check_alive_asks_the_offers_api():
    offer = recruitee_offers()["offers"][0]
    client, seen = served({OFFER_API: json_response({"offer": offer})})
    async with client:
        result = await recruitee.check_alive(client, OFFER_PAGE + "?utm_source=x")
    assert result == LivenessResult("alive", "recruitee_api_200", True)
    assert [str(r.url) for r in seen] == [OFFER_API]


@pytest.mark.asyncio
async def test_recruitee_check_alive_calls_an_offer_dead_only_when_the_page_is_gone_too():
    client, seen = served({OFFER_API: httpx.Response(404, text="{}"),
                           OFFER_PAGE: httpx.Response(404, text="Not Found")})
    async with client:
        gone = await recruitee.check_alive(client, OFFER_PAGE)
    assert gone == LivenessResult("dead", "recruitee_offer_404", True)
    # The page is read only after robots.txt allows it (no robots.txt allows).
    assert [str(r.url) for r in seen] == [
        OFFER_API, "https://petalmd.recruitee.com/robots.txt", OFFER_PAGE]


@pytest.mark.asyncio
async def test_recruitee_check_alive_does_not_fetch_a_page_robots_txt_disallows():
    client, seen = served({
        OFFER_API: httpx.Response(404, text="{}"),
        "https://petalmd.recruitee.com/robots.txt": httpx.Response(
            200, text="User-agent: *\nDisallow: /o/\n"),
        OFFER_PAGE: httpx.Response(404, text="Not Found"),
    })
    async with client:
        result = await recruitee.check_alive(client, OFFER_PAGE)
    assert result == LivenessResult("unknown", "robots_disallowed")
    assert OFFER_PAGE not in [str(r.url) for r in seen]


@pytest.mark.asyncio
async def test_recruitee_check_alive_leaves_a_renamed_offer_undecided():
    moved = httpx.Response(301, headers={"location": "https://petalmd.recruitee.com/o/renamed"})
    client, _ = served({OFFER_API: httpx.Response(404, text="{}"), OFFER_PAGE: moved,
                        "https://petalmd.recruitee.com/o/renamed": httpx.Response(200, text="<html>")})
    async with client:
        result = await recruitee.check_alive(client, OFFER_PAGE)
    assert result == LivenessResult("unknown", "recruitee_redirected")


@pytest.mark.asyncio
async def test_recruitee_check_alive_reads_a_bot_wall_and_odd_payloads_as_unknown():
    client, _ = served({OFFER_API: httpx.Response(403, text="blocked")})
    async with client:
        blocked = await recruitee.check_alive(client, OFFER_PAGE)
    client, _ = served({OFFER_API: json_response({"offer": {"id": 5, "status": "archived"}})})
    async with client:
        archived = await recruitee.check_alive(client, OFFER_PAGE)
    client, _ = served({})
    async with client:
        unsupported = await recruitee.check_alive(client, "https://petalmd.recruitee.com/")
    assert blocked == LivenessResult("unknown", "bot_wall_403")
    assert archived == LivenessResult("unknown", "recruitee_api_unreadable")
    assert unsupported == LivenessResult("unknown", "unsupported_url")


# --- Teamtailor -------------------------------------------------------------------------

TEAMTAILOR_FEED = "https://owlco.na.teamtailor.com/jobs.rss"
OWLCO = {"host": "owlco.na.teamtailor.com"}


def rss_response(text: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=text.encode("utf-8"),
                          headers={"content-type": "application/rss+xml; charset=utf-8"})


async def list_teamtailor(text: str, slug: str = "owlco", config: dict = OWLCO):
    client, _ = served({TEAMTAILOR_FEED: rss_response(text)})
    async with client:
        return await teamtailor.list_jobs(client, slug, "Owl.co", config=config)


def feed_of(first: int, count: int) -> str:
    """An RSS feed of ``count`` jobs numbered from ``first`` (ids 100000 + n):
    the fixture's real first item, copied with a new id, guid and title."""
    text = load("teamtailor_jobs_owlco.rss")
    item = re.search(r"<item>.*?</item>", text, re.DOTALL).group(0)
    items = [
        item.replace("719055-part-time-data-quality-analyst", f"{100000 + n}-job-{n}")
            .replace("40d7e5f3-239d-4930-a95c-c7bdc670f6f8", f"guid-{n}")
            .replace("<title>Part-time Data Quality Analyst</title>", f"<title>Job {n}</title>")
        for n in range(first, first + count)
    ]
    return text[:text.index("<item>")] + "\n".join(items) + "\n  </channel>\n</rss>\n"


def paged(total: int):
    """A route that serves a feed of ``total`` jobs the way Teamtailor does:
    ``per_page`` jobs from ``offset``, and a valid feed with none past the end."""
    def answer(request: httpx.Request) -> httpx.Response:
        offset, per_page = int(request.url.params["offset"]), int(request.url.params["per_page"])
        return rss_response(feed_of(offset, max(0, min(per_page, total - offset))))
    return answer


@pytest.mark.asyncio
async def test_teamtailor_parses_every_field_of_the_rss_feed():
    client, seen = served({TEAMTAILOR_FEED: rss_response(load("teamtailor_jobs_owlco.rss"))})
    async with client:
        jobs, complete, total = await teamtailor.list_jobs(client, "owlco", "Owl.co", config=OWLCO)

    assert (complete, total, len(jobs)) == (True, 3, 3)
    assert [str(r.url).split("?")[0] for r in seen] == [TEAMTAILOR_FEED]
    # Without a page size the feed shows only its first 100 jobs.
    assert dict(seen[0].url.params) == {"per_page": "100", "offset": "0"}
    assert seen[0].headers["user-agent"] == http.USER_AGENT

    first = jobs[0]
    assert first.title == "Part-time Data Quality Analyst"
    assert first.company == "Owl.co"
    assert first.url == "https://owlco.na.teamtailor.com/jobs/719055-part-time-data-quality-analyst"
    assert first.external_id == "teamtailor:owlco:719055"
    assert first.location == "Toronto, Canada"
    # "Tue, 13 Oct 2026 11:06:34 -0700" in UTC.
    assert first.posted_date == datetime.datetime(2026, 10, 13, 18, 6, 34, tzinfo=UTC)
    assert first.department == "Review"
    assert first.work_type == "Remote"
    assert first.employment_type == ""
    assert first.description.startswith("We’re building applied")
    assert "<" not in first.description

    hybrid = next(j for j in jobs if j.external_id.endswith(":559258"))
    assert (hybrid.work_type, hybrid.location) == ("Hybrid", "Vancouver, Canada")


@pytest.mark.asyncio
async def test_teamtailor_keeps_every_place_of_a_multi_location_job_in_one_job():
    jobs, _, _ = await list_teamtailor(load("teamtailor_jobs_owlco.rss"))
    both = next(j for j in jobs if j.external_id.endswith(":559259"))
    assert both.location == "New York, United States; Toronto, Canada"
    assert both.location_hint == "Toronto, Canada"
    assert both.work_type == "On Site"


@pytest.mark.asyncio
async def test_teamtailor_asks_the_tenant_host_the_registry_names_else_its_own_subdomain():
    text = load("teamtailor_jobs_owlco.rss")
    client, seen = served({"https://usg.example.com/jobs.rss": rss_response(text),
                           "https://owlco.teamtailor.com/jobs.rss": rss_response(text)})
    async with client:
        await teamtailor.list_jobs(client, "usg", "USG", config={"host": "usg.example.com"})
        await teamtailor.list_jobs(client, "owlco", "Owl.co", config={})
    assert [(r.url.host, r.url.path) for r in seen] == [("usg.example.com", "/jobs.rss"),
                                                        ("owlco.teamtailor.com", "/jobs.rss")]


@pytest.mark.asyncio
async def test_teamtailor_takes_the_url_the_feed_gives_even_on_the_employer_s_domain():
    text = load("teamtailor_jobs_owlco.rss").replace(
        "https://owlco.na.teamtailor.com/jobs/719055-part-time-data-quality-analyst",
        "https://careers.owl.example/en-GB/jobs/719055-part-time-data-quality-analyst?utm_source=rss#top",
    )
    jobs, _, _ = await list_teamtailor(text)
    assert jobs[0].url == "https://careers.owl.example/en-GB/jobs/719055-part-time-data-quality-analyst"
    assert jobs[0].external_id == "teamtailor:owlco:719055"


@pytest.mark.asyncio
async def test_teamtailor_places_a_fully_remote_job_with_no_place_as_remote():
    text = load("teamtailor_jobs_owlco.rss")
    start = text.index("<tt:locations>")
    stripped = text[:start] + text[text.index("</tt:locations>", start) + len("</tt:locations>"):]
    jobs, _, _ = await list_teamtailor(stripped)
    assert (jobs[0].location, jobs[0].work_type) == ("Remote", "Remote")
    assert jobs[0].location_hint == ""


@pytest.mark.asyncio
async def test_teamtailor_does_not_call_a_board_complete_when_an_item_could_not_be_read():
    text = load("teamtailor_jobs_owlco.rss").replace(
        "<link>https://owlco.na.teamtailor.com/jobs/559258-enterprise-account-executive</link>",
        "<link></link>",
    )
    jobs, complete, total = await list_teamtailor(text)
    assert (len(jobs), complete, total) == (2, False, 3)


@pytest.mark.asyncio
async def test_teamtailor_falls_back_to_the_guid_when_the_link_has_no_job_number():
    text = load("teamtailor_jobs_owlco.rss").replace(
        "https://owlco.na.teamtailor.com/jobs/719055-part-time-data-quality-analyst",
        "https://owlco.na.teamtailor.com/jobs/part-time-data-quality-analyst",
    )
    jobs, _, _ = await list_teamtailor(text)
    assert jobs[0].external_id == "teamtailor:owlco:40d7e5f3-239d-4930-a95c-c7bdc670f6f8"


@pytest.mark.asyncio
async def test_teamtailor_pages_through_a_feed_longer_than_one_page():
    client, seen = served({TEAMTAILOR_FEED: paged(140)})  # a full page of 100, then 40
    async with client:
        jobs, complete, total = await teamtailor.list_jobs(client, "owlco", "Owl.co", config=OWLCO)

    assert (len(jobs), complete, total) == (140, True, 140)
    assert [dict(r.url.params) for r in seen] == [
        {"per_page": "100", "offset": "0"}, {"per_page": "100", "offset": "100"}]
    assert len({job.external_id for job in jobs}) == 140
    assert (jobs[0].external_id, jobs[-1].external_id) == ("teamtailor:owlco:100000",
                                                           "teamtailor:owlco:100139")


@pytest.mark.asyncio
async def test_teamtailor_asks_for_one_more_page_after_an_exactly_full_one():
    client, seen = served({TEAMTAILOR_FEED: paged(100)})
    async with client:
        jobs, complete, total = await teamtailor.list_jobs(client, "owlco", "Owl.co", config=OWLCO)
    # The second page is empty: a full page alone never proves the feed ended.
    assert (len(jobs), complete, total) == (100, True, 100)
    assert [r.url.params["offset"] for r in seen] == ["0", "100"]


@pytest.mark.asyncio
async def test_teamtailor_reports_a_feed_that_never_ends_as_incomplete():
    client, seen = served({TEAMTAILOR_FEED: paged(10 ** 9)})
    async with client:
        jobs, complete, total = await teamtailor.list_jobs(client, "owlco", "Owl.co", config=OWLCO)
    cap = teamtailor.MAX_PAGES * teamtailor.PAGE_SIZE
    assert (len(jobs), complete, total) == (cap, False, cap)
    assert len(seen) == teamtailor.MAX_PAGES


@pytest.mark.asyncio
async def test_teamtailor_stops_paging_at_the_deadline_and_says_so(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(teamtailor, "_now", lambda: clock[0])
    feed = paged(350)

    def slow_pages(request):
        clock[0] += 5.0  # every page takes five seconds
        return feed(request)

    client, seen = served({TEAMTAILOR_FEED: slow_pages})
    async with client:
        jobs, complete, total = await teamtailor.list_jobs(
            client, "owlco", "Owl.co", config=OWLCO, deadline=1007.0)
    # t=1000 and t=1005 are before the deadline, so two pages are read; at
    # t=1010 the walk stops with 150 jobs still unlisted.
    assert len(seen) == 2
    assert (len(jobs), complete, total) == (200, False, 200)


@pytest.mark.asyncio
async def test_teamtailor_always_reads_the_first_page_even_past_the_deadline(monkeypatch):
    monkeypatch.setattr(teamtailor, "_now", lambda: 5000.0)
    client, seen = served({TEAMTAILOR_FEED: paged(3)})
    async with client:
        short = await teamtailor.list_jobs(client, "owlco", "Owl.co", config=OWLCO, deadline=1.0)
    # A feed that ends on its first page is whole, whatever the clock says.
    assert (len(short[0]), short[1], short[2], len(seen)) == (3, True, 3, 1)

    client, seen = served({TEAMTAILOR_FEED: paged(300)})
    async with client:
        cut = await teamtailor.list_jobs(client, "owlco", "Owl.co", config=OWLCO, deadline=1.0)
    assert (len(cut[0]), cut[1], cut[2], len(seen)) == (100, False, 100, 1)


@pytest.mark.asyncio
async def test_teamtailor_calls_a_walk_incomplete_when_a_job_repeats_across_pages():
    def shifted(request):
        # A job published between the two requests pushes the last job of page
        # one (number 99) onto page two.
        return rss_response(feed_of(0, 100) if request.url.params["offset"] == "0"
                            else feed_of(99, 20))

    client, _ = served({TEAMTAILOR_FEED: shifted})
    async with client:
        jobs, complete, total = await teamtailor.list_jobs(client, "owlco", "Owl.co", config=OWLCO)
    assert (len(jobs), complete, total) == (119, False, 120)
    assert len({job.external_id for job in jobs}) == 119


@pytest.mark.asyncio
async def test_teamtailor_empty_feed_missing_tenant_and_foreign_documents():
    channel = '<?xml version="1.0"?><rss version="2.0"><channel><title>Owl</title></channel></rss>'
    assert await list_teamtailor(channel) == ([], True, 0)
    client, _ = served({})
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await teamtailor.list_jobs(client, "owlco", "Owl.co", config=OWLCO)
    with pytest.raises(ValueError):
        await list_teamtailor("<html><body>Welcome to our career site</body></html>")
    with pytest.raises(ValueError):
        await list_teamtailor("<rss version='2.0'><notchannel/></rss>")


@pytest.mark.asyncio
async def test_teamtailor_refuses_a_feed_that_declares_entities():
    bomb = ('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa">]>'
            '<rss version="2.0"><channel><title>&a;</title></channel></rss>')
    with pytest.raises(ValueError):
        await list_teamtailor(bomb)


@pytest.mark.asyncio
@pytest.mark.parametrize("slug, config", [
    ("evil.com/x#", {}), ("a b", {}), ("", {}), ("owlco", {"host": "evil.com/x"}),
    ("owlco", {"host": "localhost"}), ("owlco", {"host": "a b.example.com"}),
])
async def test_teamtailor_refuses_a_slug_or_host_that_is_not_a_hostname(slug, config):
    client, seen = served({})
    async with client:
        with pytest.raises(ValueError):
            await teamtailor.list_jobs(client, slug, "Evil", config=config)
    assert seen == []


# --- BambooHR ---------------------------------------------------------------------------

CARFAX_LIST = "https://carfaxcanada.bamboohr.com/careers/list"
URBAN_DETAIL = "https://urban.bamboohr.com/careers/113/detail"


def carfax_list(**overrides):
    """Carfax Canada's fixture with ``overrides`` applied to its first posting."""
    payload = load_json("bamboohr_list_carfaxcanada.json")
    payload["result"][0].update(overrides)
    return payload


async def list_carfax(payload):
    client, _ = served({CARFAX_LIST: json_response(payload)})
    async with client:
        return await bamboohr.list_jobs(client, "carfaxcanada", "Carfax Canada", config={})


@pytest.mark.asyncio
async def test_bamboohr_parses_every_field_of_the_careers_list():
    client, seen = served({CARFAX_LIST: json_response(load("bamboohr_list_carfaxcanada.json"))})
    async with client:
        jobs, complete, total = await bamboohr.list_jobs(
            client, "carfaxcanada", "Carfax Canada", config={})

    assert (complete, total, len(jobs)) == (True, 2, 2)
    assert [str(r.url) for r in seen] == [CARFAX_LIST]
    assert seen[0].headers["user-agent"] == http.USER_AGENT
    assert seen[0].headers["accept"] == "application/json"

    first = jobs[0]
    assert first.title == "Manager - Revenue Operations & Analytics"
    assert first.company == "Carfax Canada"
    assert first.url == "https://carfaxcanada.bamboohr.com/careers/82"
    assert first.external_id == "bamboohr:carfaxcanada:82"
    assert first.location == "London, Ontario"
    assert first.department == "Automotive Sales"
    assert first.employment_type == "Full-Time"
    assert first.work_type == "Hybrid"  # locationType "2"
    # The list has neither, so the ingest fetches the detail for the description.
    assert first.description == ""
    assert first.posted_date is None
    assert first.detail_ref == "82"


@pytest.mark.asyncio
async def test_bamboohr_reads_remote_postings_with_no_place_and_pads_in_titles():
    client, _ = served({"https://flyio.bamboohr.com/careers/list":
                        json_response(load("bamboohr_list_flyio.json"))})
    async with client:
        jobs, complete, total = await bamboohr.list_jobs(client, "flyio", "Fly.io", config={})
    # The fixture keeps three of the tenant's ten postings: a partial list.
    assert (len(jobs), complete, total) == (3, False, 10)
    remote, hybrid, vp = jobs
    assert (remote.location, remote.work_type) == ("Remote", "Remote")
    assert remote.employment_type == "US Employee"
    assert hybrid.title == "Executive Assistant to the CEO"  # padded in the source
    assert (hybrid.location, hybrid.work_type) == ("chicago, Illinois", "Hybrid")
    assert (vp.title, vp.location) == ("VP Finance", "Remote")


@pytest.mark.asyncio
async def test_bamboohr_reads_on_site_and_the_is_remote_flag():
    jobs, _, _ = await list_carfax(carfax_list(locationType="0"))
    assert jobs[0].work_type == "On Site"
    jobs, _, _ = await list_carfax(carfax_list(locationType="0", isRemote=True))
    assert jobs[0].work_type == "Remote"


@pytest.mark.asyncio
async def test_bamboohr_adds_the_ats_location_s_country_when_the_tenant_sets_one():
    ats = {"country": "Canada", "state": None, "province": "Ontario", "city": "Ottawa"}
    jobs, _, _ = await list_carfax(carfax_list(location={"city": None, "state": None}, atsLocation=ats))
    assert jobs[0].location == "Ottawa, Ontario, Canada"


@pytest.mark.asyncio
async def test_bamboohr_does_not_call_a_board_complete_when_a_posting_could_not_be_read():
    payload = load_json("bamboohr_list_carfaxcanada.json")
    payload["result"].append({"id": "99"})
    payload["meta"]["totalCount"] = 3
    jobs, complete, total = await list_carfax(payload)
    assert (len(jobs), complete, total) == (2, False, 3)


@pytest.mark.asyncio
async def test_bamboohr_empty_board_missing_tenant_and_foreign_payload():
    assert await list_carfax({"meta": {"totalCount": 0}, "result": []}) == ([], True, 0)
    client, _ = served({})
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await bamboohr.list_jobs(client, "carfaxcanada", "Carfax Canada", config={})
    with pytest.raises(ValueError):
        await list_carfax({"unexpected": True})


@pytest.mark.asyncio
async def test_bamboohr_raises_for_a_tenant_whose_list_redirects_to_the_home_page():
    # What a tenant without a careers page (or one that does not exist) answers.
    home = httpx.Response(200, text="<!DOCTYPE html><title>BambooHR</title>",
                          headers={"content-type": "text/html; charset=utf-8"})
    client, _ = served({CARFAX_LIST: httpx.Response(302, headers={"location": "https://www.bamboohr.com"}),
                        "https://www.bamboohr.com": home})
    async with client:
        with pytest.raises(ValueError):
            await bamboohr.list_jobs(client, "carfaxcanada", "Carfax Canada", config={})


@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["evil.com/x#", "a b", "", "-lead"])
async def test_bamboohr_refuses_a_slug_that_is_not_a_subdomain(slug):
    client, seen = served({})
    async with client:
        with pytest.raises(ValueError):
            await bamboohr.list_jobs(client, slug, "Evil", config={})
        assert await bamboohr.fetch_job(client, "carfaxcanada", "", config={}) == {}
        with pytest.raises(ValueError):
            await bamboohr.fetch_job(client, slug, "1", config={})
    assert seen == []


@pytest.mark.asyncio
async def test_bamboohr_fetch_job_returns_the_description_and_commitment():
    client, seen = served({URBAN_DETAIL: json_response(load("bamboohr_detail_urban_113.json"))})
    async with client:
        detail = await bamboohr.fetch_job(client, "urban", "113", config={})
    assert str(seen[0].url) == URBAN_DETAIL
    assert detail["employment_type"] == "Full-Time"
    assert detail["description"].startswith("Urban Infraconstruction is looking for a")
    assert "Senior Estimator" in detail["description"]
    assert "<" not in detail["description"]


@pytest.mark.asyncio
async def test_bamboohr_fetch_job_raises_for_a_gone_posting_and_a_foreign_payload():
    client, _ = served({URBAN_DETAIL: json_response({"type": "not_found"}, status=404)})
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await bamboohr.fetch_job(client, "urban", "113", config={})
    client, _ = served({URBAN_DETAIL: json_response({"result": {}})})
    async with client:
        with pytest.raises(ValueError):
            await bamboohr.fetch_job(client, "urban", "113", config={})


@pytest.mark.asyncio
async def test_bamboohr_check_alive_asks_the_detail_endpoint():
    client, seen = served({URBAN_DETAIL: json_response(load("bamboohr_detail_urban_113.json"))})
    async with client:
        result = await bamboohr.check_alive(client, "https://urban.bamboohr.com/careers/113")
    assert result == LivenessResult("alive", "bamboohr_api_200", True)
    assert str(seen[0].url) == URBAN_DETAIL
    assert seen[0].headers["user-agent"] == http.USER_AGENT


@pytest.mark.asyncio
async def test_bamboohr_check_alive_reads_the_json_404_as_gone():
    gone = {"type": "not_found", "title": "Resource not found.", "meta": []}
    client, _ = served({"https://urban.bamboohr.com/careers/999999/detail":
                        json_response(gone, status=404)})
    async with client:
        result = await bamboohr.check_alive(client, "https://urban.bamboohr.com/careers/999999")
    assert result == LivenessResult("dead", "bamboohr_not_found", True)


@pytest.mark.asyncio
async def test_bamboohr_check_alive_never_calls_anything_else_dead():
    detail = "https://urban.bamboohr.com/careers/"
    html = httpx.Response(200, text="<html>BambooHR</html>", headers={"content-type": "text/html"})
    filled = load_json("bamboohr_detail_urban_113.json")
    filled["result"]["jobOpening"]["jobOpeningStatus"] = "Filled"
    answers = {
        "1": httpx.Response(404, text="not json"),   # a 404 that is not the posting's own
        "2": html,                                   # a tenant that redirects home
        "3": httpx.Response(403, text="blocked"),
        "4": json_response(filled),
    }
    results = {}
    for job_id, answer in answers.items():
        client, _ = served({f"{detail}{job_id}/detail": answer})
        async with client:
            results[job_id] = await bamboohr.check_alive(client, f"{detail}{job_id}")
    assert results == {
        "1": LivenessResult("unknown", "http_404"),
        "2": LivenessResult("unknown", "bamboohr_api_unreadable"),
        "3": LivenessResult("unknown", "bot_wall_403"),
        "4": LivenessResult("unknown", "bamboohr_status_filled"),
    }
    client, seen = served({})
    async with client:
        assert await bamboohr.check_alive(client, "https://urban.bamboohr.com/careers/list") \
            == LivenessResult("unknown", "unsupported_url")
    assert seen == []


# --- check_alive when the connection fails (Teamtailor has no check_alive) ----------------

LIVENESS_URLS = [
    (workable, "https://apply.workable.com/financeit/j/DC11C09247/"),
    (recruitee, OFFER_PAGE),
    (bamboohr, "https://urban.bamboohr.com/careers/113"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("module, url", LIVENESS_URLS, ids=[m.PLATFORM for m, _ in LIVENESS_URLS])
@pytest.mark.parametrize("error, reason", [
    (httpx.ReadTimeout("slow"), "timeout"),
    (httpx.ConnectTimeout("slow"), "timeout"),
    (httpx.ConnectError("refused"), "network_error"),
    (httpx.ReadError("reset"), "network_error"),
], ids=["read-timeout", "connect-timeout", "connect-error", "read-error"])
async def test_check_alive_answers_unknown_when_the_connection_fails(module, url, error, reason):
    def drop(request: httpx.Request) -> httpx.Response:
        raise error

    client = http.make_client(transport=httpx.MockTransport(drop))
    async with client:
        result = await module.check_alive(client, url)
    # The same reasons platform_liveness gives its own timeouts and network errors.
    assert result == LivenessResult("unknown", reason)


@pytest.mark.asyncio
async def test_recruitee_check_alive_does_not_call_an_offer_dead_when_the_confirming_page_times_out():
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    client, _ = served({OFFER_API: httpx.Response(404, text="{}"), OFFER_PAGE: slow})
    async with client:
        result = await recruitee.check_alive(client, OFFER_PAGE)
    assert result == LivenessResult("unknown", "timeout")


# --- Through the scraper, as the cron runs it -----------------------------------------------

BOARDS = [
    ("workable", "financeit", WORKABLE_API, "workable_widget_financeit.json", 5),
    ("recruitee", "petalmd", RECRUITEE_API, "recruitee_offers_petalmd.json", 5),
    # No registry entry names a host, so the feed is asked at <slug>.teamtailor.com.
    ("teamtailor", "owlco", "https://owlco.teamtailor.com/jobs.rss", "teamtailor_jobs_owlco.rss", 3),
    ("bamboohr", "carfaxcanada", CARFAX_LIST, "bamboohr_list_carfaxcanada.json", 2),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform, slug, api, fixture, count", BOARDS)
async def test_scrape_board_lists_each_platform_through_its_adapter(platform, slug, api, fixture, count):
    client, _ = served({api: json_response(load(fixture)) if fixture.endswith(".json")
                        else httpx.Response(200, text=load(fixture))})
    async with client:
        snapshot = await ATSScraper(filter_entry_level=False).scrape_board(
            client, platform, slug, "Acme")
    assert snapshot.board_key == f"{platform}:{slug}"
    assert snapshot.complete
    assert snapshot.total_listed == count
    assert len(snapshot.all_urls) == count
    assert len(snapshot.jobs) == count
    assert all(job.external_id.startswith(f"{platform}:{slug}:") for job in snapshot.jobs)


# --- Network: real boards ------------------------------------------------------------------

@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["financeit", "veritree", "findevcanada"])
async def test_network_workable_lists_a_real_board(slug):
    async with http.make_client() as client:
        jobs, complete, total = await workable.list_jobs(client, slug, slug, config={})
        assert jobs and complete and total == len(jobs)
        job = jobs[0]
        assert job.title and job.description
        assert job.url.startswith(f"https://apply.workable.com/{slug}/j/")
        assert job.external_id.startswith(f"workable:{slug}:")
        if slug == "financeit":
            assert (await workable.check_alive(client, job.url)).verdict == "alive"
            gone = await workable.check_alive(client, f"https://apply.workable.com/{slug}/j/ZZZZZZZZZZ/")
            assert gone.verdict == "dead"


@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["petalmd", "timedoctor", "squeezestudio"])
async def test_network_recruitee_lists_a_real_board(slug):
    async with http.make_client() as client:
        jobs, complete, total = await recruitee.list_jobs(client, slug, slug, config={})
        assert jobs and complete and total == len(jobs)
        job = jobs[0]
        assert job.title and job.description
        # An offer lives on the careers site's own domain when it has one
        # (Time Doctor's is timedoctor.careers).
        assert job.url.startswith("https://") and "/o/" in job.url
        assert job.external_id.startswith(f"recruitee:{slug}:")
        if slug == "petalmd":
            assert (await recruitee.check_alive(client, job.url)).verdict == "alive"
            gone = await recruitee.check_alive(client, f"https://{slug}.recruitee.com/o/no-such-offer-zzz")
            assert gone.verdict == "dead"


@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("slug, config", [
    ("owlco", {"host": "owlco.na.teamtailor.com"}),
    ("usgnorthamerica", {}),
    ("nordastelo", {}),
])
async def test_network_teamtailor_lists_a_real_board(slug, config):
    async with http.make_client() as client:
        jobs, complete, total = await teamtailor.list_jobs(client, slug, slug, config=config)
    assert jobs and complete and total == len(jobs)
    job = jobs[0]
    assert job.title and job.description and job.posted_date
    assert "/jobs/" in job.url and job.url.startswith("https://")
    assert job.external_id.startswith(f"teamtailor:{slug}:")


@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("slug", ["urban", "carfaxcanada", "calgaryscope"])
async def test_network_bamboohr_lists_a_real_board(slug):
    async with http.make_client() as client:
        jobs, complete, total = await bamboohr.list_jobs(client, slug, slug, config={})
        assert jobs and complete and total == len(jobs)
        job = jobs[0]
        assert job.title and job.detail_ref
        assert job.url == f"https://{slug}.bamboohr.com/careers/{job.detail_ref}"
        assert job.external_id == f"bamboohr:{slug}:{job.detail_ref}"
        if slug == "urban":
            detail = await bamboohr.fetch_job(client, slug, job.detail_ref, config={})
            assert detail["description"]
            assert (await bamboohr.check_alive(client, job.url)).verdict == "alive"
            gone = await bamboohr.check_alive(client, f"https://{slug}.bamboohr.com/careers/999999")
            assert gone.verdict == "dead"
