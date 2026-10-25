"""The v2 runner (python -m backend.ingest.run) end to end on SQLite with a
fake adapter: the run row, the queue, the registry mirror, source health, one
JSON log line per task, retries, the sweeps, alerts and the dry run.

The lock test runs when INGEST_TEST_POSTGRES_URL names a scratch Postgres
(never Neon)."""

import asyncio
import datetime
import json
import os
import types

import pytest
from sqlalchemy.orm import sessionmaker

from backend.data import company_registry
from backend.db.models import Company, ScrapedJob, ScrapeRun, ScrapeTask, SourceHealth
from backend.ingest import adapters, run as runner
from backend.ingest.core import pipeline, queue
from backend.services import listing_freshness
from backend.services.ats_scraper import ATSJob

POSTGRES_URL = os.environ.get("INGEST_TEST_POSTGRES_URL", "")


def _jobs(slug, count, start=0):
    return [ATSJob(title=f"Engineer {n}", company=slug.title(), location="Toronto, ON",
                   url=f"https://jobs.fakeats.test/{slug}/{n}", description=f"Role {n}.",
                   external_id=f"fakeats:{slug}:{n}")
            for n in range(start, start + count)]


@pytest.fixture
def board(monkeypatch):
    """A registry of fake-adapter boards. ``listings[slug]`` is what each
    lists; a slug in ``broken`` raises instead."""
    state = types.SimpleNamespace(listings={"acme": _jobs("acme", 3), "globex": _jobs("globex", 2),
                                            "broken": []},
                                  broken={"broken"}, calls=[])
    module = types.ModuleType("fake_adapter")
    module.PLATFORM, module.REQUIRED_ENV = "fakeats", ()

    async def list_jobs(client, slug, company_name, *, config, deadline=None):
        state.calls.append(slug)
        if slug in state.broken:
            raise RuntimeError(f"HTTP 503 from https://api.fakeats.test/{slug}?token=abc")
        jobs = state.listings[slug]
        return list(jobs), True, len(jobs)

    module.list_jobs = list_jobs
    monkeypatch.setattr(adapters, "_registry", {"fakeats": module})
    entries = [
        {"company_name": "Acme", "ats_platform": "fakeats", "board_slug": "acme",
         "enabled": True, "hq_country": "CA", "sector": "tech",
         "company_logo_url": "https://www.google.com/s2/favicons?domain=acme.example&sz=128"},
        {"company_name": "Globex", "ats_platform": "fakeats", "board_slug": "globex",
         "enabled": True, "hq_country": "US", "config": {"site": "main"}},
        {"company_name": "Broken", "ats_platform": "fakeats", "board_slug": "broken",
         "enabled": True},
        {"company_name": "Parked", "ats_platform": "fakeats", "board_slug": "parked",
         "enabled": False},
    ]
    monkeypatch.setattr(company_registry, "_load_raw", lambda: entries)
    monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})
    state.entries = entries

    # The sweeps' platform checks must not reach the network.
    async def no_checks(client, urls, **kwargs):
        return {}

    monkeypatch.setattr(listing_freshness, "check_listings", no_checks)
    return state


def _args(**kwargs):
    args = runner.parse_args([])
    for key, value in kwargs.items():
        setattr(args, key, value)
    return args


def _run(db, capsys, **kwargs):
    factory = sessionmaker(bind=db.get_bind(), autoflush=False)
    summary = asyncio.run(runner.run(_args(**kwargs), session_factory=factory,
                                     lock_url=None, ensure=False))
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    db.expire_all()
    return summary, lines


