"""Board crawls retire rows whose own listing fails the crawler's filters.

A row the crawler stored under older, looser filters (bare "london" as a
Canadian city, pre-freshness rows that never passed the entry-level check)
stayed visible forever: reconciliation confirmed and revived every LISTED
url. These tests pin the fix: a listed row whose listing is off target goes
``off_target`` (hidden), stays there while the listing still fails, and comes
back when the posting's title or location changes so it passes. Rows no crawl
reaches (board_key '' / 'unknown') are judged on their stored fields by the
cron-freshness orphan sweep. Since scraper v2 every experience level is in
scope, so only a location verdict retires (RETIRABLE_REJECTIONS), and rows an
entry-level verdict hid come back.
"""

import asyncio
import datetime
import socket

import pytest

from backend.data import company_registry
from backend.db.models import ScrapedJob
from backend.services import platform_liveness
from backend.services.ats_scraper import ATSJob, ATSScraper, BoardSnapshot
from backend.services.cross_source_dedup import stands_in_for_twins
from backend.services.listing_freshness import (
    CLOSED_LISTING_STATUSES,
    HIDDEN_LISTING_STATUSES,
    LISTING_ACTIVE,
    LISTING_EXPIRED,
    LISTING_OFF_TARGET,
    LISTING_REMOVED,
    LISTING_STALE,
    listed_status_change,
    reconcile_board,
    record_liveness,
    retire_off_target_enabled,
    retire_senior_aggregator_rows,
    retire_unreconcilable_off_target,
)
from backend.services.platform_liveness import LivenessResult

NOW = datetime.datetime.utcnow()
OLD = NOW - datetime.timedelta(days=5)
BOARD = "greenhouse:acme"
SECRET = "test-cron-secret"


def _row(db, url, board_key=BOARD, **kwargs):
    defaults = dict(
        title="Software Intern", company="Acme", location="London",
        source_platform="ats", board_key=board_key, listing_status=LISTING_ACTIVE,
        first_seen_at=OLD, last_seen_at=OLD, scraped_at=OLD,
    )
    defaults.update(kwargs)
    row = ScrapedJob(url=url, **defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _status(db, row):
    db.expire_all()
    return db.get(ScrapedJob, row.id).listing_status


def _job(title, location, department="", **kwargs):
    return ATSJob(title=title, company="Acme", location=location, url="u",
                  department=department, **kwargs)


def _cron_headers(monkeypatch):
    import backend.auth.dependencies as auth_deps

    monkeypatch.setattr(auth_deps, "CRON_SECRET", SECRET)
    return {"x-cron-secret": SECRET}


# ─── The verdict ─────────────────────────────────────────────────────────────

class TestRejection:
    @pytest.mark.parametrize("title,location,department,expected", [
        ("Software Intern", "Toronto, ON", "", None),
        ("Software Intern", "London, ON", "", None),
        ("Software Intern", "London, Ontario", "", None),
        # Full state names are North American now (the old filter dropped them).
        ("Software Intern", "Highland Park, Michigan, US", "", None),
        ("Software Intern", "Albuquerque, New Mexico", "", None),
        ("Software Intern", "London", "", "location"),
        ("Software Intern", "London, England, United Kingdom", "", "location"),
        ("Software Intern", "GB-London", "", "location"),
        ("Software Intern", "Remote - Milano, Italy", "", "location"),
        ("Software Intern", "Bangalore, IN", "", "location"),
        # No NA evidence and none against: never retires.
        ("Software Intern", "2 Locations", "", "unplaced"),
        ("Software Intern", "Hybrid", "", "unplaced"),
        ("Software Intern", "", "", "unplaced"),
        ("Software Intern", "Home Based - Americas; Home based - EMEA", "", "unplaced"),
        # A foreign name beside a count of further locations is not a
        # retire verdict: the unnamed ones may be North American.
        ("Software Intern", "Pistoia Tuscany Italy (2 Locations)", "", "unplaced"),
        ("Software Intern", "PRAGUE DC (2 Locations)", "", "unplaced"),
        # Level first: a senior London role is a level rejection.
        ("Senior Software Engineer", "London", "", "level"),
        ("Saw Operator", "Holland, Michigan, United States", "", "level"),
        # The department still counts, as it does at ingest.
        ("Software Engineer", "San Mateo, CA", "University Recruiting", None),
    ])
    def test_reason(self, title, location, department, expected):
        assert ATSScraper().rejection(_job(title, location, department)) == expected

    def test_one_country_board_skips_the_location_check(self):
        assert ATSScraper().rejection(_job("Software Intern", "London"), home_country="CA") is None

    def test_passes_filters_is_the_same_verdict(self):
        scraper = ATSScraper()
        assert scraper._passes_filters(_job("Software Intern", "2 Locations")) is False
        assert scraper._passes_filters(_job("Software Intern", "Austin, TX")) is True
        assert scraper._passes_filters(_job("Staff Engineer", "Austin, TX")) is False

    def test_a_workday_count_passes_on_its_path_hint(self):
        scraper = ATSScraper()
        toronto = _job("Software Intern", "3 Locations", location_hint="Toronto-ON")
        bangalore = _job("Software Intern", "3 Locations", location_hint="Bangalore")
        assert scraper.rejection(toronto) is None
        assert scraper.rejection(bangalore) == "unplaced"  # a hint never retires

    def test_scrape_board_reports_rejections(self, monkeypatch):
        listings = [
            ATSJob(title="Software Intern", company="Acme", location="Austin, TX", url="https://a/1"),
            ATSJob(title="Software Intern", company="Acme", location="London", url="https://a/2"),
            ATSJob(title="Staff Engineer", company="Acme", location="Austin, TX", url="https://a/3"),
            ATSJob(title="Software Intern", company="Acme", location="3 Locations", url="https://a/4"),
        ]

        async def fake_fetch(self, client, slug, company_name):
            return listings

        monkeypatch.setattr(ATSScraper, "_fetch_greenhouse", fake_fetch)
        monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})
        snap = asyncio.run(ATSScraper().scrape_board(None, "greenhouse", "acme", "Acme"))

        assert [job.url for job in snap.jobs] == ["https://a/1"]
        assert snap.all_urls == {"https://a/1", "https://a/2", "https://a/3", "https://a/4"}
        assert snap.rejected == {"https://a/2": "location", "https://a/3": "level",
                                 "https://a/4": "unplaced"}

    def test_a_url_listed_twice_keeps_its_passing_verdict(self, monkeypatch):
        listings = [
            ATSJob(title="Software Intern", company="Acme", location="London", url="https://a/1"),
            ATSJob(title="Software Intern", company="Acme", location="Toronto, ON", url="https://a/1"),
        ]

        async def fake_fetch(self, client, slug, company_name):
            return listings

        monkeypatch.setattr(ATSScraper, "_fetch_greenhouse", fake_fetch)
        monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})
        snap = asyncio.run(ATSScraper().scrape_board(None, "greenhouse", "acme", "Acme"))
        assert snap.rejected == {}


