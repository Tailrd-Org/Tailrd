"""
Reclaim storage from postings that closed long ago:
``python -m backend.scripts.reclaim_dead_descriptions [--apply]``.

Neon's free plan caps the database at 512 MB, and the descriptions of
removed and expired postings are its largest cost (about 48 MB on rows dead
over 30 days, 2026-10-25). A row that has been removed or expired for more
than ``--days`` (30), and that no user table references (application
records, saved jobs, tailored résumés, match alerts, connection requests,
pending questions), loses its ``description``, ``description_sections`` and
``change_log``. The row itself stays: its url, external_id, title, company,
fingerprint, status and timestamps are what stop a slow aggregator from
re-ingesting the posting as new.

A dry run by default: it counts the rows and the bytes they hold. ``--apply``
clears them in batches of ``--batch`` (2000) rows, one transaction each, then
runs a plain VACUUM on scraped_jobs (never VACUUM FULL, which rewrites the
table and needs twice its size; ``--no-vacuum`` skips it). A plain VACUUM
makes the space reusable by new rows without shrinking the files, so the
sizes printed after stay about where they were and stop growing. When it
closed is listing_status_changed_at, else the row's last sighting.
"""

from __future__ import annotations

import argparse
import datetime
import sys

from sqlalchemy import Text, and_, cast, exists, func, null, or_, text

DEFAULT_DAYS = 30
DEFAULT_BATCH = 2000
CLOSED = ("removed", "expired")


def _referencing_models():
    from backend.db.models import (
        ApplicationRecord, ConnectionRequest, JobMatchNotification, PendingQuestion,
        TailoredResume, UserSavedJob,
    )

    return (ApplicationRecord, UserSavedJob, TailoredResume, JobMatchNotification,
            ConnectionRequest, PendingQuestion)


def _holds(column, json: bool = False):
    """The column still holds something worth clearing. A JSON column the
    app once set to None may hold JSON null rather than SQL NULL."""
    if json:
        return and_(column.isnot(None), cast(column, Text) != "null")
    return and_(column.isnot(None), column != "")


def candidate_filter(cutoff: datetime.datetime) -> tuple:
    """Rows to strip: closed before ``cutoff``, holding text, unreferenced."""
    from backend.db.models import ScrapedJob as SJ

    closed_at = func.coalesce(SJ.listing_status_changed_at, SJ.last_seen_at,
                              SJ.first_seen_at, SJ.scraped_at)
    conditions = [
        SJ.listing_status.in_(CLOSED),
        closed_at < cutoff,
        or_(_holds(SJ.description), _holds(SJ.description_sections, json=True),
            _holds(SJ.change_log, json=True)),
    ]
    for model in _referencing_models():
        conditions.append(~exists().where(model.job_id == SJ.id))
    return tuple(conditions)


def sizes(db) -> dict:
    """The database and scraped_jobs sizes in MB (Postgres; None elsewhere)."""
    if db.get_bind().dialect.name != "postgresql":
        return {"database_mb": None, "scraped_jobs_mb": None}
    database = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
    jobs = db.execute(text("SELECT pg_total_relation_size('scraped_jobs')")).scalar()
    return {"database_mb": round(int(database) / 1048576, 1),
            "scraped_jobs_mb": round(int(jobs) / 1048576, 1)}


def measure(db, cutoff: datetime.datetime) -> dict:
    """How many rows would be stripped and the bytes their three columns
    hold (pg_column_size on Postgres, text length elsewhere), in one
    aggregate query."""
    from backend.db.models import ScrapedJob as SJ

    if db.get_bind().dialect.name == "postgresql":
        size = func.pg_column_size
    else:
        def size(column):
            return func.length(cast(column, Text))
    held = (func.coalesce(size(SJ.description), 0)
            + func.coalesce(size(SJ.description_sections), 0)
            + func.coalesce(size(SJ.change_log), 0))
    rows, total = db.query(func.count(SJ.id), func.coalesce(func.sum(held), 0)).filter(
        *candidate_filter(cutoff)).one()
    return {"rows": int(rows), "bytes": int(total), "mb": round(int(total) / 1048576, 1)}


def reclaim(db, *, apply: bool = False, days: int = DEFAULT_DAYS, batch: int = DEFAULT_BATCH,
            now: datetime.datetime | None = None, progress=None) -> dict:
    """Count (and with ``apply``, clear) the dead rows' text, in batches of
    ``batch`` ids taken in id order. Returns counts."""
    from backend.db.models import ScrapedJob as SJ

    now = now or datetime.datetime.utcnow()
    cutoff = now - datetime.timedelta(days=days)
    stats = {"cutoff": cutoff.isoformat(timespec="seconds"), **measure(db, cutoff),
             "applied": apply, "cleared": 0, "batches": 0}
    db.rollback()
    if not apply:
        return stats
    last_id = 0
    while True:
        ids = [row_id for (row_id,) in (
            db.query(SJ.id)
            .filter(SJ.id > last_id, *candidate_filter(cutoff))
            .order_by(SJ.id)
            .limit(batch)
        )]
        if not ids:
            break
        # The filter again: a posting someone saved since the select keeps
        # its text.
        cleared = (
            db.query(SJ)
            .filter(SJ.id.in_(ids), *candidate_filter(cutoff))
            .update({"description": None, "description_sections": null(), "change_log": null()},
                    synchronize_session=False)
        )
        db.commit()
        last_id = ids[-1]
        stats["cleared"] += cleared
        stats["batches"] += 1
        if progress:
            progress(f"batch {stats['batches']}: {cleared} rows cleared (up to id {last_id})")
    return stats


def vacuum(engine) -> bool:
    """A plain VACUUM (ANALYZE) of scraped_jobs, outside any transaction, as
    VACUUM requires. Postgres only; never VACUUM FULL."""
    if engine.dialect.name != "postgresql":
        return False
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql("VACUUM (ANALYZE) scraped_jobs")
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.scripts.reclaim_dead_descriptions",
        description="Strip the text of postings closed long ago (dry run by default).")
    parser.add_argument("--apply", action="store_true", help="clear the rows (default: count)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="closed for more than this many days")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="rows per transaction")
    parser.add_argument("--no-vacuum", action="store_true",
                        help="skip the plain VACUUM after --apply")
    args = parser.parse_args(argv)

    from backend.db.database import SessionLocal

    db = SessionLocal()
    try:
        before = sizes(db)
        print(f"before: database {before['database_mb']} MB, "
              f"scraped_jobs {before['scraped_jobs_mb']} MB")
        stats = reclaim(db, apply=args.apply, days=args.days, batch=max(1, args.batch),
                        progress=print)
        verb = "cleared" if args.apply else "would clear"
        print(f"{stats['rows']} rows closed before {stats['cutoff']} and unreferenced hold "
              f"{stats['mb']} MB of text; {verb} "
              f"{stats['cleared'] if args.apply else stats['rows']} rows")
        if args.apply:
            if not args.no_vacuum and vacuum(db.get_bind()):
                print("VACUUM (ANALYZE) scraped_jobs done")
            after = sizes(db)
            print(f"after: database {after['database_mb']} MB, "
                  f"scraped_jobs {after['scraped_jobs_mb']} MB")
        else:
            print("dry run: nothing changed (add --apply)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
