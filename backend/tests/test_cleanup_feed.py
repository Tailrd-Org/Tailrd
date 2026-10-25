"""One-time feed cleanup (backend/scripts/cleanup_feed.py): GitHub data fixes
converge the way ingest now would, a dry run cannot write (and runs no
migration), the sweeps' dry-run projection matches what they really do, and
liveness verdicts are applied through listing_freshness.record_liveness."""

import datetime
import importlib.util
import pathlib
import sqlite3
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.db.database import Base
from backend.db.models import ScrapedJob
from backend.services import listing_freshness, platform_liveness
from backend.services.listing_freshness import (
    LISTING_ACTIVE,
    LISTING_EXPIRED,
    LISTING_REMOVED,
    LISTING_STALE,
)
from backend.services.platform_liveness import LivenessResult

_SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "cleanup_feed.py"
_spec = importlib.util.spec_from_file_location("cleanup_feed", _SCRIPT)
cleanup_feed = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cleanup_feed  # dataclasses resolve annotations through it
_spec.loader.exec_module(cleanup_feed)

NOW = datetime.datetime(2026, 9, 27, 12, 0, 0)
DAY = datetime.timedelta(days=1)

TESLA = "https://www.tesla.com/careers/search/job/256719"
ASHBY = "https://jobs.ashbyhq.com/acme/4f1c2a9e-1111-4222-8333-944455556666"