def test_the_crawler_and_the_lifecycle_agree_on_what_retires():
    from backend.services import listing_freshness
    from backend.services.ats_scraper import RETIRABLE_REJECTIONS

    assert listing_freshness._RETIRABLE == RETIRABLE_REJECTIONS


@pytest.mark.parametrize("status, source, rejection, expected", [
    (LISTING_ACTIVE, "ats", "location", LISTING_OFF_TARGET),
    (LISTING_REMOVED, "ats", "location", LISTING_OFF_TARGET),
    (LISTING_OFF_TARGET, "ats", "location", None),     # no second transition
    # Every level is in scope (scraper v2): a level verdict retires nothing.
    (LISTING_ACTIVE, "ats", "level", None),
    (LISTING_REMOVED, "ats", "level", LISTING_ACTIVE),
    (LISTING_OFF_TARGET, "ats", None, LISTING_ACTIVE),  # passes again
    (LISTING_OFF_TARGET, "ats", "unplaced", None),      # unknown never revives
    (LISTING_REMOVED, "ats", "unplaced", LISTING_ACTIVE),  # listed = open, as before
    (LISTING_ACTIVE, "ats", None, None),
    (LISTING_REMOVED, "github", "location", LISTING_ACTIVE),  # other sources: old rule
])
def test_listed_status_change(status, source, rejection, expected):
    assert listed_status_change(status, source, rejection) == expected


# ─── Reconciliation ──────────────────────────────────────────────────────────

