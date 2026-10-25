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
from pathlib import Path

import httpx
import pytest

from backend.ingest import adapters
from backend.ingest.adapters import workable
from backend.ingest.core import http
from backend.services.ats_scraper import ATSScraper
from backend.services.platform_liveness import LivenessResult

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

TIER_1 = ("workable",)


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
])
def test_a_posting_url_belongs_to_its_adapter(url, platform):
    assert adapters.adapter_for_url(url).PLATFORM == platform


@pytest.mark.parametrize("url", [
    "https://apply.workable.com/financeit/",
    "https://apply.workable.com/api/v1/widget/accounts/financeit",
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


# --- Through the scraper, as the cron runs it -----------------------------------------------

BOARDS = [
    ("workable", "financeit", WORKABLE_API, "workable_widget_financeit.json", 5),
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
