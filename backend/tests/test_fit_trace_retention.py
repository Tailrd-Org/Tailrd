"""Trace retention (owner decision, D-153): the sweep prunes the traces and score rows of closed
jobs once the score is older than 90 days.

Traces are about 2 KB per (user, job) pair and nothing else deletes them. Their audit value ends
when the job closes, so a closed job's trace (and its score row) goes once it is 90 days old. Only
the known closed statuses (removed, expired, off_target) prune; active, stale, NULL and any status
the code does not know keep their rows whatever their age. The delete is bounded per sweep (reads
included), runs in its own transaction, re-checks its predicate in the DELETE itself, and never
deletes a trace without deleting its score row in the same transaction.
"""

import datetime

import pytest
from sqlalchemy import event

from backend.db.models import JobMatchScore, MatchTrace, ScrapedJob, User
from backend.services import fit_engine, match_notifier

NOW = datetime.datetime(2026, 10, 13, 12, 0, 0)
OLD = NOW - datetime.timedelta(days=91)
RECENT = NOW - datetime.timedelta(days=89)


@pytest.fixture(autouse=True)
def _salt(monkeypatch):
    monkeypatch.setenv("FIT_TRACE_SALT", "a-test-salt-of-at-least-16-bytes")


def _user(db, email="keep@example.com"):
    user = User(email=email, first_name="T", email_verified=True, auth_provider="local")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _job(db, slug, status):
    job = ScrapedJob(title=f"Role {slug}", company="Globex", url=f"https://jobs.example.com/{slug}",
                     description="Requirements\n- Python\n" + "x" * 200,
                     posted_date=NOW, listing_status=status)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _banked(db, user, job, when, *, subject=None):
    """A score row and its trace, written together as bank_scores does (same job, same time)."""
    subject = subject or fit_engine.subject_id(user.id, fit_engine._trace_salt())
    db.add(JobMatchScore(user_id=user.id, job_id=job.id, score=70, resume_fingerprint="fit1:x",
                         scored_at=when))
    db.add(MatchTrace(subject_id=subject, job_id=job.id, trace={"t": 1}, trace_digest="d" * 16,
                      scored_at=when))
    db.commit()


def _pairs(db):
    db.expire_all()
    return (sorted(row.job_id for row in db.query(MatchTrace).all()),
            sorted(row.job_id for row in db.query(JobMatchScore).all()))


def _prune(db, **kw):
    return fit_engine.prune_traces(db, now=NOW, **kw)


# ─── what is pruned ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["removed", "expired", "off_target"])
def test_closed_old_traces_and_scores_are_pruned(db_session, status):
    user = _user(db_session)
    job = _job(db_session, status, status)
    _banked(db_session, user, job, OLD)

    assert _prune(db_session).pruned == 1
    assert _pairs(db_session) == ([], [])


# ``stale`` is hidden from the feed since scraper v2 (until a crawl or a check vouches for it
# again), but it is no closure: most stale jobs come back, so their traces stay (D-153).
@pytest.mark.parametrize("status", ["active", "stale", None, "paused", ""])
def test_open_null_and_unknown_statuses_never_prune(db_session, status):
    user = _user(db_session)
    job = _job(db_session, f"kept-{status}", status)
    _banked(db_session, user, job, OLD - datetime.timedelta(days=400))

    assert _prune(db_session).pruned == 0
    assert _pairs(db_session) == ([job.id], [job.id])


def test_a_closed_job_keeps_its_trace_for_90_days(db_session):
    user = _user(db_session)
    job = _job(db_session, "closed-recently", "removed")
    _banked(db_session, user, job, RECENT)

    assert _prune(db_session).pruned == 0
    assert _pairs(db_session) == ([job.id], [job.id])


def test_the_delete_is_bounded_per_sweep(db_session):
    user = _user(db_session)
    other = _user(db_session, "other@example.com")
    jobs = [_job(db_session, f"closed-{i}", "removed") for i in range(3)]
    for job in jobs:
        _banked(db_session, user, job, OLD)
        _banked(db_session, other, job, OLD)

    assert _prune(db_session, limit=4).pruned == 4
    traces, scores = _pairs(db_session)
    assert len(traces) == 2 and len(scores) == 2
    assert _prune(db_session, limit=4).pruned == 2
    assert _pairs(db_session) == ([], [])