class TestReconcileRetire:
    def test_listed_off_target_row_is_retired_and_stays_retired(self, db_session):
        london = _row(db_session, "https://a/london")
        toronto = _row(db_session, "https://a/toronto", location="Toronto, ON")
        live = {london.url, toronto.url}

        stats = reconcile_board(db_session, BOARD, live, now=NOW,
                                rejected={london.url: "location"})
        assert stats["off_target"] == 1 and stats["removed"] == 0
        assert _status(db_session, london) == LISTING_OFF_TARGET
        assert _status(db_session, toronto) == LISTING_ACTIVE

        # The next crawl still rejects it: no revival, no second transition.
        stats = reconcile_board(db_session, BOARD, live, now=NOW,
                                rejected={london.url: "location"})
        assert stats["off_target"] == 0 and stats["revived"] == 0
        assert _status(db_session, london) == LISTING_OFF_TARGET
        db_session.expire_all()
        assert db_session.get(ScrapedJob, london.id).last_seen_at == NOW  # still listed

    def test_comes_back_when_the_listing_passes_again(self, db_session):
        row = _row(db_session, "https://a/1", listing_status=LISTING_OFF_TARGET)

        stats = reconcile_board(db_session, BOARD, {row.url}, now=NOW, rejected={})

        assert stats["revived"] == 1
        assert _status(db_session, row) == LISTING_ACTIVE

    def test_unplaced_listing_never_retires_nor_revives_off_target(self, db_session):
        active = _row(db_session, "https://a/1")
        off = _row(db_session, "https://a/2", listing_status=LISTING_OFF_TARGET)
        closed = _row(db_session, "https://a/3", listing_status=LISTING_REMOVED)
        unplaced = {url: "unplaced" for url in (active.url, off.url, closed.url)}

        reconcile_board(db_session, BOARD, set(unplaced), now=NOW, rejected=unplaced)

        assert _status(db_session, active) == LISTING_ACTIVE
        assert _status(db_session, off) == LISTING_OFF_TARGET
        assert _status(db_session, closed) == LISTING_ACTIVE  # listed = open, as before

    @pytest.mark.parametrize("status", [LISTING_STALE, LISTING_REMOVED, LISTING_EXPIRED])
    def test_closed_row_listed_off_target_goes_off_target_not_active(self, db_session, status):
        row = _row(db_session, "https://a/1", listing_status=status)

        reconcile_board(db_session, BOARD, {row.url}, now=NOW, rejected={row.url: "location"})

        assert _status(db_session, row) == LISTING_OFF_TARGET

    def test_only_crawler_rows_are_retired(self, db_session):
        github = _row(db_session, "https://a/1", source_platform="github",
                      listing_status=LISTING_REMOVED)
        linkedin = _row(db_session, "https://a/2", source_platform="linkedin")

        reconcile_board(db_session, BOARD, {github.url, linkedin.url}, now=NOW,
                        rejected={github.url: "location", linkedin.url: "level"})

        assert _status(db_session, github) == LISTING_ACTIVE  # the old rule
        assert _status(db_session, linkedin) == LISTING_ACTIVE

    def test_vanished_off_target_row_is_removed(self, db_session):
        """Hidden either way, but only a closed status tells a saved job, an
        application or a deep link that the posting is gone."""
        off = _row(db_session, "https://a/1", listing_status=LISTING_OFF_TARGET)
        other = _row(db_session, "https://a/2", location="Austin, TX")

        stats = reconcile_board(db_session, BOARD, {other.url}, now=NOW, rejected={})

        assert stats["removed"] == 1
        assert _status(db_session, off) == LISTING_REMOVED
        assert LISTING_REMOVED in CLOSED_LISTING_STATUSES

        # Relisted and still failing: back to off_target, not the feed.
        reconcile_board(db_session, BOARD, {off.url, other.url}, now=NOW,
                        rejected={off.url: "location"})
        assert _status(db_session, off) == LISTING_OFF_TARGET

    def test_partial_snapshot_never_removes_off_target(self, db_session):
        from backend.routers.github_sources import _confirm_listed

        off = _row(db_session, "https://a/1", listing_status=LISTING_OFF_TARGET)
        other = _row(db_session, "https://a/2", location="Austin, TX")

        _confirm_listed(db_session, BOARD, {other.url}, now=NOW, rejected={})

        assert _status(db_session, off) == LISTING_OFF_TARGET

    def test_workday_apply_url_matches_its_rejection(self, db_session):
        base = "https://acme.wd3.myworkdayjobs.com/External/job/London/Intern_R-1"
        row = _row(db_session, base + "/apply", board_key="workday:acme")

        reconcile_board(db_session, "workday:acme", {base}, now=NOW, rejected={base: "location"})

        assert _status(db_session, row) == LISTING_OFF_TARGET

    def test_without_verdicts_listed_means_active(self, db_session):
        """What the kill switch falls back to: the old rule, which also brings
        rows retired earlier back on the next crawl."""
        off = _row(db_session, "https://a/1", listing_status=LISTING_OFF_TARGET)

        reconcile_board(db_session, BOARD, {off.url}, now=NOW, rejected=None)

        assert _status(db_session, off) == LISTING_ACTIVE

    def test_partial_snapshot_retires_what_it_listed(self, db_session):
        from backend.routers.github_sources import _confirm_listed

        london = _row(db_session, "https://a/london")
        closed = _row(db_session, "https://a/closed", listing_status=LISTING_REMOVED)
        unlisted = _row(db_session, "https://a/unlisted")

        stats = _confirm_listed(db_session, BOARD, {london.url, closed.url}, now=NOW,
                                rejected={london.url: "location"})

        assert stats == {"confirmed": 2, "revived": 1, "off_target": 1}
        assert _status(db_session, london) == LISTING_OFF_TARGET
        assert _status(db_session, closed) == LISTING_ACTIVE
        assert _status(db_session, unlisted) == LISTING_ACTIVE


