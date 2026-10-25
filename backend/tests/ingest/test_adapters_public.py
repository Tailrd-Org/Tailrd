"""The public-sector and aggregator adapters: Job Bank Canada, USAJOBS, Adzuna, Jooble.

Job Bank is read from its public pages, so its fixtures are cut from the live
pages (backend/tests/fixtures/ingest/jobbank_*). USAJOBS, Adzuna and Jooble need
API keys nobody has yet; their fixtures are written to follow each provider's
published response shape exactly (see the notes at the top of each fixture
section), and the adapters stay disabled until the keys exist.

Nothing here waits on real-time pacing: ``paced`` swaps the HTTP helper's clock
for one that only advances when something sleeps.
"""

import datetime
import pathlib
import re

import httpx
import pytest

from backend.data import company_registry
from backend.ingest import adapters
from backend.ingest.adapters import jobbank_ca
from backend.ingest.core import http
from backend.services.ats_scraper import ATSScraper

FIXTURES = pathlib.Path(__file__).resolve().parent.parent / "fixtures" / "ingest"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def paced(monkeypatch):
    """A fake clock for ingest.core.http: sleeping advances it, nothing really
    waits. Returns the list of pauses the helper asked for."""
    clock = {"now": 1000.0}
    pauses: list[float] = []

    async def sleep(seconds):
        pauses.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(http, "_now", lambda: clock["now"])
    monkeypatch.setattr(http, "_sleep", sleep)
    monkeypatch.setattr(http, "_next_slot", {})
    monkeypatch.setattr(http, "_robots", {})
    return pauses


def mock_client(handler) -> httpx.AsyncClient:
    return http.make_client(transport=httpx.MockTransport(handler))


# --- Job Bank Canada ----------------------------------------------------------------------------

JOBBANK_ROBOTS = "User-agent: *\nCrawl-delay: 5\n"


def jobbank_article(posting_id: int, title: str = "") -> str:
    """A result item in the live markup, cut from the fixture's first article."""
    template = re.search(r'<article id="article-50575264".*?</article>',
                         fixture_text("jobbank_search_page.html"), re.S).group(0)
    article = template.replace("50575264", str(posting_id))
    return article.replace("sales consultant - retail", title or f"job number {posting_id}")


def jobbank_search_html(total: int, ids: list[int]) -> str:
    skeleton = fixture_text("jobbank_search_page.html")
    head = skeleton[:skeleton.index('<article id="article-')]
    tail = skeleton[skeleton.rindex("</article>") + len("</article>"):]
    head = head.replace("69,078", f"{total:,}")
    return head + "\n".join(jobbank_article(i) for i in ids) + tail


class FakeJobBank:
    """Answers robots.txt, search pages (a run of ``total`` ids, 25 a page, newest
    first) and posting pages; keeps every request it saw."""

    def __init__(self, total=0, postings=None, search=None):
        self.total = total
        self.postings = postings or {}
        self.search = search
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=JOBBANK_ROBOTS)
        if path == "/jobsearch/jobsearch":
            if self.search is not None:
                return self.search(request)
            page = int(request.url.params["page"])
            ids = [60_000_000 - n for n in range(self.total)][(page - 1) * 25:page * 25]
            return httpx.Response(200, text=jobbank_search_html(self.total, ids))
        posting_id = path.rsplit("/", 1)[-1]
        if path.startswith("/jobsearch/jobposting/") and posting_id in self.postings:
            status, body = self.postings[posting_id]
            return httpx.Response(status, text=body)
        return httpx.Response(410, text=fixture_text("jobbank_posting_gone.html"))

    def search_requests(self):
        return [r for r in self.requests if r.url.path == "/jobsearch/jobsearch"]


def test_job_bank_contract_constants():
    assert jobbank_ca.PLATFORM == "jobbank_ca"
    assert jobbank_ca.TIER == 2
    assert jobbank_ca.COUNTRIES == ("CA",)
    # robots.txt asks for "Crawl-delay: 5".
    assert jobbank_ca.RATE_LIMIT == 5.0
    assert jobbank_ca.REQUIRED_ENV == ()