def test_only_the_pruned_traces_score_rows_go(db_session):
    """A closed job scored for two users: an old pair goes, a recent pair (rescored later) stays."""
    old_user = _user(db_session)
    new_user = _user(db_session, "new@example.com")
    job = _job(db_session, "closed", "removed")
    _banked(db_session, old_user, job, OLD)
    _banked(db_session, new_user, job, RECENT)

    assert _prune(db_session).pruned == 1
    assert [row.user_id for row in db_session.query(JobMatchScore).all()] == [new_user.id]
    assert [row.subject_id for row in db_session.query(MatchTrace).all()] == [
        fit_engine.subject_id(new_user.id, fit_engine._trace_salt())]


# ─── reads bounded by the limit (review I-1) ─────────────────────────────────────────────────


def test_the_reads_are_bounded_by_the_limit(db_session, monkeypatch):
    """Many users scored the same closed jobs: pruning a small batch must not load every user's score
    rows of those jobs, nor hash a subject id per score row."""
    users = [_user(db_session, f"u{i}@example.com") for i in range(8)]
    jobs = [_job(db_session, f"many-{i}", "removed") for i in range(5)]
    for user in users:
        for job in jobs:
            _banked(db_session, user, job, OLD)

    calls = []
    real = fit_engine.subject_id
    monkeypatch.setattr(fit_engine, "subject_id", lambda uid, salt: calls.append(uid) or real(uid, salt))
    statements = []

    def capture(conn, cursor, statement, params, context, executemany):
        statements.append(statement.lower())

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert _prune(db_session, limit=3).pruned == 3
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert len(calls) <= len(users)  # one subject id per user, never one per score row
    reads_of_scores = [s for s in statements if s.lstrip().startswith("select") and "job_match_scores" in s]
    assert reads_of_scores == []  # score rows are deleted by their (user, job) pairs, never loaded
    traces, scores = _pairs(db_session)
    assert len(traces) == len(scores) == 8 * 5 - 3


# ─── the DELETE re-checks its predicate (review I-2) ─────────────────────────────────────────


def test_a_pair_reopened_and_rescored_mid_prune_is_kept(db_session):
    """Between the prune's select and its DELETE the job reopens and a concurrent bank rescores the
    pair in place (same row ids, scored_at = now): the fresh rows must survive."""
    user = _user(db_session)
    job = _job(db_session, "reopens", "removed")
    _banked(db_session, user, job, OLD)
    state = {"armed": True}

    def reopen_first(conn, cursor, statement, params, context, executemany):
        if state["armed"] and statement.lstrip().lower().startswith("delete"):
            state["armed"] = False
            cursor.execute("UPDATE scraped_jobs SET listing_status = 'active' WHERE id = ?", (job.id,))
            cursor.execute("UPDATE job_match_scores SET scored_at = ? WHERE job_id = ?", (str(NOW), job.id))
            cursor.execute("UPDATE match_traces SET scored_at = ? WHERE job_id = ?", (str(NOW), job.id))

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", reopen_first)
    try:
        result = _prune(db_session)
    finally:
        event.remove(engine, "before_cursor_execute", reopen_first)

    assert not state["armed"]
    assert result.pruned == 0
    assert _pairs(db_session) == ([job.id], [job.id])


# ─── no trace without its score row (review M-2) ─────────────────────────────────────────────


def test_a_trace_whose_score_row_is_not_found_is_kept_and_reported(db_session):
    """A trace written under another salt maps to no user: nothing is pruned, and it is counted."""
    user = _user(db_session)
    job = _job(db_session, "other-salt", "removed")
    _banked(db_session, user, job, OLD, subject=fit_engine.subject_id(user.id, b"another-salt-0123456789"))

    result = _prune(db_session)
    assert (result.pruned, result.unmatched) == (0, 1)
    assert _pairs(db_session) == ([job.id], [job.id])


