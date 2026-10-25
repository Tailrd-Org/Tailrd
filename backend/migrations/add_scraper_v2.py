"""
Migration: scraper v2 columns and indexes (docs/scraper-v2/SPEC.md, "Data model").

scraped_jobs gains eleven nullable columns, none with a default: career_family,
career_family_secondary, remote_scope, enrichment_version, fingerprint,
company_id, closed_reason, consecutive_failures, salary_is_estimated,
apply_url, expires_at. On Postgres a nullable ADD COLUMN without a default
only touches the catalog, so scraped_jobs (most of the database) is never
rewritten. source_health gains disabled_until and jobs_7d_avg.

Six indexes on scraped_jobs serve the v2 feed filters (listing_status with
career_family, with experience_level, and with country and region), the
companies join, the liveness worker's least-recently-probed rotation, and twin
lookups by fingerprint. The fingerprint index is not unique: one employer
posts the same title as separate requisitions. The new tables (companies,
scrape_runs, scrape_tasks) come from create_all at boot like every other model.

DDL only, no backfill: NULL means "not computed yet". The same first-deploy
guard as add_listing_probe_columns: inspect first and send no DDL at all once
everything exists (even CREATE INDEX IF NOT EXISTS takes a SHARE lock on
scraped_jobs before it finds the index); otherwise cap lock waits, take turns
with concurrent cold starts on an advisory lock, and use IF NOT EXISTS on
Postgres so a losing instance's DDL is a no-op instead of a duplicate-column
crash. The columns go in one short transaction and each index in its own: an
ADD COLUMN holds an ACCESS EXCLUSIVE lock until commit, which blocks every
read of scraped_jobs, so it must not wait out the index builds, while an index
build blocks only writes, and only while it runs.
"""

import logging

from sqlalchemy import inspect, text

from backend.db.database import engine as default_engine

logger = logging.getLogger(__name__)

COLUMNS: dict[str, dict[str, str]] = {
    "scraped_jobs": {
        "career_family": "VARCHAR(48)",
        "career_family_secondary": "VARCHAR(48)",
        "remote_scope": "VARCHAR(16)",
        "enrichment_version": "INTEGER",
        "fingerprint": "VARCHAR(64)",
        "company_id": "INTEGER",
        "closed_reason": "VARCHAR(32)",
        "consecutive_failures": "INTEGER",
        "salary_is_estimated": "BOOLEAN",
        "apply_url": "VARCHAR(2048)",
        "expires_at": "TIMESTAMP",
    },
    "source_health": {
        "disabled_until": "TIMESTAMP",
        "jobs_7d_avg": "FLOAT",
    },
}

# All on scraped_jobs. The names match the ORM model's, so a table that
# create_all built takes the no-DDL path.
INDEXES: dict[str, tuple[str, ...]] = {
    "ix_scraped_jobs_status_family": ("listing_status", "career_family"),
    "ix_scraped_jobs_status_level": ("listing_status", "experience_level"),
    "ix_scraped_jobs_status_geo": ("listing_status", "country", "region"),
    "ix_scraped_jobs_company_id": ("company_id",),
    "ix_scraped_jobs_status_probed": ("listing_status", "last_probed_at"),
    "ix_scraped_jobs_fingerprint": ("fingerprint",),
}

LOCK_TIMEOUT = "5s"
# Arbitrary, unique to this migration: serializes concurrent cold starts.
_ADVISORY_LOCK_KEY = 7_311_027_530_201


def _missing(engine) -> tuple[dict[str, dict[str, str]], dict[str, tuple[str, ...]]]:
    """The columns (per table) and indexes the database still lacks. A table
    that does not exist yet is left to create_all; an index whose columns the
    table cannot have is skipped rather than failing every startup."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    columns: dict[str, dict[str, str]] = {}
    after: dict[str, set[str]] = {}
    for table, wanted in COLUMNS.items():
        if table not in tables:
            logger.info("Scraper v2 migration: %s missing (create_all builds it).", table)
            continue
        existing = {col["name"] for col in inspector.get_columns(table)}
        missing = {name: ddl for name, ddl in wanted.items() if name not in existing}
        if missing:
            columns[table] = missing
        after[table] = existing | set(missing)

    indexes: dict[str, tuple[str, ...]] = {}
    if "scraped_jobs" in after:
        existing_indexes = {index["name"] for index in inspector.get_indexes("scraped_jobs")}
        for name, index_columns in INDEXES.items():
            if name in existing_indexes:
                continue
            absent = [col for col in index_columns if col not in after["scraped_jobs"]]
            if absent:
                logger.warning("Scraper v2 migration: %s skipped, scraped_jobs has no %s.",
                               name, ", ".join(absent))
                continue
            indexes[name] = index_columns
    return columns, indexes


def _run(engine, statements: list[str]) -> None:
    """One transaction under the first-deploy guard (Postgres only)."""
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ADVISORY_LOCK_KEY})
        for statement in statements:
            conn.execute(text(statement))


def run_migration(engine=None) -> None:
    """Add the scraper v2 columns (scraped_jobs, source_health) and the v2
    indexes on scraped_jobs if missing."""
    engine = engine or default_engine
    columns, indexes = _missing(engine)
    if not columns and not indexes:
        return

    # SQLite (tests, local dev) has no ADD COLUMN IF NOT EXISTS.
    if_not_exists = "IF NOT EXISTS " if engine.dialect.name == "postgresql" else ""
    alters = [
        f"ALTER TABLE {table} ADD COLUMN {if_not_exists}{name} {ddl}"
        for table, missing in columns.items()
        for name, ddl in missing.items()
    ]
    if alters:
        _run(engine, alters)
        logger.info("Ensured scraper v2 columns: %s", ", ".join(
            f"{table}.{name}" for table, missing in columns.items() for name in missing
        ))
    for name, index_columns in indexes.items():
        _run(engine, [
            f"CREATE INDEX IF NOT EXISTS {name} ON scraped_jobs ({', '.join(index_columns)})"
        ])
        logger.info("Ensured index %s", name)
