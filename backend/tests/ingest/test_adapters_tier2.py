"""Tier 2 adapters (scraper v2 Task 4): Oracle Cloud Recruiting, iCIMS, Taleo,
SuccessFactors.

Each adapter is tested offline against saved real responses (trimmed, in
backend/tests/fixtures/ingest/) served through httpx.MockTransport, with a fake
clock standing in for the one-request-a-second pacing so nothing is slept. The
``network`` tests list real boards and run only with RUN_NETWORK_TESTS=1.
"""

import asyncio
import json
import pathlib
import re
import time

import httpx
import pytest

from backend.ingest import adapters
from backend.ingest.adapters import icims, jibe, oracle_hcm, successfactors, taleo
from backend.ingest.core import http
from backend.services.ats_scraper import ATSScraper, entry_tier
from backend.services.platform_liveness import ALIVE, DEAD, UNKNOWN

UA = "TailrdBot/2.0 (+https://www.tailrd.ca)"
FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "ingest"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeClock:
    """Stands in for http._now and http._sleep: every wait is recorded and none
    is slept, so a test of three pages to one host runs instantly."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(http, "_now", fake.time)
    monkeypatch.setattr(http, "_sleep", fake.sleep)
    monkeypatch.setattr(http, "_next_slot", {})
    monkeypatch.setattr(http, "_robots", {})
    return fake


def client_for(handler) -> httpx.AsyncClient:
    return http.make_client(transport=httpx.MockTransport(handler))


# =============================================================================
# Oracle Cloud Recruiting
# =============================================================================

DTCC = {"host": "ebxr.fa.us2.oraclecloud.com", "site": "CX_1"}
WMATA = {"host": "ibpbjb.fa.ocs.oraclecloud.com", "site": "CX_2011"}
VANDERBILT = {"host": "ecsr.fa.us2.oraclecloud.com", "site": "CX_1"}
JPMORGAN = {"host": "jpmc.fa.oraclecloud.com", "site": "CX_1001"}


class OracleServer:
    """The finder, answering like the live API: the requested slice of
    ``requisitions`` under a count. ``totals`` scripts the count per request
    (the last repeats), for a board that changes while it is read."""

    def __init__(self, requisitions, *, total=None, totals=None, template="oracle_hcm_list_dtcc.json"):
        self.requisitions = list(requisitions)
        self.total = len(self.requisitions) if total is None else total
        self.totals = list(totals) if totals else None
        self.template = template
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        offset = int(re.search(r"offset=(\d+)", url).group(1))
        limit = int(re.search(r"limit=(\d+)", url).group(1))
        if self.totals:
            total = self.totals.pop(0) if len(self.totals) > 1 else self.totals[0]
        else:
            total = self.total
        body = fixture(self.template)
        body["items"][0].update(TotalJobsCount=total, Offset=offset, Limit=limit,
                                requisitionList=self.requisitions[offset:offset + limit])
        return httpx.Response(200, json=body)

    def offsets(self) -> list[int]:
        return [int(re.search(r"offset=(\d+)", str(r.url)).group(1)) for r in self.requests]


def dtcc_requisitions() -> list[dict]:
    return fixture("oracle_hcm_list_dtcc.json")["items"][0]["requisitionList"]


async def oracle_list(server, config=DTCC, **kwargs):
    async with client_for(server) as client:
        return await oracle_hcm.list_jobs(client, "dtcc", "DTCC", config=config, **kwargs)


def test_oracle_is_registered_and_owns_its_posting_urls():
    assert adapters.get_adapter("oracle_hcm") is oracle_hcm
    assert adapters.adapter_enabled("oracle_hcm")
    assert (oracle_hcm.PLATFORM, oracle_hcm.TIER, oracle_hcm.RATE_LIMIT) == ("oracle_hcm", 2, 1.0)
    for url in (
        "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210587260",
        "https://fa-etnv-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/healthpartners/job/1",
    ):
        assert adapters.adapter_for_url(url) is oracle_hcm
    assert adapters.adapter_for_url("https://example.com/hcmUI/CandidateExperience/en/sites/CX/job/1") is None
    assert adapters.adapter_for_url("https://careers.example.com/jobs/1") is None


@pytest.mark.asyncio
async def test_oracle_maps_a_dtcc_board(clock):
    jobs, complete, total = await oracle_list(OracleServer(dtcc_requisitions()))
    assert (len(jobs), complete, total) == (5, True, 5)
    by_id = {job.detail_ref: job for job in jobs}
    brooklyn = by_id["214861"]
    assert brooklyn.title == "Data Center Facilities Engineering Senior Associate"
    assert brooklyn.company == "DTCC"
    assert brooklyn.location == "Brooklyn, NY, United States"
    assert brooklyn.url == "https://ebxr.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/214861"
    assert brooklyn.external_id == "oracle_hcm:dtcc:214861"
    assert (brooklyn.posted_date.year, brooklyn.posted_date.month, brooklyn.posted_date.day) == (2026, 10, 23)
    assert brooklyn.posted_date.utcoffset().total_seconds() == 0
    assert brooklyn.work_type == "On Site"
    assert brooklyn.description == ""  # the list only has a snippet; fetch_job reads the text
    assert [job.external_id for job in jobs] == [f"oracle_hcm:dtcc:{job.detail_ref}" for job in jobs]
    assert len({job.url for job in jobs}) == 5


@pytest.mark.asyncio
async def test_oracle_a_london_posting_also_open_in_tampa_is_north_american(clock):
    jobs, _, _ = await oracle_list(OracleServer(dtcc_requisitions()))
    by_id = {job.detail_ref: job for job in jobs}
    london = by_id["213969"]
    assert (london.location, london.location_hint) == ("LONDON, United Kingdom", "Tampa, FL, United States")
    hyderabad = by_id["215016"]
    assert (hyderabad.location, hyderabad.location_hint) == ("Hyderabad, India", "")
    # Chennai is the other Hyderabad posting's second place: nothing North American.
    assert by_id["211966"].location_hint == ""
    # The scraper's own location filter, which reads the hint, agrees.
    scraper = ATSScraper(filter_entry_level=False)
    assert scraper.rejection(london) is None
    assert scraper.rejection(by_id["214861"]) is None
    assert scraper.rejection(hyderabad) == "location"


@pytest.mark.asyncio
async def test_oracle_reads_workplace_type_department_and_other_places(clock):
    requisitions = fixture("oracle_hcm_list_wmata.json")["items"][0]["requisitionList"]
    jobs, _, _ = await oracle_list(OracleServer(requisitions, template="oracle_hcm_list_wmata.json"),
                                   config=WMATA)
    by_id = {job.detail_ref: job for job in jobs}
    assert by_id["326"].work_type == "Hybrid"
    assert by_id["304"].work_type == "On Site"
    assert by_id["326"].department == "85400 Enterprise Applications"
    assert by_id["326"].location_hint == "VA, United States; MD, United States"
    # The commitment words feed the same entry-level read as every other board.
    assert entry_tier(by_id["326"].title, by_id["326"].department, by_id["326"].employment_type)


@pytest.mark.asyncio
async def test_oracle_reads_remote_and_hybrid_from_the_code(clock):
    requisitions = fixture("oracle_hcm_list_vanderbilt.json")["items"][0]["requisitionList"]
    jobs, _, _ = await oracle_list(OracleServer(requisitions, template="oracle_hcm_list_vanderbilt.json"),
                                   config=VANDERBILT)
    by_id = {job.detail_ref: job for job in jobs}
    assert by_id["10009027"].work_type == "Remote"
    assert by_id["10009027"].location == "United States"
    assert by_id["10009050"].work_type == "Hybrid"
    assert by_id["10009050"].title == "Associate General Counsel -Campus Network (Regulatory)"


@pytest.mark.asyncio
async def test_oracle_uses_the_job_family_as_department_and_every_commitment_word(clock):
    requisitions = fixture("oracle_hcm_list_jpmorgan.json")["items"][0]["requisitionList"]
    requisitions[0].update(JobSchedule="Part time", WorkerType="Intern", ContractType="Part time")
    jobs, _, _ = await oracle_list(OracleServer(requisitions, template="oracle_hcm_list_jpmorgan.json"),
                                   config=JPMORGAN)
    by_id = {job.detail_ref: job for job in jobs}
    assert by_id["210587260"].department == "Software Engineering"
    assert by_id["210803043"].department == "Branch Operations Lead"
    assert by_id["210803043"].location_hint == "Tacoma, WA, United States; Gig Harbor, WA, United States"
    # Distinct words only, schedule first: "Intern" is not lost behind the schedule.
    assert by_id["210587260"].employment_type == "Part time, Intern"
    assert by_id["210588623"].employment_type == ""


@pytest.mark.asyncio
async def test_oracle_pages_to_the_end_and_lists_a_duplicate_row_once(clock, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "PAGE_SIZE", 2)
    rows = dtcc_requisitions()
    rows = rows + [rows[1]]  # the index can carry an identical second row for one posting
    server = OracleServer(rows)
    jobs, complete, total = await oracle_list(server)
    assert server.offsets() == [0, 2, 4]
    assert all("limit=2," in str(r.url) for r in server.requests)
    assert (len(jobs), complete, total) == (5, True, 6)


@pytest.mark.asyncio
async def test_oracle_a_count_a_little_over_the_listing_is_still_complete(clock, monkeypatch):
    """DTCC counts 447 and lists 446, steady from the first page to the last."""
    monkeypatch.setattr(oracle_hcm, "PAGE_SIZE", 2)
    server = OracleServer(dtcc_requisitions(), total=6)
    jobs, complete, total = await oracle_list(server)
    assert server.offsets() == [0, 2, 4, 5]  # the empty page at 5 is what ends the list
    assert (len(jobs), complete, total) == (5, True, 6)


@pytest.mark.asyncio
async def test_oracle_a_listing_far_short_of_the_count_is_incomplete(clock):
    jobs, complete, total = await oracle_list(OracleServer(dtcc_requisitions(), total=40))
    assert (len(jobs), complete, total) == (5, False, 40)


@pytest.mark.asyncio
async def test_oracle_a_board_that_counts_postings_but_lists_none_is_incomplete(clock):
    jobs, complete, total = await oracle_list(OracleServer([], total=3))
    assert (jobs, complete, total) == ([], False, 3)


@pytest.mark.asyncio
async def test_oracle_a_board_with_no_postings_is_complete_and_empty(clock):
    jobs, complete, total = await oracle_list(OracleServer([], total=0))
    assert (jobs, complete, total) == ([], True, 0)


@pytest.mark.asyncio
async def test_oracle_a_count_that_changes_mid_crawl_is_incomplete(clock, monkeypatch):
    """A removal between two pages shifts the list up and skips a live posting;
    the count dropping is the only sign of it."""
    monkeypatch.setattr(oracle_hcm, "PAGE_SIZE", 2)
    server = OracleServer(dtcc_requisitions(), totals=[5, 5, 4])
    jobs, complete, total = await oracle_list(server)
    assert (len(jobs), complete, total) == (5, False, 5)


@pytest.mark.asyncio
async def test_oracle_the_page_cap_leaves_the_board_incomplete(clock, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "PAGE_SIZE", 2)
    monkeypatch.setattr(oracle_hcm, "MAX_PAGES", 2)
    monkeypatch.setattr(oracle_hcm, "HEAD_PAGES", 2)
    server = OracleServer(dtcc_requisitions())
    jobs, complete, total = await oracle_list(server)
    assert server.offsets() == [0, 2]
    assert (len(jobs), complete, total) == (4, False, 5)


@pytest.mark.asyncio
async def test_oracle_the_deadline_stops_the_crawl_after_the_head_pages(clock, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "PAGE_SIZE", 2)
    monkeypatch.setattr(oracle_hcm, "HEAD_PAGES", 1)
    server = OracleServer(dtcc_requisitions())
    jobs, complete, total = await oracle_list(server, deadline=time.monotonic() - 1)
    assert server.offsets() == [0]  # the newest-first head is read whatever the clock says
    assert (len(jobs), complete, total) == (2, False, 5)


@pytest.mark.asyncio
async def test_oracle_a_wrong_site_code_raises_instead_of_reading_as_an_empty_board(clock):
    def wrong_site(request):
        return httpx.Response(200, text=fixture_text("oracle_hcm_wrong_site.json"))

    with pytest.raises(ValueError, match="requisitionList"):
        await oracle_list(wrong_site)


@pytest.mark.asyncio
async def test_oracle_a_server_error_raises_instead_of_returning_a_snapshot(clock):
    def down(request):
        return httpx.Response(503, text="busy")

    with pytest.raises(httpx.HTTPStatusError):
        await oracle_list(down)

    def forbidden(request):
        return httpx.Response(403, text="no")

    with pytest.raises(httpx.HTTPStatusError):
        await oracle_list(forbidden)


@pytest.mark.asyncio
async def test_oracle_rejects_a_config_that_cannot_work(clock):
    server = OracleServer(dtcc_requisitions())
    for config in ({}, {"host": DTCC["host"]}, {"site": "CX_1"},
                   {"host": "careers.example.com", "site": "CX_1"}):
        with pytest.raises(ValueError, match="oracle_hcm/dtcc"):
            await oracle_list(server, config=config)
    assert server.requests == []  # a bad config never reaches the network


@pytest.mark.asyncio
async def test_oracle_reads_the_host_as_written_with_or_without_a_scheme(clock):
    server = OracleServer(dtcc_requisitions())
    jobs, _, _ = await oracle_list(server, config={"host": "HTTPS://EBXR.fa.us2.oraclecloud.com/", "site": "CX_1"})
    assert server.requests[0].url.host == "ebxr.fa.us2.oraclecloud.com"
    assert jobs[0].url.startswith("https://ebxr.fa.us2.oraclecloud.com/hcmUI/")


@pytest.mark.asyncio
async def test_oracle_sends_the_finder_the_career_site_sends(clock):
    server = OracleServer(dtcc_requisitions())
    await oracle_list(server)
    url = str(server.requests[0].url)
    assert url.startswith("https://ebxr.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
                          "recruitingCEJobRequisitions?onlyData=true")
    assert "finder=findReqs;siteNumber=CX_1,limit=200,offset=0,sortBy=POSTING_DATES_DESC" in url
    assert "expand=requisitionList.secondaryLocations" in url


@pytest.mark.asyncio
async def test_oracle_identifies_itself_and_waits_a_second_between_pages(clock, monkeypatch):
    monkeypatch.setattr(oracle_hcm, "PAGE_SIZE", 2)
    server = OracleServer(dtcc_requisitions())
    await oracle_list(server)
    assert len(server.requests) == 3
    assert {r.headers["user-agent"] for r in server.requests} == {UA}
    assert clock.sleeps == [1.0, 1.0]  # the first page is free, each later one waits RATE_LIMIT


@pytest.mark.asyncio
async def test_oracle_fetch_job_reads_the_description_and_commitment(clock):
    seen = []

    def detail(request):
        seen.append(request)
        return httpx.Response(200, text=fixture_text("oracle_hcm_detail_inova.json"))

    async with client_for(detail) as client:
        result = await oracle_hcm.fetch_job(client, "inova", "691805",
                                            config={"host": "elar.fa.us2.oraclecloud.com", "site": "CX_1"})
    assert set(result) == {"description", "employment_type"}
    assert result["employment_type"] == "Full time"
    assert "Clinical Technician 1" in result["description"]
    assert "<p>" not in result["description"] and "&nbsp;" not in result["description"]
    # The employer-wide "about us" blurb is not part of the posting's text.
    assert "leading nonprofit healthcare provider" not in result["description"]
    assert len(result["description"]) <= oracle_hcm.DESCRIPTION_MAX_CHARS
    url = str(seen[0].url)
    assert url.startswith("https://elar.fa.us2.oraclecloud.com/hcmRestApi/resources/latest/"
                          "recruitingCEJobRequisitionDetails?expand=all&onlyData=true")
    assert "finder=ById;Id=%22691805%22,siteNumber=CX_1" in url
    assert seen[0].headers["user-agent"] == UA


@pytest.mark.asyncio
async def test_oracle_fetch_job_orders_the_sections_and_caps_the_length(clock):
    body = fixture("oracle_hcm_detail_inova.json")
    item = body["items"][0]
    item.update(ExternalDescriptionStr="<p>About the role</p>",
                ExternalResponsibilitiesStr="<ul><li>Lead rounds</li></ul>",
                ExternalQualificationsStr="<p>Active RN license</p>")

    async with client_for(lambda request: httpx.Response(200, json=body)) as client:
        config = {"host": "elar.fa.us2.oraclecloud.com", "site": "CX_1"}
        text = (await oracle_hcm.fetch_job(client, "inova", "691805", config=config))["description"]
        assert text.index("About the role") < text.index("Lead rounds") < text.index("Active RN license")
        item["ExternalDescriptionStr"] = "<p>" + "word " * 5000 + "</p>"
        long_text = (await oracle_hcm.fetch_job(client, "inova", "691805", config=config))["description"]
    assert len(long_text) == oracle_hcm.DESCRIPTION_MAX_CHARS


@pytest.mark.asyncio
async def test_oracle_fetch_job_is_empty_when_the_posting_is_gone(clock):
    config = {"host": "jpmc.fa.oraclecloud.com", "site": "CX_1001"}

    async with client_for(lambda request: httpx.Response(200, text=fixture_text("oracle_hcm_detail_empty.json"))) as client:
        assert await oracle_hcm.fetch_job(client, "jpmorgan", "999999999", config=config) == {}
        assert await oracle_hcm.fetch_job(client, "jpmorgan", "", config=config) == {}
    async with client_for(lambda request: httpx.Response(404, text="")) as client:
        assert await oracle_hcm.fetch_job(client, "jpmorgan", "1", config=config) == {}
    async with client_for(lambda request: httpx.Response(403, text="")) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await oracle_hcm.fetch_job(client, "jpmorgan", "1", config=config)


ORACLE_POSTING = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210587260"


@pytest.mark.asyncio
async def test_oracle_check_alive_asks_the_details_finder(clock):
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, text=fixture_text("oracle_hcm_detail_inova.json"))

    async with client_for(answer) as client:
        result = await oracle_hcm.check_alive(client, ORACLE_POSTING)
    assert (result.verdict, result.reason, result.authoritative) == (ALIVE, "oracle_api_item", True)
    assert str(seen[0].url) == (
        "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        "?expand=all&onlyData=true&finder=ById;Id=%22210587260%22,siteNumber=CX_1001")
    assert seen[0].headers["user-agent"] == UA


@pytest.mark.asyncio
async def test_oracle_check_alive_is_dead_only_on_an_empty_item_list(clock):
    async with client_for(lambda request: httpx.Response(200, text=fixture_text("oracle_hcm_detail_empty.json"))) as client:
        result = await oracle_hcm.check_alive(client, ORACLE_POSTING)
    assert (result.verdict, result.reason, result.authoritative) == (DEAD, "oracle_items_0", True)

    for status in (403, 404):
        async with client_for(lambda request, s=status: httpx.Response(s, text="")) as client:
            assert (await oracle_hcm.check_alive(client, ORACLE_POSTING)).verdict == UNKNOWN
    async with client_for(lambda request: httpx.Response(503, text="")) as client:
        assert (await oracle_hcm.check_alive(client, ORACLE_POSTING)) == (UNKNOWN, "http_503", False)
    async with client_for(lambda request: httpx.Response(200, text="<html>not json</html>")) as client:
        assert (await oracle_hcm.check_alive(client, ORACLE_POSTING)) == (UNKNOWN, "oracle_api_unreadable", False)
    async with client_for(lambda request: httpx.Response(200, json={"items": "?"})) as client:
        assert (await oracle_hcm.check_alive(client, ORACLE_POSTING)) == (UNKNOWN, "oracle_api_unreadable", False)


@pytest.mark.asyncio
async def test_oracle_check_alive_never_calls_a_host_that_is_not_oracle(clock):
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, json={"items": []})

    async with client_for(answer) as client:
        for url in ("https://evil.example.com/hcmUI/CandidateExperience/en/sites/CX/job/1",
                    "https://jpmc.fa.oraclecloud.com/en/sites/CX_1001/job/not-a-number",
                    "https://careers.example.com/jobs/1", ""):
            assert (await oracle_hcm.check_alive(client, url)) == (UNKNOWN, "oracle_url_unreadable", False)
    assert seen == []


@pytest.mark.asyncio
async def test_oracle_check_alive_reads_posting_urls_in_every_known_shape(clock):
    seen = []

    def answer(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"items": [{"Id": "1"}]})

    urls = {
        "https://a.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/12": ("a.fa.us2.oraclecloud.com", "CX", "12"),
        "https://b.fa.ca2.oraclecloud.com/hcmUI/CandidateExperience/fr/sites/CX_1/jobs/preview/34/": ("b.fa.ca2.oraclecloud.com", "CX_1", "34"),
        "https://c.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/My%20Site/requisitions/preview/56": ("c.fa.ocs.oraclecloud.com", "My%20Site", "56"),
    }
    async with client_for(answer) as client:
        for url in urls:
            assert (await oracle_hcm.check_alive(client, url)).verdict == ALIVE
    for (host, site, number), got in zip(urls.values(), seen):
        assert got.startswith(f"https://{host}/hcmRestApi/")
        assert f"Id=%22{number}%22,siteNumber={site}" in got


# --- network: three real tenants (RUN_NETWORK_TESTS=1) -------------------------

NETWORK_TENANTS = [
    ("atco", "ATCO", {"host": "eezy.fa.ca2.oraclecloud.com", "site": "CX"}),
    ("cottagehealth", "Cottage Health", {"host": "eglz.fa.us2.oraclecloud.com", "site": "CX"}),
    ("vanderbilt", "Vanderbilt University", {"host": "ecsr.fa.us2.oraclecloud.com", "site": "CX_1"}),
]


@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("slug,company,config", NETWORK_TENANTS, ids=[t[0] for t in NETWORK_TENANTS])
async def test_oracle_network_lists_a_real_board_and_reads_one_posting(slug, company, config):
    async with http.make_client() as client:
        jobs, complete, total = await oracle_hcm.list_jobs(client, slug, company, config=config)
        assert jobs and total >= len(jobs) and complete
        assert all(job.external_id.startswith(f"oracle_hcm:{slug}:") for job in jobs)
        assert all(job.url.startswith(f"https://{config['host']}/hcmUI/CandidateExperience/") for job in jobs)
        detail = await oracle_hcm.fetch_job(client, slug, jobs[0].detail_ref, config=config)
        assert len(detail["description"]) > 100
        assert (await oracle_hcm.check_alive(client, jobs[0].url)).verdict == ALIVE


@pytest.mark.network
@pytest.mark.asyncio
async def test_oracle_network_reads_the_newest_page_of_a_very_large_board(monkeypatch):
    """JPMorgan Chase lists about 7,300 postings; one page is enough to check the
    shape, and the crawl says plainly that it stopped early."""
    monkeypatch.setattr(oracle_hcm, "MAX_PAGES", 1)
    monkeypatch.setattr(oracle_hcm, "HEAD_PAGES", 1)
    async with http.make_client() as client:
        jobs, complete, total = await oracle_hcm.list_jobs(client, "jpmorgan", "JPMorgan Chase", config=JPMORGAN)
    assert len(jobs) == 200 and total > 1000 and not complete


# =============================================================================
# iCIMS
# =============================================================================

KINAXIS = {"host": "careers-kinaxis.icims.com"}
ROBOTS = fixture_text("icims_robots.txt")  # kinaxis' real file: it bars login, referral and candidate pages only


class IcimsServer:
    """One iCIMS portal: robots.txt, the search pages by ``pr`` and posting pages
    by path. A page number it has no page for is the empty page the portal sends
    past the last one. Records every request."""

    def __init__(self, pages=None, *, robots=ROBOTS, postings=None):
        self.pages = pages if pages is not None else {}
        self.robots = robots
        self.postings = postings or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404) if self.robots is None else httpx.Response(200, text=self.robots)
        if path == "/jobs/search":
            page = self.pages.get(int(request.url.params.get("pr", "0")), fixture_text("icims_kinaxis_search_4.html"))
            return httpx.Response(200, text=page)
        if path in self.postings:
            status, body = self.postings[path]
            return httpx.Response(status, text=body)
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.raw_path.decode() for r in self.requests]


def kinaxis_pages() -> dict[int, str]:
    return {i: fixture_text(f"icims_kinaxis_search_{i + 1}.html") for i in range(3)}


async def icims_list(server, slug="kinaxis", company="Kinaxis", config=KINAXIS, **kwargs):
    async with client_for(server) as client:
        return await icims.list_jobs(client, slug, company, config=config, **kwargs)


def test_icims_is_registered_and_owns_its_posting_urls():
    assert adapters.get_adapter("icims") is icims
    assert adapters.adapter_enabled("icims")
    assert (icims.PLATFORM, icims.TIER, icims.RATE_LIMIT) == ("icims", 2, 1.0)
    assert adapters.adapter_for_url("https://careers-kinaxis.icims.com/jobs/35595/senior-payroll-specialist/job") is icims
    assert adapters.adapter_for_url("https://us-erac.icims.com/jobs/1234/x/job?in_iframe=1") is icims
    assert adapters.adapter_for_url("https://example.com/jobs/1234/x/job") is None
    assert adapters.adapter_for_url("https://careers-kinaxis.icims.com/jobs/search") is None


@pytest.mark.asyncio
async def test_icims_pages_through_a_portal_to_the_last_page(clock):
    server = IcimsServer(kinaxis_pages())
    jobs, complete, total = await icims_list(server)
    assert server.paths() == ["/robots.txt", "/jobs/search?pr=0&in_iframe=1",
                              "/jobs/search?pr=1&in_iframe=1", "/jobs/search?pr=2&in_iframe=1"]
    assert (len(jobs), complete, total) == (8, True, 8)
    assert [job.detail_ref.split("/")[2] for job in jobs] == [
        "35595", "35592", "35590", "35323", "35322", "35319", "34510", "34500"]
    assert {r.headers["user-agent"] for r in server.requests} == {UA}
    assert clock.sleeps == [1.0, 1.0, 1.0]  # robots.txt is paced like every other request to the host


@pytest.mark.asyncio
async def test_icims_maps_the_cards_of_a_portal_with_dates_and_categories(clock):
    jobs, _, _ = await icims_list(IcimsServer(kinaxis_pages()))
    payroll = jobs[0]
    assert payroll.title == "Senior Payroll Specialist"
    assert payroll.company == "Kinaxis"
    assert payroll.location == "Ottawa, ON, Canada"
    assert payroll.url == "https://careers-kinaxis.icims.com/jobs/35595/senior-payroll-specialist/job"  # no ?in_iframe=1
    assert payroll.external_id == "icims:kinaxis:35595"
    assert payroll.detail_ref == "/jobs/35595/senior-payroll-specialist/job"
    assert (payroll.department, payroll.employment_type, payroll.work_type) == ("Finance", "Full-Time", "On Site")
    assert payroll.posted_date.isoformat() == "2026-10-23T11:54:00+00:00"  # the title attribute, not "2 days ago"
    assert payroll.description == ""  # the posting page carries it; fetch_job reads it
    remote = jobs[1]
    assert (remote.location, remote.work_type) == ("Remote, United States", "Remote")
    assert remote.location_hint == "Remote, Canada"  # its additional location, spelled out
    assert len({job.url for job in jobs}) == 8


@pytest.mark.asyncio
async def test_icims_reads_a_country_coded_place_as_the_country_first(clock):
    """"CA-Remote" is Canada, not California; foreign places stay as written."""
    jobs, _, _ = await icims_list(IcimsServer(kinaxis_pages()))
    by_id = {job.external_id: job for job in jobs}
    assert by_id["icims:kinaxis:34510"].location == "Remote, Canada"
    assert by_id["icims:kinaxis:34500"].location == "IN-KA-Bangalore"
    assert icims._place("US-NC-Winston-Salem") == "Winston-Salem, NC, United States"
    assert icims._place("CA-QC-Saint-Laurent") == "Saint-Laurent, QC, Canada"
    assert icims._place("US-NY") == "NY, United States"
    assert icims._place("Greater Toronto Area") == "Greater Toronto Area"
    assert icims._places("US-NJ-MORRISTOWN | US-IL-CHICAGO | US-NJ-MORRISTOWN") == (
        "MORRISTOWN, NJ, United States | CHICAGO, IL, United States")


@pytest.mark.asyncio
async def test_icims_an_intern_position_type_feeds_the_entry_level_read(clock):
    jobs, _, _ = await icims_list(IcimsServer(kinaxis_pages()))
    coop = next(job for job in jobs if job.external_id == "icims:kinaxis:35323")
    assert (coop.employment_type, coop.department) == ("Intern", "Engineering")
    assert coop.location_hint == "Toronto, ON, Canada"
    assert entry_tier(coop.title, coop.department, coop.employment_type)


@pytest.mark.asyncio
async def test_icims_reads_the_labels_of_other_portals(clock):
    """Crum & Forster: "Job Locations" with several places, "Category", "Type"."""
    server = IcimsServer({0: fixture_text("icims_cfins_search.html")})
    jobs, _, _ = await icims_list(server, "cfins", "Crum & Forster", {"host": "careers-cfins.icims.com"})
    by_id = {job.external_id: job for job in jobs}
    assert by_id["icims:cfins:5188"].location == "ORANGE, CA, United States"
    assert (by_id["icims:cfins:5188"].department, by_id["icims:cfins:5188"].employment_type) == ("Claims", "Regular")
    assert by_id["icims:cfins:5188"].posted_date is None  # this portal shows none
    assert by_id["icims:cfins:5184"].location == (
        "MORRISTOWN, NJ, United States | CHICAGO, IL, United States | JERSEY CITY, NJ, United States")
    intern = by_id["icims:cfins:5180"]
    assert intern.department == "INTERN"
    assert entry_tier(intern.title, intern.department, intern.employment_type)


@pytest.mark.asyncio
async def test_icims_reads_position_type_and_schedule_and_decodes_entities(clock):
    """Novelis: "Job Type" (Cooperative, Internship) beside "Schedule"."""
    server = IcimsServer({0: fixture_text("icims_novelis_search.html")})
    jobs, _, _ = await icims_list(server, "novelis", "Novelis", {"host": "careers-novelis.icims.com"})
    by_id = {job.external_id: job for job in jobs}
    assert by_id["icims:novelis:55849"].employment_type == "Cooperative, Full Time"
    assert by_id["icims:novelis:53425"].title == "R&D Lab Intern"
    assert by_id["icims:novelis:53425"].department == "Research & Development"
    assert entry_tier(by_id["icims:novelis:55849"].title, by_id["icims:novelis:55849"].department,
                      by_id["icims:novelis:55849"].employment_type)
    assert by_id["icims:novelis:55612"].location == "Ashville, OH, United States | Warren, OH, United States"


@pytest.mark.asyncio
async def test_icims_builds_the_place_from_city_and_never_reads_a_closing_date_as_posted(clock):
    """Vancouver Coastal Health: a "City" label, "Closing Date", "Position Start Date"."""
    server = IcimsServer({0: fixture_text("icims_vch_search.html")})
    jobs, _, _ = await icims_list(server, "vch", "Vancouver Coastal Health", {"host": "careers-vch.icims.com"})
    assert [job.location for job in jobs] == ["Victoria", "Vancouver", "Vancouver"]
    assert all(job.posted_date is None for job in jobs)
    assert jobs[0].employment_type == "Regular Part-Time"


@pytest.mark.asyncio
async def test_icims_reads_a_relative_date_with_its_stamp_and_labels_with_colons(clock):
    """Humber River Health shows "2 days ago (10/23/2026 3:30 PM)" and no place;
    Guelph's labels are "Job ID:" and "Job Type :"."""
    humber = await icims_list(IcimsServer({0: fixture_text("icims_humber_search.html")}), "humber", "Humber River Health",
                              {"host": "careersen-hrrh.icims.com"})
    first = humber[0][0]
    assert first.posted_date.isoformat() == "2026-10-23T15:30:00+00:00"
    assert (first.location, first.department, first.employment_type) == ("", "13 East Medicine", "Temporary Full-Time")
    guelph = await icims_list(IcimsServer({0: fixture_text("icims_guelph_search.html")}), "guelph", "City of Guelph",
                              {"host": "careers-guelph.icims.com"})
    assert [job.employment_type for job in guelph[0]] == ["Contract", "Contract", "Full Time"]
    assert guelph[0][2].url == "https://careers-guelph.icims.com/jobs/4312/trails-technician/job"