# ─── cron-ats end to end ─────────────────────────────────────────────────────

class TestCronAts:
    def _run(self, client, monkeypatch, snapshot_kwargs):
        monkeypatch.setattr(company_registry, "load_companies",
                            lambda **kw: [("greenhouse", "acme", "Acme")])

        async def fake_scrape_board(self, client, platform, slug, company_name):
            return BoardSnapshot(platform=platform, slug=slug, company=company_name,
                                 **snapshot_kwargs)

        monkeypatch.setattr(ATSScraper, "scrape_board", fake_scrape_board)
        res = client.post("/github-sources/cron-ats", headers=_cron_headers(monkeypatch))
        assert res.status_code == 200, res.text
        return res.json()

    def test_retires_off_target_rows_and_keeps_them_retired(self, client, db_session, monkeypatch):
        london = _row(db_session, "https://boards.greenhouse.io/acme/jobs/1")
        snap = dict(all_urls={london.url}, rejected={london.url: "location"},
                    complete=True, total_listed=1)

        body = self._run(client, monkeypatch, snap)
        assert body["off_target"] == 1
        assert body["removed"] == 0
        assert _status(db_session, london) == LISTING_OFF_TARGET

        body = self._run(client, monkeypatch, snap)
        assert body["off_target"] == 0 and body["revived"] == 0
        assert _status(db_session, london) == LISTING_OFF_TARGET

    def test_partial_snapshot_retires_too(self, client, db_session, monkeypatch):
        london = _row(db_session, "https://boards.greenhouse.io/acme/jobs/1",
                      listing_status=LISTING_STALE)

        body = self._run(client, monkeypatch, dict(
            all_urls={london.url}, rejected={london.url: "location"},
            complete=False, total_listed=5000))

        assert body["off_target"] == 1 and body["revived"] == 0
        assert _status(db_session, london) == LISTING_OFF_TARGET

    @pytest.mark.parametrize("value", ["0", "false", "OFF"])
    def test_kill_switch_turns_the_retire_off(self, client, db_session, monkeypatch, value):
        monkeypatch.setenv("CRON_ATS_RETIRE_OFF_TARGET", value)
        assert not retire_off_target_enabled()
        london = _row(db_session, "https://boards.greenhouse.io/acme/jobs/1")
        earlier = _row(db_session, "https://boards.greenhouse.io/acme/jobs/2",
                       listing_status=LISTING_OFF_TARGET)

        body = self._run(client, monkeypatch, dict(
            all_urls={london.url, earlier.url},
            rejected={london.url: "location", earlier.url: "level"},
            complete=True, total_listed=2))

        assert body["off_target"] == 0
        assert _status(db_session, london) == LISTING_ACTIVE
        assert _status(db_session, earlier) == LISTING_ACTIVE  # back, as before the change

    def test_retire_is_on_by_default(self, monkeypatch):
        monkeypatch.delenv("CRON_ATS_RETIRE_OFF_TARGET", raising=False)
        assert retire_off_target_enabled()
        monkeypatch.setenv("CRON_ATS_RETIRE_OFF_TARGET", "1")
        assert retire_off_target_enabled()

    def test_the_classifier_verdict_reaches_stored_rows(self, client, db_session, monkeypatch):
        """No re-filter of its own: the crawl's verdict reaches stored rows
        through scrape_board and reconciliation. With every level in scope
        (scraper v2) a non-entry title stays listed, a row the old
        entry-level filter hid comes back, and every kept row's
        experience_level heals to the taxonomy's label."""
        from backend.ingest import taxonomy

        hourly = _row(db_session, "https://boards.greenhouse.io/acme/jobs/1",
                      title="Operations Associate, Dallas, #118", location="Dallas, TX")
        pm_intern = _row(db_session, "https://boards.greenhouse.io/acme/jobs/2",
                         title="Product Manager Intern", location="Austin, TX",
                         listing_status=LISTING_OFF_TARGET, experience_level="new_grad")
        audit = _row(db_session, "https://boards.greenhouse.io/acme/jobs/3",
                     title="Internal Audit Analyst", location="Austin, TX",
                     experience_level="internship")
        listings = [ATSJob(title=row.title, company="Acme", location=row.location, url=row.url)
                    for row in (hourly, pm_intern, audit)]

        async def fake_fetch(self, client, slug, company_name):
            return listings

        monkeypatch.setattr(ATSScraper, "_fetch_greenhouse", fake_fetch)
        monkeypatch.setattr(company_registry, "load_companies",
                            lambda **kw: [("greenhouse", "acme", "Acme")])
        monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})
        res = client.post("/github-sources/cron-ats", headers=_cron_headers(monkeypatch))
        assert res.status_code == 200, res.text
        body = res.json()

        assert (body["off_target"], body["revived"], body["relabeled"]) == (0, 1, 3)
        assert _status(db_session, hourly) == LISTING_ACTIVE
        assert _status(db_session, pm_intern) == LISTING_ACTIVE
        assert db_session.get(ScrapedJob, pm_intern.id).experience_level == "internship"
        for row in (hourly, audit):
            stored = db_session.get(ScrapedJob, row.id).experience_level
            assert stored == taxonomy.classify_experience(row.title) != "internship"

    def test_new_rows_get_a_word_bounded_experience_level(self, client, db_session, monkeypatch):
        payroll = "https://jobs.lever.co/acme/1"
        coop = "https://jobs.lever.co/acme/2"
        analyst = "https://jobs.lever.co/acme/3"
        jobs = [
            ATSJob(title="International Payroll Analyst", company="Acme",
                   location="Austin, TX", url=payroll),
            # Lever's commitment says what the bare title doesn't.
            ATSJob(title="RF Validation Associate", company="Acme", location="Austin, TX",
                   url=coop, employment_type="Intern"),
            # A description's mention is no commitment.
            ATSJob(title="Financial Analyst", company="Acme", location="Austin, TX",
                   url=analyst, description="Prior internship experience is a plus."),
        ]
        monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})

        body = self._run(client, monkeypatch, dict(
            jobs=jobs, all_urls={payroll, coop, analyst}, complete=True, total_listed=3))

        from backend.ingest import taxonomy

        assert body["new_jobs"] == 3
        rows = {row.url: row for row in db_session.query(ScrapedJob).all()}
        assert rows[coop].experience_level == "internship"
        for url, title in ((payroll, "International Payroll Analyst"),
                           (analyst, "Financial Analyst")):
            assert rows[url].experience_level == taxonomy.classify_experience(title)
            assert rows[url].experience_level != "internship"
        assert rows[analyst].employment_type == "internship"  # the extractor's reading