def test_a_run_end_to_end(db_session, board, capsys):
    summary, lines = _run(db_session, capsys)

    assert summary["status"] == "completed"
    run = db_session.query(ScrapeRun).one()
    assert (run.status, run.trigger, run.tasks_total, run.tasks_ok, run.tasks_failed) == (
        "completed", "manual", 3, 2, 1)
    assert (run.jobs_seen, run.jobs_new) == (5, 5)
    assert run.stats["platforms"]["fakeats"]["inserted"] == 5

    # One task per board; the broken one used its three attempts.
    tasks = {task.payload["slug"]: task for task in db_session.query(ScrapeTask)}
    assert {slug: (t.status, t.attempts) for slug, t in tasks.items()} == {
        "acme": ("done", 1), "globex": ("done", 1), "broken": ("failed", 3)}
    assert "token=abc" not in tasks["broken"].last_error
    assert board.calls.count("broken") == 3

    # One JSON line per task attempt, between the run's first and last.
    events = [line["event"] for line in lines]
    assert events[0] == "run_started" and events[-1] == "run_finished"
    task_lines = [line for line in lines if line["event"] == "task"]
    assert sorted((line["board"], line["status"]) for line in task_lines) == [
        ("fakeats:acme", "ok"), ("fakeats:broken", "failed"), ("fakeats:broken", "retry"),
        ("fakeats:broken", "retry"), ("fakeats:globex", "ok")]
    acme = next(line for line in task_lines if line["board"] == "fakeats:acme")
    assert (acme["inserted"], acme["new"], acme["complete"]) == (3, 3, True)
    assert "rows_read" in acme

    # The registry is mirrored, and new rows point at their employer.
    companies = {c.ats_token: c for c in db_session.query(Company)}
    assert set(companies) == {"acme", "globex", "broken", "parked"}
    assert (companies["acme"].country, companies["acme"].domain) == ("CA", "acme.example")
    assert companies["globex"].config == {"site": "main"} and not companies["parked"].enabled
    rows = db_session.query(ScrapedJob).all()
    assert {row.company_id for row in rows} == {companies["acme"].id, companies["globex"].id}
    assert {row.external_id for row in rows if row.company == "Acme"} == {
        "fakeats:acme:0", "fakeats:acme:1", "fakeats:acme:2"}

    # Source health: one failure per run, however many attempts.
    health = {h.board_key: h for h in db_session.query(SourceHealth)}
    assert health["fakeats:acme"].jobs_7d_avg == 3
    assert health["fakeats:broken"].consecutive_failures == 1


def test_a_second_run_adds_nothing_and_closes_what_a_board_dropped(db_session, board, capsys):
    _run(db_session, capsys)
    board.listings["acme"] = board.listings["acme"][:1]

    summary, _lines = _run(db_session, capsys, trigger="cron")

    assert summary["totals"]["new_jobs"] == 0
    assert summary["totals"]["removed"] == 2
    latest = db_session.query(ScrapeRun).order_by(ScrapeRun.id.desc()).first()
    assert (latest.trigger, latest.jobs_new, latest.jobs_closed) == ("cron", 0, 2)
    closed = db_session.query(ScrapedJob).filter(ScrapedJob.listing_status == "removed").all()
    assert {row.closed_reason for row in closed} == {"delisted"}


def test_boards_in_cooldown_are_not_crawled(db_session, board, capsys):
    db_session.add(SourceHealth(board_key="fakeats:globex", platform="fakeats", slug="globex",
                                consecutive_failures=0,
                                disabled_until=datetime.datetime.utcnow() + datetime.timedelta(days=1)))
    db_session.commit()

    summary, _lines = _run(db_session, capsys)

    assert "globex" not in board.calls
    assert summary["boards_skipped_cooldown"] == 1 and summary["boards"] == 2


def test_boards_platforms_and_limit_narrow_the_plan(db_session, board, capsys):
    _run(db_session, capsys, boards="acme,fakeats:globex", limit=1)
    assert board.calls == ["acme"] or board.calls == ["globex"]

    board.calls.clear()
    _run(db_session, capsys, platforms="greenhouse")
    assert board.calls == []


def test_a_dead_run_is_marked_abandoned(db_session, board, capsys):
    dead = ScrapeRun(trigger="cron", status="running")
    db_session.add(dead)
    db_session.commit()
    queue.enqueue(db_session, dead.id, "list", [{"platform": "fakeats", "slug": "acme",
                                                 "company": "Acme"}])

    _run(db_session, capsys)

    dead = db_session.get(ScrapeRun, dead.id)
    assert dead.status == "abandoned"
    assert queue.counts(db_session, dead.id) == {queue.FAILED: 1}