@pytest.mark.asyncio
async def test_icims_a_portal_with_no_postings_is_complete_and_empty(clock):
    server = IcimsServer({0: fixture_text("icims_kinaxis_search_4.html")})
    assert await icims_list(server) == ([], True, 0)


@pytest.mark.asyncio
async def test_icims_a_page_that_is_not_a_job_search_raises(clock):
    with pytest.raises(RuntimeError, match="did not answer with an iCIMS job search page"):
        await icims_list(IcimsServer({0: "<html><body>Down for maintenance</body></html>"}))
    layout = '<html><form id="searchForm"></form><a href="https://x.icims.com/jobs/12/a/job">A</a></html>'
    with pytest.raises(RuntimeError, match="layout this adapter does not read"):
        await icims_list(IcimsServer({0: layout}))


@pytest.mark.asyncio
async def test_icims_a_portal_that_sends_its_search_to_an_employer_site_raises(clock):
    def handler(request):
        if request.url.host == "jobs.uci.edu":
            return httpx.Response(200, text="<html>custom front end</html>")
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "https://jobs.uci.edu/jobs"})

    with pytest.raises(RuntimeError, match="sends its job search to jobs.uci.edu"):
        await icims_list(handler, "uci", "UC Irvine", {"host": "careers-uci.icims.com"})


