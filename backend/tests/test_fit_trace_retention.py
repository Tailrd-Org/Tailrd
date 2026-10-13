"""Trace retention (owner decision, D-153): the sweep prunes the traces and score rows of jobs
that can no longer be applied to, once the score is older than 90 days.

Traces are about 2 KB per (user, job) pair and nothing else deletes them. Their audit value ends
when the job closes, so a closed job's trace (and its score row) goes once it is 90 days old. A job
that can still be applied to (active, or stale: still shown in the feed) keeps its trace whatever
its age. The delete is bounded per sweep and runs in its own transaction.
"""

import datetime

import pytest

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


def _banked(db, user, job, when):
    """A score row and its trace, written together as bank_scores does (same job, same time)."""
    subject = fit_engine.subject_id(user.id, fit_engine._trace_salt())
    db.add(JobMatchScore(user_id=user.id, job_id=job.id, score=70, resume_fingerprint="fit1:x",
                         scored_at=when))
    db.add(MatchTrace(subject_id=subject, job_id=job.id, trace={"t": 1}, trace_digest="d" * 16,
                      scored_at=when))
    db.commit()


def _pairs(db):
    return (sorted(row.job_id for row in db.query(MatchTrace).all()),
            sorted(row.job_id for row in db.query(JobMatchScore).all()))


def test_closed_old_traces_and_scores_are_pruned(db_session):
    user = _user(db_session)
    removed = _job(db_session, "removed", "removed")
    expired = _job(db_session, "expired", "expired")
    _banked(db_session, user, removed, OLD)
    _banked(db_session, user, expired, OLD)

    assert fit_engine.prune_traces(db_session, now=NOW) == 2
    assert _pairs(db_session) == ([], [])


def test_jobs_that_can_be_applied_to_keep_their_traces_at_any_age(db_session):
    user = _user(db_session)
    kept = [_job(db_session, "active", "active"), _job(db_session, "stale", "stale"),
            _job(db_session, "unknown", None)]
    for job in kept:
        _banked(db_session, user, job, OLD - datetime.timedelta(days=400))

    assert fit_engine.prune_traces(db_session, now=NOW) == 0
    ids = sorted(job.id for job in kept)
    assert _pairs(db_session) == (ids, ids)


def test_a_closed_job_keeps_its_trace_for_90_days(db_session):
    user = _user(db_session)
    job = _job(db_session, "closed-recently", "removed")
    _banked(db_session, user, job, RECENT)

    assert fit_engine.prune_traces(db_session, now=NOW) == 0
    assert _pairs(db_session) == ([job.id], [job.id])


def test_the_delete_is_bounded_per_sweep(db_session):
    user = _user(db_session)
    other = _user(db_session, "other@example.com")
    jobs = [_job(db_session, f"closed-{i}", "removed") for i in range(3)]
    for job in jobs:
        _banked(db_session, user, job, OLD)
        _banked(db_session, other, job, OLD)

    assert fit_engine.prune_traces(db_session, now=NOW, limit=4) == 4
    traces, scores = _pairs(db_session)
    assert len(traces) == 2 and len(scores) == 2
    assert fit_engine.prune_traces(db_session, now=NOW, limit=4) == 2
    assert _pairs(db_session) == ([], [])


def test_only_the_pruned_traces_score_rows_go(db_session):
    """A closed job scored for two users: an old pair goes, a recent pair (rescored later) stays."""
    old_user = _user(db_session)
    new_user = _user(db_session, "new@example.com")
    job = _job(db_session, "closed", "removed")
    _banked(db_session, old_user, job, OLD)
    _banked(db_session, new_user, job, RECENT)

    assert fit_engine.prune_traces(db_session, now=NOW) == 1
    assert [row.user_id for row in db_session.query(JobMatchScore).all()] == [new_user.id]
    assert [row.subject_id for row in db_session.query(MatchTrace).all()] == [
        fit_engine.subject_id(new_user.id, fit_engine._trace_salt())]


@pytest.mark.asyncio
async def test_the_sweep_reports_traces_pruned(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(fit_engine, "prune_traces", lambda db, **kw: calls.append(kw) or 7)

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["traces_pruned"] == 7
    assert calls and calls[0]["limit"] == match_notifier.TRACE_PRUNE_LIMIT == 5000


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
async def test_disabled_sweep_prunes_nothing(db_session, monkeypatch):
    monkeypatch.setenv("MATCH_ALERTS_ENABLED", "false")
    result = await match_notifier.sweep_match_alerts(db_session)
    assert result["traces_pruned"] == 0