def test_job_bank_is_discovered_enabled_and_owns_posting_urls_only():
    assert adapters.get_adapter("jobbank_ca") is jobbank_ca
    assert adapters.adapter_enabled("jobbank_ca")  # no key needed
    assert adapters.adapter_for_url("https://www.jobbank.gc.ca/jobsearch/jobposting/50575264") is jobbank_ca
    assert adapters.adapter_for_url("https://www.jobbank.gc.ca/jobsearch/jobsearch?sort=D") is not jobbank_ca


def test_search_page_is_read_into_listings():
    total, items = jobbank_ca.parse_search_page(fixture_text("jobbank_search_page.html"))
    assert total == 69078
    assert [item.posting_id for item in items] == [
        "50575264", "50575449", "50575638", "49881300", "50532380", "50575016"]

    jobs = [jobbank_ca._to_job(item, "federal-government", "Job Bank Canada") for item in items]
    first, second, posted_on_job_bank, ampersand, commission, various = jobs

    assert first.external_id == "jobbank_ca:federal-government:50575264"
    # The public posting URL: no session id, no "?source=searchresults".
    assert first.url == "https://www.jobbank.gc.ca/jobsearch/jobposting/50575264"
    assert first.detail_ref == "50575264"
    # Job Bank files titles in lower case; the feed shows them capitalised.
    assert first.title == "Sales Consultant - Retail"
    assert first.company == "Portes et fenêtres Abritek inc."
    assert first.location == "Saint-Georges, QC"
    assert first.posted_date == datetime.datetime(2026, 10, 25, tzinfo=datetime.timezone.utc)
    assert first.salary_text == "27.61-30.44 CAD hourly"
    assert first.work_type is None  # partner postings carry no on-site/remote flag
    assert first.description == "" and first.apply_url == ""

    assert second.salary_text == "CAD 23.17 hourly"
    assert posted_on_job_bank.work_type == "On Site"
    assert posted_on_job_bank.salary_text == "37-39 CAD hourly"  # "(to be negotiated)" dropped
    assert ampersand.company == "Fantasie Salon & Spa Inc."
    assert commission.work_type == "Hybrid"
    assert commission.salary_text == "60% commission per piece"  # no amount per period: kept as written
    # A title the employer cased itself stays as it is.
    assert various.title.startswith("Instrumental music teacher - private")
    assert various.location == "Various locations"


def test_search_page_parser_rejects_a_page_that_is_not_a_search():
    with pytest.raises(ValueError):
        jobbank_ca.parse_search_page("<html><body><h1>Request Rejected</h1></body></html>")
    assert jobbank_ca.parse_search_page(fixture_text("jobbank_search_empty.html")) == (0, [])


@pytest.mark.asyncio
async def test_list_jobs_pages_to_the_end_and_is_complete(paced):
    site = FakeJobBank(total=60)
    async with mock_client(site) as client:
        jobs, complete, total = await jobbank_ca.list_jobs(
            client, "federal-government", "Job Bank Canada",
            config={"params": {"fet": "3"}, "max_pages": 10})

    assert (len(jobs), complete, total) == (60, True, 60)
    assert len({job.url for job in jobs}) == 60
    pages = site.search_requests()
    assert [r.url.params["page"] for r in pages] == ["1", "2", "3"]
    assert all(r.url.params["sort"] == "D" and r.url.params["fet"] == "3" for r in pages)
    # Every request names us; the filters in the config reach the site.
    assert all(r.headers["user-agent"] == http.USER_AGENT for r in site.requests)
    # robots.txt first (the HTTP helper fetches it at its own default pace),
    # then the pages, a crawl-delay apart.
    assert site.requests[0].url.path == "/robots.txt"
    assert len(paced) == 3 and all(pause >= jobbank_ca.RATE_LIMIT - 0.001 for pause in paced[1:])