@pytest.mark.asyncio
async def test_icims_robots_txt_that_bars_the_search_stops_the_board(clock):
    for robots in ("User-agent: *\nDisallow: /jobs/search\n", "User-agent: TailrdBot\nDisallow: /\n"):
        server = IcimsServer(kinaxis_pages(), robots=robots)
        with pytest.raises(PermissionError, match="robots.txt"):
            await icims_list(server)
        assert server.paths() == ["/robots.txt"]  # not one page was read
        http._robots.clear()


@pytest.mark.asyncio
async def test_icims_a_missing_robots_txt_allows(clock):
    jobs, complete, _ = await icims_list(IcimsServer(kinaxis_pages(), robots=None))
    assert len(jobs) == 8 and complete


@pytest.mark.asyncio
async def test_icims_the_page_cap_leaves_the_board_incomplete(clock, monkeypatch):
    monkeypatch.setattr(icims, "MAX_PAGES", 2)
    monkeypatch.setattr(icims, "HEAD_PAGES", 2)
    server = IcimsServer(kinaxis_pages())
    jobs, complete, total = await icims_list(server)
    assert server.paths()[1:] == ["/jobs/search?pr=0&in_iframe=1", "/jobs/search?pr=1&in_iframe=1"]
    assert (len(jobs), complete, total) == (6, False, 6)


