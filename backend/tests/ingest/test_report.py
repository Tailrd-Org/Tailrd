"""The ingest report (python -m backend.ingest.report): the last run, yield
per source, live jobs by family, level, country and region."""

import datetime
import json

from sqlalchemy.orm import sessionmaker

from backend.db import database
from backend.db.models import ScrapedJob, ScrapeRun
from backend.ingest import report

NOW = datetime.datetime(2026, 10, 25, 12, 0, 0)


def _row(db, n, **kwargs):
    fields = dict(title=f"Job {n}", company="Acme", url=f"https://x/{n}", source_platform="ats",
                  board_key="workday:acme", listing_status="active", country="CA", region="ON",
                  career_family="engineering", experience_level="entry",
                  first_seen_at=NOW - datetime.timedelta(days=30))
    fields.update(kwargs)
    db.add(ScrapedJob(**fields))


def _seed(db):
    _row(db, 1, first_seen_at=NOW - datetime.timedelta(hours=2))
    _row(db, 2, region="QC", career_family="legal", experience_level="senior")
    _row(db, 3, country="US", region="TX", board_key="greenhouse:globex",
         first_seen_at=NOW - datetime.timedelta(days=3))
    _row(db, 4, source_platform="linkedin", board_key="", career_family=None)
    # Not live: closed, a duplicate, no company.
    _row(db, 5, listing_status="removed")
    _row(db, 6, duplicate_of=1)
    _row(db, 7, company=" ")
    db.add(ScrapeRun(trigger="cron", status="completed", started_at=NOW,
                     finished_at=NOW + datetime.timedelta(minutes=42), tasks_total=10,
                     tasks_ok=9, tasks_failed=1, jobs_seen=500, jobs_new=40, jobs_closed=12,
                     notes="workday:acme: yield warning: 2 jobs listed against a 7-day average of 20.0",
                     stats={"caps": {"company_cap": 7, "run_cap": 0}, "boards_deferred": 0,
                            "platforms": {"workday": {"ok": 9, "failed": 1, "found": 500,
                                                      "inserted": 40, "removed": 12}}}))
    db.commit()


def test_the_report_counts_only_live_jobs(db_session):
    _seed(db_session)

    built = report.build_report(db_session, now=NOW)

    assert built["live_jobs"] == 4
    assert built["by_country"] == [["CA", 3], ["US", 1]]
    assert dict((family, n) for family, n in built["by_family"]) == {
        "engineering": 2, "legal": 1, "(none)": 1}
    assert ["CA", "ON", 2] in built["top_regions"]
    assert built["sources"]["ats:workday"] == {"live": 2, "new_24h": 1, "new_7d": 1}
    assert built["sources"]["ats:greenhouse"]["new_7d"] == 1
    assert built["sources"]["linkedin"]["live"] == 1
    run = built["last_run"]
    assert (run["id"], run["seconds"], run["jobs_new"]) == (1, 42 * 60, 40)
    assert built["storage"] == {"database_mb": None, "scraped_jobs_mb": None}


def test_the_text_report_names_every_section(db_session):
    _seed(db_session)
    text = report.format_report(report.build_report(db_session, now=NOW))

    for expected in ("Last run #1 (cron, completed)", "held back by the caps: company_cap 7",
                     "workday: 9 boards ok", "note: workday:acme: yield warning",
                     "Yield by source", "ats:workday", "Live jobs: 4", "By career family:",
                     "By experience level:", "By country:", "Top regions:", "Storage:"):
        assert expected in text, expected


def test_an_empty_database_reports_no_run(db_session):
    built = report.build_report(db_session, now=NOW)
    assert built["last_run"] is None and built["live_jobs"] == 0
    assert "No ingest run yet." in report.format_report(built)


def test_the_command_prints_json(db_session, capsys, monkeypatch):
    _seed(db_session)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=db_session.get_bind()))

    assert report.main(["--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["live_jobs"] == 4 and printed["last_run"]["trigger"] == "cron"
