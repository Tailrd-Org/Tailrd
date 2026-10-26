"""INGEST_V2_RUNNER, the cut-over from cron-ats to the v2 runner.

Both paths stamp source_health.last_success_at when a board's crawl succeeds.
With the flag on, cron-ats leaves every board crawled in the last 20 hours to
the other path, so the two never crawl one board twice in a cycle. With the
flag off (the default) it crawls its shard exactly as it always did.
"""

import datetime

import pytest

from backend.data import company_registry
from backend.db.models import SourceHealth
from backend.routers import github_sources
from backend.services.ats_scraper import ATSJob, ATSScraper, BoardSnapshot

SECRET = "test-cron-secret"


def _hours_ago(hours):
    return datetime.datetime.utcnow() - datetime.timedelta(hours=hours)


def _health(db, slug, *, hours_ago=None, failures=0):
    db.add(SourceHealth(
        board_key=f"greenhouse:{slug}", platform="greenhouse", slug=slug,
        last_success_at=None if hours_ago is None else _hours_ago(hours_ago),
        consecutive_failures=failures,
        last_failure_at=_hours_ago(1) if failures else None,
    ))
    db.commit()


def _stamp(db, slug):
    db.expire_all()
    row = db.query(SourceHealth).filter(SourceHealth.board_key == f"greenhouse:{slug}").one()
    return row.last_success_at


@pytest.fixture
def cron(client, db_session, monkeypatch):
    """Call cron-ats over ``slugs`` (one shard, so every board is in it) and
    return (response body, the slugs the scraper was asked to crawl)."""
    import backend.auth.dependencies as auth_deps

    monkeypatch.setattr(auth_deps, "CRON_SECRET", SECRET)
    monkeypatch.setenv("CRON_ATS_SHARDS", "1")
    monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})
    crawled = []

    async def fake_scrape_board(self, http, platform, slug, company_name):
        crawled.append(slug)
        job = ATSJob(title="Software Developer Intern", company=company_name,
                     location="Toronto, ON", description="Build and ship internal tools.",
                     url=f"https://boards.greenhouse.io/{slug}/jobs/1")
        return BoardSnapshot(platform=platform, slug=slug, company=company_name, jobs=[job],
                             all_urls={job.url}, complete=True, total_listed=1)

    monkeypatch.setattr(ATSScraper, "scrape_board", fake_scrape_board)

    def call(slugs, flag=None):
        if flag is None:
            monkeypatch.delenv("INGEST_V2_RUNNER", raising=False)
        else:
            monkeypatch.setenv("INGEST_V2_RUNNER", flag)
        monkeypatch.setattr(company_registry, "load_companies",
                            lambda **kw: [("greenhouse", slug, slug.title()) for slug in slugs])
        crawled.clear()
        res = client.post("/github-sources/cron-ats", headers={"x-cron-secret": SECRET})
        assert res.status_code == 200, res.text
        return res.json(), sorted(crawled)

    return call


# --- the flag off: nothing changes ------------------------------------------


def test_off_by_default_every_board_is_crawled_whenever_it_was_last_crawled(cron, db_session):
    _health(db_session, "fresh", hours_ago=1)
    _health(db_session, "stale", hours_ago=30)

    body, crawled = cron(["fresh", "stale", "never"])

    assert crawled == ["fresh", "never", "stale"]
    assert "boards_skipped_v2" not in body
    assert body["status"] == "completed" and body["new_jobs"] == 3


@pytest.mark.parametrize("flag", ["", " ", "0", "false", "no", "off", "banana"])
def test_anything_but_a_yes_leaves_the_flag_off(cron, db_session, flag):
    _health(db_session, "fresh", hours_ago=1)

    body, crawled = cron(["fresh"], flag=flag)

    assert crawled == ["fresh"]
    assert "boards_skipped_v2" not in body


# --- the flag on --------------------------------------------------------------


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on", " 1 "])
def test_with_the_flag_on_a_board_crawled_in_the_last_20_hours_is_skipped(cron, db_session, flag):
    assert github_sources.V2_RUNNER_FRESH_HOURS == 20
    _health(db_session, "just-now", hours_ago=1)
    _health(db_session, "almost", hours_ago=19.5)
    _health(db_session, "lapsed", hours_ago=20.5)
    _health(db_session, "old", hours_ago=70)
    _health(db_session, "never-succeeded", hours_ago=None, failures=2)

    body, crawled = cron(["just-now", "almost", "lapsed", "old", "never-succeeded", "no-row"],
                         flag=flag)

    assert crawled == ["lapsed", "never-succeeded", "no-row", "old"]
    assert body["boards_skipped_v2"] == 2
    assert body["new_jobs"] == 4
    assert body["boards_failed"] == 0


def test_a_skipped_board_is_left_exactly_as_it_was(cron, db_session):
    _health(db_session, "fresh", hours_ago=2)
    _health(db_session, "lapsed", hours_ago=25)
    before = _stamp(db_session, "fresh")

    cron(["fresh", "lapsed"], flag="1")

    assert _stamp(db_session, "fresh") == before
    assert _stamp(db_session, "lapsed") > _hours_ago(0.1)


def test_a_board_this_endpoint_crawled_is_not_crawled_again_in_the_same_cycle(cron):
    first, crawled_first = cron(["a", "b"], flag="1")
    second, crawled_second = cron(["a", "b"], flag="1")

    assert crawled_first == ["a", "b"] and first["boards_skipped_v2"] == 0
    assert crawled_second == [] and second["boards_skipped_v2"] == 2
    assert second["status"] == "completed" and second["new_jobs"] == 0


def test_a_board_in_cooldown_is_counted_once_as_cooldown(cron, db_session):
    _health(db_session, "broken", hours_ago=1, failures=5)

    body, crawled = cron(["broken"], flag="1")

    assert crawled == []
    assert body["boards_skipped_cooldown"] == 1
    assert body["boards_skipped_v2"] == 0


def test_turning_the_flag_off_again_resumes_crawling_everything(cron, db_session):
    _health(db_session, "fresh", hours_ago=1)

    _on, crawled_on = cron(["fresh"], flag="1")
    off, crawled_off = cron(["fresh"], flag="0")

    assert crawled_on == []
    assert crawled_off == ["fresh"]
    assert "boards_skipped_v2" not in off
