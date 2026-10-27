"""
The v2 work queue: the scrape_tasks rows a run works through.

A task is one unit of work for one run (kind list | fetch | enrich | liveness;
the runner enqueues a ``list`` task per board). A worker claims tasks by
leasing them: ``running`` until ``lease_until``. A worker that dies leaves its
lease to run out, and the next claim takes the task back. Every claim counts
an attempt: a task that fails, or whose lease runs out, on its MAX_ATTEMPTS-th
attempt is ``failed`` with its last error; one with attempts left goes back to
``pending``. Finished tasks (done or failed) older than RETAIN_DAYS are deleted
at the start of a run, so the table holds about a week of runs.

Postgres claims with SELECT ... FOR UPDATE SKIP LOCKED, so two workers never
take the same task and neither waits on the other's rows; SQLite (the tests)
has no row locks, and one process needs none.
"""

from __future__ import annotations

import datetime
from typing import Iterable, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, object_session

from backend.db.models import ScrapeTask

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
FINISHED = (DONE, FAILED)

MAX_ATTEMPTS = 3
RETAIN_DAYS = 7
_ERROR_CHARS = 2000


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def enqueue(db: Session, run_id: int, kind: str, payloads: Iterable[dict]) -> list[ScrapeTask]:
    """One pending task of ``kind`` per payload, for run ``run_id``. Commits."""
    tasks = [ScrapeTask(run_id=run_id, kind=kind, payload=payload, status=PENDING, attempts=0)
             for payload in payloads]
    if tasks:
        db.add_all(tasks)
        db.commit()
    return tasks


def claim(db: Session, run_id: int, limit: int, lease_seconds: float,
          now: Optional[datetime.datetime] = None) -> list[ScrapeTask]:
    """Lease up to ``limit`` of the run's tasks, oldest first: pending ones,
    and running ones whose lease ran out (their worker is gone). Each claim
    is an attempt; a lapsed task that already had MAX_ATTEMPTS fails instead.
    Commits. Returns the leased tasks."""
    now = now or _utcnow()
    lapsed = (ScrapeTask.status == RUNNING) & (ScrapeTask.lease_until < now)
    spent = (
        db.query(ScrapeTask)
        .filter(ScrapeTask.run_id == run_id, lapsed, ScrapeTask.attempts >= MAX_ATTEMPTS)
        .update({"status": FAILED, "finished_at": now, "lease_until": None,
                 "last_error": func.coalesce(ScrapeTask.last_error, "lease expired")},
                synchronize_session=False)
    )
    if spent:
        db.commit()
    if limit <= 0:
        return []

    query = (
        db.query(ScrapeTask)
        .filter(ScrapeTask.run_id == run_id,
                or_(ScrapeTask.status == PENDING, lapsed),
                ScrapeTask.attempts < MAX_ATTEMPTS)
        .order_by(ScrapeTask.id)
        .limit(limit)
    )
    if db.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    tasks = query.all()
    lease_until = now + datetime.timedelta(seconds=lease_seconds)
    for task in tasks:
        task.status = RUNNING
        task.attempts = (task.attempts or 0) + 1
        task.lease_until = lease_until
    db.commit()
    return tasks


def finish(task: ScrapeTask, ok: bool, error: Optional[str] = None,
           now: Optional[datetime.datetime] = None) -> str:
    """End one attempt of a claimed task: ``done`` when it worked; when it
    failed, ``failed`` with ``error`` once it has had MAX_ATTEMPTS, else
    back to ``pending`` for another claim. Commits. Returns the new status."""
    db = object_session(task)
    now = now or _utcnow()
    if ok:
        status = DONE
    elif (task.attempts or 0) >= MAX_ATTEMPTS:
        status = FAILED
    else:
        status = PENDING
    task.status = status
    task.lease_until = None
    if not ok:
        task.last_error = (error or "failed")[:_ERROR_CHARS]
    task.finished_at = now if status in FINISHED else None
    db.commit()
    return status


def fail_unfinished(db: Session, run_id: int, error: str,
                    now: Optional[datetime.datetime] = None) -> int:
    """Fail every task of run ``run_id`` that has not finished (a run that
    died, or one that ran out of time): they are not retried by a later run,
    which enqueues its own. Commits. Returns the count."""
    now = now or _utcnow()
    count = (
        db.query(ScrapeTask)
        .filter(ScrapeTask.run_id == run_id, ScrapeTask.status.notin_(FINISHED))
        .update({"status": FAILED, "finished_at": now, "lease_until": None,
                 "last_error": error[:_ERROR_CHARS]},
                synchronize_session=False)
    )
    db.commit()
    return count


def delete_finished(db: Session, now: Optional[datetime.datetime] = None,
                    days: int = RETAIN_DAYS) -> int:
    """Delete finished tasks (done or failed) older than ``days``. Commits.
    Returns the count."""
    now = now or _utcnow()
    count = (
        db.query(ScrapeTask)
        .filter(ScrapeTask.status.in_(FINISHED),
                ScrapeTask.finished_at < now - datetime.timedelta(days=days))
        .delete(synchronize_session=False)
    )
    db.commit()
    return count


def counts(db: Session, run_id: int) -> dict[str, int]:
    """{status: tasks} for one run."""
    return {
        status: int(n)
        for status, n in (
            db.query(ScrapeTask.status, func.count(ScrapeTask.id))
            .filter(ScrapeTask.run_id == run_id)
            .group_by(ScrapeTask.status)
            .all()
        )
    }
