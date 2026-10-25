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
from backend.ingest.adapters import oracle_hcm
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


POSTING = "https://jpmc.fa.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/job/210587260"


@pytest.mark.asyncio
async def test_oracle_check_alive_asks_the_details_finder(clock):
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(200, text=fixture_text("oracle_hcm_detail_inova.json"))

    async with client_for(answer) as client:
        result = await oracle_hcm.check_alive(client, POSTING)
    assert (result.verdict, result.reason, result.authoritative) == (ALIVE, "oracle_api_item", True)
    assert str(seen[0].url) == (
        "https://jpmc.fa.oraclecloud.com/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        "?expand=all&onlyData=true&finder=ById;Id=%22210587260%22,siteNumber=CX_1001")
    assert seen[0].headers["user-agent"] == UA


@pytest.mark.asyncio
async def test_oracle_check_alive_is_dead_only_on_an_empty_item_list(clock):
    async with client_for(lambda request: httpx.Response(200, text=fixture_text("oracle_hcm_detail_empty.json"))) as client:
        result = await oracle_hcm.check_alive(client, POSTING)
    assert (result.verdict, result.reason, result.authoritative) == (DEAD, "oracle_items_0", True)

    for status in (403, 404):
        async with client_for(lambda request, s=status: httpx.Response(s, text="")) as client:
            assert (await oracle_hcm.check_alive(client, POSTING)).verdict == UNKNOWN
    async with client_for(lambda request: httpx.Response(503, text="")) as client:
        assert (await oracle_hcm.check_alive(client, POSTING)) == (UNKNOWN, "http_503", False)
    async with client_for(lambda request: httpx.Response(200, text="<html>not json</html>")) as client:
        assert (await oracle_hcm.check_alive(client, POSTING)) == (UNKNOWN, "oracle_api_unreadable", False)
    async with client_for(lambda request: httpx.Response(200, json={"items": "?"})) as client:
        assert (await oracle_hcm.check_alive(client, POSTING)) == (UNKNOWN, "oracle_api_unreadable", False)


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