@pytest.mark.asyncio
async def test_icims_the_deadline_stops_the_crawl_after_the_head_pages(clock, monkeypatch):
    monkeypatch.setattr(icims, "HEAD_PAGES", 1)
    server = IcimsServer(kinaxis_pages())
    jobs, complete, _ = await icims_list(server, deadline=time.monotonic() - 1)
    assert server.paths()[1:] == ["/jobs/search?pr=0&in_iframe=1"]
    assert (len(jobs), complete) == (3, False)


@pytest.mark.asyncio
async def test_icims_a_page_count_that_changes_mid_crawl_is_incomplete(clock):
    pages = kinaxis_pages()
    pages[1] = pages[1].replace("Page 2 of 3", "Page 2 of 4")
    jobs, complete, _ = await icims_list(IcimsServer(pages))
    assert (len(jobs), complete) == (8, False)


@pytest.mark.asyncio
async def test_icims_an_empty_page_before_the_last_one_promised_is_incomplete(clock):
    jobs, complete, _ = await icims_list(IcimsServer({0: kinaxis_pages()[0]}))
    assert (len(jobs), complete) == (3, False)


@pytest.mark.asyncio
async def test_icims_a_card_it_cannot_read_makes_the_board_incomplete(clock):
    pages = kinaxis_pages()
    pages[0] = pages[0] + '<li class="iCIMS_JobCardItem"><div class="row">no link</div></li>'
    jobs, complete, _ = await icims_list(IcimsServer(pages))
    assert (len(jobs), complete) == (8, False)


@pytest.mark.asyncio
async def test_icims_a_posting_shifted_onto_the_next_page_is_listed_once(clock):
    pages = kinaxis_pages()
    pages[1] = pages[1] + icims._CARD_RE.search(pages[0]).group(0)
    jobs, complete, total = await icims_list(IcimsServer(pages))
    assert (len(jobs), complete, total) == (8, True, 8)


@pytest.mark.asyncio
async def test_icims_a_server_error_raises_instead_of_returning_a_snapshot(clock):
    def down(request):
        return httpx.Response(404 if request.url.path == "/robots.txt" else 503, text="busy")

    with pytest.raises(httpx.HTTPStatusError):
        await icims_list(down)


@pytest.mark.asyncio
async def test_icims_rejects_a_config_that_cannot_work(clock):
    server = IcimsServer(kinaxis_pages())
    for config in ({}, {"host": ""}, {"host": "careers.example.com"}):
        with pytest.raises(ValueError, match="icims/kinaxis"):
            await icims_list(server, config=config)
    assert server.requests == []
    jobs, _, _ = await icims_list(server, config={"host": "HTTPS://Careers-Kinaxis.icims.com/"})
    assert len(jobs) == 8 and server.requests[0].url.host == "careers-kinaxis.icims.com"


KINAXIS_JOB = "/jobs/35595/senior-payroll-specialist/job"


@pytest.mark.asyncio
async def test_icims_fetch_job_reads_the_json_ld_description(clock):
    server = IcimsServer(postings={KINAXIS_JOB: (200, fixture_text("icims_kinaxis_job.html"))})
    async with client_for(server) as client:
        result = await icims.fetch_job(client, "kinaxis", KINAXIS_JOB, config=KINAXIS)
    assert set(result) == {"description", "employment_type"}
    assert result["employment_type"] == "Full-time"
    assert "About Kinaxis" in result["description"]
    assert "<p>" not in result["description"] and "&nbsp;" not in result["description"]
    assert 0 < len(result["description"]) <= icims.DESCRIPTION_MAX_CHARS
    assert server.paths() == ["/robots.txt", KINAXIS_JOB]
    assert {r.headers["user-agent"] for r in server.requests} == {UA}


@pytest.mark.asyncio
async def test_icims_fetch_job_ignores_an_uninformative_commitment(clock):
    path = "/jobs/6075/manager-insights-and-reporting/job"
    server = IcimsServer(postings={path: (200, fixture_text("icims_mackenzie_job.html"))})
    async with client_for(server) as client:
        result = await icims.fetch_job(client, "mackenzie", path, config={"host": "careersen-mackenzieinvestments.icims.com"})
    assert result["employment_type"] == ""  # schema.org "OTHER" says nothing
    assert result["description"].startswith("Job Description")
    assert "Mackenzie Investments" in result["description"]


@pytest.mark.asyncio
async def test_icims_fetch_job_caps_the_description_and_reads_a_list_of_commitments(clock):
    path = "/jobs/7/long/job"
    block = json.dumps({"@type": "JobPosting", "description": "<p>" + "word " * 5000 + "</p>",
                        "employmentType": ["FULL_TIME", "INTERN"]})
    page = f'<html><script type="application/ld+json">{block}</script></html>'
    async with client_for(IcimsServer(postings={path: (200, page)})) as client:
        result = await icims.fetch_job(client, "kinaxis", path, config=KINAXIS)
    assert len(result["description"]) == icims.DESCRIPTION_MAX_CHARS
    assert result["employment_type"] == "Full-time, Intern"


@pytest.mark.asyncio
async def test_icims_fetch_job_is_empty_when_there_is_nothing_to_read(clock):
    server = IcimsServer(postings={
        "/jobs/1/gone/job": (410, "<html>gone</html>"),
        "/jobs/2/missing/job": (404, ""),
        "/jobs/3/plain/job": (200, "<html><body>no job data</body></html>"),
        "/jobs/4/forbidden/job": (403, ""),
    })
    async with client_for(server) as client:
        for path in ("/jobs/1/gone/job", "/jobs/2/missing/job", "/jobs/3/plain/job"):
            assert await icims.fetch_job(client, "kinaxis", path, config=KINAXIS) == {}
        with pytest.raises(httpx.HTTPStatusError):
            await icims.fetch_job(client, "kinaxis", "/jobs/4/forbidden/job", config=KINAXIS)
        before = len(server.requests)
        for ref in ("", "/jobs/search", "https://elsewhere.example/jobs/5/x/job"):
            assert await icims.fetch_job(client, "kinaxis", ref, config=KINAXIS) == {}
        assert len(server.requests) == before  # a ref that is not a posting path is not fetched


@pytest.mark.asyncio
async def test_icims_fetch_job_respects_robots_txt(clock):
    server = IcimsServer(robots="User-agent: *\nDisallow: /jobs/\n",
                         postings={KINAXIS_JOB: (200, fixture_text("icims_kinaxis_job.html"))})
    async with client_for(server) as client:
        assert await icims.fetch_job(client, "kinaxis", KINAXIS_JOB, config=KINAXIS) == {}
    assert server.paths() == ["/robots.txt"]


@pytest.mark.asyncio
async def test_icims_check_alive_reads_the_posting_page(clock):
    url = "https://careers-kinaxis.icims.com" + KINAXIS_JOB + "?in_iframe=1"
    server = IcimsServer(postings={KINAXIS_JOB: (200, fixture_text("icims_kinaxis_job.html"))})
    async with client_for(server) as client:
        result = await icims.check_alive(client, url)
    assert (result.verdict, result.reason, result.authoritative) == (ALIVE, "icims_job_posting", True)
    assert server.paths() == ["/robots.txt", KINAXIS_JOB]  # the tracking query is not sent on


@pytest.mark.asyncio
async def test_icims_check_alive_is_dead_on_410_the_portals_word_for_gone(clock):
    base = "https://careers-kinaxis.icims.com"
    server = IcimsServer(postings={"/jobs/9/gone/job": (410, fixture_text("icims_kinaxis_search_1.html")),
                                   "/jobs/8/missing/job": (404, "")})
    async with client_for(server) as client:
        # The 410 page is a full job-search page full of other postings; the status decides.
        assert (await icims.check_alive(client, base + "/jobs/9/gone/job")) == (DEAD, "icims_410", True)
        assert (await icims.check_alive(client, base + "/jobs/8/missing/job")) == (DEAD, "icims_404", True)


@pytest.mark.asyncio
async def test_icims_check_alive_does_not_guess(clock):
    base = "https://careers-kinaxis.icims.com"
    server = IcimsServer(postings={
        "/jobs/1/a/job": (200, "<html><body>no job data</body></html>"),
        "/jobs/4/d/job": (200, "<html><body><h1>Sorry</h1><p>This job is no longer available.</p></body></html>"),
        "/jobs/2/b/job": (403, ""),
        "/jobs/3/c/job": (503, "busy"),
    })
    async with client_for(server) as client:
        assert (await icims.check_alive(client, base + "/jobs/1/a/job")) == (UNKNOWN, "icims_no_posting_data", False)
        assert (await icims.check_alive(client, base + "/jobs/4/d/job")) == (DEAD, "icims_body_closed", False)
        assert (await icims.check_alive(client, base + "/jobs/2/b/job")) == (UNKNOWN, "http_403", False)
        assert (await icims.check_alive(client, base + "/jobs/3/c/job")) == (UNKNOWN, "http_503", False)
    robots = IcimsServer(robots="User-agent: *\nDisallow: /jobs/\n", postings={"/jobs/1/a/job": (200, "x")})
    http._robots.clear()
    async with client_for(robots) as client:
        assert (await icims.check_alive(client, base + "/jobs/1/a/job")) == (UNKNOWN, "robots_disallowed", False)
    assert robots.paths() == ["/robots.txt"]


@pytest.mark.asyncio
async def test_icims_check_alive_notices_a_portal_that_moved_to_an_employer_site(clock):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "jobs.uci.edu":
            return httpx.Response(200, text='<script type="application/ld+json">{"@type": "JobPosting"}</script>')
        return httpx.Response(302, headers={"Location": "https://jobs.uci.edu/jobs/7"})

    async with client_for(handler) as client:
        result = await icims.check_alive(client, "https://careers-uci.icims.com/jobs/7/x/job")
    assert (result.verdict, result.reason) == (UNKNOWN, "icims_redirected")


@pytest.mark.asyncio
async def test_icims_check_alive_does_not_read_another_sites_404_as_the_portals_answer(clock):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        if request.url.host == "jobs.uci.edu":
            return httpx.Response(404, text="not found")
        return httpx.Response(302, headers={"Location": "https://jobs.uci.edu/jobs/7"})

    async with client_for(handler) as client:
        result = await icims.check_alive(client, "https://careers-uci.icims.com/jobs/7/x/job")
    assert (result.verdict, result.reason) == (UNKNOWN, "icims_redirected")


@pytest.mark.asyncio
async def test_icims_check_alive_never_calls_a_host_that_is_not_icims(clock):
    server = IcimsServer()
    async with client_for(server) as client:
        for url in ("https://evil.example.com/jobs/1/x/job", "https://careers-kinaxis.icims.com/jobs/search",
                    "https://careers-kinaxis.icims.com/", ""):
            assert (await icims.check_alive(client, url)) == (UNKNOWN, "icims_url_unreadable", False)
    assert server.requests == []


