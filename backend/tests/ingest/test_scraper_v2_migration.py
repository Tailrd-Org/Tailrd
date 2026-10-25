"""migrations/add_scraper_v2.py: the scraper v2 columns and indexes.

It runs in the lifespan of every cold-starting instance at once, so it must
send no DDL at all once applied, cap its lock waits on Postgres, and never
rewrite scraped_jobs (nullable columns without a default). There's no
Postgres in CI, so the Postgres path is checked against the SQL a recording
engine receives; the SQLite path runs for real.
"""

import contextlib

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError

from backend.db.database import Base
from backend.migrations import add_scraper_v2

NEW_JOB_COLUMNS = [
    ("career_family", "VARCHAR(48)"),
    ("career_family_secondary", "VARCHAR(48)"),
    ("remote_scope", "VARCHAR(16)"),
    ("enrichment_version", "INTEGER"),
    ("fingerprint", "VARCHAR(64)"),
    ("company_id", "INTEGER"),
    ("closed_reason", "VARCHAR(32)"),
    ("consecutive_failures", "INTEGER"),
    ("salary_is_estimated", "BOOLEAN"),
    ("apply_url", "VARCHAR(2048)"),
    ("expires_at", "TIMESTAMP"),
]
NEW_HEALTH_COLUMNS = [("disabled_until", "TIMESTAMP"), ("jobs_7d_avg", "FLOAT")]
NEW_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_scraped_jobs_status_family ON scraped_jobs (listing_status, career_family)",
    "CREATE INDEX IF NOT EXISTS ix_scraped_jobs_status_level ON scraped_jobs (listing_status, experience_level)",
    "CREATE INDEX IF NOT EXISTS ix_scraped_jobs_status_geo ON scraped_jobs (listing_status, country, region)",
    "CREATE INDEX IF NOT EXISTS ix_scraped_jobs_company_id ON scraped_jobs (company_id)",
    "CREATE INDEX IF NOT EXISTS ix_scraped_jobs_status_probed ON scraped_jobs (listing_status, last_probed_at)",
    "CREATE INDEX IF NOT EXISTS ix_scraped_jobs_fingerprint ON scraped_jobs (fingerprint)",
]
# What scraped_jobs and source_health held before this migration, as far as
# the new indexes care.
OLD_JOB_COLUMNS = ["id", "url", "listing_status", "experience_level", "country", "region",
                   "last_probed_at"]
OLD_HEALTH_COLUMNS = ["id", "board_key", "consecutive_failures"]


class _RecordingPostgres:
    """Stands in for a Postgres engine: records every statement, and a BEGIN
    marker per transaction."""

    class dialect:
        name = "postgresql"

    def __init__(self):
        self.sql: list[str] = []
        self.transactions = 0

    @contextlib.contextmanager
    def begin(self):
        self.transactions += 1
        self.sql.append("BEGIN")
        engine = self

        class _Conn:
            def execute(self, statement, params=None):
                engine.sql.append(" ".join(str(statement).split()))

        yield _Conn()


class _Inspector:
    def __init__(self, tables: dict[str, dict[str, list[str]]]):
        self.tables = tables

    def get_table_names(self):
        return list(self.tables)

    def get_columns(self, table):
        return [{"name": name} for name in self.tables[table]["columns"]]

    def get_indexes(self, table):
        return [{"name": name} for name in self.tables[table]["indexes"]]


def _fake_postgres(monkeypatch, tables):
    engine = _RecordingPostgres()
    monkeypatch.setattr(add_scraper_v2, "inspect", lambda _engine: _Inspector(tables))
    return engine


def _guard(sql: list[str], start: int) -> None:
    assert sql[start] == "BEGIN"
    assert sql[start + 1] == "SET LOCAL lock_timeout = '5s'"
    assert sql[start + 2] == "SELECT pg_advisory_xact_lock(:key)"


# --- Postgres -----------------------------------------------------------------------

def test_postgres_ddl_caps_lock_waits_and_tolerates_a_lost_race(monkeypatch):
    engine = _fake_postgres(monkeypatch, {
        "scraped_jobs": {"columns": OLD_JOB_COLUMNS, "indexes": ["ix_scraped_jobs_listing_status"]},
        "source_health": {"columns": OLD_HEALTH_COLUMNS, "indexes": []},
    })

    add_scraper_v2.run_migration(engine)

    # The columns: one short transaction, IF NOT EXISTS so a losing cold
    # start's ALTER is a no-op, nullable without a default so the table is
    # never rewritten.
    _guard(engine.sql, 0)
    alters = engine.sql[3:3 + len(NEW_JOB_COLUMNS) + len(NEW_HEALTH_COLUMNS)]
    assert alters == (
        [f"ALTER TABLE scraped_jobs ADD COLUMN IF NOT EXISTS {name} {ddl}"
         for name, ddl in NEW_JOB_COLUMNS]
        + [f"ALTER TABLE source_health ADD COLUMN IF NOT EXISTS {name} {ddl}"
           for name, ddl in NEW_HEALTH_COLUMNS]
    )
    assert not any("DEFAULT" in sql or "NOT NULL" in sql for sql in alters)
    # Then each index in its own transaction, so the ACCESS EXCLUSIVE lock of
    # the ALTERs is never held through an index build.
    rest = engine.sql[3 + len(alters):]
    assert len(rest) == 4 * len(NEW_INDEXES)
    for i, create in enumerate(NEW_INDEXES):
        _guard(rest, 4 * i)
        assert rest[4 * i + 3] == create
    assert engine.transactions == 1 + len(NEW_INDEXES)


