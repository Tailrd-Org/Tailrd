"""Where the scraper, the registry and the liveness checks hand a platform
beyond the five built-ins to its v2 adapter, and where they don't."""

import types

import httpx
import pytest

from backend.data import company_registry
from backend.ingest import adapters
from backend.services import platform_liveness
from backend.services.ats_scraper import ATSJob, ATSScraper
from backend.services.platform_liveness import LivenessResult, check_listing


def make_adapter(platform, *, jobs=(), complete=True, total=None, required_env=(),
                 host_patterns=(), check_alive=None):
    """An adapter module as the contract describes it; ``calls`` records
    every list_jobs call."""
    module = types.ModuleType(f"fake_adapter_{platform}")
    module.PLATFORM = platform
    module.TIER = 1
    module.COUNTRIES = ("CA", "US")
    module.RATE_LIMIT = 1.0
    module.REQUIRED_ENV = tuple(required_env)
    module.HOST_PATTERNS = tuple(host_patterns)
    module.calls = []

    async def list_jobs(client, slug, company_name, *, config, deadline=None):
        module.calls.append({"client": client, "slug": slug, "company_name": company_name,
                             "config": config, "deadline": deadline})
        return list(jobs), complete, len(jobs) if total is None else total

    module.list_jobs = list_jobs
    if check_alive is not None:
        module.check_alive = check_alive
    return module


@pytest.fixture
def use_adapters(monkeypatch):
    def install(*modules):
        monkeypatch.setattr(adapters, "_registry", {m.PLATFORM: m for m in modules})
    return install


@pytest.fixture
def registry(monkeypatch):
    def install(entries):
        monkeypatch.setattr(company_registry, "_load_raw", lambda: entries)
    return install


def _job(n, title, location):
    return ATSJob(title=title, company="Acme", location=location,
                  url=f"https://jobs.fakeats.test/acme/{n}", external_id=f"fakeats:acme:{n}")


ACME = [
    _job(1, "Software Engineering Intern", "Toronto, ON"),
    _job(2, "Senior Staff Engineer", "Toronto, ON"),
    _job(3, "Data Analyst Intern", "London, United Kingdom"),
]


# --- ATSScraper.scrape_board ---------------------------------------------------------

@pytest.mark.asyncio
async def test_scrape_board_lists_an_adapter_platform_through_its_adapter(use_adapters, registry):
    adapter = make_adapter("fakeats", jobs=ACME, total=40)
    use_adapters(adapter)
    registry([{"company_name": "Acme", "ats_platform": "fakeats", "board_slug": "acme",
               "enabled": True, "config": {"region": "ca", "pages": 2}}])

    async with httpx.AsyncClient() as client:
        snapshot = await ATSScraper(deadline=123.0).scrape_board(client, "fakeats", "acme", "Acme")

    assert adapter.calls == [{"client": client, "slug": "acme", "company_name": "Acme",
                              "config": {"region": "ca", "pages": 2}, "deadline": 123.0}]
    assert snapshot.board_key == "fakeats:acme"
    assert (snapshot.complete, snapshot.total_listed) == (True, 40)
    # Every listing proves life; the scraper's own filters apply as to a built-in.
    assert snapshot.all_urls == {job.url for job in ACME}
    assert [job.url for job in snapshot.jobs] == [ACME[0].url]
    assert snapshot.rejected == {ACME[1].url: "level", ACME[2].url: "location"}


@pytest.mark.asyncio
async def test_an_incomplete_adapter_listing_stays_incomplete(use_adapters, registry):
    use_adapters(make_adapter("fakeats", jobs=ACME[:1], complete=False, total=500))
    registry([])

    async with httpx.AsyncClient() as client:
        snapshot = await ATSScraper().scrape_board(client, "fakeats", "acme", "Acme")

    assert (snapshot.complete, snapshot.total_listed) == (False, 500)
    assert snapshot.all_urls == {ACME[0].url}


