"""Connector parsing pinned against saved board payloads (one per platform).

These are the fixtures that catch a source changing its response shape, the
classic silent-failure mode where a parser starts returning zero jobs and
nobody notices until the catalogue goes stale.
"""

import json
import re
from collections import defaultdict
from pathlib import Path

import httpx
import pytest

from backend.data import company_registry
from backend.services.ats_scraper import (
    ATSJob,
    ATSScraper,
    fetch_workday_detail,
    workday_public_base,
)
from backend.services.na_location import job_country

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FixtureTransport(httpx.AsyncBaseTransport):
    """Route by URL fragment; a list value pages by call order (Workday)."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.requests: list[httpx.Request] = []
        self._page_counts: dict[str, int] = {}

    async def handle_async_request(self, request):
        self.requests.append(request)
        for fragment, payload in self.responses.items():
            if fragment in str(request.url):
                if isinstance(payload, list) and payload and isinstance(payload[0], dict) \
                        and "jobPostings" in payload[0]:
                    index = self._page_counts.get(fragment, 0)
                    self._page_counts[fragment] = index + 1
                    page = payload[min(index, len(payload) - 1)]
                    return httpx.Response(200, json=page)
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})


def _unfiltered() -> ATSScraper:
    return ATSScraper(filter_entry_level=False, filter_north_america=False)


def _filtered() -> ATSScraper:
    return ATSScraper(filter_entry_level=True, filter_north_america=True)


# ─── Greenhouse ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_greenhouse_fixture_parses_ids_salary_and_content():
    transport = FixtureTransport({"boards-api.greenhouse.io": _load("greenhouse_board.json")})
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _unfiltered().scrape_board(client, "greenhouse", "acme", "Acme")

    assert snapshot.board_key == "greenhouse:acme"
    assert snapshot.complete
    assert len(snapshot.jobs) == 3
    intern = next(j for j in snapshot.jobs if "Intern" in j.title)
    assert intern.external_id == "4285367"
    assert intern.url == "https://boards.greenhouse.io/acme/jobs/4285367"
    assert "Python" in intern.description
    assert "45000-55000 CAD" == intern.salary_text
    assert intern.posted_date is not None


@pytest.mark.asyncio
async def test_greenhouse_snapshot_all_urls_includes_filtered_out_jobs():
    """The senior role fails the entry-level filter but its URL must stay in
    all_urls, reconciliation would otherwise mark live jobs as removed."""
    transport = FixtureTransport({"boards-api.greenhouse.io": _load("greenhouse_board.json")})
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _filtered().scrape_board(client, "greenhouse", "acme", "Acme")

    filtered_titles = {j.title for j in snapshot.jobs}
    assert "Senior Staff Architect" not in filtered_titles
    assert "https://boards.greenhouse.io/acme/jobs/4285368" in snapshot.all_urls
    assert len(snapshot.all_urls) == 3


# ─── Lever ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lever_fixture_parses_commitment_and_salary():
    transport = FixtureTransport({"api.lever.co": _load("lever_board.json")})
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _unfiltered().scrape_board(client, "lever", "acme", "Acme")

    assert len(snapshot.jobs) == 3
    new_grad = next(j for j in snapshot.jobs if "New Grad" in j.title)
    assert new_grad.external_id == "a1b2c3d4-0001"
    assert new_grad.employment_type == "Full-time"
    assert "90000-110000 CAD" in new_grad.salary_text
    assert "TypeScript" in new_grad.description

    intern = next(j for j in snapshot.jobs if "Intern" in j.title)
    assert intern.employment_type == "Intern"


# ─── Ashby ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ashby_fixture_parses_employment_and_compensation():
    transport = FixtureTransport({"api.ashbyhq.com": _load("ashby_board.json")})
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _unfiltered().scrape_board(client, "ashby", "acme", "Acme")

    assert len(snapshot.jobs) == 2
    intern = next(j for j in snapshot.jobs if "Intern" in j.title)
    assert intern.external_id == "f47ac10b-0001"
    assert intern.employment_type == "Intern"
    assert "per hour" in intern.salary_text
    assert "<p>" not in intern.description


# ─── Other locations (Ashby secondaryLocations, Lever allLocations) ─────────
# A posting whose primary location is abroad but which is also open in North
# America (live 2026-09-29: cohere "London" + San Francisco, New York, Toronto,
# Montreal; oyster "EMEA" + Canada) is a North American posting, not a
# "location" reject that retires its stored row.

@pytest.mark.asyncio
async def test_ashby_secondary_locations_feed_the_na_verdict(monkeypatch):
    monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})

    def posting(job_id, location, secondary):
        return {"id": job_id, "title": "Machine Learning Intern", "location": location,
                "secondaryLocations": [{"location": place} for place in secondary],
                "jobUrl": f"https://jobs.ashbyhq.com/acme/{job_id}", "employmentType": "Intern"}

    transport = FixtureTransport({"api.ashbyhq.com": {"jobs": [
        posting("a1", "London", ["San Francisco", "New York", "Toronto"]),
        posting("a2", "EMEA", ["Canada"]),
        posting("a3", "London", ["Paris", "Berlin"]),
        posting("a4", "London", []),
    ]}})
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _filtered().scrape_board(client, "ashby", "acme", "Acme")

    passed = {job.url.rsplit("/", 1)[-1]: job for job in snapshot.jobs}
    assert set(passed) == {"a1", "a2"}
    assert snapshot.rejected == {"https://jobs.ashbyhq.com/acme/a3": "location",
                                 "https://jobs.ashbyhq.com/acme/a4": "location"}
    # The displayed location stays the primary; the country comes from the rest.
    assert passed["a1"].location == "London"
    assert passed["a1"].location_hint == "San Francisco; New York; Toronto"
    assert job_country(passed["a2"].location, hint=passed["a2"].location_hint) == "CA"


@pytest.mark.asyncio
async def test_lever_all_locations_feed_the_na_verdict(monkeypatch):
    monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})

    def posting(job_id, location, everywhere):
        return {"id": job_id, "text": "Software Engineer Intern",
                "hostedUrl": f"https://jobs.lever.co/acme/{job_id}",
                "categories": {"location": location, "allLocations": everywhere,
                               "commitment": "Intern"}}

    transport = FixtureTransport({"api.lever.co": [
        posting("l1", "London, UK", ["London, UK", "New York, NY"]),
        posting("l2", "London, UK", ["London, UK", "Dublin, Ireland"]),
    ]})
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _filtered().scrape_board(client, "lever", "acme", "Acme")

    assert [job.url for job in snapshot.jobs] == ["https://jobs.lever.co/acme/l1"]
    assert snapshot.jobs[0].location_hint == "New York, NY"
    assert job_country("London, UK", hint=snapshot.jobs[0].location_hint) == "US"
    assert snapshot.rejected == {"https://jobs.lever.co/acme/l2": "location"}


@pytest.mark.asyncio
async def test_a_bare_remote_other_location_vouches_for_nothing(monkeypatch):
    """Live 2026-09-29: Perplexity's "Belgrade" posting is also open
    "Remote", and Hopper's "Brazil - Remote" one "Curitiba - Remote". Neither
    names a North American place, so neither is a North American posting."""
    monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})

    def ashby(job_id, location, secondary):
        return {"id": job_id, "title": "Machine Learning Intern", "location": location,
                "secondaryLocations": [{"location": place} for place in secondary],
                "jobUrl": f"https://jobs.ashbyhq.com/acme/{job_id}", "employmentType": "Intern"}

    transport = FixtureTransport({
        "api.ashbyhq.com": {"jobs": [
            ashby("a1", "Belgrade", ["Remote"]),
            ashby("a2", "Brazil - Remote", ["Curitiba - Remote", "Remote - Worldwide"]),
            ashby("a3", "London", ["Remote", "Remote - US", "Toronto, ON; Remote"]),
            ashby("a4", "London", ["Remote - North America"]),
        ]},
        "api.lever.co": [
            {"id": "l1", "text": "Software Engineer Intern",
             "hostedUrl": "https://jobs.lever.co/acme/l1",
             "categories": {"location": "London, UK", "allLocations": ["London, UK", "Remote"],
                            "commitment": "Intern"}},
        ],
    })
    async with httpx.AsyncClient(transport=transport) as client:
        ashby_board = await _filtered().scrape_board(client, "ashby", "acme", "Acme")
        lever_board = await _filtered().scrape_board(client, "lever", "acme", "Acme")

    passed = {job.url.rsplit("/", 1)[-1]: job.location_hint for job in ashby_board.jobs}
    assert passed == {"a3": "Remote - US; Toronto, ON; Remote", "a4": "Remote - North America"}
    assert ashby_board.rejected == {"https://jobs.ashbyhq.com/acme/a1": "location",
                                    "https://jobs.ashbyhq.com/acme/a2": "location"}
    assert lever_board.jobs == []
    assert lever_board.rejected == {"https://jobs.lever.co/acme/l1": "location"}


# ─── Padded titles and locations ─────────────────────────────────────────────
# Boards pad them (2,301 titles and 358 locations in the 2026-09-29 crawl:
# Carvana's " Entry-level Auto Body Inspector", SoFi's "Frisco, TX ",
# ServiceNow's "Austin, Texas , us"). Every fetcher strips them.

@pytest.mark.asyncio
async def test_every_fetcher_strips_titles_and_locations(workday_registry):
    transport = FixtureTransport({
        "boards-api.greenhouse.io": {"jobs": [
            {"id": 1, "title": " Software Intern ", "location": {"name": "Frisco, TX "},
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"}]},
        "api.lever.co": [
            {"id": "l1", "text": "Software Intern ", "hostedUrl": "https://jobs.lever.co/acme/l1",
             "categories": {"location": " Toronto, ON"}}],
        "api.ashbyhq.com": {"jobs": [
            {"id": "a1", "title": "\tSoftware Intern", "location": "Toronto ",
             "jobUrl": "https://jobs.ashbyhq.com/acme/a1"}]},
        "api.smartrecruiters.com": {"totalFound": 1, "content": [
            {"id": "1", "name": "Software Intern  ",
             "location": {"city": "Austin ", "region": "Texas ", "country": "us"}}]},
        "/wday/cxs/acmebank/external/jobs": [{"total": 1, "jobPostings": [
            {"title": " Software Intern", "locationsText": "Toronto, ON ",
             "externalPath": "/job/Toronto-ON/Software-Intern_R1", "bulletFields": ["R1"]}]}],
    })
    expected = {("greenhouse", "acme"): "Frisco, TX", ("lever", "acme"): "Toronto, ON",
                ("ashby", "acme"): "Toronto", ("smartrecruiters", "acme"): "Austin, Texas, us",
                ("workday", "acmebank"): "Toronto, ON"}
    async with httpx.AsyncClient(transport=transport) as client:
        for (platform, slug), location in expected.items():
            snapshot = await _unfiltered().scrape_board(client, platform, slug, "Acme")
            [job] = snapshot.jobs
            assert (job.title, job.location) == ("Software Intern", location), platform


# ─── SmartRecruiters ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_smartrecruiters_fixture_parses_and_reports_complete():
    transport = FixtureTransport({"api.smartrecruiters.com": _load("smartrecruiters_board.json")})
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _unfiltered().scrape_board(client, "smartrecruiters", "Acme", "Acme")

    assert snapshot.complete
    assert snapshot.total_listed == 2
    intern = next(j for j in snapshot.jobs if "Intern" in j.title)
    assert intern.external_id == "744000012345"
    assert intern.employment_type == "Intern"
    assert "Montreal" in intern.location


# ─── Workday ─────────────────────────────────────────────────────────────────

@pytest.fixture
def workday_registry(monkeypatch):
    monkeypatch.setattr(
        company_registry, "load_workday_bases",
        lambda: {"acmebank": "https://acmebank.wd3.myworkdayjobs.com/wday/cxs/acmebank/external"},
    )


@pytest.mark.asyncio
async def test_workday_fixture_pages_and_parses(workday_registry):
    transport = FixtureTransport({
        "/wday/cxs/acmebank/external/jobs": [
            _load("workday_board_page1.json"),
            _load("workday_board_page2.json"),
            {"total": 23, "jobPostings": []},
        ],
    })
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _unfiltered().scrape_board(client, "workday", "acmebank", "Acme Bank")

    assert len(snapshot.jobs) == 3
    assert snapshot.total_listed == 23
    assert not snapshot.complete  # 3 fetched < 23 listed → partial

    coop = next(j for j in snapshot.jobs if "Co-op" in j.title)
    assert coop.external_id == "R-48123"
    assert coop.url == (
        "https://acmebank.wd3.myworkdayjobs.com/external"
        "/job/Toronto-ON-CAN/Software-Developer-Co-op--Fall-2026-_R-48123"
    )
    assert coop.detail_ref.startswith("/job/")
    assert coop.posted_date is not None

    # Pagination sent increasing offsets.
    offsets = [json.loads(r.content)["offset"] for r in transport.requests]
    assert offsets == [0, 20, 40]


@pytest.mark.asyncio
async def test_workday_entry_filter_applies(workday_registry):
    transport = FixtureTransport({
        "/wday/cxs/acmebank/external/jobs": [
            _load("workday_board_page1.json"),
            _load("workday_board_page2.json"),
            {"total": 23, "jobPostings": []},
        ],
    })
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _filtered().scrape_board(client, "workday", "acmebank", "Acme Bank")

    titles = {j.title for j in snapshot.jobs}
    assert "Vice President, Risk Management" not in titles
    assert any("Co-op" in t for t in titles)
    # The VP posting is still on the board as far as reconciliation knows.
    assert any("Vice-President" in u for u in snapshot.all_urls)


@pytest.mark.asyncio
async def test_workday_detail_fetch(workday_registry):
    transport = FixtureTransport({
        "/wday/cxs/acmebank/external/job/": _load("workday_detail.json"),
    })
    async with httpx.AsyncClient(transport=transport) as client:
        detail = await fetch_workday_detail(
            client, "acmebank",
            "/job/Toronto-ON-CAN/Software-Developer-Co-op--Fall-2026-_R-48123",
        )

    assert "Java" in detail["description"]
    assert "<p>" not in detail["description"]
    assert detail["employment_type"] == "Full time"


@pytest.mark.asyncio
async def test_workday_without_template_returns_empty_incomplete(monkeypatch):
    monkeypatch.setattr(company_registry, "load_workday_bases", lambda: {})
    async with httpx.AsyncClient() as client:
        snapshot = await _unfiltered().scrape_board(client, "workday", "nobase", "No Base")
    assert snapshot.jobs == []
    assert not snapshot.complete


def test_workday_public_base_derivation():
    assert workday_public_base(
        "https://bmo.wd3.myworkdayjobs.com/wday/cxs/bmo/external"
    ) == "https://bmo.wd3.myworkdayjobs.com/external"
    assert workday_public_base(
        "https://salesforce.wd12.myworkdayjobs.com/wday/cxs/salesforce/External_Career_Site/"
    ) == "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"


# ─── Registry gating ─────────────────────────────────────────────────────────

def test_registry_supports_workday_only_with_template():
    companies = company_registry.load_companies()
    workday_slugs = {slug for platform, slug, _ in companies if platform == "workday"}
    bases = company_registry.load_workday_bases()
    # Every supported workday board has a CxS base; none ship without one.
    assert workday_slugs, "expected at least one workday board with a template"
    assert workday_slugs <= set(bases.keys())


def test_every_disabled_board_says_why():
    # The reason is what stops the next person from re-enabling a dead slug,
    # or from searching again for a board already known to be gone.
    for entry in company_registry._load_raw():
        if not entry.get("enabled", True):
            assert (entry.get("disabled_reason") or "").strip(), entry["company_name"]


def test_every_enabled_workday_board_has_a_template():
    # load_companies() silently skips a template-less workday entry: it reads
    # as covered in the registry and is never crawled (28 were, until 2026-09).
    for entry in company_registry._load_raw():
        if entry.get("ats_platform") == "workday" and entry.get("enabled", True):
            assert (entry.get("workday_url_template") or "").strip(), entry["company_name"]


def test_workday_templates_are_cxs_bases_of_their_tenant():
    for slug, base in company_registry.load_workday_bases().items():
        # myworkdayjobs.com names the tenant in the host; myworkdaysite.com
        # serves every tenant from a bare pod host (wd1) and names it only in
        # the path.
        m = re.match(
            r"^https://(?:([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com|wd\d+\.myworkdaysite\.com)"
            r"/wday/cxs/([a-z0-9-]+)/[^/]+$",
            base, re.IGNORECASE)
        assert m, (slug, base)
        assert m.group(1) is None or m.group(1).lower() == m.group(2).lower(), (slug, base)


def test_multi_site_workday_tenants_never_use_the_bare_tenant_slug():
    # _adopt_site_rows() hands tenant-keyed rows only to a site whose slug is
    # not the tenant; a site crawled as the bare tenant would reconcile, and
    # remove, its sibling site's rows.
    by_tenant = defaultdict(list)
    for slug, base in company_registry.load_workday_bases().items():
        tenant = re.match(r"https://([^.]+)\.", base).group(1).lower()
        by_tenant[tenant].append(slug)
    for tenant, slugs in by_tenant.items():
        if len(slugs) > 1:
            assert tenant not in slugs, (tenant, slugs)


# The 2026-09-29 rollout of the template-less Workday entries: slug → tenant.
_WAVE_ONE_WORKDAY = {
    "adobe": "adobe", "boeing": "boeing", "capitalone": "capitalone",
    "lifeworks": "lifeworks", "pwc-us-entry": "pwc", "rbc-early": "rbc",
    "workday": "workday",
}


def test_workday_rollout_crawls_wave_one_and_holds_the_rest():
    # TD, RBC's global site and both Morgan Stanley sites keep a verified
    # template but wait for a product call (their passes are mostly corporate
    # "Associate"/"Analyst" titles), and so do Booz Allen and Leidos (mid-level
    # or clearance analysts: "Billing Analyst, Mid", "Target Analyst Reporter
    # - Level 2"); PwC's worldwide campus site waits for country-from-detail.
    # Held means listed with a base, never crawled.
    crawled = {slug for platform, slug, _ in company_registry.load_companies()
               if platform == "workday"}
    bases = company_registry.load_workday_bases()
    for slug, tenant in _WAVE_ONE_WORKDAY.items():
        assert slug in crawled, slug
        assert bases[slug].startswith(f"https://{tenant}.wd"), (slug, bases[slug])
    for slug in ("td", "rbc-global", "morganstanley", "morganstanley-private", "pwc-campus",
                 "bah", "leidos"):
        assert slug in bases and slug not in crawled, slug


def test_only_one_country_workday_boards_carry_a_country_hint():
    # A hint waives the NA filter for the whole board, so it only goes on a
    # site whose every posting is in that country. Checked live 2026-09-29
    # against the CxS country facets: RBC's early-talent site also posts in
    # Malaysia (13 of 45), TELUS Health in Australia, NZ and the UK, Capital
    # One and Boeing in the UK, the rest worldwide. PwC's US entry-level site
    # is US only: 197 of 213 by location text, the 16 "N Locations" ones by
    # their requisition country.
    countries = company_registry.load_board_countries()
    assert countries["workday:pwc-us-entry"] == "US"
    for slug in set(_WAVE_ONE_WORKDAY) - {"pwc-us-entry"}:
        assert f"workday:{slug}" not in countries, slug


@pytest.mark.asyncio
async def test_us_only_board_keeps_its_multi_location_postings():
    # PwC lists a multi-city posting as "15 Locations", which the NA filter
    # can't place (14 of its 211 entry-level postings on 2026-09-29); the
    # board's "US" hint keeps them. The level filter still applies.
    def posting(title, where, req):
        return {"title": title, "locationsText": where, "postedOn": "Posted Today",
                "externalPath": f"/job/IL-Rosemont/{title.replace(' ', '-')}_{req}",
                "bulletFields": [req]}

    page = {"total": 3, "jobPostings": [
        posting("Transfer Pricing - Intern - Summer 2027", "15 Locations", "760001WD"),
        posting("Tax - Intern - Winter 2027", "CA-San Francisco", "760002WD"),
        posting("Transfer Pricing PhD - Senior Associate", "9 Locations", "760003WD"),
    ]}
    transport = FixtureTransport({"/wday/cxs/pwc/US_Entry_Level_Careers/jobs": [page]})
    async with httpx.AsyncClient(transport=transport) as client:
        snapshot = await _filtered().scrape_board(client, "workday", "pwc-us-entry", "PwC")

    assert {j.title for j in snapshot.jobs} == {
        "Transfer Pricing - Intern - Summer 2027", "Tax - Intern - Winter 2027",
    }
    assert snapshot.complete and len(snapshot.all_urls) == 3


# ─── North America filter ────────────────────────────────────────────────────

@pytest.mark.parametrize("location", [
    "Los Gatos", "Burbank", "Las Vegas", "King of Prussia", "London, ON",
    "London, Ontario", "London, Ontario, Canada", "Toronto", "New York, NY, USA",
    "San Francisco HQ",
])
def test_north_america_filter_keeps(location):
    assert ATSScraper()._is_north_america(location)


@pytest.mark.parametrize("location", [
    "London", "London Office", "London, UK", "London, England",
    "London, United Kingdom", "GB-London", "Hybrid - London",
])
def test_north_america_filter_drops_london_uk(location):
    # Bare "london" was a Canadian city here, so every London, UK role passed.
    assert not ATSScraper()._is_north_america(location)


def test_one_country_board_keeps_a_bare_ambiguous_city():
    # BDO Canada's Workday board lists bare cities: its "London" is Ontario.
    assert company_registry.load_board_countries()["workday:bdo"] == "CA"
    job = ATSJob(title="Financial Analyst", company="BDO", location="London",
                 url="https://bdo.wd3.myworkdayjobs.com/Bdo/job/London/x_JR1")
    scraper = ATSScraper()
    assert scraper._passes_filters(job, "CA")
    assert not scraper._passes_filters(job)
