"""
Migration: storage for the deterministic fit engine (services/fit_engine.py, Task 9.1).

1. ``job_match_scores`` gains seven nullable columns: raw_score FLOAT, band VARCHAR(20),
   outcome VARCHAR(30), confidence FLOAT, engine_version VARCHAR(20), config_hash VARCHAR(20),
   posting_key VARCHAR(40) (the posting a score was computed from; fix round 1, I-1).
2. ``job_match_scores.score`` becomes nullable: an engine result that shows no number
   (GATE_FAIL, NEEDS_REVIEW, a low-confidence band) stores NULL, never a numeric sentinel that
   could leak into a display, an average or a threshold (DECISIONS D-86).
3. ``match_traces``, ``match_calibration`` and ``match_fingerprints`` (Task 9.2, D-107) exist. create_all builds them on startup before
   any migration runs (on Postgres under its advisory lock); on SQLite they are also created
   here if missing, so a standalone run is complete. Postgres leaves table creation to
   create_all: a second, unlocked CREATE path would reintroduce the cold-start race it guards.

Additive and idempotent: every step checks first, no row is changed or lost. On Postgres it is
catalog-only DDL (nullable ADD COLUMN, DROP NOT NULL) under the same first-deploy guard as the
other cold-start migrations: a capped lock wait, an advisory lock across concurrent cold starts,
IF NOT EXISTS. SQLite (tests and CI only) cannot drop a NOT NULL constraint in place, so the
table is rebuilt there with every row copied, which is the documented SQLite procedure, inside
an explicit BEGIN ... COMMIT on the raw connection (pysqlite's legacy mode would otherwise
autocommit the DDL, so a failed copy would leave a half-built table): any failure rolls back to
the original table. Nothing references job_match_scores, so no foreign key is affected.
"""

import logging

from sqlalchemy import inspect, text

from backend.db.database import engine as default_engine

logger = logging.getLogger(__name__)

TABLE = "job_match_scores"
COLUMNS = {
    "raw_score": "FLOAT",
    "band": "VARCHAR(20)",
    "outcome": "VARCHAR(30)",
    "confidence": "FLOAT",
    "engine_version": "VARCHAR(20)",
    "config_hash": "VARCHAR(20)",
    "posting_key": "VARCHAR(40)",
}

LOCK_TIMEOUT = "5s"
# Arbitrary, unique to this migration: serializes concurrent cold starts.
_ADVISORY_LOCK_KEY = 7_311_027_530_102
_SQLITE_OLD = "job_match_scores__fit_old"


def _columns(engine) -> dict[str, dict]:
    return {col["name"]: col for col in inspect(engine).get_columns(TABLE)}


def _ensure_tables(engine) -> None:
    from backend.db.models import MatchCalibration, MatchFingerprint, MatchTrace

    for model in (MatchTrace, MatchCalibration, MatchFingerprint):
        model.__table__.create(bind=engine, checkfirst=True)


def _postgres(engine, columns: dict[str, dict]) -> None:
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
        conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY})
        for name, ddl in COLUMNS.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {name} {ddl}"))
        if not columns["score"].get("nullable", True):
            conn.execute(text(f"ALTER TABLE {TABLE} ALTER COLUMN score DROP NOT NULL"))


def _sqlite_rebuild(engine) -> None:
    """Rebuild job_match_scores from the model (score nullable, the new columns present) and copy
    every row, atomically: an explicit BEGIN on the raw DBAPI connection, ROLLBACK on any error."""
    from sqlalchemy.schema import CreateIndex, CreateTable

    from backend.db.models import JobMatchScore

    table = JobMatchScore.__table__
    old_columns = list(_columns(engine))
    indexes = [ix["name"] for ix in inspect(engine).get_indexes(TABLE) if ix.get("name")]
    new_columns = {col.name for col in table.columns}
    shared = ", ".join(name for name in old_columns if name in new_columns)
    statements = [f"ALTER TABLE {TABLE} RENAME TO {_SQLITE_OLD}"]
    statements += [f'DROP INDEX IF EXISTS "{name}"' for name in indexes]  # names are global in SQLite
    statements.append(str(CreateTable(table).compile(dialect=engine.dialect)))
    statements += [str(CreateIndex(index).compile(dialect=engine.dialect)) for index in table.indexes]
    statements.append(f"INSERT INTO {TABLE} ({shared}) SELECT {shared} FROM {_SQLITE_OLD}")
    statements.append(f"DROP TABLE {_SQLITE_OLD}")

    raw = engine.raw_connection()
    try:
        dbapi = raw.driver_connection
        previous = dbapi.isolation_level
        dbapi.isolation_level = None  # manual transactions: nothing autocommits behind our back
        cursor = dbapi.cursor()
        try:
            cursor.execute("BEGIN")
            for statement in statements:
                cursor.execute(statement)
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise
        finally:
            cursor.close()
            dbapi.isolation_level = previous
    finally:
        raw.close()


def _sqlite(engine, columns: dict[str, dict]) -> None:
    if not columns["score"].get("nullable", True):
        _sqlite_rebuild(engine)
        columns = _columns(engine)
    missing = [name for name in COLUMNS if name not in columns]
    if missing:
        with engine.begin() as conn:
            for name in missing:
                conn.execute(text(f"ALTER TABLE {TABLE} ADD COLUMN {name} {COLUMNS[name]}"))


FINGERPRINTS = "match_fingerprints"


def _fingerprint_month(engine) -> None:
    """``match_fingerprints.as_of`` (fix round 1 of Task 9.2): nullable, additive, idempotent."""
    if FINGERPRINTS not in inspect(engine).get_table_names():
        return
    if "as_of" in {col["name"] for col in inspect(engine).get_columns(FINGERPRINTS)}:
        return
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY})
            conn.execute(text(f"ALTER TABLE {FINGERPRINTS} ADD COLUMN IF NOT EXISTS as_of DATE"))
        else:
            conn.execute(text(f"ALTER TABLE {FINGERPRINTS} ADD COLUMN as_of DATE"))
    logger.info("Added %s.as_of", FINGERPRINTS)


def run_migration(engine=None) -> None:
    """Bring job_match_scores, match_traces, match_calibration and match_fingerprints to the
    fit-engine shape."""
    engine = engine or default_engine
    _fingerprint_month(engine)
    if TABLE not in inspect(engine).get_table_names():
        logger.info("Fit-engine migration skipped: %s missing (create_all builds it).", TABLE)
        return
    columns = _columns(engine)
    needs_columns = any(name not in columns for name in COLUMNS)
    needs_nullable = not columns["score"].get("nullable", True)
    if needs_columns or needs_nullable:
        if engine.dialect.name == "postgresql":
            _postgres(engine, columns)
        else:
            _sqlite(engine, columns)
        logger.info("Ensured fit-engine columns on %s", TABLE)
    if engine.dialect.name != "postgresql":
        _ensure_tables(engine)