@pytest.mark.asyncio
async def test_a_disabled_or_unknown_platform_keeps_the_empty_incomplete_snapshot(
        use_adapters, registry, monkeypatch):
    monkeypatch.delenv("T1_FAKE_API_KEY", raising=False)
    adapter = make_adapter("keyedats", jobs=ACME, required_env=("T1_FAKE_API_KEY",))
    use_adapters(adapter)
    registry([])

    async with httpx.AsyncClient() as client:
        for platform in ("keyedats", "nosuchats"):
            snapshot = await ATSScraper().scrape_board(client, platform, "acme", "Acme")
            assert (snapshot.jobs, snapshot.all_urls, snapshot.complete) == ([], set(), False)
        assert adapter.calls == []

        monkeypatch.setenv("T1_FAKE_API_KEY", "key")  # the key arrives: it crawls
        snapshot = await ATSScraper().scrape_board(client, "keyedats", "acme", "Acme")

    assert len(adapter.calls) == 1 and snapshot.complete is True


# --- posting URLs fit to store ----------------------------------------------------------

def _intern(url):
    return ATSJob(title="Software Engineering Intern", company="Acme", location="Toronto, ON",
                  url=url)


@pytest.mark.asyncio
async def test_a_listing_whose_url_is_not_an_http_page_is_dropped(use_adapters, registry):
    """A board's own HTML can carry any scheme: an iCIMS anchor
    href="javascript:/jobs/1/%0aalert(document.cookie)" passes the job path
    check, urljoin keeps it, and the feed renders the stored URL as a link."""
    unsafe = [_intern(url) for url in (
        "javascript:///jobs/1/%0aalert(document.cookie)", "JavaScript:alert(1)",
        "data:text/html,<script>alert(1)</script>", "ftp://jobs.fakeats.test/acme/9",
        "//jobs.fakeats.test/acme/8", "/jobs/7", "https:///jobs/6")]
    use_adapters(make_adapter("fakeats", jobs=[ACME[0], *unsafe]))
    registry([])

    async with httpx.AsyncClient() as client:
        snapshot = await ATSScraper().scrape_board(client, "fakeats", "acme", "Acme")

    assert snapshot.all_urls == {ACME[0].url}
    assert [job.url for job in snapshot.jobs] == [ACME[0].url]
    assert snapshot.complete is False  # what it could not use never votes on removals


@pytest.mark.asyncio
async def test_a_host_configured_board_keeps_only_listings_on_its_host(use_adapters, registry):
    on_host = _intern("https://careers-acme.icims.com/jobs/1/intern/job")
    elsewhere = _intern("https://evil.example/jobs/2/intern/job")
    look_alike = _intern("https://careers-acme.icims.com.evil.example/jobs/3/intern/job")
    use_adapters(make_adapter("icims", jobs=[on_host, elsewhere, look_alike]))
    registry([{"company_name": "Acme", "ats_platform": "icims", "board_slug": "acme",
               "enabled": True, "config": {"host": "HTTPS://Careers-Acme.icims.com/"}}])

    async with httpx.AsyncClient() as client:
        snapshot = await ATSScraper().scrape_board(client, "icims", "acme", "Acme")

    assert snapshot.all_urls == {on_host.url}
    assert snapshot.complete is False


@pytest.mark.asyncio
async def test_other_boards_keep_http_listings_on_any_host(use_adapters, registry):
    """A Recruitee offer's careers_url may be the employer's own domain."""
    offer = _intern("https://careers.acme.example/o/software-engineering-intern")
    use_adapters(make_adapter("recruitee", jobs=[offer]))
    registry([{"company_name": "Acme", "ats_platform": "recruitee", "board_slug": "acme",
               "enabled": True, "config": {"host": "acme.recruitee.com"}}])

    async with httpx.AsyncClient() as client:
        snapshot = await ATSScraper().scrape_board(client, "recruitee", "acme", "Acme")

    assert snapshot.all_urls == {offer.url} and snapshot.complete is True