@pytest.mark.asyncio
async def test_list_jobs_stops_at_the_page_cap_and_is_incomplete(paced):
    site = FakeJobBank(total=400)
    async with mock_client(site) as client:
        jobs, complete, total = await jobbank_ca.list_jobs(
            client, "ontario", "Job Bank Canada", config={"params": {"fprov": "ON"}, "max_pages": 2})

    assert (len(jobs), complete, total) == (50, False, 400)
    assert len(site.search_requests()) == 2


@pytest.mark.asyncio
async def test_list_jobs_defaults_to_a_few_hundred_postings(paced):
    site = FakeJobBank(total=900)
    async with mock_client(site) as client:
        jobs, complete, total = await jobbank_ca.list_jobs(client, "all", "Job Bank Canada", config={})

    assert (len(jobs), complete, total) == (25 * jobbank_ca.DEFAULT_MAX_PAGES, False, 900)


@pytest.mark.asyncio
async def test_a_result_count_that_is_a_multiple_of_the_page_size_ends_on_an_empty_page(paced):
    site = FakeJobBank(total=50)
    async with mock_client(site) as client:
        jobs, complete, total = await jobbank_ca.list_jobs(client, "x", "Job Bank Canada", config={})

    assert (len(jobs), complete, total) == (50, True, 50)
    assert len(site.search_requests()) == 3  # the third page is the empty one


@pytest.mark.asyncio
async def test_an_empty_search_is_never_called_complete(paced):
    # A hiccup that serves an empty page must not read as "everything was taken down".
    site = FakeJobBank(search=lambda request: httpx.Response(200, text=fixture_text("jobbank_search_empty.html")))
    async with mock_client(site) as client:
        assert await jobbank_ca.list_jobs(client, "x", "Job Bank Canada", config={}) == ([], False, 0)


@pytest.mark.asyncio
async def test_a_first_page_that_is_not_a_search_page_raises(paced):
    site = FakeJobBank(search=lambda request: httpx.Response(200, text="<html>Request Rejected</html>"))
    async with mock_client(site) as client:
        with pytest.raises(ValueError):
            await jobbank_ca.list_jobs(client, "x", "Job Bank Canada", config={})

    blocked = FakeJobBank(search=lambda request: httpx.Response(403, text="Forbidden"))
    async with mock_client(blocked) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await jobbank_ca.list_jobs(client, "x", "Job Bank Canada", config={})


@pytest.mark.asyncio
async def test_a_later_page_that_fails_keeps_what_was_listed_as_incomplete(paced):
    def search(request):
        if request.url.params["page"] == "1":
            return httpx.Response(200, text=jobbank_search_html(80, list(range(500, 525))))
        return httpx.Response(403, text="Forbidden")

    async with mock_client(FakeJobBank(search=search)) as client:
        jobs, complete, total = await jobbank_ca.list_jobs(client, "x", "Job Bank Canada", config={})

    assert (len(jobs), complete, total) == (25, False, 80)


@pytest.mark.asyncio
async def test_the_deadline_stops_the_crawl_after_the_first_page(paced):
    site = FakeJobBank(total=100)
    async with mock_client(site) as client:
        jobs, complete, _total = await jobbank_ca.list_jobs(
            client, "x", "Job Bank Canada", config={}, deadline=0.0)

    assert (len(jobs), complete) == (25, False)
    assert len(site.search_requests()) == 1


@pytest.mark.asyncio
async def test_a_posting_the_list_shifts_onto_two_pages_is_kept_once(paced):
    def search(request):
        page = request.url.params["page"]
        ids = list(range(1000, 1025)) if page == "1" else list(range(1024, 1049))[:10]
        return httpx.Response(200, text=jobbank_search_html(34, ids))

    async with mock_client(FakeJobBank(search=search)) as client:
        jobs, complete, total = await jobbank_ca.list_jobs(client, "x", "Job Bank Canada", config={})

    assert len(jobs) == 34 and len({job.url for job in jobs}) == 34
    assert (complete, total) == (True, 34)