def _row(db, url, **kwargs):
    defaults = dict(
        title="Software Engineer, New Grad", company="Acme", location="Toronto, ON, Canada",
        source_platform="ats", board_key="greenhouse:acme", listing_status=LISTING_ACTIVE,
        first_seen_at=NOW - DAY, last_seen_at=NOW - DAY, scraped_at=NOW - DAY,
    )
    defaults.update(kwargs)
    row = ScrapedJob(url=url, **defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _github(db, url, **kwargs):
    defaults = dict(source_platform="github", board_key="", company="**Tesla**",
                    title="**Software Engineer, New Grad**",
                    first_seen_at=datetime.datetime(2026, 8, 1),
                    scraped_at=datetime.datetime(2026, 8, 1),
                    last_seen_at=datetime.datetime(2026, 8, 1))
    defaults.update(kwargs)
    return _row(db, url, **defaults)


def _report(db, dry_run=True):
    report = cleanup_feed.Report(dry_run=dry_run)
    report.visible_start = cleanup_feed.visible_ids(db)
    return report


# ─── pure helpers ────────────────────────────────────────────────────────────

class TestHelpers:
    def test_yearless_date_goes_back_a_year(self):
        first_seen = datetime.datetime(2026, 8, 1)
        assert cleanup_feed.corrected_posted_date(
            datetime.datetime(2026, 12, 12), first_seen) == datetime.datetime(2025, 12, 12)
        # 'Aug 02' seen late on Aug 1 in US time is clock skew, not last year.
        assert cleanup_feed.corrected_posted_date(
            datetime.datetime(2026, 8, 2), first_seen) == datetime.datetime(2026, 8, 2)
        assert cleanup_feed.corrected_posted_date(
            datetime.datetime(2026, 7, 1), first_seen) == datetime.datetime(2026, 7, 1)

    def test_leap_day_and_missing_reference(self):
        assert cleanup_feed.corrected_posted_date(
            datetime.datetime(2028, 2, 29), datetime.datetime(2027, 6, 1)
        ) == datetime.datetime(2027, 2, 28)
        assert cleanup_feed.corrected_posted_date(datetime.datetime(2026, 12, 1), None) \
            == datetime.datetime(2026, 12, 1)
        assert cleanup_feed.corrected_posted_date(None, NOW) is None

    def test_has_utm(self):
        assert cleanup_feed.has_utm(TESLA + "?utm_source=vansh")
        assert cleanup_feed.has_utm("https://x.io/j?id=1&UTM_Medium=list")
        assert not cleanup_feed.has_utm(TESLA)
        assert not cleanup_feed.has_utm("https://x.io/j?gh_jid=12&outcome=utm")

    def test_host_family(self):
        assert cleanup_feed.host_family(
            "https://bmo.wd3.myworkdayjobs.com/External/job/X_R1") == "workday"
        assert cleanup_feed.host_family("https://careers.acme.com/?gh_jid=12") == "greenhouse"
        assert cleanup_feed.host_family("https://www.linkedin.com/jobs/view/1") == "linkedin"
        assert cleanup_feed.host_family("https://careers.molsoncoors.com/job/1") == "other"
        assert cleanup_feed.host_family("https:/.workable.com/x/j/1") == "malformed"

    @pytest.mark.parametrize("statement", [
        "SELECT scraped_jobs.id FROM scraped_jobs WHERE scraped_jobs.url = ?",
        "  select count(*) from (select 1)",
        "/* note */ SELECT 1",
        "SHOW transaction_read_only",
        "SET TRANSACTION READ ONLY",
        'PRAGMA main.table_xinfo("scraped_jobs")',
    ])
    def test_read_only_statements_pass(self, statement):
        assert cleanup_feed.is_read_only_statement(statement)

    @pytest.mark.parametrize("statement", [
        "UPDATE scraped_jobs SET listing_status='removed' WHERE id = 1",
        "INSERT INTO scraped_jobs (url) VALUES ('x')",
        "DELETE FROM scraped_jobs",
        "ALTER TABLE scraped_jobs ADD COLUMN last_probed_at TIMESTAMP",
        "CREATE INDEX ix ON scraped_jobs (url)",
        "SELECT * INTO copy FROM scraped_jobs",
        "WITH x AS (UPDATE scraped_jobs SET title='' RETURNING id) SELECT * FROM x",
        "PRAGMA query_only = OFF",
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
        "-- sneaky\nUPDATE scraped_jobs SET title = ''",
    ])
    def test_writes_refused(self, statement):
        assert not cleanup_feed.is_read_only_statement(statement)


# ─── phase a ─────────────────────────────────────────────────────────────────

class TestPhaseA:
    def test_github_fixes_converge_and_are_idempotent(self, db_session):
        canonical = _github(db_session, TESLA, posted_date=datetime.datetime(2026, 12, 12))
        utm_copy = _github(db_session, TESLA + "?utm_source=vansh",
                           posted_date=datetime.datetime(2026, 12, 12))
        on_time = _github(db_session, "https://jobs.lever.co/acme/aaaa", company="__Acme__",
                          title="Intern", posted_date=datetime.datetime(2026, 8, 2))
        # Trailing slash on the clean row, utm on the copy: still one posting.
        ashby = _github(db_session, ASHBY + "/", company="Acme", title="Intern",
                        posted_date=datetime.datetime(2026, 7, 20))
        ashby_utm = _github(db_session, ASHBY + "?utm_source=vansh", company="Acme",
                            title="Intern", posted_date=datetime.datetime(2026, 7, 20))
        direct = _row(db_session, "https://boards.greenhouse.io/acme/jobs/1", company="**Acme**")

        report = _report(db_session, dry_run=False)
        stats = cleanup_feed.phase_a(db_session, report, write=True)
        db_session.expire_all()

        assert stats["dates_fixed"] == 2  # both Tesla rows, 2026-12-12 -> 2025-12-12
        assert canonical.posted_date == datetime.datetime(2025, 12, 12)
        assert on_time.posted_date == datetime.datetime(2026, 8, 2)
        assert canonical.company == "Tesla"
        assert canonical.title == "Software Engineer, New Grad"
        assert on_time.company == "Acme"
        assert direct.company == "**Acme**"  # not a GitHub row: untouched

        assert utm_copy.duplicate_of == canonical.id
        assert ashby_utm.duplicate_of == ashby.id
        assert canonical.duplicate_of is None and ashby.duplicate_of is None
        assert stats["utm_twins_hidden"] == 2
        assert report.hidden == {utm_copy.id: "a:utm_twin", ashby_utm.id: "a:utm_twin"}
        # Soft hide only: the rows are all still there, statuses untouched.
        assert db_session.query(ScrapedJob).count() == 6
        assert utm_copy.listing_status == LISTING_ACTIVE

        again = cleanup_feed.phase_a(db_session, _report(db_session, dry_run=False), write=True)
        assert again == {key: 0 for key in again}

    def test_collapsed_group_is_left_alone(self, db_session):
        # dedup_sweep once kept the utm copy and hid the clean URL. Pointing
        # the utm row at its own duplicate would build a cycle: leave it.
        utm_copy = _github(db_session, TESLA + "?utm_source=vansh", company="Tesla",
                           title="Engineer")
        clean = _github(db_session, TESLA, company="Tesla", title="Engineer",
                        duplicate_of=utm_copy.id)
        stats = cleanup_feed.phase_a(db_session, _report(db_session), write=True)
        db_session.expire_all()
        assert stats["utm_twins_hidden"] == 0
        assert utm_copy.duplicate_of is None
        assert clean.duplicate_of == utm_copy.id

    def test_live_row_is_kept_when_both_carry_utm(self, db_session):
        dead = _github(db_session, TESLA + "?utm_source=vansh", company="Tesla",
                       title="Engineer", listing_status=LISTING_REMOVED)
        live = _github(db_session, TESLA + "?utm_medium=list", company="Tesla",
                       title="Engineer")
        cleanup_feed.phase_a(db_session, _report(db_session), write=True)
        db_session.expire_all()
        assert dead.duplicate_of == live.id
        assert live.duplicate_of is None

    def test_dry_run_changes_nothing_but_reports(self, db_session):
        canonical = _github(db_session, TESLA, posted_date=datetime.datetime(2026, 12, 12))
        utm_copy = _github(db_session, TESLA + "?utm_source=vansh")
        report = _report(db_session)
        stats = cleanup_feed.phase_a(db_session, report, write=False)
        db_session.expire_all()
        assert stats["dates_fixed"] == 1
        assert stats["companies_cleaned"] == 2
        assert stats["utm_twins_hidden_visible"] == 1
        assert report.hidden == {utm_copy.id: "a:utm_twin"}
        assert canonical.posted_date == datetime.datetime(2026, 12, 12)
        assert canonical.company == "**Tesla**"
        assert utm_copy.duplicate_of is None

    def test_reposts_behind_closed_dedup_winners_come_back(self, db_session):
        rows = _closed_winner_rows(db_session)

        stats = cleanup_feed.phase_a(db_session, _report(db_session, dry_run=False), write=True)
        db_session.expire_all()

        assert stats["closed_winner_twins_released"] == 1
        assert stats["closed_winner_twins_repointed"] == 0
        # Back in the feed, first in line for the verifier; status untouched.
        assert rows["repost"].duplicate_of is None
        assert rows["repost"].last_probed_at is None
        assert rows["repost"].listing_status == LISTING_ACTIVE
        # Same LinkedIn posting as the expired winner: stays hidden.
        assert rows["same_posting"].duplicate_of == rows["old"].id
        # A mirror of a requisition its board removed: stays hidden.
        assert rows["mirror"].duplicate_of == rows["direct"].id
        again = cleanup_feed.phase_a(db_session, _report(db_session, dry_run=False), write=True)
        assert again["closed_winner_twins_released"] == 0

    def test_closed_winner_dry_run_reports_without_writing(self, guarded_db):
        plain, seed, guarded = guarded_db
        rows = _closed_winner_rows(seed)
        before = _snapshot(plain)

        seen = cleanup_feed.install_read_only_guard(guarded)
        db = sessionmaker(bind=guarded)()
        try:
            stats = cleanup_feed.phase_a(db, _report(db), write=False)
        finally:
            db.close()

        assert stats["closed_winner_twins_released"] == 1
        assert _snapshot(plain) == before
        assert seen and all(cleanup_feed.is_read_only_statement(s) for s in seen)
        seed.expire_all()
        assert rows["repost"].duplicate_of == rows["old"].id

    @pytest.mark.parametrize("write", [False, True])
    def test_one_posting_behind_a_closed_winner_comes_back_once(self, db_session, write):
        # Prod TD 61534/62435: one posting under two host spellings, the
        # lower id without a description. Both modes report one row back in
        # the feed and one under it, never the same posting twice.
        linkedin = dict(source_platform="linkedin", board_key="", last_seen_at=None,
                        title_norm="software engineer new grad")
        old = _row(db_session, LI_OLD, listing_status=LISTING_EXPIRED,
                   description="A real description of the posting. " * 5, **linkedin)
        bare = _row(db_session, "https://ca.linkedin.com/jobs/view/"
                    "software-engineer-new-grad-at-acme-4464900190",
                    duplicate_of=old.id, **linkedin)
        described = _row(db_session, "https://www.linkedin.com/jobs/view/4464900190",
                         duplicate_of=old.id,
                         description="The same posting, described in full. " * 5, **linkedin)

        stats = cleanup_feed.phase_a(db_session, _report(db_session, dry_run=not write),
                                     write=write)
        db_session.expire_all()

        assert stats["closed_winner_twins_released"] == 1
        assert stats["closed_winner_twins_repointed"] == 1
        if write:
            assert described.duplicate_of is None
            assert bare.duplicate_of == described.id
        else:
            assert bare.duplicate_of == described.duplicate_of == old.id


LI_OLD = "https://ca.linkedin.com/jobs/view/software-engineer-new-grad-at-acme-4440083739"
LI_NEW = "https://ca.linkedin.com/jobs/view/software-engineer-new-grad-at-acme-4470027097"


def _closed_winner_rows(db):
    """A LinkedIn repost hidden behind an older copy that has since expired,
    a same-posting copy, and a LinkedIn mirror of a removed board row."""
    linkedin = dict(source_platform="linkedin", board_key="", last_seen_at=None)
    old = _row(db, LI_OLD, listing_status=LISTING_EXPIRED,
               description="A real description of the posting. " * 5, **linkedin)
    direct = _row(db, "https://boards.greenhouse.io/acme/jobs/77", title="QA Intern",
                  listing_status=LISTING_REMOVED)
    return {
        "old": old,
        "direct": direct,
        "repost": _row(db, LI_NEW, duplicate_of=old.id, last_probed_at=NOW - DAY, **linkedin),
        "same_posting": _row(db, "https://www.linkedin.com/jobs/view/4440083739",
                             duplicate_of=old.id, **linkedin),
        "mirror": _row(db, "https://ca.linkedin.com/jobs/view/4465089143", title="QA Intern",
                       duplicate_of=direct.id, **linkedin),
    }


# ─── phase b ─────────────────────────────────────────────────────────────────

def _lifecycle_rows(db):
    return {
        # active, no board confirmation for 5 days -> stale (still visible)
        "to_stale": _row(db, "https://boards.greenhouse.io/acme/jobs/10",
                         last_seen_at=NOW - 5 * DAY),
        # stale with no positive evidence for 40 days, checked since -> expired
        # (by the terminal step after phase c)
        "terminal": _row(db, "https://boards.greenhouse.io/acme/jobs/11",
                         listing_status=LISTING_STALE, last_seen_at=NOW - 40 * DAY,
                         first_seen_at=NOW - 60 * DAY, scraped_at=NOW - 60 * DAY,
                         last_probed_at=NOW - DAY),
        # LinkedIn row 30 days old -> expired (21-day fast expiry)
        "linkedin": _row(db, "https://www.linkedin.com/jobs/view/123", source_platform="linkedin",
                         board_key="", first_seen_at=NOW - 30 * DAY,
                         scraped_at=NOW - 30 * DAY, last_seen_at=None),
        # fresh board-confirmed row: untouched
        "fresh": _row(db, "https://boards.greenhouse.io/acme/jobs/12"),
    }


class TestPhaseB:
    def test_dry_run_projects_exactly_what_apply_does(self, db_session):
        rows = _lifecycle_rows(db_session)
        dry = _report(db_session)
        stats = cleanup_feed.phase_b(db_session, dry, write=False, now=NOW)
        db_session.expire_all()
        assert rows["to_stale"].listing_status == LISTING_ACTIVE  # nothing written
        assert stats["sweep_stale"]["rows"] == 1
        # terminal expiry is not part of the pre-check sweeps any more
        assert "sweep_terminal_expiry" not in stats
        projected = set(dry.hidden)
        # A row going stale leaves the visible feed too (stale is hidden since scraper v2).
        assert projected == {rows["to_stale"].id, rows["linkedin"].id}

        applied = _report(db_session, dry_run=False)
        cleanup_feed.phase_b(db_session, applied, write=True, now=NOW)
        db_session.expire_all()
        assert set(applied.hidden) == projected
        assert rows["to_stale"].listing_status == LISTING_STALE
        assert rows["terminal"].listing_status == LISTING_STALE
        assert rows["linkedin"].listing_status == LISTING_EXPIRED
        assert rows["fresh"].listing_status == LISTING_ACTIVE

    def test_terminal_step_dry_run_projects_what_apply_does(self, db_session):
        rows = _lifecycle_rows(db_session)
        dry = _report(db_session)
        cleanup_feed.phase_terminal(db_session, dry, write=False, now=NOW)
        db_session.expire_all()
        assert rows["terminal"].listing_status == LISTING_STALE  # nothing written
        assert dry.hidden == {rows["terminal"].id: "b:sweep_terminal_expiry"}

        applied = _report(db_session, dry_run=False)
        cleanup_feed.phase_terminal(db_session, applied, write=True, now=NOW)
        db_session.expire_all()
        assert applied.hidden == dry.hidden
        assert rows["terminal"].listing_status == LISTING_EXPIRED

    def test_terminal_step_needs_the_probe_column(self, db_session, monkeypatch):
        rows = _lifecycle_rows(db_session)
        monkeypatch.setattr(cleanup_feed, "has_probe_column", lambda db: False)
        stats = cleanup_feed.phase_terminal(db_session, _report(db_session, dry_run=False),
                                            write=True, now=NOW)
        db_session.expire_all()
        assert stats == {"skipped": "last_probed_at missing"}
        assert rows["terminal"].listing_status == LISTING_STALE
        # a dry run still reports the reset, never names the missing column
        dry_stats = cleanup_feed.phase_terminal(db_session, _report(db_session), write=False,
                                                now=NOW)
        assert set(dry_stats) == {"reset_legacy_seen"}


# ─── phase c ─────────────────────────────────────────────────────────────────

GH_DEAD = "https://boards.greenhouse.io/acme/jobs/1"
GH_OPEN = "https://boards.greenhouse.io/acme/jobs/2"
INDEED = "https://www.indeed.com/viewjob?jk=abc"
LINKEDIN = "https://www.linkedin.com/jobs/view/42"

VERDICTS = {
    GH_DEAD: LivenessResult("dead", "gh_api_404", True),
    GH_OPEN: LivenessResult("alive", "gh_api_200", True),
    INDEED: LivenessResult("unknown", "indeed_unprobeable"),
    LINKEDIN: LivenessResult("alive", "linkedin_apply_cta"),
}
CALLS: list[dict] = []  # what each fake check_listings call was handed


@pytest.fixture
def fake_checks(monkeypatch):
    """check_listings answers from VERDICTS and records what it was asked;
    record_liveness is spied on, never replaced."""
    asked: list[str] = []
    recorded: list[tuple[int, str, str]] = []

    async def fake_check_listings(client, urls, *, concurrency=8, deadline=None, cache=None,
                                  board_keys=None):
        asked.extend(urls)
        CALLS.append({"cache": cache, "board_keys": dict(board_keys or {})})
        return {url: VERDICTS[url] for url in urls if url in VERDICTS}

    CALLS.clear()

    real_record = listing_freshness.record_liveness

    def spy(db, row_id, listing_status, result, now=None):
        recorded.append((row_id, listing_status, result.verdict))
        return real_record(db, row_id, listing_status, result, now=now)

    monkeypatch.setattr(platform_liveness, "check_listings", fake_check_listings)
    monkeypatch.setattr(listing_freshness, "record_liveness", spy)
    return asked, recorded


def _liveness_rows(db):
    return {
        "dead": _row(db, GH_DEAD),
        "revive": _row(db, GH_OPEN, listing_status=LISTING_STALE, last_seen_at=NOW - 10 * DAY),
        "unknown": _row(db, INDEED, source_platform="indeed", board_key=""),
        "weak": _row(db, LINKEDIN, source_platform="linkedin", board_key=""),
        "hidden": _row(db, "https://boards.greenhouse.io/acme/jobs/3",
                       duplicate_of=1),
        "blank_company": _row(db, "https://boards.greenhouse.io/acme/jobs/4", company=" "),
    }


class TestPhaseC:
    @pytest.mark.asyncio
    async def test_apply_maps_verdicts_through_record_liveness(self, db_session, fake_checks):
        asked, recorded = fake_checks
        rows = _liveness_rows(db_session)
        before_seen = {name: row.last_seen_at for name, row in rows.items()}
        report = _report(db_session, dry_run=False)

        stats = await cleanup_feed.phase_c(db_session, report, cleanup_feed.ProbeOptions(),
                                           write=True, now=NOW)
        db_session.expire_all()

        assert sorted(asked) == sorted([GH_DEAD, GH_OPEN, INDEED, LINKEDIN])
        assert sorted(r[0] for r in recorded) == sorted(
            rows[name].id for name in ("dead", "revive", "unknown", "weak"))
        assert stats["outcomes"] == {"removed": 1, "revived": 1, "unverified": 2}

        assert rows["dead"].listing_status == LISTING_REMOVED
        assert rows["dead"].listing_status_changed_at == NOW
        assert rows["revive"].listing_status == LISTING_ACTIVE
        assert rows["revive"].last_seen_at == NOW
        for name in ("unknown", "weak"):
            assert rows[name].listing_status == LISTING_ACTIVE
            assert rows[name].last_seen_at == before_seen[name]  # not evidence
        for name in ("dead", "revive", "unknown", "weak"):
            assert rows[name].last_probed_at == NOW
        assert rows["hidden"].last_probed_at is None
        assert rows["blank_company"].last_probed_at is None

        assert report.hidden == {rows["dead"].id: "c:dead"}
        assert report.unknown_by_host == {"www.indeed.com": 1}
        assert report.weak_alive_by_family == {"linkedin": 1}
        report.phase_c = stats
        assert cleanup_feed.phase_d(report)["unknown_total"] == 1

    @pytest.mark.asyncio
    async def test_dry_run_reports_without_writing(self, db_session, fake_checks):
        _asked, recorded = fake_checks
        rows = _liveness_rows(db_session)
        report = _report(db_session)
        stats = await cleanup_feed.phase_c(db_session, report, cleanup_feed.ProbeOptions(),
                                           write=False, now=NOW)
        db_session.expire_all()
        assert len(recorded) == 4  # the same mapping ran, against the recorder
        assert stats["outcomes"] == {"removed": 1, "revived": 1, "unverified": 2}
        assert stats["dead_samples"] == [(GH_DEAD, "gh_api_404")]
        assert stats["combos"]["dead|gh_api_404|greenhouse"] == 1
        assert rows["dead"].listing_status == LISTING_ACTIVE
        assert rows["revive"].listing_status == LISTING_STALE
        assert all(row.last_probed_at is None for row in rows.values())

    @pytest.mark.asyncio
    async def test_host_filter_and_limit_narrow_the_run(self, db_session, fake_checks):
        asked, _recorded = fake_checks
        _liveness_rows(db_session)
        options = cleanup_feed.ProbeOptions(host_filter="greenhouse.io", limit=1)
        stats = await cleanup_feed.phase_c(db_session, _report(db_session), options,
                                           write=False, now=NOW)
        assert asked == [GH_DEAD]
        assert stats["eligible"] == 2 and stats["checked"] == 1
        # Half the checked rows were dead, so the unchecked one projects ~1 more.
        assert stats["extrapolated_dead"] == 1

    @pytest.mark.asyncio
    async def test_rows_hidden_by_earlier_phases_are_not_checked(self, db_session, fake_checks):
        asked, _recorded = fake_checks
        rows = _liveness_rows(db_session)
        report = _report(db_session)
        report.hide([rows["dead"].id], "b:sweep_aggregator_expiry")
        await cleanup_feed.phase_c(db_session, report, cleanup_feed.ProbeOptions(),
                                   write=False, now=NOW)
        assert GH_DEAD not in asked

    @pytest.mark.asyncio
    async def test_checks_that_got_no_answer_are_deferred_not_stamped(
        self, db_session, fake_checks, monkeypatch,
    ):
        """A rate-limited or skipped check never sent a usable request: it
        is not recorded, and the row keeps its place for the next run."""
        _asked, recorded = fake_checks
        walled = "https://www.linkedin.com/jobs/view/7"
        skipped = "https://www.linkedin.com/jobs/view/8"
        monkeypatch.setitem(VERDICTS, walled, LivenessResult("unknown", "bot_wall_429"))
        monkeypatch.setitem(VERDICTS, skipped, LivenessResult("unknown", "host_skipped"))
        rows = {name: _row(db_session, url, source_platform="linkedin", board_key="")
                for name, url in (("walled", walled), ("skipped", skipped))}
        rows["dead"] = _row(db_session, GH_DEAD)

        stats = await cleanup_feed.phase_c(db_session, _report(db_session, dry_run=False),
                                           cleanup_feed.ProbeOptions(), write=True, now=NOW)
        db_session.expire_all()

        assert [r[0] for r in recorded] == [rows["dead"].id]
        assert stats["deferred"] == 2
        assert stats["deferred_reasons"] == {"bot_wall_429": 1, "host_skipped": 1}
        assert rows["walled"].last_probed_at is None
        assert rows["skipped"].last_probed_at is None
        assert rows["dead"].last_probed_at == NOW

    @pytest.mark.asyncio
    async def test_board_keys_and_an_uncapped_run_state_reach_the_checks(
        self, db_session, fake_checks,
    ):
        jane = "https://www.janestreet.com/join-jane-street/apply/8631912002?gh_jid=8631912002"
        _row(db_session, jane, board_key="greenhouse:janestreet")
        _row(db_session, LINKEDIN, source_platform="linkedin", board_key="")

        await cleanup_feed.phase_c(db_session, _report(db_session), cleanup_feed.ProbeOptions(),
                                   write=False, now=NOW)

        (call,) = CALLS
        assert call["board_keys"] == {jane: "greenhouse:janestreet", LINKEDIN: ""}
        # paced, but not held to the cron's per-run LinkedIn budget
        assert platform_liveness._run_state(call["cache"]).caps == {}


# ─── run(): terminal expiry after the checks ─────────────────────────────────

class TestRunOrder:
    @pytest.mark.asyncio
    async def test_terminal_expiry_counts_phase_c_checks_and_spares_unchecked_rows(
        self, db_session, fake_checks, monkeypatch,
    ):
        """Phase c's checks run before terminal expiry: a stale row the check
        reached (and couldn't judge) ends; one it never reached stays for
        cron-freshness to check; one its platform says is open comes back."""
        checked = "https://careers.acme.com/job/1"
        unreached = "https://careers.acme.com/job/2"
        monkeypatch.setitem(VERDICTS, checked, LivenessResult("unknown", "http_200"))
        stale = dict(listing_status=LISTING_STALE, last_seen_at=NOW - 40 * DAY,
                     first_seen_at=NOW - 60 * DAY, scraped_at=NOW - 60 * DAY,
                     listing_status_changed_at=NOW - 45 * DAY)
        rows = {
            "checked": _row(db_session, checked, **stale),
            "unreached": _row(db_session, unreached, **stale),  # no verdict: deadline
            "open": _row(db_session, GH_OPEN, **stale),
        }

        report = await cleanup_feed.run(db_session, write=True, phases="bc", now=NOW)
        db_session.expire_all()

        assert rows["checked"].listing_status == LISTING_EXPIRED
        assert report.hidden[rows["checked"].id] == "b:sweep_terminal_expiry"
        assert rows["unreached"].listing_status == LISTING_STALE
        assert rows["unreached"].last_probed_at is None
        assert rows["open"].listing_status == LISTING_ACTIVE

    @pytest.mark.asyncio
    async def test_legacy_verifier_stamps_are_not_evidence(self, db_session, fake_checks,
                                                           monkeypatch):
        """The old verifier bumped last_seen_at on every probe. On a board no
        crawl reconciles, a stale row's stamp after it went stale is one of
        those: it goes back to when the row went stale, so the row ends now
        instead of three weeks from its last legacy stamp."""
        legacy = "https://jobs.molsoncoors.com/job/MQX1"
        real_board = "https://boards.greenhouse.io/acme/jobs/77"
        for url in (legacy, real_board):
            monkeypatch.setitem(VERDICTS, url, LivenessResult("unknown", "http_200"))
        went_stale = NOW - 40 * DAY
        shape = dict(listing_status=LISTING_STALE, listing_status_changed_at=went_stale,
                     last_seen_at=NOW - 2 * DAY, first_seen_at=NOW - 90 * DAY,
                     scraped_at=NOW - 90 * DAY)
        rows = {
            "legacy": _row(db_session, legacy, board_key="unknown", **shape),
            # a crawled board's last_seen_at is real evidence: untouched
            "real_board": _row(db_session, real_board, board_key="greenhouse:acme", **shape),
        }

        report = await cleanup_feed.run(db_session, write=True, phases="bc", now=NOW)
        db_session.expire_all()

        assert rows["legacy"].last_seen_at == went_stale
        assert rows["legacy"].listing_status == LISTING_EXPIRED
        assert rows["real_board"].last_seen_at == NOW - 2 * DAY
        assert rows["real_board"].listing_status == LISTING_STALE
        assert report.phase_b["reset_legacy_seen"]["rows"] == 1


# ─── the dry-run guard ───────────────────────────────────────────────────────

@pytest.fixture
def guarded_db(tmp_path):
    """A throwaway SQLite database: seeded through a normal engine, then
    handed out through an engine with the dry-run guard installed."""
    url = f"sqlite:///{tmp_path / 'guard.db'}"
    plain = create_engine(url)
    Base.metadata.create_all(bind=plain)
    seed = sessionmaker(bind=plain)()
    guarded = create_engine(url)
    yield plain, seed, guarded
    seed.close()
    plain.dispose()
    guarded.dispose()


def _snapshot(engine):
    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text("SELECT * FROM scraped_jobs ORDER BY id"))]