def test_web_url_and_config_host():
    from backend.services.ats_scraper import CONFIG_HOST_PLATFORMS, config_host, web_url

    assert web_url("https://careers-acme.icims.com/jobs/1") and web_url("http://a.example")
    for bad in ("javascript:alert(1)", " javascript:alert(1)", "data:,x", "ftp://a.example/1",
                "//a.example/1", "/jobs/1", "https://", "", None, 7):
        assert not web_url(bad), bad
    assert config_host({"host": "HTTPS://Careers-Acme.icims.com/jobs"}) == "careers-acme.icims.com"
    assert config_host({}) == config_host(None) == ""
    assert CONFIG_HOST_PLATFORMS == {"icims", "jibe", "oracle_hcm", "successfactors", "taleo"}


# --- company_registry ------------------------------------------------------------------

def test_load_companies_also_accepts_platforms_whose_adapter_is_enabled(
        use_adapters, registry, monkeypatch):
    monkeypatch.delenv("T1_FAKE_API_KEY", raising=False)
    use_adapters(make_adapter("fakeats"),
                 make_adapter("keyedats", required_env=("T1_FAKE_API_KEY",)))
    registry([
        {"company_name": "Acme", "ats_platform": "greenhouse", "board_slug": "acme", "enabled": True},
        {"company_name": "Beta", "ats_platform": "FakeATS", "board_slug": "beta", "enabled": True},
        {"company_name": "Gamma", "ats_platform": "keyedats", "board_slug": "gamma", "enabled": True},
        {"company_name": "Delta", "ats_platform": "custom", "board_slug": "delta", "enabled": True},
        {"company_name": "Eps", "ats_platform": "fakeats", "board_slug": "eps", "enabled": False},
    ])

    assert company_registry.SUPPORTED_PLATFORMS == {
        "greenhouse", "lever", "ashby", "smartrecruiters", "workday"}
    # keyedats lacks its key, custom has no adapter, eps is disabled.
    assert company_registry.load_companies() == [("greenhouse", "acme", "Acme"),
                                                 ("fakeats", "beta", "Beta")]
    monkeypatch.setenv("T1_FAKE_API_KEY", "key")
    assert ("keyedats", "gamma", "Gamma") in company_registry.load_companies()
    assert len(company_registry.load_companies(supported_only=False)) == 4
    assert len(company_registry.load_companies(include_disabled=True, supported_only=False)) == 5


def test_load_board_config_returns_the_entrys_config_object(registry):
    registry([
        {"company_name": "JPMorgan Chase", "ats_platform": "oracle_hcm", "board_slug": "jpmc",
         "config": {"host": "jpmc.fa.oraclecloud.com", "site": "CX_1001", "facets": ["a"]}},
        {"company_name": "JPMorgan Chase", "ats_platform": "oracle_hcm", "board_slug": "jpmc",
         "config": {"host": "listed-twice.example"}},
        {"company_name": "Acme", "ats_platform": "greenhouse", "board_slug": "acme"},
        {"company_name": "Odd", "ats_platform": "workable", "board_slug": "odd", "config": ["x"]},
        {"company_name": None, "ats_platform": 7, "board_slug": "bad"},  # malformed: skipped
    ])

    config = company_registry.load_board_config("Oracle_HCM", " jpmc ")
    assert config == {"host": "jpmc.fa.oraclecloud.com", "site": "CX_1001", "facets": ["a"]}
    config["facets"].append("b")  # a copy: the cached registry is untouched
    assert company_registry.load_board_config("oracle_hcm", "jpmc")["facets"] == ["a"]
    assert company_registry.load_board_config("greenhouse", "acme") == {}
    assert company_registry.load_board_config("workable", "odd") == {}
    assert company_registry.load_board_config("workable", "missing") == {}


def test_load_board_countries_reads_country_not_hq_country(registry):
    # country: the board posts in that one country (switches the location
    # filter off); hq_country: where the employer is based, never read here.
    registry([
        {"company_name": "SickKids", "ats_platform": "workday", "board_slug": "sickkids",
         "country": "ca", "hq_country": "CA"},
        {"company_name": "RBC", "ats_platform": "workday", "board_slug": "rbc", "hq_country": "CA"},
        {"company_name": "Mayo Clinic", "ats_platform": "fakeats", "board_slug": "mayo", "country": "US"},
    ])

    assert company_registry.load_board_countries() == {"workday:sickkids": "CA",
                                                       "fakeats:mayo": "US"}


