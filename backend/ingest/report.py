"""
The ingest report: ``python -m backend.ingest.report`` (``--json`` for the
raw numbers).

  - the last run (scrape_runs): when, how long, boards, jobs seen, new and
    closed, what the storage caps held back, its notes
  - yield per source: the last run's boards and jobs per platform, and per
    source (each ATS platform, LinkedIn, Indeed, the GitHub lists) the live
    jobs and the new ones in the last 24 hours and 7 days
  - live jobs by career family, experience level, country and the top 15
    regions
  - storage: the database and scraped_jobs sizes

Live means what the feed shows: not hidden (listing_freshness.
HIDDEN_LISTING_STATUSES), not a duplicate, with a company. Every number is
an aggregate computed in SQL; no job row leaves the database.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from collections import Counter

from sqlalchemy import func, text

TOP_REGIONS = 15


def _live(query):
    from backend.db.models import ScrapedJob
    from backend.services.listing_freshness import HIDDEN_LISTING_STATUSES

    return query.filter(
        ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES),
        ScrapedJob.duplicate_of.is_(None),
        func.trim(func.coalesce(ScrapedJob.company, "")) != "",
    )


def _source(source_platform: str | None, board_key: str | None) -> str:
    """A row's source: the ATS platform for crawled rows ("ats:workday"),
    else its source_platform (linkedin, indeed, github)."""
    source = source_platform or "unknown"
    if source == "ats":
        platform = (board_key or "").split(":", 1)[0]
        return f"ats:{platform or 'unknown'}"
    return source


def _last_run(db) -> dict | None:
    from backend.db.models import ScrapeRun

    run = db.query(ScrapeRun).order_by(ScrapeRun.id.desc()).first()
    if run is None:
        return None
    stats = run.stats or {}
    seconds = None
    if run.finished_at and run.started_at:
        seconds = round((run.finished_at - run.started_at).total_seconds())
    return {
        "id": run.id, "trigger": run.trigger, "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "seconds": seconds, "tasks_total": run.tasks_total, "tasks_ok": run.tasks_ok,
        "tasks_failed": run.tasks_failed, "jobs_seen": run.jobs_seen, "jobs_new": run.jobs_new,
        "jobs_closed": run.jobs_closed, "boards_deferred": stats.get("boards_deferred"),
        "caps": stats.get("caps") or {}, "platforms": stats.get("platforms") or {},
        "notes": run.notes or "",
    }


def _per_source(db, now: datetime.datetime) -> dict:
    from backend.db.models import ScrapedJob

    out: dict[str, Counter] = {}

    def add(rows, key):
        for source_platform, board_key, count in rows:
            out.setdefault(_source(source_platform, board_key), Counter())[key] += int(count)

    grouped = (ScrapedJob.source_platform, ScrapedJob.board_key)
    add(_live(db.query(*grouped, func.count(ScrapedJob.id))).group_by(*grouped).all(), "live")
    for key, days in (("new_24h", 1), ("new_7d", 7)):
        add(db.query(*grouped, func.count(ScrapedJob.id))
            .filter(ScrapedJob.first_seen_at >= now - datetime.timedelta(days=days))
            .group_by(*grouped).all(), key)
    return {source: dict(counts) for source, counts in
            sorted(out.items(), key=lambda item: -item[1]["live"])}


def _live_by(db, *columns) -> list:
    from backend.db.models import ScrapedJob

    rows = (_live(db.query(*columns, func.count(ScrapedJob.id)))
            .group_by(*columns).order_by(func.count(ScrapedJob.id).desc()).all())
    return [[*(value if value not in (None, "") else "(none)" for value in row[:-1]), int(row[-1])]
            for row in rows]


def _storage(db) -> dict:
    if db.get_bind().dialect.name != "postgresql":
        return {"database_mb": None, "scraped_jobs_mb": None}
    database = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
    jobs = db.execute(text("SELECT pg_total_relation_size('scraped_jobs')")).scalar()
    return {"database_mb": round(int(database) / 1048576, 1),
            "scraped_jobs_mb": round(int(jobs) / 1048576, 1)}


def build_report(db, now: datetime.datetime | None = None) -> dict:
    """Every number the report prints."""
    from backend.db.models import ScrapedJob

    now = now or datetime.datetime.utcnow()
    live_total = _live(db.query(func.count(ScrapedJob.id))).scalar() or 0
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "last_run": _last_run(db),
        "sources": _per_source(db, now),
        "live_jobs": int(live_total),
        "by_family": _live_by(db, ScrapedJob.career_family),
        "by_level": _live_by(db, ScrapedJob.experience_level),
        "by_country": _live_by(db, ScrapedJob.country),
        "top_regions": _live_by(db, ScrapedJob.country, ScrapedJob.region)[:TOP_REGIONS],
        "storage": _storage(db),
    }


def format_report(report: dict) -> str:
    """The report as text for a terminal or a workflow log."""
    lines = [f"Tailrd ingest report ({report['generated_at']} UTC)", ""]
    run = report["last_run"]
    if run is None:
        lines.append("No ingest run yet.")
    else:
        lines.append(f"Last run #{run['id']} ({run['trigger']}, {run['status']}) "
                     f"started {run['started_at']}, {run['seconds']} s")
        lines.append(f"  boards: {run['tasks_ok']} ok, {run['tasks_failed']} failed of "
                     f"{run['tasks_total']}, {run['boards_deferred'] or 0} deferred")
        lines.append(f"  jobs: {run['jobs_seen']} seen, {run['jobs_new']} new, "
                     f"{run['jobs_closed']} closed")
        held = {k: v for k, v in run["caps"].items() if v}
        if held:
            lines.append("  held back by the caps: "
                         + ", ".join(f"{k} {v}" for k, v in sorted(held.items())))
        for platform, stats in sorted(run["platforms"].items()):
            lines.append(f"  {platform}: {stats.get('ok', 0)} boards ok, "
                         f"{stats.get('failed', 0)} failed, {stats.get('found', 0)} listed, "
                         f"{stats.get('inserted', 0)} new, {stats.get('removed', 0)} closed")
        for note in run["notes"].splitlines()[:20]:
            lines.append(f"  note: {note}")
    lines += ["", "Yield by source (live / new 24 h / new 7 d):"]
    for source, counts in report["sources"].items():
        lines.append(f"  {source:<26} {counts.get('live', 0):>7} {counts.get('new_24h', 0):>7} "
                     f"{counts.get('new_7d', 0):>7}")
    lines += ["", f"Live jobs: {report['live_jobs']}"]
    for title, key in (("By career family", "by_family"), ("By experience level", "by_level"),
                       ("By country", "by_country"), ("Top regions", "top_regions")):
        lines.append(f"{title}:")
        for row in report[key]:
            label = " ".join(str(part) for part in row[:-1])
            lines.append(f"  {label:<36} {row[-1]:>7}")
    storage = report["storage"]
    lines += ["", f"Storage: database {storage['database_mb']} MB, "
                  f"scraped_jobs {storage['scraped_jobs_mb']} MB"]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.ingest.report",
                                     description="Print the scraper v2 ingest report.")
    parser.add_argument("--json", action="store_true", help="print the numbers as JSON")
    args = parser.parse_args(argv)
    from backend.db.database import SessionLocal

    db = SessionLocal()
    try:
        report = build_report(db)
    finally:
        db.close()
    print(json.dumps(report, indent=2, default=str) if args.json else format_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