class TestDryRunGuard:
    def test_guard_refuses_writes_before_they_reach_the_database(self, guarded_db):
        _plain, seed, guarded = guarded_db
        _row(seed, GH_DEAD)
        seen = cleanup_feed.install_read_only_guard(guarded)
        db = sessionmaker(bind=guarded)()
        try:
            assert db.execute(text("SELECT count(*) FROM scraped_jobs")).scalar() == 1
            for statement in ("UPDATE scraped_jobs SET title = 'x'",
                              "DELETE FROM scraped_jobs",
                              "INSERT INTO scraped_jobs (url, title, company) VALUES ('u', 't', 'c')",
                              "CREATE TABLE sneaky (x INTEGER)",
                              "ALTER TABLE scraped_jobs ADD COLUMN sneaky TEXT"):
                with pytest.raises(cleanup_feed.DryRunWriteError):
                    db.execute(text(statement))
                db.rollback()
            assert all(cleanup_feed.is_read_only_statement(s) for s in seen)
        finally:
            db.close()

    def test_database_itself_is_read_only_under_the_guard(self, guarded_db):
        _plain, seed, guarded = guarded_db
        _row(seed, GH_DEAD)
        cleanup_feed.install_read_only_guard(guarded)
        with guarded.connect() as conn:
            raw = conn.connection.driver_connection  # bypasses the statement hook
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                raw.execute("UPDATE scraped_jobs SET title = 'x'")

    @pytest.mark.asyncio
    async def test_full_dry_run_issues_no_writes(self, guarded_db, fake_checks):
        plain, seed, guarded = guarded_db
        _github(seed, TESLA, posted_date=datetime.datetime(2026, 12, 12))
        utm_copy = _github(seed, TESLA + "?utm_source=vansh")
        lifecycle = _lifecycle_rows(seed)
        live = _liveness_rows(seed)
        _row(seed, "https://careers.acme.com/job/9", board_key="")  # legacy, unkeyed
        before = _snapshot(plain)

        seen = cleanup_feed.install_read_only_guard(guarded)
        db = sessionmaker(bind=guarded)()
        try:
            report = await cleanup_feed.run(db, write=False, now=NOW)
        finally:
            db.close()

        assert _snapshot(plain) == before
        assert seen and all(cleanup_feed.is_read_only_statement(s) for s in seen)
        assert report.hidden[utm_copy.id] == "a:utm_twin"
        assert report.hidden[lifecycle["terminal"].id] == "b:sweep_terminal_expiry"
        assert report.hidden[lifecycle["linkedin"].id] == "b:sweep_aggregator_expiry"
        assert report.hidden[live["dead"].id] == "c:dead"
        assert report.phase_b["board_keys_marked_unknown"] == 1
        assert report.visible_after == len(report.visible_start) - len(report.hidden)

    def test_main_dry_run_never_migrates(self, guarded_db, monkeypatch):
        """The CLI path: no --apply means the guard goes on and the DDL
        migration is never called; --apply runs it."""
        from backend.db import database
        from backend.migrations import add_listing_probe_columns

        _plain, seed, guarded = guarded_db
        _github(seed, TESLA, posted_date=datetime.datetime(2026, 12, 12))
        migrations: list[str] = []
        monkeypatch.setattr(add_listing_probe_columns, "run_migration",
                            lambda: migrations.append("ran"))
        monkeypatch.setattr(cleanup_feed, "_DATABASE_URL", "sqlite:///guard.db")
        monkeypatch.setattr(database, "engine", guarded)
        monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=guarded))
        guards: list = []
        real_guard = cleanup_feed.install_read_only_guard
        monkeypatch.setattr(cleanup_feed, "install_read_only_guard",
                            lambda engine: guards.append(engine) or real_guard(engine))

        assert cleanup_feed.main(["--phase", "a,b"]) == 0
        assert migrations == [] and guards == [guarded]

    def test_main_apply_runs_the_migration(self, guarded_db, monkeypatch):
        from backend.db import database
        from backend.migrations import add_listing_probe_columns

        plain, seed, _guarded = guarded_db
        row = _github(seed, TESLA, posted_date=datetime.datetime(2026, 12, 12))
        migrations: list[str] = []
        monkeypatch.setattr(add_listing_probe_columns, "run_migration",
                            lambda: migrations.append("ran"))
        monkeypatch.setattr(cleanup_feed, "_DATABASE_URL", "sqlite:///guard.db")
        monkeypatch.setattr(database, "engine", plain)
        monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=plain))

        assert cleanup_feed.main(["--apply", "--phase", "a"]) == 0
        assert migrations == ["ran"]
        seed.expire_all()
        assert row.posted_date == datetime.datetime(2025, 12, 12)