# ─── Rows no crawl reaches ───────────────────────────────────────────────────

class TestOrphanRetire:
    def test_retires_only_what_fails_the_filters(self, db_session):
        cleaner = _row(db_session, "https://jobs.nokia.com/1", board_key="unknown",
                       title="Cleaner", location="Toronto, ON")
        london = _row(db_session, "https://jobs.nokia.com/2", board_key="",
                      title="Software Intern", location="London, UK")
        coop = _row(db_session, "https://huaweicanada.recruitee.com/o/3", board_key="unknown",
                    title="Co-op Engineer - AI Software Engineering", location="Markham, ON")
        unplaced = _row(db_session, "https://jobs.nokia.com/4", board_key="unknown",
                        title="Software Intern", location="Multiple Locations")

        stats = retire_unreconcilable_off_target(db_session, now=NOW)

        # Every level is in scope (scraper v2): only the foreign row goes.
        assert stats["off_target"] == 1
        assert (stats["level"], stats["location"]) == (0, 1)
        assert _status(db_session, cleaner) == LISTING_ACTIVE
        assert _status(db_session, london) == LISTING_OFF_TARGET
        assert _status(db_session, coop) == LISTING_ACTIVE
        assert _status(db_session, unplaced) == LISTING_ACTIVE

    def test_leaves_crawled_boards_and_other_sources_alone(self, db_session):
        crawled = _row(db_session, "https://boards.greenhouse.io/acme/jobs/1",
                       title="Cleaner")
        linkedin = _row(db_session, "https://www.linkedin.com/jobs/view/1", board_key="unknown",
                        source_platform="linkedin", title="Cleaner")
        rogue_li = _row(db_session, "https://ca.linkedin.com/jobs/view/2", board_key="unknown",
                        title="Cleaner")  # a LinkedIn card stored as 'ats'
        github = _row(db_session, "https://example.com/careers/1", board_key="",
                      source_platform="github", title="Cleaner")
        twin = _row(db_session, "https://jobs.nokia.com/9", board_key="unknown",
                    title="Cleaner", duplicate_of=crawled.id)

        stats = retire_unreconcilable_off_target(db_session, now=NOW)

        assert stats["off_target"] == 0
        for row in (crawled, linkedin, rogue_li, github, twin):
            assert _status(db_session, row) == LISTING_ACTIVE

    def test_a_row_the_filters_no_longer_reject_comes_back(self, db_session):
        """Hidden under an earlier rule: no crawl will re-judge it, so the
        sweep re-reads its own off_target rows on every run."""
        passes = _row(db_session, "https://huaweicanada.recruitee.com/o/1", board_key="unknown",
                      title="Co-op Engineer - AI Software Engineering", location="Markham, ON",
                      listing_status=LISTING_OFF_TARGET)
        # Hidden for its level before scraper v2 kept every level.
        cleaner = _row(db_session, "https://jobs.nokia.com/2", board_key="unknown",
                       title="Cleaner", location="Toronto, ON", listing_status=LISTING_OFF_TARGET)
        fails = _row(db_session, "https://jobs.nokia.com/4", board_key="unknown",
                     title="Software Intern", location="London, UK",
                     listing_status=LISTING_OFF_TARGET)
        crawled = _row(db_session, "https://boards.greenhouse.io/acme/jobs/3",
                       title="Software Intern", location="Toronto, ON",
                       listing_status=LISTING_OFF_TARGET)  # its board crawl decides

        stats = retire_unreconcilable_off_target(db_session, now=NOW)

        assert stats["restored"] == 2
        assert _status(db_session, passes) == LISTING_ACTIVE
        assert _status(db_session, cleaner) == LISTING_ACTIVE
        assert _status(db_session, fails) == LISTING_OFF_TARGET
        assert _status(db_session, crawled) == LISTING_OFF_TARGET

    def test_kill_switch(self, db_session, monkeypatch):
        monkeypatch.setenv("CRON_ATS_RETIRE_OFF_TARGET", "0")
        cleaner = _row(db_session, "https://jobs.nokia.com/1", board_key="unknown",
                       title="Cleaner")

        stats = retire_unreconcilable_off_target(db_session, now=NOW)

        assert stats["off_target"] == 0 and stats["disabled"]
        assert _status(db_session, cleaner) == LISTING_ACTIVE

    def test_kill_switch_rolls_the_retire_back(self, db_session, monkeypatch):
        """No crawl ever re-judges an orphan, so switching the retire off must
        bring back what this sweep hid, bounded per run, and nothing else."""
        bangalore = _row(db_session, "https://jobs.nokia.com/1", board_key="unknown",
                         title="Software Intern", location="Bangalore, India")
        london = _row(db_session, "https://jobs.nokia.com/2", board_key="",
                      title="Software Intern", location="London, UK")
        # Retired by others: a crawl's verdict (its next crawl brings it
        # back) and the senior aggregator sweep's (it rolls back its own).
        crawled = _row(db_session, "https://boards.greenhouse.io/acme/jobs/3",
                       listing_status=LISTING_OFF_TARGET)
        senior = _aggregator(db_session, 4, "Senior HR Specialist",
                             listing_status=LISTING_OFF_TARGET)
        assert retire_unreconcilable_off_target(db_session, now=NOW)["off_target"] == 2

        monkeypatch.setenv("CRON_ATS_RETIRE_OFF_TARGET", "0")
        stats = retire_unreconcilable_off_target(db_session, now=NOW, limit=1)

        assert stats["disabled"] and (stats["off_target"], stats["restored"]) == (0, 1)
        assert _status(db_session, bangalore) == LISTING_ACTIVE
        assert _status(db_session, london) == LISTING_OFF_TARGET  # the next run's
        assert retire_unreconcilable_off_target(db_session, now=NOW)["restored"] == 1
        assert _status(db_session, london) == LISTING_ACTIVE
        assert retire_unreconcilable_off_target(db_session, now=NOW)["restored"] == 0
        assert _status(db_session, crawled) == LISTING_OFF_TARGET
        assert _status(db_session, senior) == LISTING_OFF_TARGET

    def test_cron_freshness_runs_it_before_the_checks(self, client, db_session, monkeypatch):
        from backend.services import listing_freshness

        cleaner = _row(db_session, "https://jobs.nokia.com/1", board_key="unknown",
                       title="Software Intern", location="London, UK")
        probed: list = []

        async def fake_verify(db, client_, limit=0, now=None, *, deadline=None, cache=None, **kw):
            probed.append(_status(db, cleaner))
            return {"checked": 0}

        for name in ("verify_stale_listings", "verify_unconfirmed_active_listings",
                     "verify_recent_aggregator_listings"):
            monkeypatch.setattr(listing_freshness, name, fake_verify)

        body = client.post("/jobs/cron-freshness", headers=_cron_headers(monkeypatch)).json()

        assert body["orphans_off_target"]["off_target"] == 1
        assert probed and all(status == LISTING_OFF_TARGET for status in probed)