def test_postgres_takes_no_lock_once_applied(monkeypatch):
    # Every cold start after the first: even CREATE INDEX IF NOT EXISTS takes
    # a SHARE lock on scraped_jobs before it finds the index.
    engine = _fake_postgres(monkeypatch, {
        "scraped_jobs": {
            "columns": OLD_JOB_COLUMNS + [name for name, _ in NEW_JOB_COLUMNS],
            "indexes": list(add_scraper_v2.INDEXES),
        },
        "source_health": {
            "columns": OLD_HEALTH_COLUMNS + [name for name, _ in NEW_HEALTH_COLUMNS],
            "indexes": [],
        },
    })

    add_scraper_v2.run_migration(engine)

    assert engine.sql == []
    assert engine.transactions == 0


def test_postgres_repairs_a_missing_index_alone(monkeypatch):
    engine = _fake_postgres(monkeypatch, {
        "scraped_jobs": {
            "columns": OLD_JOB_COLUMNS + [name for name, _ in NEW_JOB_COLUMNS],
            "indexes": [name for name in add_scraper_v2.INDEXES
                        if name != "ix_scraped_jobs_fingerprint"],
        },
        "source_health": {
            "columns": OLD_HEALTH_COLUMNS + [name for name, _ in NEW_HEALTH_COLUMNS],
            "indexes": [],
        },
    })

    add_scraper_v2.run_migration(engine)

    _guard(engine.sql, 0)
    assert engine.sql[3:] == [NEW_INDEXES[-1]]


def test_a_missing_table_is_left_to_create_all(monkeypatch):
    engine = _fake_postgres(monkeypatch, {
        "source_health": {"columns": OLD_HEALTH_COLUMNS, "indexes": []},
    })

    add_scraper_v2.run_migration(engine)

    assert not any("scraped_jobs" in sql for sql in engine.sql)
    assert [sql for sql in engine.sql if sql.startswith("ALTER")] == [
        f"ALTER TABLE source_health ADD COLUMN IF NOT EXISTS {name} {ddl}"
        for name, ddl in NEW_HEALTH_COLUMNS
    ]


# --- SQLite -------------------------------------------------------------------------

def _record(engine) -> list[str]:
    sent: list[str] = []
    event.listen(engine, "before_cursor_execute",
                 lambda _c, _cur, statement, *_a: sent.append(" ".join(statement.split())))
    return sent


def _ddl(sent: list[str]) -> list[str]:
    return [s for s in sent if s.upper().startswith(("ALTER", "CREATE", "SET", "SELECT PG_"))]


def test_sqlite_migration_runs_twice_and_the_second_run_sends_no_ddl(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'v2.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE scraped_jobs (id INTEGER PRIMARY KEY, url VARCHAR, listing_status VARCHAR,"
            " experience_level VARCHAR, country VARCHAR, region VARCHAR, last_probed_at TIMESTAMP)"
        ))
        conn.execute(text("INSERT INTO scraped_jobs (id, url, listing_status) VALUES (1, 'u', 'active')"))
        conn.execute(text(
            "CREATE TABLE source_health (id INTEGER PRIMARY KEY, board_key VARCHAR,"
            " consecutive_failures INTEGER)"
        ))
    sent = _record(engine)

    add_scraper_v2.run_migration(engine)
    first = _ddl(sent)
    sent.clear()
    add_scraper_v2.run_migration(engine)  # second run: no DDL at all

    # SQLite has neither SET, advisory locks nor ADD COLUMN IF NOT EXISTS.
    assert first == (
        [f"ALTER TABLE scraped_jobs ADD COLUMN {name} {ddl}" for name, ddl in NEW_JOB_COLUMNS]
        + [f"ALTER TABLE source_health ADD COLUMN {name} {ddl}" for name, ddl in NEW_HEALTH_COLUMNS]
        + NEW_INDEXES
    )
    assert _ddl(sent) == []
    inspector = inspect(engine)
    assert {name for name, _ in NEW_JOB_COLUMNS} <= {c["name"] for c in inspector.get_columns("scraped_jobs")}
    assert {name for name, _ in NEW_HEALTH_COLUMNS} <= {c["name"] for c in inspector.get_columns("source_health")}
    assert set(add_scraper_v2.INDEXES) <= {i["name"] for i in inspector.get_indexes("scraped_jobs")}
    with engine.connect() as conn:  # the existing row is kept, its new columns NULL
        row = conn.execute(text("SELECT url, career_family, company_id FROM scraped_jobs")).one()
    assert tuple(row) == ("u", None, None)


