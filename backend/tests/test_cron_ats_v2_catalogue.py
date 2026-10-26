"""CRON_ATS_V2_CATALOGUE: what cron-ats crawls and stores.

Off (the default), cron-ats keeps the catalogue it had before scraper v2: the
boards it crawled then (company_registry.load_student_catalogue), entry-level
postings only, and a stored row whose listing is not entry level retires as
off_target. So merging scraper v2 changes nothing about what production
stores. On, it crawls every registry board, the v2 adapters' too, and stores
every experience level. The orphan sweep of cron-freshness follows the same
switch: off, it retires an orphan row for its level and never restores one.
"""

import datetime
import types

import pytest

from backend.data import company_registry
from backend.db.models import ScrapedJob
from backend.ingest import adapters
from backend.routers import github_sources
from backend.services.ats_scraper import ATSJob, ATSScraper
from backend.services.listing_freshness import LISTING_ACTIVE, LISTING_OFF_TARGET

SECRET = "test-cron-secret"
INTERN = "https://boards.greenhouse.io/acme/jobs/1"
SENIOR = "https://boards.greenhouse.io/acme/jobs/2"
BAMBOO = "https://acme.bamboohr.com/careers/7"
REGISTRY = [
    {"company_name": "Acme", "ats_platform": "greenhouse", "board_slug": "acme", "enabled": True},
    {"company_name": "Acme", "ats_platform": "bamboohr", "board_slug": "acme", "enabled": True},
]


@pytest.fixture
def cron(client, monkeypatch):
    """POST cron-ats over a registry of a Greenhouse board that was crawled
    before scraper v2 (an intern and a senior posting) and a BambooHR board
    added with it. Returns (response body, the platforms crawled)."""
    import backend.auth.dependencies as auth_deps

    monkeypatch.setattr(auth_deps, "CRON_SECRET", SECRET)
    monkeypatch.setenv("CRON_ATS_SHARDS", "1")
    monkeypatch.setattr(company_registry, "_load_raw", lambda: REGISTRY)
    monkeypatch.setattr(company_registry, "load_student_catalogue",
                        lambda: frozenset({"greenhouse:acme"}))
    monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})

    async def fetch_greenhouse(self, client, slug, company_name):
        return [ATSJob(title="Software Engineering Intern", company=company_name,
                       location="Toronto, ON", url=INTERN, external_id="1",
                       description="Build internal tools."),
                ATSJob(title="Senior Software Engineer", company=company_name,
                       location="Toronto, ON", url=SENIOR, external_id="2",
                       description="Lead the platform team.")]

    monkeypatch.setattr(ATSScraper, "_fetch_greenhouse", fetch_greenhouse)

    bamboohr = types.ModuleType("fake_bamboohr")
    bamboohr.PLATFORM, bamboohr.TIER, bamboohr.REQUIRED_ENV = "bamboohr", 1, ()
    bamboohr.HOST_PATTERNS = (r"^https?://[\w-]+\.bamboohr\.com/careers/\d+",)

    async def list_jobs(client, slug, company_name, *, config, deadline=None):
        job = ATSJob(title="Payroll Specialist", company=company_name, location="Austin, TX",
                     url=BAMBOO, external_id=f"bamboohr:{slug}:7",
                     description="Run payroll for the US entities.")
        return [job], True, 1

    bamboohr.list_jobs = list_jobs
    monkeypatch.setattr(adapters, "_registry", {"bamboohr": bamboohr})

    crawled = []
    real_scrape_board = ATSScraper.scrape_board

    async def scrape_board(self, client, platform, slug, company_name):
        crawled.append(platform)
        return await real_scrape_board(self, client, platform, slug, company_name)

    monkeypatch.setattr(ATSScraper, "scrape_board", scrape_board)

    def call(flag=None):
        if flag is None:
            monkeypatch.delenv("CRON_ATS_V2_CATALOGUE", raising=False)
        else:
            monkeypatch.setenv("CRON_ATS_V2_CATALOGUE", flag)
        crawled.clear()
        res = client.post("/github-sources/cron-ats", headers={"x-cron-secret": SECRET})
        assert res.status_code == 200, res.text
        return res.json(), sorted(crawled)

    return call


def _statuses(db):
    db.expire_all()
    return {row.url: row.listing_status for row in db.query(ScrapedJob).all()}


def _stored_senior(db):
    db.add(ScrapedJob(title="Senior Software Engineer", company="Acme", url=SENIOR,
                      location="Toronto, ON", source_platform="ats", board_key="greenhouse:acme",
                      listing_status=LISTING_ACTIVE, experience_level="senior"))
    db.commit()


# --- off: the catalogue cron-ats had before scraper v2 ------------------------


def test_off_by_default_stores_the_entry_level_postings_of_the_old_boards(cron, db_session):
    body, crawled = cron()

    assert crawled == ["greenhouse"]  # the BambooHR board waits for the switch
    assert body["new_jobs"] == 1
    assert _statuses(db_session) == {INTERN: LISTING_ACTIVE}


@pytest.mark.parametrize("flag", ["", " ", "0", "false", "no", "off", "banana"])
def test_anything_but_a_yes_leaves_the_switch_off(cron, db_session, flag):
    body, crawled = cron(flag)

    assert crawled == ["greenhouse"]
    assert set(_statuses(db_session)) == {INTERN}