@pytest.mark.asyncio
async def test_a_listing_short_of_the_reported_count_is_incomplete(paced):
    # The site says 80, the pages ran out at 35: something was skipped.
    def search(request):
        ids = list(range(1000, 1025)) if request.url.params["page"] == "1" else list(range(2000, 2010))
        return httpx.Response(200, text=jobbank_search_html(80, ids))

    async with mock_client(FakeJobBank(search=search)) as client:
        jobs, complete, total = await jobbank_ca.list_jobs(client, "x", "Job Bank Canada", config={})

    assert (len(jobs), complete, total) == (35, False, 80)


@pytest.mark.asyncio
async def test_robots_txt_can_close_the_search_pages(paced):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /jobsearch/\n")
        raise AssertionError("no page may be fetched when robots.txt disallows it")

    async with mock_client(handler) as client:
        with pytest.raises(RuntimeError):
            await jobbank_ca.list_jobs(client, "x", "Job Bank Canada", config={})


@pytest.mark.asyncio
async def test_config_cannot_override_the_page_or_the_sort(paced):
    site = FakeJobBank(total=10)
    async with mock_client(site) as client:
        await jobbank_ca.list_jobs(client, "x", "Job Bank Canada",
                                   config={"params": {"page": "9", "sort": "M", "fsrc": "16"}})

    request = site.search_requests()[0]
    assert (request.url.params["page"], request.url.params["sort"], request.url.params["fsrc"]) == ("1", "D", "16")


@pytest.mark.asyncio
async def test_fetch_job_reads_the_description_and_terms_of_a_posting(paced):
    site = FakeJobBank(postings={
        "50575638": (200, fixture_text("jobbank_posting_verified.html")),
        "50575264": (200, fixture_text("jobbank_posting_external.html")),
    })
    async with mock_client(site) as client:
        verified = await jobbank_ca.fetch_job(client, "x", "50575638", config={})
        partner = await jobbank_ca.fetch_job(client, "x", "50575264", config={})
        gone = await jobbank_ca.fetch_job(client, "x", "40000000", config={})

    assert verified["description"].startswith("Education: College, CEGEP or other non-university certificate")
    assert "Tasks: Develop budget to determine cost of food" in verified["description"]
    assert verified["employment_type"] == "Permanent employment, Full time"
    assert verified["apply_url"] == ""  # posted on Job Bank: applied for there
    # A partner posting carries no description on Job Bank, but links to the
    # board it came from.
    assert partner == {
        "description": "",
        "employment_type": "Permanent employment, Full time",
        "apply_url": "https://www.jobillico.com/en/job-offer/portes-et-fenetres-abritek-inc-.eVpuAo/"
                     "conseiller-aux-ventes/17561571?ji_visitsrc=136",
    }
    assert gone == {}


@pytest.mark.asyncio
async def test_check_alive_reads_the_posting_page(paced, monkeypatch):
    live = fixture_text("jobbank_posting_verified.html")  # advertised until 2026-11-08
    site = FakeJobBank(postings={
        "50575638": (200, live),
        "50000001": (200, "<html><body>A page without a posting</body></html>"),
        "50000002": (403, "Forbidden"),
    })
    monkeypatch.setattr(jobbank_ca, "_today", lambda: datetime.date(2026, 10, 25))
    async with mock_client(site) as client:
        alive = await jobbank_ca.check_alive(client, "https://www.jobbank.gc.ca/jobsearch/jobposting/50575638")
        gone = await jobbank_ca.check_alive(client, "https://www.jobbank.gc.ca/jobsearch/jobposting/40000000")
        unreadable = await jobbank_ca.check_alive(client, "https://www.jobbank.gc.ca/jobsearch/jobposting/50000001")
        walled = await jobbank_ca.check_alive(client, "https://www.jobbank.gc.ca/jobsearch/jobposting/50000002")
        odd = await jobbank_ca.check_alive(client, "https://www.jobbank.gc.ca/jobsearch/jobsearch")
        monkeypatch.setattr(jobbank_ca, "_today", lambda: datetime.date(2026, 11, 9))
        expired = await jobbank_ca.check_alive(client, "https://www.jobbank.gc.ca/jobsearch/jobposting/50575638")

    assert (alive.verdict, alive.authoritative) == ("alive", True)
    assert (gone.verdict, gone.reason, gone.authoritative) == ("dead", "jobbank_http_410", True)
    assert (expired.verdict, expired.reason, expired.authoritative) == ("dead", "jobbank_expired", True)
    assert unreadable.verdict == "unknown" and walled.verdict == "unknown" and odd.verdict == "unknown"