def test_sqlite_migration_sends_no_ddl_on_tables_create_all_built(tmp_path):
    # The model declares the same columns and index names, so the tables
    # create_all builds on a first deploy take the no-DDL path.
    from backend.db.models import ScrapedJob, SourceHealth

    engine = create_engine(f"sqlite:///{tmp_path / 'built.db'}")
    Base.metadata.create_all(bind=engine, tables=[ScrapedJob.__table__, SourceHealth.__table__])
    sent = _record(engine)

    add_scraper_v2.run_migration(engine)

    assert _ddl(sent) == []


def test_create_all_builds_the_new_tables(tmp_path):
    from backend.db.models import Company, ScrapeRun, ScrapeTask

    engine = create_engine(f"sqlite:///{tmp_path / 'tables.db'}")
    Base.metadata.create_all(bind=engine, tables=[
        Company.__table__, ScrapeRun.__table__, ScrapeTask.__table__,
    ])
    inspector = inspect(engine)

    assert {c["name"] for c in inspector.get_columns("companies")} == {
        "id", "name", "domain", "country", "sector", "ats", "ats_token", "careers_url",
        "config", "enabled", "created_at", "updated_at",
    }
    assert {"column_names": ["ats", "ats_token"], "name": "uq_companies_ats_token"} in [
        {"column_names": uq["column_names"], "name": uq["name"]}
        for uq in inspector.get_unique_constraints("companies")
    ]
    assert {c["name"] for c in inspector.get_columns("scrape_runs")} == {
        "id", "started_at", "finished_at", "trigger", "status", "tasks_total", "tasks_ok",
        "tasks_failed", "jobs_seen", "jobs_new", "jobs_closed", "notes", "stats",
    }
    assert {c["name"] for c in inspector.get_columns("scrape_tasks")} == {
        "id", "run_id", "kind", "payload", "status", "attempts", "lease_until", "last_error",
        "created_at", "finished_at",
    }
    lease = [i for i in inspector.get_indexes("scrape_tasks") if i["name"] == "ix_scrape_tasks_status_lease"]
    assert lease and lease[0]["column_names"] == ["status", "lease_until"]


def test_new_models_round_trip_with_their_defaults(db_session):
    from backend.db.models import Company, ScrapeRun, ScrapeTask, ScrapedJob, SourceHealth

    run = ScrapeRun()
    db_session.add(run)
    db_session.flush()
    db_session.add_all([
        ScrapeTask(run_id=run.id, kind="list", payload={"board": "greenhouse:acme"}),
        Company(name="Acme", ats="greenhouse", ats_token="acme", config={"x": 1}),
        ScrapedJob(title="Engineer", company="Acme", url="https://example.com/1",
                   career_family="software_engineering", fingerprint="f" * 64, company_id=1,
                   salary_is_estimated=True, apply_url="https://acme.example/apply"),
        SourceHealth(board_key="greenhouse:acme", jobs_7d_avg=12.5),
    ])
    db_session.commit()

    run = db_session.query(ScrapeRun).one()
    assert (run.trigger, run.status, run.tasks_total, run.jobs_new) == ("manual", "running", 0, 0)
    assert run.started_at is not None
    task = db_session.query(ScrapeTask).one()
    assert (task.status, task.attempts, task.payload) == ("pending", 0, {"board": "greenhouse:acme"})
    assert db_session.query(Company).one().enabled is True
    job = db_session.query(ScrapedJob).one()
    assert (job.career_family, job.salary_is_estimated, job.enrichment_version) == (
        "software_engineering", True, None)
    assert db_session.query(SourceHealth).one().disabled_until is None


# --- lifespan -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifespan_runs_it_and_a_failure_fails_startup_for_the_next_start_to_retry(monkeypatch):
    """Every ScrapedJob query maps the new columns: an instance whose
    migration failed must not serve, and the next cold start retries."""
    import backend.main as main

    attempts: list[int] = []

    def times_out_once():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise OperationalError("ALTER TABLE ...", {},
                                   Exception("canceling statement due to lock timeout"))

    monkeypatch.setattr(main, "run_scraper_v2_migration", times_out_once)

    with pytest.raises(OperationalError):
        async with main.lifespan(main.app):
            pytest.fail("an instance whose schema migration failed must not serve")

    async with main.lifespan(main.app):
        pass

    assert attempts == [1, 2]