def test_yield_warnings_reach_the_webhook(db_session, board, capsys, monkeypatch):
    board.listings["acme"] = _jobs("acme", 20)
    _run(db_session, capsys)
    board.listings["acme"] = _jobs("acme", 2)
    posted = []

    class Answer:
        def raise_for_status(self):
            return None

    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://hooks.example/secret-path")
    monkeypatch.setattr("httpx.post", lambda url, **kw: posted.append((url, kw)) or Answer())

    summary, lines = _run(db_session, capsys)

    assert summary["yield_warnings"] == 1 and summary["alert_posted"] is True
    assert "fakeats:acme" in posted[0][1]["json"]["text"]
    run = db_session.query(ScrapeRun).order_by(ScrapeRun.id.desc()).first()
    assert "fakeats:acme: yield warning" in run.notes
    assert all("secret-path" not in json.dumps(line) for line in lines)


def test_a_dry_run_writes_nothing(db_session, board, capsys):
    db_session.add(ScrapedJob(title="Gone", company="Acme", url="https://jobs.fakeats.test/acme/old",
                              board_key="fakeats:acme", listing_status="active"))
    db_session.commit()

    summary, lines = _run(db_session, capsys, dry_run=True)

    assert summary["status"] == "dry_run" and summary["new"] == 5
    assert db_session.query(ScrapeRun).count() == 0
    assert db_session.query(ScrapeTask).count() == 0
    assert db_session.query(ScrapedJob).count() == 1
    assert db_session.query(Company).count() == 0
    acme = next(line for line in lines if line.get("board") == "fakeats:acme")
    assert (acme["new"], acme["known"], acme["would_remove"]) == (3, 0, 1)


def test_the_registry_mirror_writes_only_changes(db_session, board):
    first = runner.mirror_registry(db_session)
    assert first == {"inserted": 4, "updated": 0, "disabled": 0}
    assert runner.mirror_registry(db_session) == {"inserted": 0, "updated": 0, "disabled": 0}

    board.entries[0]["company_name"] = "Acme Corp"
    del board.entries[1]
    assert runner.mirror_registry(db_session) == {"inserted": 0, "updated": 1, "disabled": 1}


def test_the_runner_runs_the_migrations_main_py_runs():
    modules = runner.boot_migrations()
    assert modules[-1] == "backend.migrations.add_scraper_v2"
    assert "backend.migrations.add_listing_probe_columns" in modules
    assert len(modules) == len(set(modules)) >= 20
    for name in modules:
        assert callable(getattr(__import__(name, fromlist=["run_migration"]), "run_migration"))


def test_the_schema_step_is_idempotent():
    from backend.db.database import engine

    assert runner.ensure_schema(engine) == runner.ensure_schema(engine)


def test_errors_are_published_without_secrets(monkeypatch):
    module = types.ModuleType("keyed_adapter")
    module.PLATFORM, module.REQUIRED_ENV = "keyed", ("KEYED_API_KEY",)
    module.list_jobs = lambda *a, **k: None
    monkeypatch.setattr(adapters, "_registry", {"keyed": module})
    monkeypatch.setenv("KEYED_API_KEY", "s3cr3t-value")
    monkeypatch.setenv("DATABASE_URL_DIRECT", "postgresql://owner:hunter2pw@db.example/neondb")

    message = pipeline.safe_error(RuntimeError(
        "GET https://api.keyed.test/s3cr3t-value/jobs?app_key=zzz failed; "
        "could not connect as owner:hunter2pw"))

    assert "s3cr3t-value" not in message and "zzz" not in message
    assert "hunter2pw" not in message and "[redacted]" in message


def test_without_a_postgres_url_there_is_nothing_to_lock():
    with runner.run_lock(None) as locked:
        assert locked
    with runner.run_lock("sqlite:///./x.db") as locked:
        assert locked


@pytest.mark.skipif(not POSTGRES_URL, reason="set INGEST_TEST_POSTGRES_URL to a scratch Postgres")
def test_two_runs_never_overlap_on_postgres(db_session, board, capsys):
    url = POSTGRES_URL + ("&" if "?" in POSTGRES_URL else "?") + "sslmode=disable"
    with runner.run_lock(url) as first:
        assert first
        with runner.run_lock(url) as second:
            assert not second
        factory = sessionmaker(bind=db_session.get_bind())
        summary = asyncio.run(runner.run(_args(), session_factory=factory, lock_url=url,
                                         ensure=False))
        assert summary == {"status": "skipped"}
        assert json.loads(capsys.readouterr().out.splitlines()[-1])["event"] == "skipped"
    with runner.run_lock(url) as again:
        assert again
