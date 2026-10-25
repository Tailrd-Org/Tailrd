"""backend/scripts/reclaim_dead_descriptions.py: the text of postings closed
over 30 days ago that no user table references is cleared, in batches, only
with --apply; every row stays.

The Postgres test runs when INGEST_TEST_POSTGRES_URL names a scratch Postgres
(never Neon): JSON null versus SQL NULL, and the plain VACUUM."""

import datetime
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from backend.db import database
from backend.db.models import (
    ApplicationRecord, ConnectionRequest, JobMatchNotification, PendingQuestion, ScrapedJob,
    TailoredResume, UserSavedJob,
)
from backend.scripts import reclaim_dead_descriptions as reclaim

NOW = datetime.datetime(2026, 10, 25, 12, 0, 0)
OLD = NOW - datetime.timedelta(days=40)
TEXT = dict(description="A long posting. " * 50, description_sections={"about": "x"},
            change_log=[{"at": "2026-09-01", "changed": ["title"]}])


def _row(db, n, **kwargs):
    fields = dict(title=f"Job {n}", company="Acme", url=f"https://x/{n}", source_platform="ats",
                  listing_status="removed", listing_status_changed_at=OLD, **TEXT)
    fields.update(kwargs)
    row = ScrapedJob(**fields)
    db.add(row)
    db.commit()
    return row.id


@pytest.fixture
def seeded(db_session):
    ids = {
        "dead": _row(db_session, 1),
        "expired": _row(db_session, 2, listing_status="expired"),
        # No status change stamp: its last sighting dates it.
        "unstamped": _row(db_session, 3, listing_status_changed_at=None, last_seen_at=OLD),
        "recent": _row(db_session, 4, listing_status_changed_at=NOW - datetime.timedelta(days=10)),
        "active": _row(db_session, 5, listing_status="active"),
        "bare": _row(db_session, 6, description="", description_sections=None, change_log=None),
    }
    referenced = {
        "applied": lambda job: ApplicationRecord(job_id=job, company="Acme", role="Analyst"),
        "saved": lambda job: UserSavedJob(user_id=1, job_id=job),
        "tailored": lambda job: TailoredResume(job_id=job, original_text="a", tailored_text="b"),
        "alerted": lambda job: JobMatchNotification(user_id=1, job_id=job),
        "connection": lambda job: ConnectionRequest(job_id=job, contact_name="Ann", company="Acme"),
        "question": lambda job: PendingQuestion(job_id=job, question="Why?"),
    }
    for n, (name, make) in enumerate(referenced.items(), start=10):
        ids[name] = _row(db_session, n)
        db_session.add(make(ids[name]))
    db_session.commit()
    return ids


def _text_of(db, row_id):
    db.expire_all()
    row = db.get(ScrapedJob, row_id)
    return row.description, row.description_sections, row.change_log


def test_a_dry_run_counts_and_changes_nothing(db_session, seeded):
    stats = reclaim.reclaim(db_session, now=NOW)

    assert stats["rows"] == 3 and stats["bytes"] > 3 * 700 and stats["cleared"] == 0
    assert _text_of(db_session, seeded["dead"])[0] == TEXT["description"]


def test_apply_clears_only_old_unreferenced_closed_rows(db_session, seeded):
    stats = reclaim.reclaim(db_session, apply=True, batch=2, now=NOW)

    assert (stats["cleared"], stats["batches"]) == (3, 2)
    for name in ("dead", "expired", "unstamped"):
        assert _text_of(db_session, seeded[name]) == (None, None, None)
    for name in ("recent", "active", "applied", "saved", "tailored", "alerted", "connection",
                 "question"):
        assert _text_of(db_session, seeded[name])[0] == TEXT["description"], name
    assert db_session.query(ScrapedJob).count() == len(seeded)  # no row deleted

    again = reclaim.reclaim(db_session, apply=True, now=NOW)
    assert (again["rows"], again["cleared"]) == (0, 0)


def test_the_days_option_moves_the_cutoff(db_session, seeded):
    assert reclaim.reclaim(db_session, days=5, now=NOW)["rows"] == 4  # "recent" too


def test_the_command_is_a_dry_run_unless_told(db_session, seeded, monkeypatch, capsys):
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=db_session.get_bind()))

    assert reclaim.main([]) == 0
    out = capsys.readouterr().out
    assert "would clear" in out and "dry run: nothing changed" in out
    assert _text_of(db_session, seeded["dead"])[0] == TEXT["description"]

    assert reclaim.main(["--apply", "--days", "30"]) == 0
    assert "after:" in capsys.readouterr().out
    assert _text_of(db_session, seeded["dead"]) == (None, None, None)


POSTGRES_URL = os.environ.get("INGEST_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES_URL, reason="set INGEST_TEST_POSTGRES_URL to a scratch Postgres")
def test_on_postgres_json_null_counts_as_empty_and_vacuum_runs():
    engine = create_engine(POSTGRES_URL)
    database.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    try:
        db.execute(text("DELETE FROM scraped_jobs"))
        db.commit()
        real = _row(db, 1)
        # Set to None through the ORM: JSON null, not SQL NULL, on Postgres.
        json_null = _row(db, 2, description="", description_sections=None, change_log=None)
        stats = reclaim.reclaim(db, apply=True, now=NOW)

        assert stats["rows"] == 1 and stats["bytes"] > 0
        assert _text_of(db, real) == (None, None, None)
        assert db.execute(text("SELECT description_sections IS NULL FROM scraped_jobs "
                               "WHERE id = :id"), {"id": real}).scalar()
        assert _text_of(db, json_null)[0] == ""
        assert reclaim.vacuum(engine) is True
        assert reclaim.sizes(db)["scraped_jobs_mb"] > 0
    finally:
        db.rollback()
        db.execute(text("DELETE FROM scraped_jobs"))
        db.commit()
        db.close()
        engine.dispose()