# --- network: three real portals (RUN_NETWORK_TESTS=1) -------------------------

ICIMS_NETWORK_PORTALS = [
    ("kinaxis", "Kinaxis", {"host": "careers-kinaxis.icims.com"}),
    ("mackenzieinvestments", "Mackenzie Investments", {"host": "careersen-mackenzieinvestments.icims.com"}),
    ("guelph", "City of Guelph", {"host": "careers-guelph.icims.com"}),
]


@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("slug,company,config", ICIMS_NETWORK_PORTALS, ids=[p[0] for p in ICIMS_NETWORK_PORTALS])
async def test_icims_network_lists_a_real_portal_and_reads_one_posting(slug, company, config):
    async with http.make_client() as client:
        jobs, complete, total = await icims.list_jobs(client, slug, company, config=config)
        assert jobs and complete and total == len(jobs)
        assert all(job.external_id.startswith(f"icims:{slug}:") for job in jobs)
        assert all(job.url.startswith(f"https://{config['host']}/jobs/") and "?" not in job.url for job in jobs)
        detail = await icims.fetch_job(client, slug, jobs[0].detail_ref, config=config)
        assert len(detail["description"]) > 100
        assert (await icims.check_alive(client, jobs[0].url)).verdict == ALIVE


# =============================================================================
# Taleo
# =============================================================================

BAYSHORE = {"host": "bayshore.taleo.net", "section": "bs_ex"}
BAYSHORE_DETAIL = "/careersection/bs_ex/jobdetail.ftl"