# ─── LinkedIn/Indeed rows with a plainly senior title ────────────────────────

def _aggregator(db, n, title, source="linkedin", **kwargs):
    url = (f"https://www.linkedin.com/jobs/view/{n}" if source == "linkedin"
           else f"https://ca.indeed.com/viewjob?jk={n}")
    kwargs.setdefault("location", "Toronto, ON")
    return _row(db, url, board_key="", source_platform=source, title=title, **kwargs)


class TestSeniorAggregatorRetire:
    def test_retires_only_hard_senior_aggregator_titles(self, db_session):
        senior = _aggregator(db_session, 1, "Senior HR Specialist")
        director = _aggregator(db_session, 2, "Director of Engineering", source="indeed")
        stale = _aggregator(db_session, 3, "Principal Engineer", listing_status=LISTING_STALE)
        level_two = _aggregator(db_session, 4, "Software Engineer II, Backend")  # soft: stays
        coop = _aggregator(db_session, 5, "Software Engineering Intern - 8 months")
        hourly = _aggregator(db_session, 6, "Operations Associate, Dallas, #118")
        expired = _aggregator(db_session, 7, "Senior Mobile Engineer",
                              listing_status=LISTING_EXPIRED)
        crawler = _row(db_session, "https://boards.greenhouse.io/acme/jobs/8",
                       title="Senior Engineer", location="Austin, TX")
        github = _row(db_session, "https://example.com/careers/9", board_key="",
                      source_platform="github", title="Senior Engineer")

        stats = retire_senior_aggregator_rows(db_session, now=NOW)

        assert stats == {"checked": 6, "off_target": 3}
        for row in (senior, director, stale):
            assert _status(db_session, row) == LISTING_OFF_TARGET
        for row in (level_two, coop, hourly, crawler, github):
            assert _status(db_session, row) == LISTING_ACTIVE
        assert _status(db_session, expired) == LISTING_EXPIRED

    def test_a_hidden_twin_goes_with_its_winner(self, db_session):
        """A twin left visible behind a retired LinkedIn winner would be handed
        back to the feed by release_from_closed_winners (a retired aggregator
        row speaks for no one)."""
        from backend.services.cross_source_dedup import release_from_closed_winners

        winner = _aggregator(db_session, 1, "Senior Data Engineer", city="toronto",
                             title_norm="senior data engineer")
        twin = _aggregator(db_session, 2, "Senior Data Engineer", source="indeed",
                           city="toronto", title_norm="senior data engineer",
                           duplicate_of=winner.id)

        assert retire_senior_aggregator_rows(db_session, now=NOW)["off_target"] == 2
        assert release_from_closed_winners(db_session) == []
        assert _status(db_session, twin) == LISTING_OFF_TARGET
        assert db_session.get(ScrapedJob, twin.id).duplicate_of == winner.id

    def test_nothing_brings_a_retired_row_back(self, db_session):
        from backend.services.listing_freshness import sweep_aggregator_expiry

        row = _aggregator(db_session, 1, "Senior HR Specialist")
        retire_senior_aggregator_rows(db_session, now=NOW)

        record_liveness(db_session, row.id, LISTING_OFF_TARGET,
                        LivenessResult("alive", "linkedin_open", True), now=NOW)
        db_session.commit()
        sweep_aggregator_expiry(db_session, now=NOW + datetime.timedelta(days=60))

        assert _status(db_session, row) == LISTING_OFF_TARGET

    def test_a_title_the_veto_no_longer_matches_comes_back(self, db_session):
        """Retired under an earlier, broader veto: the next run re-reads the
        title, as a board crawl re-reads its listings."""
        narrowed = _aggregator(db_session, 1, "Junior Planner / Planner / Senior Planner (PFT)",
                               source="indeed", listing_status=LISTING_OFF_TARGET)
        aged = _aggregator(db_session, 2, "Junior Planner / Planner / Senior Planner (FT)",
                           source="indeed", listing_status=LISTING_OFF_TARGET,
                           first_seen_at=NOW - datetime.timedelta(days=40),
                           scraped_at=NOW - datetime.timedelta(days=40))
        senior = _aggregator(db_session, 3, "Senior HR Specialist",
                             listing_status=LISTING_OFF_TARGET)

        stats = retire_senior_aggregator_rows(db_session, now=NOW)

        assert (stats["restored"], stats["expired"]) == (1, 1)
        assert _status(db_session, narrowed) == LISTING_ACTIVE
        assert _status(db_session, aged) == LISTING_EXPIRED  # the expiry would have ended it
        assert _status(db_session, senior) == LISTING_OFF_TARGET

    def test_kill_switch(self, db_session, monkeypatch):
        monkeypatch.setenv("CRON_ATS_RETIRE_OFF_TARGET", "0")
        row = _aggregator(db_session, 1, "Senior HR Specialist")

        stats = retire_senior_aggregator_rows(db_session, now=NOW)

        assert stats["off_target"] == 0 and stats["disabled"]
        assert _status(db_session, row) == LISTING_ACTIVE

    def test_kill_switch_rolls_the_retire_back(self, db_session, monkeypatch):
        """Nothing else revives these rows, so switching the retire off must
        bring back what this sweep hid: twins included, and a row the
        aggregator expiry would have ended meanwhile goes to expired."""
        young = _aggregator(db_session, 1, "Senior HR Specialist", city="toronto",
                            title_norm="senior hr specialist")
        twin = _aggregator(db_session, 2, "Senior HR Specialist", source="indeed",
                           city="toronto", title_norm="senior hr specialist",
                           duplicate_of=young.id)
        aged_at = NOW - datetime.timedelta(days=30)
        aged = _aggregator(db_session, 3, "Director of Engineering",
                           first_seen_at=aged_at, scraped_at=aged_at)
        orphan = _row(db_session, "https://jobs.nokia.com/4", board_key="unknown",
                      title="Cleaner", listing_status=LISTING_OFF_TARGET)
        assert retire_senior_aggregator_rows(db_session, now=NOW)["off_target"] == 3

        monkeypatch.setenv("CRON_ATS_RETIRE_OFF_TARGET", "0")
        stats = retire_senior_aggregator_rows(db_session, now=NOW)

        assert stats["disabled"] and (stats["restored"], stats["expired"]) == (2, 1)
        assert _status(db_session, young) == LISTING_ACTIVE
        assert _status(db_session, twin) == LISTING_ACTIVE
        assert db_session.get(ScrapedJob, twin.id).duplicate_of == young.id  # still hidden
        assert _status(db_session, aged) == LISTING_EXPIRED
        assert _status(db_session, orphan) == LISTING_OFF_TARGET  # the orphan sweep's
        assert retire_senior_aggregator_rows(db_session, now=NOW)["restored"] == 0

    def test_cron_freshness_runs_it(self, client, db_session, monkeypatch):
        from backend.services import listing_freshness

        row = _aggregator(db_session, 1, "Director of Engineering")

        async def fake_verify(db, client_, limit=0, now=None, *, deadline=None, cache=None, **kw):
            return {"checked": 0}

        for name in ("verify_stale_listings", "verify_unconfirmed_active_listings",
                     "verify_recent_aggregator_listings"):
            monkeypatch.setattr(listing_freshness, name, fake_verify)

        body = client.post("/jobs/cron-freshness", headers=_cron_headers(monkeypatch)).json()

        assert body["aggregator_senior_off_target"]["off_target"] == 1
        assert _status(db_session, row) == LISTING_OFF_TARGET