@pytest.mark.asyncio
async def test_check_alive_reports_unknown_when_the_site_is_unreachable(paced):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=JOBBANK_ROBOTS)
        raise httpx.ConnectError("no route", request=request)

    async with mock_client(handler) as client:
        result = await jobbank_ca.check_alive(client, "https://www.jobbank.gc.ca/jobsearch/jobposting/50575638")

    assert result.verdict == "unknown"


@pytest.mark.asyncio
async def test_a_job_bank_board_goes_through_the_scraper(paced, monkeypatch):
    entries = [{"company_name": "Job Bank Canada: federal government", "ats_platform": "jobbank_ca",
                "board_slug": "federal-government", "enabled": True, "country": "CA", "hq_country": "CA",
                "config": {"params": {"fet": "3"}, "max_pages": 5}}]
    monkeypatch.setattr(company_registry, "_load_raw", lambda: entries)
    site = FakeJobBank(total=30)
    async with mock_client(site) as client:
        snapshot = await ATSScraper(filter_entry_level=False).scrape_board(
            client, "jobbank_ca", "federal-government", "Job Bank Canada: federal government")

    assert snapshot.complete and snapshot.total_listed == 30
    assert len(snapshot.all_urls) == 30 and len(snapshot.jobs) == 30
    assert site.search_requests()[0].url.params["fet"] == "3"

    # The default filters still apply to adapter rows, as for the built-in
    # boards; "country": "CA" in the registry keeps the location filter off.
    async with mock_client(FakeJobBank(total=30)) as client:
        filtered = await ATSScraper().scrape_board(
            client, "jobbank_ca", "federal-government", "Job Bank Canada: federal government")
    assert filtered.jobs == [] and len(filtered.all_urls) == 30
    assert set(filtered.rejected.values()) == {"level"}


@pytest.mark.network
@pytest.mark.asyncio
async def test_job_bank_live_federal_postings():
    """The real site, politely: one search page, one posting page, two liveness
    checks. Takes about half a minute because of the 5 second crawl delay."""
    async with http.make_client() as client:
        jobs, complete, total = await jobbank_ca.list_jobs(
            client, "federal-government", "Job Bank Canada",
            config={"params": {"fet": "3"}, "max_pages": 1})
        assert len(jobs) == 25 and total >= 25
        assert complete is (total <= 25)
        for job in jobs:
            # Some federal postings carry no place in the listing; the others name one.
            assert job.title and job.company
            assert re.fullmatch(r"https://www\.jobbank\.gc\.ca/jobsearch/jobposting/\d+", job.url)
            assert job.external_id == f"jobbank_ca:federal-government:{job.detail_ref}"
        assert len({job.url for job in jobs}) == 25

        detail = await jobbank_ca.fetch_job(client, "federal-government", jobs[0].detail_ref, config={})
        assert set(detail) == {"description", "employment_type", "apply_url"}

        assert (await jobbank_ca.check_alive(client, jobs[0].url)).verdict == "alive"
        # Posting ids this old have been removed; the site answers 410.
        gone = await jobbank_ca.check_alive(client, "https://www.jobbank.gc.ca/jobsearch/jobposting/40000000")
        assert (gone.verdict, gone.authoritative) == ("dead", True)