class TaleoServer:
    """A Taleo career section: robots.txt (none, as on taleo.net), its search page,
    the JSON search by ``pageNo`` and posting pages by job id. ``rows`` are the
    requisitions in order; ``size`` is the page size and ``total`` the count the
    search reports, which may differ from the rows (hidden requisitions).
    ``totals`` scripts the count per search request (the last repeats)."""

    def __init__(self, rows=None, *, page=None, size=25, total=None, totals=None, robots=None, details=None):
        self.rows = list(rows or [])
        self.page = fixture_text("taleo_jobsearch_bayshore.html") if page is None else page
        self.size = size
        self.total = len(self.rows) if total is None else total
        self.totals = list(totals) if totals else None
        self.robots = robots
        self.details = details or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404) if self.robots is None else httpx.Response(200, text=self.robots)
        if path.endswith("/jobsearch.ftl"):
            return httpx.Response(200, text=self.page)
        if path == "/careersection/rest/jobboard/searchjobs":
            number = json.loads(request.content)["pageNo"]
            total = (self.totals.pop(0) if len(self.totals) > 1 else self.totals[0]) if self.totals else self.total
            start = (number - 1) * self.size
            return httpx.Response(200, json={
                "requisitionList": self.rows[start:start + self.size], "facetResults": [],
                "pagingData": {"currentPageNo": number, "pageSize": self.size, "totalCount": total},
                "queryString": "", "careerSectionUnAvailable": False, "supportedLanguages": ["en"]})
        if path.endswith("/jobdetail.ftl"):
            status, body = self.details.get(request.url.params.get("job"), (404, ""))
            return httpx.Response(status, text=body)
        return httpx.Response(404)

    def searches(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/searchjobs")]

    def page_numbers(self) -> list[int]:
        return [json.loads(r.content)["pageNo"] for r in self.searches()]


def taleo_rows(count: int, first_id: int = 1000) -> list[dict]:
    return [{"jobId": str(first_id + i), "contestNo": str(5000 + i), "linkedColumn": 0, "locationsColumns": [1],
             "column": [f"Job {i}", '["ON-Toronto"]', "Remote"]} for i in range(count)]


def fixture_rows(name: str) -> list[dict]:
    return fixture(name)["requisitionList"]


async def taleo_list(server, slug="bayshore", company="Bayshore HealthCare", config=BAYSHORE, **kwargs):
    async with client_for(server) as client:
        return await taleo.list_jobs(client, slug, company, config=config, **kwargs)


def test_taleo_is_registered_and_claims_no_urls():
    assert adapters.get_adapter("taleo") is taleo
    assert adapters.adapter_enabled("taleo")
    assert (taleo.PLATFORM, taleo.TIER, taleo.RATE_LIMIT) == ("taleo", 2, 1.0)
    # A gone posting and the maintenance page look alike from outside, and the generic page
    # check already reads the maintenance page as unknown: Taleo URLs stay with it.
    assert taleo.HOST_PATTERNS == () and not hasattr(taleo, "check_alive")
    assert adapters.adapter_for_url("https://bayshore.taleo.net/careersection/bs_ex/jobdetail.ftl?job=525949&lang=en") is None


@pytest.mark.asyncio
async def test_taleo_maps_a_career_section_with_locations_and_a_work_arrangement(clock):
    server = TaleoServer(fixture_rows("taleo_search_bayshore.json"))
    jobs, complete, total = await taleo_list(server)
    assert (len(jobs), complete, total) == (6, True, 6)
    first = jobs[0]
    assert first.title == "Administrative Assistant"
    assert first.company == "Bayshore HealthCare"
    assert first.location == "ON-Mississauga"
    assert first.url == "https://bayshore.taleo.net/careersection/bs_ex/jobdetail.ftl?job=525949&lang=en"
    assert first.external_id == "taleo:bayshore:525949"
    assert first.detail_ref == "525949"
    assert (first.work_type, first.employment_type, first.department) == ("Remote", "", "")
    assert first.posted_date is None and first.description == ""
    assert jobs[1].work_type == "On Site"  # "In-Office"
    assert jobs[4].work_type == "On Site"  # "Home Care" says nothing about where
    assert len({job.url for job in jobs}) == 6


@pytest.mark.asyncio
async def test_taleo_reads_commitments_and_never_takes_a_date_column_for_a_posting_date(clock):
    """City of Edmonton: columns are the title, "Permanent" and a closing date; no place."""
    jobs, _, _ = await taleo_list(TaleoServer(fixture_rows("taleo_search_edmonton.json")), "edmonton", "City of Edmonton",
                                  {"host": "edmonton.taleo.net", "section": "1"})
    assert [job.employment_type for job in jobs] == ["Permanent", "Temporary", "Temporary", "Temporary", "Permanent", "Temporary"]
    assert all(job.location == "" and job.posted_date is None for job in jobs)
    assert jobs[0].url == "https://edmonton.taleo.net/careersection/1/jobdetail.ftl?job=522795&lang=en"
    assert jobs[1].title == "Day Shift Emergency Communications Specialist (Limited to EFFU Members Only)"  # padding trimmed


@pytest.mark.asyncio
async def test_taleo_reads_a_campus_as_the_place_and_ignores_a_posting_date_column(clock):
    jobs, _, _ = await taleo_list(TaleoServer(fixture_rows("taleo_search_humber.json")), "humber", "Humber College",
                                  {"host": "humber.taleo.net", "section": "hbr_ex"})
    assert [job.location for job in jobs] == ["Lakeshore Campus", "North Campus", "Multiple Locations", "North Campus", "North Campus"]
    assert all(job.posted_date is None and job.employment_type == "" for job in jobs)


def test_taleo_a_column_is_a_commitment_only_when_every_word_is_one():
    assert taleo._only_words("Permanent Full-Time", taleo._COMMITMENT_WORDS)
    assert taleo._only_words("Fixed-Term", taleo._COMMITMENT_WORDS)
    assert not taleo._only_words("Long Term Care", taleo._COMMITMENT_WORDS)
    assert not taleo._only_words("Contract Management", taleo._COMMITMENT_WORDS)
    assert not taleo._only_words("", taleo._COMMITMENT_WORDS)
    assert taleo._only_words("Hybrid", taleo._WORKPLACE_WORDS) and taleo._only_words("In-Office", taleo._WORKPLACE_WORDS)
    assert not taleo._only_words("Remote Nursing", taleo._WORKPLACE_WORDS)


@pytest.mark.asyncio
async def test_taleo_reads_several_places_from_the_locations_column(clock):
    row = {"jobId": "7", "linkedColumn": 0, "locationsColumns": [1, 2],
           "column": ["Float Nurse", '["ON-Toronto", "ON-Ottawa"]', '["ON-Toronto"]']}
    jobs, _, _ = await taleo_list(TaleoServer([row]))
    assert jobs[0].location == "ON-Toronto | ON-Ottawa"


@pytest.mark.asyncio
async def test_taleo_pages_through_the_whole_board_and_sends_what_its_own_page_sends(clock, monkeypatch):
    server = TaleoServer(taleo_rows(5), size=2)
    jobs, complete, total = await taleo_list(server)
    assert server.page_numbers() == [1, 2, 3]
    assert (len(jobs), complete, total) == (5, True, 5)
    first = server.searches()[0]
    assert str(first.url) == "https://bayshore.taleo.net/careersection/rest/jobboard/searchjobs?lang=en&portal=8100120144"
    assert first.method == "POST"
    assert {r.headers["user-agent"] for r in server.requests} == {UA}
    assert first.headers["x-requested-with"] == "XMLHttpRequest"
    assert (first.headers["tz"], first.headers["tzname"]) == ("GMT-05:00", "America/New_York")
    assert first.headers["accept-encoding"] == "identity"  # albertahs.taleo.net mislabels compressed replies
    body = json.loads(first.content)
    assert body["fieldData"]["fields"] == {"KEYWORD": "", "LOCATION": ""} and body["multilineEnabled"] is False


@pytest.mark.asyncio
async def test_taleo_a_count_above_the_listing_is_normal_and_still_complete(clock):
    """City of Edmonton reports 56 and lists 44: the count includes requisitions a
    visitor is not shown, and a page can come back short."""
    rows = taleo_rows(44)
    server = TaleoServer(rows, size=25, total=56)
    jobs, complete, total = await taleo_list(server)
    assert server.page_numbers() == [1, 2, 3]  # the count implies three pages, the last one empty
    assert (len(jobs), complete, total) == (44, True, 56)


@pytest.mark.asyncio
async def test_taleo_stops_at_the_last_page_the_count_implies(clock):
    """Asking past the last page returns the last page again; the count says when to stop."""
    server = TaleoServer(taleo_rows(14), size=25, total=16)
    jobs, complete, _ = await taleo_list(server)
    assert server.page_numbers() == [1]
    assert (len(jobs), complete) == (14, True)


@pytest.mark.asyncio
async def test_taleo_a_count_that_changes_mid_crawl_is_incomplete(clock):
    server = TaleoServer(taleo_rows(6), size=2, totals=[6, 6, 5])
    jobs, complete, total = await taleo_list(server)
    assert (len(jobs), complete, total) == (6, False, 6)


@pytest.mark.asyncio
async def test_taleo_a_row_it_cannot_read_makes_the_board_incomplete(clock):
    rows = taleo_rows(3) + [{"column": ["No id"], "linkedColumn": 0}]
    jobs, complete, _ = await taleo_list(TaleoServer(rows))
    assert (len(jobs), complete) == (3, False)


@pytest.mark.asyncio
async def test_taleo_a_board_with_no_postings_is_complete_and_empty(clock):
    assert await taleo_list(TaleoServer([])) == ([], True, 0)


@pytest.mark.asyncio
async def test_taleo_the_page_cap_and_the_deadline_leave_the_board_incomplete(clock, monkeypatch):
    monkeypatch.setattr(taleo, "MAX_PAGES", 2)
    monkeypatch.setattr(taleo, "HEAD_PAGES", 2)
    server = TaleoServer(taleo_rows(7), size=2)
    jobs, complete, total = await taleo_list(server)
    assert server.page_numbers() == [1, 2]
    assert (len(jobs), complete, total) == (4, False, 7)
    monkeypatch.setattr(taleo, "MAX_PAGES", 10)
    monkeypatch.setattr(taleo, "HEAD_PAGES", 1)
    server = TaleoServer(taleo_rows(7), size=2)
    jobs, complete, _ = await taleo_list(server, deadline=time.monotonic() - 1)
    assert server.page_numbers() == [1]  # the newest-first head is read whatever the clock says
    assert (len(jobs), complete) == (2, False)


@pytest.mark.asyncio
async def test_taleo_reads_the_portal_from_the_page_or_the_config(clock):
    server = TaleoServer(taleo_rows(2))
    await taleo_list(server)
    assert [r.url.path for r in server.requests] == [
        "/robots.txt", "/careersection/bs_ex/jobsearch.ftl", "/careersection/rest/jobboard/searchjobs"]
    named = TaleoServer(taleo_rows(2))
    await taleo_list(named, config={**BAYSHORE, "portal": "555"})
    assert "portal=555" in str(named.searches()[0].url)
    assert not any(r.url.path.endswith("/jobsearch.ftl") for r in named.requests)  # nothing to look up


@pytest.mark.asyncio
async def test_taleo_a_career_section_without_a_json_search_raises(clock):
    legacy = '<html><form name="ftlform" id="ftlform" method="post" action="jobsearch.ftl"></form></html>'
    with pytest.raises(RuntimeError, match="no JSON job search"):
        await taleo_list(TaleoServer(taleo_rows(2), page=legacy))


@pytest.mark.asyncio
async def test_taleo_rejects_a_config_that_cannot_work(clock):
    server = TaleoServer(taleo_rows(2))
    for config in ({}, {"host": "bayshore.taleo.net"}, {"section": "bs_ex"},
                   {"host": "tre.tbe.taleo.net", "section": "x"},  # Taleo Business Edition is another product
                   {"host": "careers.example.com", "section": "x"},
                   {"host": "bayshore.taleo.net", "section": "../etc"},
                   {**BAYSHORE, "portal": "abc"}):
        with pytest.raises(ValueError, match="taleo/bayshore"):
            await taleo_list(server, config=config)
    assert server.requests == []


@pytest.mark.asyncio
async def test_taleo_a_reply_without_a_requisition_list_or_a_section_that_is_down_raises(clock):
    def no_list(request):
        if request.url.path.endswith("/searchjobs"):
            return httpx.Response(200, json={"pagingData": {"totalCount": 3}})
        return httpx.Response(200, text=fixture_text("taleo_jobsearch_bayshore.html")) if request.url.path.endswith(".ftl") else httpx.Response(404)

    with pytest.raises(ValueError, match="without a requisition list"):
        await taleo_list(no_list)

    def down(request):
        if request.url.path.endswith("/searchjobs"):
            return httpx.Response(200, json={"requisitionList": [], "pagingData": {"totalCount": 0}, "careerSectionUnAvailable": True})
        return httpx.Response(200, text=fixture_text("taleo_jobsearch_bayshore.html")) if request.url.path.endswith(".ftl") else httpx.Response(404)

    with pytest.raises(RuntimeError, match="unavailable"):
        await taleo_list(down)


@pytest.mark.asyncio
async def test_taleo_a_server_error_raises_instead_of_returning_a_snapshot(clock):
    def broken(request):
        if request.url.path.endswith("/searchjobs"):
            return httpx.Response(500, text="Internal Server Error")
        return httpx.Response(200, text=fixture_text("taleo_jobsearch_bayshore.html")) if request.url.path.endswith(".ftl") else httpx.Response(404)

    with pytest.raises(httpx.HTTPStatusError):
        await taleo_list(broken)


@pytest.mark.asyncio
async def test_taleo_robots_txt_that_bars_the_career_section_stops_the_board(clock):
    server = TaleoServer(taleo_rows(2), robots="User-agent: *\nDisallow: /careersection/\n")
    with pytest.raises(PermissionError, match="robots.txt"):
        await taleo_list(server)
    assert [r.url.path for r in server.requests] == ["/robots.txt"]


@pytest.mark.asyncio
async def test_taleo_fetch_job_reads_the_description_from_the_data_island(clock):
    server = TaleoServer(details={"525949": (200, fixture_text("taleo_jobdetail_bayshore.html"))})
    async with client_for(server) as client:
        result = await taleo.fetch_job(client, "bayshore", "525949", config=BAYSHORE)
    assert set(result) == {"description", "employment_type"}
    assert result["description"].startswith("JOB SUMMARY")
    assert "Administrative Assistant" in result["description"]
    assert "<" not in result["description"]
    assert 200 < len(result["description"]) <= 10_000
    detail = [r for r in server.requests if r.url.path == BAYSHORE_DETAIL][0]
    assert str(detail.url) == "https://bayshore.taleo.net/careersection/bs_ex/jobdetail.ftl?job=525949&lang=en"
    assert detail.headers["user-agent"] == UA and detail.headers["accept-encoding"] == "identity"


@pytest.mark.asyncio
async def test_taleo_fetch_job_is_empty_when_there_is_nothing_to_read(clock):
    server = TaleoServer(details={"1": (200, fixture_text("taleo_jobdetail_gone.html")), "2": (404, ""),
                                  "3": (410, ""), "4": (403, "")})
    async with client_for(server) as client:
        for ref in ("1", "2", "3"):
            assert await taleo.fetch_job(client, "bayshore", ref, config=BAYSHORE) == {}
        with pytest.raises(httpx.HTTPStatusError):
            await taleo.fetch_job(client, "bayshore", "4", config=BAYSHORE)
        before = len(server.requests)
        for ref in ("", "abc", "12; DROP"):
            assert await taleo.fetch_job(client, "bayshore", ref, config=BAYSHORE) == {}
        assert len(server.requests) == before  # a ref that is not a job id is not fetched


# --- network: three real career sections (RUN_NETWORK_TESTS=1) -----------------

TALEO_NETWORK_BOARDS = [
    ("bayshore", "Bayshore HealthCare", {"host": "bayshore.taleo.net", "section": "bs_ex"}),
    ("edmonton", "City of Edmonton", {"host": "edmonton.taleo.net", "section": "1"}),
    ("humber", "Humber College", {"host": "humber.taleo.net", "section": "hbr_ex"}),
]


@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("slug,company,config", TALEO_NETWORK_BOARDS, ids=[b[0] for b in TALEO_NETWORK_BOARDS])
async def test_taleo_network_lists_a_real_career_section_and_reads_one_posting(slug, company, config):
    async with http.make_client() as client:
        jobs, complete, total = await taleo.list_jobs(client, slug, company, config=config)
        assert jobs and complete and total >= len(jobs)
        assert all(job.external_id.startswith(f"taleo:{slug}:") for job in jobs)
        assert all(job.url.startswith(f"https://{config['host']}/careersection/{config['section']}/jobdetail.ftl?job=") for job in jobs)
        detail = await taleo.fetch_job(client, slug, jobs[0].detail_ref, config=config)
        assert len(detail["description"]) > 100


# =============================================================================
# SuccessFactors (Recruiting Marketing / Jobs2Web front ends)
# =============================================================================

ROGERS = {"host": "jobs.rogers.com"}
SF_ROBOTS = fixture_text("successfactors_robots.txt")  # the real file: it bars the apply, alert and error pages
FULL_ROW = re.compile(r"<tr\b[^>]*data-row[^>]*>.*?</tr>", re.S)
ROGERS_JOB = "/job/London-Civil-Specialist-ON/1424344000/"


class SfServer:
    """One Recruiting Marketing site: robots.txt, the search by ``startrow`` (a
    start it has no page for is the empty page) and posting pages by path, each
    (status, body[, headers]). Records every request."""

    def __init__(self, pages=None, *, robots=SF_ROBOTS, jobs=None):
        self.pages = pages if pages is not None else {}
        self.robots = robots
        self.jobs = jobs or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(404) if self.robots is None else httpx.Response(200, text=self.robots)
        if path == "/search/":
            start = int(request.url.params.get("startrow", "0"))
            return httpx.Response(200, text=self.pages.get(start, fixture_text("successfactors_search_empty.html")))
        if path.startswith("/errorpage"):
            return httpx.Response(200, text="<html><body>Sorry, something went wrong</body></html>")
        if path in self.jobs:
            status, body, *headers = self.jobs[path]
            return httpx.Response(status, text=body, headers=headers[0] if headers else {})
        return httpx.Response(404)

    def starts(self) -> list[int]:
        return [int(r.url.params.get("startrow", "0")) for r in self.requests if r.url.path == "/search/"]


def sf_rows() -> list[str]:
    """Eight distinct real rows: Rogers' four and Telus' four."""
    return (FULL_ROW.findall(fixture_text("successfactors_search_rogers.html"))
            + FULL_ROW.findall(fixture_text("successfactors_search_telus.html")))


def sf_page(rows: list[str], first: int, total: int, label: bool = True) -> str:
    shown = f'<span class="paginationLabel">Results <b>{first + 1} – {first + len(rows)}</b> of <b>{total}</b></span>' if label else ""
    return f'<html><body><div class="pagination-label-row">{shown}</div><table id="searchresults"><tbody>{"".join(rows)}</tbody></table></body></html>'


def sf_pages(size: int, total: int | None = None) -> dict[int, str]:
    rows = sf_rows()
    total = len(rows) if total is None else total
    return {start: sf_page(rows[start:start + size], start, total) for start in range(0, len(rows), size)}


async def sf_list(server, slug="rogers", company="Rogers Communications", config=ROGERS, **kwargs):
    async with client_for(server) as client:
        return await successfactors.list_jobs(client, slug, company, config=config, **kwargs)


def test_successfactors_is_registered_and_claims_no_urls():
    assert adapters.get_adapter("successfactors") is successfactors
    assert adapters.adapter_enabled("successfactors")
    assert (successfactors.PLATFORM, successfactors.TIER, successfactors.RATE_LIMIT) == ("successfactors", 2, 1.0)
    # The sites sit on employers' domains and a URL shape fits other sites too; the generic
    # page check already reads the product's /errorpage redirect as dead.
    assert successfactors.HOST_PATTERNS == () and not hasattr(successfactors, "check_alive")
    assert adapters.adapter_for_url("https://jobs.rogers.com" + ROGERS_JOB) is None


@pytest.mark.asyncio
async def test_successfactors_maps_the_rows_of_a_site_with_a_location_and_a_date_column(clock):
    server = SfServer({0: fixture_text("successfactors_search_rogers.html")})
    jobs, complete, total = await sf_list(server)
    assert (len(jobs), complete, total) == (4, False, 175)  # four rows of a 175-posting board: not the whole board
    first = jobs[0]
    assert first.title == "Civil Specialist"
    assert first.company == "Rogers Communications"
    assert first.location == "London, ON, Canada"
    assert first.url == "https://jobs.rogers.com/job/London-Civil-Specialist-ON/1424344000/"
    assert first.external_id == "successfactors:rogers:1424344000"
    assert first.detail_ref == "/job/London-Civil-Specialist-ON/1424344000/"
    assert first.posted_date.isoformat() == "2026-10-25T00:00:00+00:00"
    assert (first.work_type, first.department, first.employment_type) == ("On Site", "", "")
    # "Brampton, ON, CA +1 more" is one place and a count of others; the count goes.
    assert "+1 more" in fixture_text("successfactors_search_rogers.html")
    assert jobs[1].location == "Brampton, ON, Canada"
    assert jobs[3].url == "https://jobs.rogers.com/job/Toronto-Sr-Analyst%2C-Wireline-eCommerce-ON/1413792300/"
    assert {r.headers["user-agent"] for r in server.requests} == {UA}


@pytest.mark.asyncio
async def test_successfactors_reads_department_and_drops_postal_codes(clock):
    jobs, _, _ = await sf_list(SfServer({0: fixture_text("successfactors_search_telus.html")}), "telus", "TELUS",
                               {"host": "careers.telus.com"})
    assert jobs[0].department == "Sales & Professional Services"
    assert jobs[0].location == "Toronto, ON, Canada"  # "Toronto, ON, CA, M5J 2V5"
    assert jobs[1].title == "Retail Support Associate (Seasonal) | TELUS Devonshire Mall, Windsor"
    assert [job.location for job in jobs] == ["Toronto, ON, Canada", "Windsor, ON, Canada", "Concord, ON, Canada", "Vancouver, BC, Canada"]


@pytest.mark.asyncio
async def test_successfactors_takes_the_place_from_the_title_cell_when_there_is_no_location_column(clock):
    """University of Toronto: columns are title, facility, "Faculty" and date."""
    jobs, _, _ = await sf_list(SfServer({0: fixture_text("successfactors_search_uoft.html")}), "uoft", "University of Toronto",
                               {"host": "jobs.utoronto.ca"})
    assert jobs[0].location == "Toronto, ON, Canada"
    assert jobs[0].title == "Lecturer / Assistant Professor - Emergency & Trauma Radiologist - Sunnybrook Health Sciences Centre"
    assert jobs[0].url.startswith("https://jobs.utoronto.ca/job/Toronto-Lecturer-Assistant-Professor-Emergency-&-Trauma-")
    assert all(job.employment_type == "" for job in jobs)  # "Faculty" is an employee class, not a commitment
    assert jobs[0].posted_date.date().isoformat() == "2026-10-25"


@pytest.mark.asyncio
async def test_successfactors_reads_the_count_in_the_sites_language(clock):
    """Dollarama: "Resultats 1 - 20 sur 32", no date column, titles in French."""
    server = SfServer({0: fixture_text("successfactors_search_dollarama.html")})
    jobs, complete, total = await sf_list(server, "dollarama", "Dollarama", {"host": "jobs.dollarama.com"})
    assert (len(jobs), complete, total) == (4, False, 32)
    assert jobs[0].title == "Gestionnaire technique réseau"
    assert (jobs[0].location, jobs[0].department, jobs[0].posted_date) == ("Mont-Royal, QC", "Siège Social", None)


def test_successfactors_place_text_is_spelled_out():
    assert successfactors._place("Toronto, ON, CA, M1K5L1") == "Toronto, ON, Canada"
    assert successfactors._place("Los Angeles, CA, US") == "Los Angeles, CA, United States"
    assert successfactors._place("Brampton, ON, CA +1 more…") == "Brampton, ON, Canada"
    assert successfactors._place("CA") == "Canada" and successfactors._place("us") == "United States"
    assert successfactors._place("Mont-Royal, QC") == "Mont-Royal, QC"
    assert successfactors._place("London, GB") == "London, GB"
    assert successfactors._place("Ottawa") == "Ottawa"


@pytest.mark.asyncio
async def test_successfactors_pages_through_the_whole_site(clock):
    server = SfServer(sf_pages(3))
    jobs, complete, total = await sf_list(server)
    assert server.starts() == [0, 3, 6]
    assert (len(jobs), complete, total) == (8, True, 8)
    assert [r.url.path for r in server.requests][0] == "/robots.txt"
    assert str(server.requests[1].url) == "https://jobs.rogers.com/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow=0"
    assert clock.sleeps == [1.0, 1.0, 1.0]  # robots.txt is paced like every other request to the host


@pytest.mark.asyncio
async def test_successfactors_a_board_that_counts_more_than_it_lists_is_incomplete(clock):
    pages = sf_pages(4, total=10)  # the label promises ten and the second page is the last
    jobs, complete, total = await sf_list(SfServer(pages))
    assert (len(jobs), complete, total) == (8, False, 10)


@pytest.mark.asyncio
async def test_successfactors_a_count_that_changes_mid_crawl_is_incomplete(clock):
    rows = sf_rows()
    pages = {0: sf_page(rows[:3], 0, 8), 3: sf_page(rows[3:6], 3, 7), 6: sf_page(rows[6:], 6, 7)}
    jobs, complete, total = await sf_list(SfServer(pages))
    assert (len(jobs), complete, total) == (8, False, 8)


@pytest.mark.asyncio
async def test_successfactors_a_row_it_cannot_read_makes_the_board_incomplete(clock):
    rows = sf_rows()[:3] + ['<tr class="data-row"><td class="colTitle">no link</td></tr>']
    jobs, complete, _ = await sf_list(SfServer({0: sf_page(rows, 0, 4)}))
    assert (len(jobs), complete) == (3, False)


@pytest.mark.asyncio
async def test_successfactors_a_site_without_a_count_cannot_be_called_complete(clock):
    jobs, complete, total = await sf_list(SfServer({0: sf_page(sf_rows()[:3], 0, 3, label=False)}))
    assert (len(jobs), complete, total) == (3, False, 3)


@pytest.mark.asyncio
async def test_successfactors_follows_next_page_links_when_the_site_shows_no_count(clock):
    rows = sf_rows()
    first = sf_page(rows[:3], 0, 8, label=False).replace("</body>", '<a href="?q=&amp;startrow=3">2</a></body>')
    jobs, complete, _ = await sf_list(SfServer({0: first, 3: sf_page(rows[3:6], 3, 8, label=False)}))
    assert len(jobs) == 6 and not complete


@pytest.mark.asyncio
async def test_successfactors_a_site_with_no_postings_is_complete_and_empty(clock):
    assert await sf_list(SfServer({})) == ([], True, 0)


@pytest.mark.asyncio
async def test_successfactors_a_page_that_is_not_a_job_search_raises(clock):
    with pytest.raises(RuntimeError, match="did not answer with a Recruiting Marketing job search page"):
        await sf_list(SfServer({0: "<html><body>Please sign in</body></html>"}))


@pytest.mark.asyncio
async def test_successfactors_a_site_that_sends_its_search_elsewhere_raises(clock):
    def handler(request):
        if request.url.host == "careers.example.org":
            return httpx.Response(200, text="<html>elsewhere</html>")
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "https://careers.example.org/jobs"})

    with pytest.raises(RuntimeError, match="sends its job search to careers.example.org"):
        await sf_list(handler)