# ─── The rest of the lifecycle ───────────────────────────────────────────────

def test_off_target_is_hidden_from_the_feed(client, db_session):
    _row(db_session, "https://a/1", listing_status=LISTING_OFF_TARGET)
    visible = _row(db_session, "https://a/2", location="Austin, TX")

    ids = [job["id"] for job in client.get("/jobs").json()]

    assert ids == [visible.id]
    assert LISTING_OFF_TARGET in HIDDEN_LISTING_STATUSES
    assert LISTING_OFF_TARGET not in CLOSED_LISTING_STATUSES


def test_platform_alive_never_revives_off_target(db_session):
    row = _row(db_session, "https://a/1", listing_status=LISTING_OFF_TARGET)

    status = record_liveness(db_session, row.id, LISTING_OFF_TARGET,
                             LivenessResult("alive", "greenhouse_api_200", True), now=NOW)
    db_session.commit()

    assert status == LISTING_OFF_TARGET
    db_session.expire_all()
    assert db_session.get(ScrapedJob, row.id).last_seen_at == NOW


def test_check_live_probes_off_target_instead_of_calling_it_dead(client, db_session, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "",
                                          ("93.184.216.34", 0))])

    async def fake_check_listing(client, url, *, cache=None, board_key=""):
        return LivenessResult("alive", "greenhouse_api_200", True)

    monkeypatch.setattr(platform_liveness, "check_listing", fake_check_listing)
    row = _row(db_session, "https://boards.greenhouse.io/acme/jobs/1",
               listing_status=LISTING_OFF_TARGET)

    body = client.post(f"/jobs/{row.id}/check-live").json()

    assert body == {"id": row.id, "listing_status": LISTING_OFF_TARGET, "verdict": "alive"}


def test_off_target_direct_row_keeps_standing_in_for_its_twins():
    assert stands_in_for_twins(LISTING_OFF_TARGET, "ats", "https://boards.greenhouse.io/a/jobs/1")
    assert not stands_in_for_twins(LISTING_OFF_TARGET, "ats", "https://www.linkedin.com/jobs/view/1")
    assert not stands_in_for_twins(LISTING_EXPIRED, "ats", "https://boards.greenhouse.io/a/jobs/1")


def test_ingest_metrics_counts_rows_retired_today(client, db_session, monkeypatch):
    _row(db_session, "https://a/1", listing_status=LISTING_OFF_TARGET,
         listing_status_changed_at=datetime.datetime.utcnow())
    _row(db_session, "https://a/2", listing_status=LISTING_OFF_TARGET,
         listing_status_changed_at=datetime.datetime.utcnow() - datetime.timedelta(days=3))
    monkeypatch.setattr(company_registry, "load_companies", lambda **kw: [])

    body = client.get("/jobs/ingest-metrics", headers=_cron_headers(monkeypatch)).json()

    assert body["off_target_24h"] == 1
    assert body["by_listing_status"][LISTING_OFF_TARGET] == 2