def test_a_trace_without_a_score_row_is_kept(db_session):
    user = _user(db_session)
    job = _job(db_session, "no-score", "removed")
    _banked(db_session, user, job, OLD)
    db_session.query(JobMatchScore).delete()
    db_session.commit()

    result = _prune(db_session)
    assert (result.pruned, result.unmatched) == (0, 1)
    assert _pairs(db_session) == ([job.id], [])


# ─── through the sweep ───────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sweep_runs_the_real_prune(db_session):
    user = _user(db_session)
    closed = _job(db_session, "sweep-closed", "removed")
    _banked(db_session, user, closed, datetime.datetime.utcnow() - datetime.timedelta(days=200))

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["traces_pruned"] == 1
    assert _pairs(db_session) == ([], [])


@pytest.mark.asyncio
async def test_the_sweep_reports_traces_pruned(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(fit_engine, "prune_traces",
                        lambda db, **kw: calls.append(kw) or fit_engine.PruneResult(pruned=7, unmatched=0))

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["traces_pruned"] == 7
    assert "traces_prune_unmatched" not in result
    assert calls and calls[0]["limit"] == match_notifier.TRACE_PRUNE_LIMIT == 5000


@pytest.mark.asyncio
async def test_the_sweep_reports_unmatched_traces(db_session, monkeypatch):
    monkeypatch.setattr(fit_engine, "prune_traces",
                        lambda db, **kw: fit_engine.PruneResult(pruned=0, unmatched=3))
    result = await match_notifier.sweep_match_alerts(db_session)
    assert result["traces_pruned"] == 0
    assert result["traces_prune_unmatched"] == 3


@pytest.mark.asyncio
async def test_the_sweep_skips_pruning_without_the_salt(db_session, monkeypatch):
    monkeypatch.delenv("FIT_TRACE_SALT", raising=False)
    user = _user(db_session)
    job = _job(db_session, "no-salt", "removed")
    db_session.add(JobMatchScore(user_id=user.id, job_id=job.id, score=70, resume_fingerprint="fit1:x",
                                 scored_at=OLD))
    db_session.add(MatchTrace(subject_id="s" * 24, job_id=job.id, trace={}, trace_digest="d" * 16, scored_at=OLD))
    db_session.commit()

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["traces_pruned"] == 0
    assert result["traces_prune_skipped"] == "fit_trace_salt_missing"
    assert _pairs(db_session) == ([job.id], [job.id])


@pytest.mark.asyncio
async def test_the_sweep_skips_pruning_once_its_deadline_is_spent(db_session, monkeypatch):
    monkeypatch.setenv("CRON_MATCH_BUDGET_S", "100")
    clock = iter([0.0] + [500.0] * 50)
    monkeypatch.setattr(match_notifier, "_now", lambda: next(clock))
    monkeypatch.setattr(fit_engine, "prune_traces",
                        lambda db, **kw: (_ for _ in ()).throw(AssertionError("pruned after the deadline")))

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["traces_pruned"] == 0
    assert result["traces_prune_skipped"] == "deadline"


@pytest.mark.asyncio
async def test_a_failed_prune_is_rolled_back_and_reported(db_session, monkeypatch):
    def boom(db, **kw):
        raise RuntimeError("lock timeout")

    monkeypatch.setattr(fit_engine, "prune_traces", boom)

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["traces_pruned"] == 0
    assert result["traces_prune_error"] == "RuntimeError"
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_retention_runs_with_alerts_disabled(db_session, monkeypatch):
    """Turning alerts off (an email incident) must not stop retention (review M-3)."""
    monkeypatch.setenv("MATCH_ALERTS_ENABLED", "false")
    user = _user(db_session)
    job = _job(db_session, "disabled", "expired")
    _banked(db_session, user, job, datetime.datetime.utcnow() - datetime.timedelta(days=200))

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["status"] == "disabled"
    assert result["traces_pruned"] == 1
    assert _pairs(db_session) == ([], [])
