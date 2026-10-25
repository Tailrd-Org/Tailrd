"""The v2 work queue (ingest/core/queue.py): claims lease tasks, a lapsed
lease is claimed again, a task gets at most MAX_ATTEMPTS, and finished tasks
age out after a week.

The Postgres-only test runs when INGEST_TEST_POSTGRES_URL names a scratch
Postgres database (never Neon): two sessions claiming at once must split the
tasks, which only FOR UPDATE SKIP LOCKED guarantees."""

import datetime
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Query, sessionmaker

from backend.db.models import ScrapeRun, ScrapeTask
from backend.ingest.core import queue

NOW = datetime.datetime(2026, 10, 25, 12, 0, 0)


def _run(db):
    run = ScrapeRun(trigger="manual")
    db.add(run)
    db.commit()
    return run.id


def _boards(n):
    return [{"platform": "greenhouse", "slug": f"board-{i}", "company": f"Co {i}"}
            for i in range(n)]


def test_claim_leases_the_oldest_pending_tasks(db_session):
    run_id = _run(db_session)
    queue.enqueue(db_session, run_id, "list", _boards(5))

    first = queue.claim(db_session, run_id, limit=2, lease_seconds=600, now=NOW)
    second = queue.claim(db_session, run_id, limit=10, lease_seconds=600, now=NOW)

    assert [task.payload["slug"] for task in first] == ["board-0", "board-1"]
    assert [task.payload["slug"] for task in second] == ["board-2", "board-3", "board-4"]
    for task in first + second:
        assert (task.status, task.attempts) == (queue.RUNNING, 1)
        assert task.lease_until == NOW + datetime.timedelta(seconds=600)
    assert queue.claim(db_session, run_id, limit=10, lease_seconds=600, now=NOW) == []


def test_claims_stay_inside_their_run(db_session):
    mine, other = _run(db_session), _run(db_session)
    queue.enqueue(db_session, other, "list", _boards(2))
    assert queue.claim(db_session, mine, limit=10, lease_seconds=60, now=NOW) == []


def test_a_lapsed_lease_is_claimed_again(db_session):
    run_id = _run(db_session)
    queue.enqueue(db_session, run_id, "list", _boards(1))
    (task,) = queue.claim(db_session, run_id, limit=1, lease_seconds=60, now=NOW)

    # Its worker died: within the lease nobody takes it, after it anyone may.
    assert queue.claim(db_session, run_id, 1, 60, now=NOW + datetime.timedelta(seconds=59)) == []
    (again,) = queue.claim(db_session, run_id, 1, 60, now=NOW + datetime.timedelta(seconds=61))
    assert again.id == task.id and again.attempts == 2


def test_a_task_gets_at_most_three_attempts(db_session):
    run_id = _run(db_session)
    queue.enqueue(db_session, run_id, "list", _boards(1))

    statuses = []
    for attempt in range(1, 4):
        (task,) = queue.claim(db_session, run_id, 1, 60, now=NOW)
        assert task.attempts == attempt
        statuses.append(queue.finish(task, ok=False, error=f"HTTP 503 #{attempt}", now=NOW))

    assert statuses == [queue.PENDING, queue.PENDING, queue.FAILED]
    assert (task.last_error, task.finished_at) == ("HTTP 503 #3", NOW)
    assert queue.claim(db_session, run_id, 1, 60, now=NOW) == []


def test_a_lease_that_lapses_on_the_last_attempt_fails_the_task(db_session):
    run_id = _run(db_session)
    queue.enqueue(db_session, run_id, "list", _boards(1))
    when = NOW
    for _ in range(3):
        assert len(queue.claim(db_session, run_id, 1, 60, now=when)) == 1
        when += datetime.timedelta(minutes=5)  # its worker died each time
    assert queue.claim(db_session, run_id, 1, 60, now=when) == []

    db_session.expire_all()
    task = db_session.query(ScrapeTask).one()
    assert (task.status, task.attempts, task.last_error) == (queue.FAILED, 3, "lease expired")