def test_off_a_stored_row_no_longer_entry_level_retires_as_before(cron, db_session):
    """The off_target level retirement cron-ats did before scraper v2: the
    board still lists the senior posting, but it is not the catalogue's."""
    _stored_senior(db_session)

    body, _crawled = cron()

    assert body["off_target"] == 1
    assert _statuses(db_session) == {INTERN: LISTING_ACTIVE, SENIOR: LISTING_OFF_TARGET}


# --- on: every board and every level ----------------------------------------


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_on_every_board_and_every_level_is_stored(cron, db_session, flag):
    body, crawled = cron(flag)

    assert crawled == ["bamboohr", "greenhouse"]
    assert body["new_jobs"] == 3
    assert _statuses(db_session) == {INTERN: LISTING_ACTIVE, SENIOR: LISTING_ACTIVE,
                                     BAMBOO: LISTING_ACTIVE}


def test_on_a_stored_row_of_any_level_stays_active(cron, db_session):
    _stored_senior(db_session)

    body, _crawled = cron("1")

    assert body["off_target"] == 0
    assert _statuses(db_session)[SENIOR] == LISTING_ACTIVE


def test_the_switch_is_read_on_every_call(monkeypatch):
    monkeypatch.delenv("CRON_ATS_V2_CATALOGUE", raising=False)
    assert github_sources.v2_catalogue_enabled() is False
    monkeypatch.setenv("CRON_ATS_V2_CATALOGUE", "on")
    assert github_sources.v2_catalogue_enabled() is True


# --- the orphan sweep of cron-freshness -------------------------------------


@pytest.fixture
def freshness(client, db_session, monkeypatch):
    """POST cron-freshness over two orphan rows no board crawl reaches (board
    key 'unknown'): a Senior Software Engineer that is active, and a Cleaner
    that production's sweep retired for its level before scraper v2. Returns
    (the orphan sweep's stats, the two rows' statuses)."""
    import backend.auth.dependencies as auth_deps
    from backend.services import listing_freshness

    monkeypatch.setattr(auth_deps, "CRON_SECRET", SECRET)

    async def no_probe(db, client_, limit=0, now=None, *, deadline=None, cache=None, **kw):
        return {"checked": 0}

    for name in ("verify_stale_listings", "verify_unconfirmed_active_listings",
                 "verify_recent_aggregator_listings"):
        monkeypatch.setattr(listing_freshness, name, no_probe)

    def make(n, title, status):
        db_session.add(ScrapedJob(
            title=title, company="Acme", url=f"https://jobs.nokia.com/{n}",
            location="Toronto, ON", source_platform="ats", board_key="unknown",
            listing_status=status, experience_level="new_grad",
            first_seen_at=datetime.datetime.utcnow(), last_seen_at=datetime.datetime.utcnow(),
            scraped_at=datetime.datetime.utcnow()))
        db_session.commit()

    make(1, "Senior Software Engineer", LISTING_ACTIVE)
    make(2, "Cleaner", LISTING_OFF_TARGET)

    def call(flag=None):
        if flag is None:
            monkeypatch.delenv("CRON_ATS_V2_CATALOGUE", raising=False)
        else:
            monkeypatch.setenv("CRON_ATS_V2_CATALOGUE", flag)
        res = client.post("/jobs/cron-freshness", headers={"x-cron-secret": SECRET})
        assert res.status_code == 200, res.text
        db_session.expire_all()
        statuses = {row.title: row.listing_status for row in db_session.query(ScrapedJob).all()}
        return res.json()["orphans_off_target"], statuses

    return call


def test_off_the_orphan_sweep_retires_for_the_level_and_restores_nothing(freshness):
    """Production retired orphan rows for their level before scraper v2. Left
    on the every-level rule, the sweep brought them back hourly after the
    merge: the Cleaner into the default feed, the rest into the level list."""
    stats, statuses = freshness()

    assert statuses == {"Senior Software Engineer": LISTING_OFF_TARGET,
                        "Cleaner": LISTING_OFF_TARGET}
    assert (stats["level"], stats["off_target"], stats.get("restored", 0)) == (1, 1, 0)


@pytest.mark.parametrize("flag", ["1", "true", "yes", "on"])
def test_on_the_orphan_sweep_keeps_every_level_and_restores_what_the_level_hid(freshness, flag):
    stats, statuses = freshness(flag)

    assert statuses == {"Senior Software Engineer": LISTING_ACTIVE, "Cleaner": LISTING_ACTIVE}
    assert (stats["level"], stats["off_target"], stats["restored"]) == (0, 0, 1)


# --- the boards crawled before scraper v2 -----------------------------------


def test_the_student_catalogue_is_the_registry_cron_ats_crawled_before_v2():
    """The 252 boards origin/main crawled, every one still enabled here: off,
    cron-ats crawls exactly them, all on the five built-in platforms."""
    catalogue = company_registry.load_student_catalogue()
    boards = company_registry.load_companies(student_catalogue=True)
    keys = {f"{platform}:{slug}" for platform, slug, _name in boards}

    assert len(catalogue) == 252
    assert keys == catalogue
    assert {platform for platform, _slug, _name in boards} <= company_registry.SUPPORTED_PLATFORMS
    assert keys < {f"{platform}:{slug}" for platform, slug, _name in company_registry.load_companies()}