# --- platform_liveness ----------------------------------------------------------------

POSTING = "https://jobs.fakeats.test/acme/1"


def _pages(status: int):
    """A transport answering every URL with ``status``; returns (transport, asked URLs)."""
    asked: list[str] = []

    def answer(request):
        asked.append(str(request.url))
        return httpx.Response(status, text="<html><body>A posting</body></html>")

    return httpx.MockTransport(answer), asked


@pytest.mark.asyncio
async def test_a_url_an_adapter_owns_is_asked_through_its_check_alive(use_adapters):
    seen = []

    async def check_alive(client, url):
        seen.append((client, url))
        return LivenessResult("dead", "fakeats_api_404", True)

    use_adapters(make_adapter("fakeats", host_patterns=(r"jobs\.fakeats\.test/",),
                              check_alive=check_alive))
    transport, asked = _pages(200)

    async with platform_liveness.make_client(transport=transport) as client:
        result = await check_listing(client, POSTING)

    assert result == LivenessResult("dead", "fakeats_api_404", True)
    assert seen == [(client, POSTING)]
    assert asked == []  # the page itself was never fetched


@pytest.mark.asyncio
async def test_an_owned_url_without_check_alive_gets_the_page_check(use_adapters):
    use_adapters(make_adapter("fakeats", host_patterns=(r"jobs\.fakeats\.test/",)))
    transport, asked = _pages(404)

    async with platform_liveness.make_client(transport=transport) as client:
        result = await check_listing(client, POSTING)

    assert result == LivenessResult("dead", "http_404")
    assert asked == [POSTING]


@pytest.mark.asyncio
async def test_a_disabled_adapter_is_not_asked(use_adapters, monkeypatch):
    monkeypatch.delenv("T1_FAKE_API_KEY", raising=False)
    calls = []

    async def check_alive(client, url):
        calls.append(url)
        return LivenessResult("alive", "fakeats_api_200", True)

    use_adapters(make_adapter("fakeats", required_env=("T1_FAKE_API_KEY",),
                              host_patterns=(r"jobs\.fakeats\.test/",), check_alive=check_alive))
    transport, asked = _pages(404)

    async with platform_liveness.make_client(transport=transport) as client:
        result = await check_listing(client, POSTING)

    assert result == LivenessResult("dead", "http_404")
    assert calls == [] and asked == [POSTING]


@pytest.mark.asyncio
async def test_the_built_in_platform_checks_still_come_first(use_adapters):
    calls = []

    async def check_alive(client, url):
        calls.append(url)
        return LivenessResult("alive", "adapter", True)

    # An adapter claiming Greenhouse URLs never shadows the Greenhouse API check.
    use_adapters(make_adapter("ghclone", host_patterns=(r"greenhouse\.io",), check_alive=check_alive))
    transport, asked = _pages(404)

    async with platform_liveness.make_client(transport=transport) as client:
        result = await check_listing(client, "https://boards.greenhouse.io/acme/jobs/123")

    assert result == LivenessResult("dead", "gh_api_404", True)
    assert asked == ["https://boards-api.greenhouse.io/v1/boards/acme/jobs/123"]
    assert calls == []


@pytest.mark.asyncio
async def test_a_check_alive_answer_that_is_not_a_liveness_result_reads_as_unknown(use_adapters):
    async def check_alive(client, url):
        return None

    use_adapters(make_adapter("fakeats", host_patterns=(r"jobs\.fakeats\.test/",),
                              check_alive=check_alive))
    transport, _asked = _pages(200)

    async with platform_liveness.make_client(transport=transport) as client:
        assert await check_listing(client, POSTING) == LivenessResult("unknown", "error")