@pytest.mark.asyncio
async def test_successfactors_robots_txt_that_bars_the_search_stops_the_board(clock):
    server = SfServer(sf_pages(3), robots="User-agent: *\nDisallow: /search/\n")
    with pytest.raises(PermissionError, match="robots.txt"):
        await sf_list(server)
    assert [r.url.path for r in server.requests] == ["/robots.txt"]


@pytest.mark.asyncio
async def test_successfactors_the_page_cap_and_the_deadline_leave_the_board_incomplete(clock, monkeypatch):
    monkeypatch.setattr(successfactors, "MAX_PAGES", 2)
    monkeypatch.setattr(successfactors, "HEAD_PAGES", 2)
    server = SfServer(sf_pages(3))
    jobs, complete, total = await sf_list(server)
    assert server.starts() == [0, 3]
    assert (len(jobs), complete, total) == (6, False, 8)
    monkeypatch.setattr(successfactors, "MAX_PAGES", 10)
    monkeypatch.setattr(successfactors, "HEAD_PAGES", 1)
    server = SfServer(sf_pages(3))
    jobs, complete, _ = await sf_list(server, deadline=time.monotonic() - 1)
    assert server.starts() == [0]  # the newest-first head is read whatever the clock says
    assert (len(jobs), complete) == (3, False)


@pytest.mark.asyncio
async def test_successfactors_rejects_a_config_that_cannot_work(clock):
    server = SfServer(sf_pages(3))
    for config in ({}, {"host": ""}, {"host": "not a host"}, {"host": "career17.sapsf.com"},
                   {"host": "career4.successfactors.com"}, {"host": "career5.successfactors.eu"}):
        with pytest.raises(ValueError, match="successfactors/rogers"):
            await sf_list(server, config=config)
    assert server.requests == []  # the robots-barred back end is never touched
    jobs, _, _ = await sf_list(server, config={"host": "HTTPS://Jobs.Rogers.com/"})
    assert len(jobs) == 8 and server.requests[0].url.host == "jobs.rogers.com"


@pytest.mark.asyncio
async def test_successfactors_a_server_error_raises_instead_of_returning_a_snapshot(clock):
    def down(request):
        return httpx.Response(404 if request.url.path == "/robots.txt" else 503, text="busy")

    with pytest.raises(httpx.HTTPStatusError):
        await sf_list(down)


@pytest.mark.asyncio
async def test_successfactors_fetch_job_reads_the_description_span_with_its_nested_spans(clock):
    server = SfServer(jobs={ROGERS_JOB: (200, fixture_text("successfactors_job_rogers.html"))})
    async with client_for(server) as client:
        result = await successfactors.fetch_job(client, "rogers", ROGERS_JOB, config=ROGERS)
    assert set(result) == {"description", "employment_type"}
    assert result["description"].startswith("Our Technology team wakes up every day")
    assert "<span" not in result["description"] and "<p>" not in result["description"]
    assert 1000 < len(result["description"]) <= successfactors.DESCRIPTION_MAX_CHARS
    assert [r.url.path for r in server.requests] == ["/robots.txt", ROGERS_JOB]
    assert {r.headers["user-agent"] for r in server.requests} == {UA}


@pytest.mark.asyncio
async def test_successfactors_fetch_job_is_empty_when_there_is_nothing_to_read(clock):
    server = SfServer(jobs={
        "/job/A-Gone-ON/100000001/": (404, ""), "/job/B-Gone-ON/100000002/": (410, ""),
        "/job/C-Moved-ON/100000003/": (302, "", {"Location": "/errorpage/?errortype=Exception"}),
        "/job/D-Plain-ON/100000004/": (200, "<html><body>no description here</body></html>"),
        "/job/E-Forbidden-ON/100000005/": (403, ""),
    })
    async with client_for(server) as client:
        for path in ("/job/A-Gone-ON/100000001/", "/job/B-Gone-ON/100000002/", "/job/C-Moved-ON/100000003/",
                     "/job/D-Plain-ON/100000004/"):
            assert await successfactors.fetch_job(client, "rogers", path, config=ROGERS) == {}
        with pytest.raises(httpx.HTTPStatusError):
            await successfactors.fetch_job(client, "rogers", "/job/E-Forbidden-ON/100000005/", config=ROGERS)
        before = len(server.requests)
        for ref in ("", "/search/", "https://elsewhere.example/job/x/123456789/"):
            assert await successfactors.fetch_job(client, "rogers", ref, config=ROGERS) == {}
        assert len(server.requests) == before


@pytest.mark.asyncio
async def test_successfactors_fetch_job_respects_robots_txt(clock):
    server = SfServer(robots="User-agent: *\nDisallow: /job/\n", jobs={ROGERS_JOB: (200, fixture_text("successfactors_job_rogers.html"))})
    async with client_for(server) as client:
        assert await successfactors.fetch_job(client, "rogers", ROGERS_JOB, config=ROGERS) == {}
    assert [r.url.path for r in server.requests] == ["/robots.txt"]


