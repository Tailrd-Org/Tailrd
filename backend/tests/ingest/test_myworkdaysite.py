"""myworkdaysite.com Workday boards: the public posting URL is
https://{host}/recruiting/{tenant}/{site}{externalPath}, which Workday's own
externalUrl confirmed live on 2026-10-25 for a posting of each of the 23
boards (the tenant is in the path, not in the host as on myworkdayjobs.com;
Franciscan Health's site name differs from the registry's in letter case
only, and both spellings answer)."""

import httpx
import pytest

from backend.data import company_registry
from backend.ingest.core import http
from backend.services.ats_scraper import ATSScraper, workday_public_base


@pytest.fixture(autouse=True)
def setup_db():
    """No database here: this module skips the root conftest's per-test
    create_all/drop_all."""
    yield


def test_the_public_base_of_a_myworkdaysite_board():
    assert workday_public_base(
        "https://wd1.myworkdaysite.com/wday/cxs/upenn/careers-at-penn"
    ) == "https://wd1.myworkdaysite.com/recruiting/upenn/careers-at-penn"
    assert workday_public_base(
        "https://wd501.myworkdaysite.com/wday/cxs/byuh/BYU_H/"
    ) == "https://wd501.myworkdaysite.com/recruiting/byuh/BYU_H"


def test_the_public_base_of_a_myworkdayjobs_board_is_unchanged():
    assert workday_public_base(
        "https://bmo.wd3.myworkdayjobs.com/wday/cxs/bmo/external"
    ) == "https://bmo.wd3.myworkdayjobs.com/external"
    assert workday_public_base(
        "https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/"
    ) == "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"
    # Not a CxS base: handed back without its trailing slash.
    assert workday_public_base("https://example.com/careers/") == "https://example.com/careers"
    assert workday_public_base("") == ""


@pytest.mark.asyncio
async def test_a_crawl_builds_the_public_url_on_the_myworkdaysite_host(monkeypatch):
    monkeypatch.setattr(company_registry, "load_workday_bases", lambda: {
        "upenn": "https://wd1.myworkdaysite.com/wday/cxs/upenn/careers-at-penn"})
    # A posting as the live CxS list returned it on 2026-10-25.
    payload = {"total": 1, "jobPostings": [{
        "title": "Penn Medicine Clinician",
        "externalPath": "/job/HUP/Penn-Medicine-Clinician_JR00128810",
        "locationsText": "HUP", "postedOn": "Posted 2 Days Ago",
        "bulletFields": ["JR00128810"],
    }]}
    asked = []

    def respond(request):
        asked.append(str(request.url))
        return httpx.Response(200, json=payload)

    scraper = ATSScraper(filter_entry_level=False, filter_north_america=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        snapshot = await scraper.scrape_board(client, "workday", "upenn", "University of Pennsylvania")

    assert asked == ["https://wd1.myworkdaysite.com/wday/cxs/upenn/careers-at-penn/jobs"]
    [job] = snapshot.jobs
    assert job.url == ("https://wd1.myworkdaysite.com/recruiting/upenn/careers-at-penn"
                       "/job/HUP/Penn-Medicine-Clinician_JR00128810")
    assert job.detail_ref == "/job/HUP/Penn-Medicine-Clinician_JR00128810"
    assert snapshot.board_key == "workday:upenn" and snapshot.complete


def _site_boards():
    return [entry for entry in company_registry._load_raw()
            if ".myworkdaysite.com/" in (entry.get("workday_url_template") or "")]


def test_the_23_held_boards_are_back_in_service():
    boards = _site_boards()
    assert len(boards) == 23
    crawled = {slug for platform, slug, _name in company_registry.load_companies()
               if platform == "workday"}
    bases = company_registry.load_workday_bases()
    for entry in boards:
        slug = entry["board_slug"]
        assert entry.get("enabled") is True, slug
        assert "held_workday_url_template" not in entry and "disabled_reason" not in entry, slug
        assert slug in crawled and bases[slug] == entry["workday_url_template"], slug


def test_every_myworkdaysite_careers_url_is_the_public_base_of_its_template():
    # careers_url was copied from the live career site; the derived base
    # agrees with it for every board, so a posting URL built from it is the
    # one Workday publishes.
    for entry in _site_boards():
        assert workday_public_base(entry["workday_url_template"]) == entry["careers_url"].rstrip("/"), (
            entry["board_slug"])


@pytest.mark.network
@pytest.mark.asyncio
async def test_workday_publishes_the_url_the_crawler_builds():
    """Live (RUN_NETWORK_TESTS=1): one posting of the University of
    Pennsylvania board, whose own externalUrl is the public URL
    workday_public_base() builds for it."""
    base = company_registry.load_workday_bases()["upenn"]
    async with http.make_client() as client:
        listing = await http.request(
            client, "POST", f"{base}/jobs",
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers={"Accept": "application/json", "Content-Type": "application/json"})
        path = listing.json()["jobPostings"][0]["externalPath"]
        detail = await http.request(client, "GET", f"{base}{path}", headers={"Accept": "application/json"})

    assert detail.json()["jobPostingInfo"]["externalUrl"] == workday_public_base(base) + path