def test_finish_marks_a_task_done(db_session):
    run_id = _run(db_session)
    queue.enqueue(db_session, run_id, "list", _boards(2))
    first, second = queue.claim(db_session, run_id, 2, 60, now=NOW)

    assert queue.finish(first, ok=True, now=NOW) == queue.DONE
    assert (first.finished_at, first.lease_until) == (NOW, None)
    assert queue.counts(db_session, run_id) == {queue.DONE: 1, queue.RUNNING: 1}

    assert queue.fail_unfinished(db_session, run_id, "run ended", now=NOW) == 1
    db_session.expire_all()
    assert (second.status, second.last_error) == (queue.FAILED, "run ended")


def test_finished_tasks_older_than_a_week_are_deleted(db_session):
    run_id = _run(db_session)
    queue.enqueue(db_session, run_id, "list", _boards(4))
    old_done, old_failed, new_done, pending = db_session.query(ScrapeTask).order_by(ScrapeTask.id)
    for task, status, age in ((old_done, queue.DONE, 8), (old_failed, queue.FAILED, 9),
                              (new_done, queue.DONE, 6)):
        task.status, task.finished_at = status, NOW - datetime.timedelta(days=age)
    db_session.commit()

    assert queue.delete_finished(db_session, now=NOW) == 2
    db_session.expire_all()
    assert {task.id for task in db_session.query(ScrapeTask)} == {new_done.id, pending.id}


def test_postgres_claims_skip_locked_rows(db_session, monkeypatch):
    """On Postgres the claim asks for FOR UPDATE SKIP LOCKED; on SQLite it
    is a plain select."""
    run_id = _run(db_session)
    queue.enqueue(db_session, run_id, "list", _boards(1))
    asked = []
    original = Query.with_for_update

    def spy(self, **kwargs):
        asked.append(kwargs)
        return original(self, **kwargs)

    monkeypatch.setattr(Query, "with_for_update", spy)
    queue.claim(db_session, run_id, 1, 60, now=NOW)
    assert asked == []

    from sqlalchemy.dialects import postgresql

    statement = (db_session.query(ScrapeTask).limit(1)
                 .with_for_update(skip_locked=True).statement)
    assert "FOR UPDATE SKIP LOCKED" in str(statement.compile(dialect=postgresql.dialect()))
    monkeypatch.setattr(db_session.get_bind().dialect, "name", "postgresql")
    queue.claim(db_session, run_id, 1, 60, now=NOW + datetime.timedelta(minutes=5))
    assert asked[-1] == {"skip_locked": True}


POSTGRES_URL = os.environ.get("INGEST_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="set INGEST_TEST_POSTGRES_URL to a scratch Postgres")
def test_two_postgres_workers_never_claim_the_same_task():
    from backend.db.database import Base

    engine = create_engine(POSTGRES_URL)
    Base.metadata.create_all(engine, tables=[ScrapeRun.__table__, ScrapeTask.__table__])
    Session = sessionmaker(bind=engine)
    one, two = Session(), Session()
    try:
        run = ScrapeRun(trigger="manual")
        one.add(run)
        one.commit()
        queue.enqueue(one, run.id, "list", _boards(6))

        # Worker one holds its claim's row locks open; worker two must skip
        # those rows instead of waiting on them or taking them too.
        held = (one.query(ScrapeTask).filter(ScrapeTask.run_id == run.id)
                .order_by(ScrapeTask.id).limit(3).with_for_update(skip_locked=True).all())
        taken = queue.claim(two, run.id, limit=10, lease_seconds=60)
        one.rollback()

        assert {task.id for task in taken}.isdisjoint({task.id for task in held})
        assert len(taken) == 3
    finally:
        one.close()
        two.close()
        with engine.begin() as conn:
            conn.exec_driver_sql("DELETE FROM scrape_tasks")
            conn.exec_driver_sql("DELETE FROM scrape_runs")
        engine.dispose()