# --- network: three real Recruiting Marketing sites (RUN_NETWORK_TESTS=1) ------

SF_NETWORK_SITES = [
    ("rogers", "Rogers Communications", {"host": "jobs.rogers.com"}),
    ("hydroone", "Hydro One", {"host": "jobs.hydroone.com"}),
    ("saskatoon", "City of Saskatoon", {"host": "careers.saskatoon.ca"}),
]


@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("slug,company,config", SF_NETWORK_SITES, ids=[s[0] for s in SF_NETWORK_SITES])
async def test_successfactors_network_lists_a_real_site_and_reads_one_posting(slug, company, config):
    async with http.make_client() as client:
        jobs, complete, total = await successfactors.list_jobs(client, slug, company, config=config)
        assert jobs and complete and total == len(jobs)
        assert all(job.external_id.startswith(f"successfactors:{slug}:") for job in jobs)
        assert all(job.url.startswith(f"https://{config['host']}/job/") for job in jobs)
        detail = await successfactors.fetch_job(client, slug, jobs[0].detail_ref, config=config)
        assert len(detail["description"]) > 100


# =============================================================================
# Jibe (iCIMS Career Sites)
# =============================================================================

JIBE_AKIMA = {"host": "careers.akima.com"}
JIBE_ROBOTS = fixture_text("jibe_robots.txt")  # the real file: "Allow: /" and "Crawl-delay: 5", as on every Jibe site checked


class JibeServer:
    """One Jibe career site: robots.txt and the jobs API, which answers the
    requested ``page`` of ``limit`` postings under a ``totalCount``. ``totals``
    scripts the count per request (the last repeats). Records every request."""

    def __init__(self, items=None, *, total=None, totals=None, robots=JIBE_ROBOTS):
        self.items = list(items or [])
        self.total = len(self.items) if total is None else total
        self.totals = list(totals) if totals else None
        self.robots = robots
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(404) if self.robots is None else httpx.Response(200, text=self.robots)
        if request.url.path == "/api/jobs":
            page, limit = int(request.url.params["page"]), int(request.url.params["limit"])
            total = (self.totals.pop(0) if len(self.totals) > 1 else self.totals[0]) if self.totals else self.total
            start = (page - 1) * limit
            return httpx.Response(200, json={"jobs": self.items[start:start + limit], "totalCount": total, "count": total,
                                             "locations": [], "filter": {}})
        return httpx.Response(404)

    def pages(self) -> list[int]:
        return [int(r.url.params["page"]) for r in self.requests if r.url.path == "/api/jobs"]


def jibe_items() -> list[dict]:
    """Four real postings: two from Akima, two from State Farm (one open in four places)."""
    return fixture("jibe_akima_page.json")["jobs"] + fixture("jibe_statefarm_page.json")["jobs"]


async def jibe_list(server, slug="akima", company="Akima", config=JIBE_AKIMA, **kwargs):
    async with client_for(server) as client:
        return await jibe.list_jobs(client, slug, company, config=config, **kwargs)


def test_jibe_is_registered_claims_no_urls_and_has_no_per_posting_calls():
    assert adapters.get_adapter("jibe") is jibe
    assert adapters.adapter_enabled("jibe")
    assert (jibe.PLATFORM, jibe.TIER, jibe.RATE_LIMIT) == ("jibe", 2, 5.0)
    # Career sites live on the employer's own domain, so no URL can be claimed by its host.
    assert jibe.HOST_PATTERNS == () and adapters.adapter_for_url("https://careers.akima.com/jobs/26403") is None
    # The list carries every description and the API has no per-posting call.
    assert not hasattr(jibe, "fetch_job") and not hasattr(jibe, "check_alive")


@pytest.mark.asyncio
async def test_jibe_maps_a_posting_with_its_full_description(clock):
    jobs, complete, total = await jibe_list(JibeServer(jibe_items()))
    assert (len(jobs), complete, total) == (4, True, 4)
    first = jobs[0]
    assert first.title == "Customer Service Representative I - DAYS - Written Correspondence"
    assert first.company == "Akima"
    assert first.location == "Portsmouth, New Hampshire, United States"
    assert first.url == "https://careers.akima.com/jobs/26403"
    assert first.external_id == "jibe:akima:26403"
    assert first.posted_date.isoformat() == "2026-10-24T21:40:00+00:00"
    assert (first.department, first.employment_type, first.work_type) == ("Office & Administrative Support", "Full-time", "On Site")
    assert first.apply_url == "https://careers-akima.icims.com/jobs/26403/login"  # the employer's own apply link
    assert first.description.startswith("As our Customer Service Representative")
    assert "<p>" not in first.description and 1000 < len(first.description) <= jibe.DESCRIPTION_MAX_CHARS
    assert len({job.url for job in jobs}) == 4


@pytest.mark.asyncio
async def test_jibe_lists_every_place_a_posting_is_open_in_and_reads_remote_and_hybrid(clock):
    jobs, _, _ = await jibe_list(JibeServer(jibe_items()), "statefarm", "State Farm", {"host": "jobs.statefarm.com"})
    by_id = {job.external_id: job for job in jobs}
    multi = by_id["jibe:statefarm:43740"]
    assert multi.location == ("Bloomington, Illinois, United States | Dunwoody, Georgia, United States | "
                              "Richardson, Texas, United States | Tempe, Arizona, United States")
    assert multi.work_type == "Hybrid"
    assert by_id["jibe:statefarm:46792"].work_type == "Remote"
    assert by_id["jibe:statefarm:46792"].url == "https://jobs.statefarm.com/jobs/46792"


@pytest.mark.asyncio
async def test_jibe_asks_for_newest_first_pages_of_100_and_waits_the_crawl_delay(clock, monkeypatch):
    monkeypatch.setattr(jibe, "PAGE_SIZE", 2)
    server = JibeServer(jibe_items())
    await jibe_list(server)
    api = [r for r in server.requests if r.url.path == "/api/jobs"]
    assert str(api[0].url) == "https://careers.akima.com/api/jobs?limit=2&page=1&sortBy=posted_date&descending=true"
    assert {r.headers["user-agent"] for r in server.requests} == {UA}
    assert server.pages() == [1, 2]
    assert clock.sleeps == [1.0, 5.0]  # robots.txt is paced as the foundation paces it, then Crawl-delay: 5


@pytest.mark.asyncio
async def test_jibe_a_site_that_asks_for_a_longer_crawl_delay_gets_it(clock, monkeypatch):
    monkeypatch.setattr(jibe, "PAGE_SIZE", 2)
    server = JibeServer(jibe_items(), robots="User-agent: *\nAllow: /\nCrawl-delay: 12\n")
    await jibe_list(server)
    assert clock.sleeps == [1.0, 12.0]


@pytest.mark.asyncio
async def test_jibe_lists_a_posting_repeated_across_pages_once(clock, monkeypatch):
    monkeypatch.setattr(jibe, "PAGE_SIZE", 3)
    items = jibe_items()
    server = JibeServer(items + [items[0]], total=5)  # a posting added mid-crawl shifts one onto the next page
    jobs, complete, total = await jibe_list(server)
    assert server.pages() == [1, 2]
    assert (len(jobs), complete, total) == (4, False, 5)  # five counted, four distinct listed: not the whole board


@pytest.mark.asyncio
async def test_jibe_a_count_above_the_listing_or_a_count_that_changes_is_incomplete(clock, monkeypatch):
    monkeypatch.setattr(jibe, "PAGE_SIZE", 2)
    assert (await jibe_list(JibeServer(jibe_items(), total=9)))[1:] == (False, 9)
    jobs, complete, total = await jibe_list(JibeServer(jibe_items(), totals=[4, 3]))
    assert (len(jobs), complete, total) == (4, False, 4)


@pytest.mark.asyncio
async def test_jibe_a_posting_it_cannot_read_makes_the_board_incomplete(clock):
    items = jibe_items() + [{"data": {"title": "No id"}}, {"nope": True}]
    jobs, complete, _ = await jibe_list(JibeServer(items, total=6))
    assert (len(jobs), complete) == (4, False)


@pytest.mark.asyncio
async def test_jibe_a_site_with_no_postings_is_complete_and_empty(clock):
    assert await jibe_list(JibeServer([])) == ([], True, 0)


@pytest.mark.asyncio
async def test_jibe_the_page_cap_and_the_deadline_leave_the_board_incomplete(clock, monkeypatch):
    monkeypatch.setattr(jibe, "PAGE_SIZE", 1)
    monkeypatch.setattr(jibe, "MAX_PAGES", 2)
    monkeypatch.setattr(jibe, "HEAD_PAGES", 2)
    server = JibeServer(jibe_items())
    jobs, complete, total = await jibe_list(server)
    assert server.pages() == [1, 2] and (len(jobs), complete, total) == (2, False, 4)
    monkeypatch.setattr(jibe, "MAX_PAGES", 10)
    monkeypatch.setattr(jibe, "HEAD_PAGES", 1)
    server = JibeServer(jibe_items())
    jobs, complete, _ = await jibe_list(server, deadline=time.monotonic() - 1)
    assert server.pages() == [1]  # the newest page is read whatever the clock says
    assert (len(jobs), complete) == (1, False)


@pytest.mark.asyncio
async def test_jibe_an_error_body_or_a_server_error_raises_instead_of_returning_a_snapshot(clock):
    def error_body(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, json={"error": "limit must be at most 100"})

    with pytest.raises(ValueError, match="without a jobs list"):
        await jibe_list(error_body)

    def down(request):
        return httpx.Response(404 if request.url.path == "/robots.txt" else 503, text="busy")

    with pytest.raises(httpx.HTTPStatusError):
        await jibe_list(down)


@pytest.mark.asyncio
async def test_jibe_robots_txt_that_bars_the_api_stops_the_board(clock):
    server = JibeServer(jibe_items(), robots="User-agent: *\nDisallow: /api/\n")
    with pytest.raises(PermissionError, match="robots.txt"):
        await jibe_list(server)
    assert [r.url.path for r in server.requests] == ["/robots.txt"]


@pytest.mark.asyncio
async def test_jibe_rejects_a_config_that_cannot_work(clock):
    server = JibeServer(jibe_items())
    for config in ({}, {"host": ""}, {"host": "not a host"}):
        with pytest.raises(ValueError, match="jibe/akima"):
            await jibe_list(server, config=config)
    assert server.requests == []
    jobs, _, _ = await jibe_list(server, config={"host": "HTTPS://Careers.Akima.com/"})
    assert len(jobs) == 4 and server.requests[0].url.host == "careers.akima.com"


# --- network: three real Jibe sites (RUN_NETWORK_TESTS=1) -----------------------

JIBE_NETWORK_SITES = [
    ("cobank", "CoBank", {"host": "careers.cobank.com"}),
    ("thunderbay", "City of Thunder Bay", {"host": "careers.thunderbay.ca"}),
    ("docusign", "DocuSign", {"host": "careers.docusign.com"}),
]


@pytest.mark.network
@pytest.mark.asyncio
@pytest.mark.parametrize("slug,company,config", JIBE_NETWORK_SITES, ids=[s[0] for s in JIBE_NETWORK_SITES])
async def test_jibe_network_lists_a_real_career_site_with_descriptions(slug, company, config):
    async with http.make_client() as client:
        jobs, complete, total = await jibe.list_jobs(client, slug, company, config=config)
    assert jobs and complete and total == len(jobs)
    assert all(job.external_id.startswith(f"jibe:{slug}:") and job.description for job in jobs)
    assert all(job.url.startswith(f"https://{config['host']}/jobs/") for job in jobs)
