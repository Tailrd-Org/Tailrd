"""The v2 runner (python -m backend.ingest.run) end to end on SQLite with a
fake adapter: the run row, the queue, the registry mirror, source health, one
JSON log line per task, retries, the sweeps, alerts and the dry run.

The lock test runs when INGEST_TEST_POSTGRES_URL names a scratch Postgres
(never Neon)."""

import asyncio
import datetime
import json
import logging
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
from sqlalchemy.exc import ArgumentError
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


# A connection string pasted wrongly into the secret. SQLAlchemy 2.0.32 answers
# one it cannot parse with the text it was given, minus the query string; no
# environment value is that text, and the workflow log is public.
PASSWORD = "s3cr3tPWxyz9"
GOOD_URL = f"postgresql://ingest:{PASSWORD}@ep-cool-name-123456-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
UNPARSABLE = {
    "neon's psql command": f"psql '{GOOD_URL}'",
    "a leading space": f" {GOOD_URL}",
    "double quotes": f'"{GOOD_URL}"',
    "single quotes": f"'{GOOD_URL}'",
}


@pytest.mark.parametrize("form", UNPARSABLE.values(), ids=UNPARSABLE.keys())
def test_safe_error_masks_the_password_of_any_url_even_one_it_was_not_told_about(monkeypatch, form):
    text = f"Could not parse SQLAlchemy URL from string '{form.split('?')[0]}'"

    monkeypatch.setenv("DATABASE_URL", form)
    assert PASSWORD not in pipeline.safe_error(ArgumentError(text))
    # Told about it by the variable, though the variable is not a URL as it stands.
    assert PASSWORD not in pipeline.safe_error(RuntimeError(f"rejected {PASSWORD}"))
    monkeypatch.delenv("DATABASE_URL")
    assert PASSWORD not in pipeline.safe_error(ArgumentError(text))
    assert "ep-cool-name" in pipeline.safe_error(ArgumentError(text)), "only the password goes"


def test_safe_error_does_not_start_the_adapter_registry(monkeypatch):
    # The registry's first use imports every adapter, and through them the
    # database module. When the connection string is what failed, each of those
    # imports fails the same way and the registry logs it with a traceback that
    # carries the message, password included.
    def discovery():
        raise AssertionError("safe_error started the adapter registry")

    monkeypatch.setattr(adapters, "_registry", None)
    monkeypatch.setattr(adapters, "_adapters", discovery)

    assert pipeline.safe_error(RuntimeError("boom")) == "RuntimeError('boom')"


def test_safe_error_still_masks_the_key_of_an_adapter_that_is_loaded(monkeypatch):
    module = types.ModuleType("backend.ingest.adapters.fake_keyed")
    module.REQUIRED_ENV = ("FAKE_KEYED_API_KEY",)
    monkeypatch.setattr(adapters, "_registry", None)
    monkeypatch.setitem(sys.modules, "backend.ingest.adapters.fake_keyed", module)
    monkeypatch.setenv("FAKE_KEYED_API_KEY", "k3y-value-123")

    assert "k3y-value-123" not in pipeline.safe_error(RuntimeError("GET /k3y-value-123/jobs"))


class _UnparsableDatabase(types.ModuleType):
    """backend.db.database as it is when the connection string will not parse:
    importing the engine from it raises what create_engine raises."""

    def __getattr__(self, name):
        if name == "SessionLocal":
            raise ArgumentError("Could not parse SQLAlchemy URL from string "
                                f"'{os.environ['DATABASE_URL'].split('?')[0]}'")
        raise AttributeError(name)


@pytest.mark.parametrize("form", UNPARSABLE.values(), ids=UNPARSABLE.keys())
def test_a_connection_string_sqlalchemy_cannot_parse_is_logged_without_its_password(
        monkeypatch, caplog, capsys, form):
    monkeypatch.setenv("DATABASE_URL", form)
    monkeypatch.delenv("DATABASE_URL_DIRECT", raising=False)
    monkeypatch.setattr(adapters, "_registry", None)
    monkeypatch.setitem(sys.modules, "backend.db.database", _UnparsableDatabase("backend.db.database"))

    with caplog.at_level(logging.WARNING):
        assert runner.main([]) == 1

    captured = capsys.readouterr()
    everything = caplog.text + captured.out + captured.err
    assert "ingest run failed" in everything and "[redacted]" in everything
    assert PASSWORD not in everything


@pytest.mark.parametrize("form", UNPARSABLE.values(), ids=UNPARSABLE.keys())
def test_the_runner_cli_prints_none_of_a_connection_string_it_cannot_parse(form):
    # The real thing, in a fresh interpreter: the real SQLAlchemy and the real
    # backend/db/database.py. CI installs SQLAlchemy 2.0.32, the version that
    # puts the string in the error.
    repo = Path(__file__).resolve().parents[3]
    env = {key: value for key, value in os.environ.items()
           if key not in ("DATABASE_URL", "DATABASE_URL_DIRECT")}
    env.update(DATABASE_URL=form, PYTHONPATH=str(repo))

    result = subprocess.run([sys.executable, "-m", "backend.ingest.run", "--no-schema", "--dry-run"],
                            cwd=repo, env=env, capture_output=True, text=True, timeout=120)

    assert result.returncode == 1, result.stdout + result.stderr
    assert PASSWORD not in result.stdout + result.stderr
    assert "ingest run failed" in result.stderr


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


def test_main_skips_the_schema_step_only_when_asked(monkeypatch):
    """--no-schema is how a database role with no DDL privilege runs the
    runner: several boot migrations send CREATE INDEX IF NOT EXISTS on every
    start, and Postgres wants table ownership for that even when the index
    exists."""
    seen = []

    async def fake_run(args, **kwargs):
        seen.append(kwargs["ensure"])
        return {"status": "completed"}

    monkeypatch.setattr(runner, "run", fake_run)

    assert runner.main(["--no-schema"]) == 0
    assert runner.main([]) == 0
    assert seen == [False, True]
