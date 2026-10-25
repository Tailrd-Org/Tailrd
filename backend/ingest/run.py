"""
The v2 crawler: ``python -m backend.ingest.run``.

One run, start to end:

  1. a session advisory lock, on DATABASE_URL_DIRECT when set (the pooled
     host's PgBouncer does not keep session locks), so two runs never overlap
  2. the schema: create_all and the boot migrations backend/main.py runs
     (both idempotent, and silent once applied)
  3. housekeeping: finished tasks older than a week deleted, and a run a crash
     left ``running`` marked abandoned
  4. one scrape_runs row, and the registry (ats_companies.json) mirrored into
     companies
  5. one ``list`` task per enabled board not in breaker cooldown
  6. the crawl: ``--concurrency`` boards at once, at most ``--per-host`` on
     one API host; each snapshot goes through pipeline.ingest_snapshot (the
     write path cron-ats shares), source_health is kept per board, and a
     failed board is retried up to the queue's three attempts
  7. the lifecycle sweeps cron-freshness runs
  8. the summary, in scrape_runs and as the last log line; yield warnings
     also go to ALERT_WEBHOOK_URL when it is set

Logs are JSON lines on stdout, one per task, never one per job. They are
public (GitHub Actions): no connection string, key or query string is ever
written (pipeline.safe_error).

``--dry-run`` crawls the planned boards and reports what each would change,
writing nothing but the schema step.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import datetime
import importlib
import json
import logging
import os
import ssl
import sys
import time
from contextlib import aclosing, contextmanager
from typing import Optional

from sqlalchemy import event, text

logger = logging.getLogger("backend.ingest.run")

# Session advisory lock key for the run (the migrations hold ...000 to ...201).
RUN_LOCK_KEY = 7_311_027_530_300
# A claimed list task is leased this long: longer than any one board's crawl.
LIST_LEASE_SECONDS = 3600
# Detail requests (descriptions a board's list payload lacks) per board per
# run. The first runs over big Workday and SmartRecruiters boards would
# otherwise spend the whole budget on a few employers; the rest come on the
# next runs.
DETAIL_PER_BOARD = 200
# Matches main.create_tables, so the runner and a cold-starting app instance
# take turns creating tables.
_CREATE_TABLES_LOCK_KEY = 7_311_027_530_000
_MAIN_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
_IN_CHUNK = 400


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def log_event(event_name: str, **fields) -> None:
    """One JSON log line on stdout."""
    print(json.dumps({"event": event_name, **fields}, default=str, separators=(",", ":")),
          flush=True)


# ─── Database plumbing ───────────────────────────────────────────────────────

def _engine_for(url: str):
    """An engine for ``url`` built the way backend/db/database.py builds the
    app's: pg8000 with TLS (``sslmode=disable`` in the URL turns TLS off, for
    a local Postgres), the query string dropped."""
    from sqlalchemy import create_engine
    from sqlalchemy.pool import NullPool

    if url.startswith("sqlite"):
        return create_engine(url)
    base, _, query = url.partition("?")
    for scheme in ("postgres://", "postgresql://"):
        if base.startswith(scheme):
            base = "postgresql+pg8000://" + base[len(scheme):]
    connect_args = {} if "sslmode=disable" in query else {"ssl_context": ssl.create_default_context()}
    return create_engine(base, connect_args=connect_args, poolclass=NullPool)


@contextmanager
def run_lock(url: Optional[str]):
    """Hold the run's session advisory lock for the block. Yields False when
    another run holds it. Without a Postgres URL (SQLite) there is nothing
    to lock and it yields True. The lock's connection is idle between its
    two statements, never inside a transaction, so no idle-in-transaction
    timeout can end it early."""
    if not url or not url.startswith(("postgres://", "postgresql")):
        yield True
        return
    engine = _engine_for(url)
    conn = engine.connect()
    try:
        got = bool(conn.execute(text("SELECT pg_try_advisory_lock(:key)"),
                                {"key": RUN_LOCK_KEY}).scalar())
        conn.commit()
        try:
            yield got
        finally:
            if got:
                try:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": RUN_LOCK_KEY})
                    conn.commit()
                except Exception:  # the connection is gone, and its lock with it
                    logger.warning("run lock: unlock failed; the closed connection released it")
    finally:
        conn.close()
        engine.dispose()


def boot_migrations(main_py: str = _MAIN_PY) -> list[str]:
    """The migration modules backend/main.py's lifespan runs, in its order.
    Read from its source, so the runner never imports the web app (and the
    ingest workflow never installs it), and a migration added there is
    run here too."""
    with open(main_py, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    local_names: dict[str, str] = {}
    lifespan = None
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("backend.migrations."):
            for alias in node.names:
                if alias.name == "run_migration":
                    local_names[alias.asname or alias.name] = node.module
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "lifespan":
            lifespan = node
    if lifespan is None:
        raise RuntimeError("backend/main.py has no lifespan to read the migrations from")
    order = []
    for statement in lifespan.body:
        call = statement.value if isinstance(statement, ast.Expr) else None
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id in local_names):
            order.append(local_names[call.func.id])
    return order


def ensure_schema(engine) -> list[str]:
    """create_all (as main.create_tables runs it: on Postgres only when a
    table is missing, under its advisory lock), then every boot migration.
    Returns the migrations run."""
    from sqlalchemy import inspect

    from backend.db import models  # noqa: F401  (every table on Base.metadata)
    from backend.db.database import Base

    if engine.dialect.name != "postgresql":
        Base.metadata.create_all(bind=engine)
    elif set(Base.metadata.tables) - set(inspect(engine).get_table_names()):
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                         {"key": _CREATE_TABLES_LOCK_KEY})
            Base.metadata.create_all(bind=conn)
    modules = boot_migrations()
    for name in modules:
        importlib.import_module(name).run_migration()
    return modules


class _RowCounter:
    """Rows the database sent back to this process (SELECT results), the
    measure of Neon transfer the task log lines carry. Postgres only: the
    SQLite driver reports no row count for a SELECT."""

    def __init__(self, engine):
        self.rows = 0
        self.engine = engine
        event.listen(engine, "after_cursor_execute", self._count)

    def _count(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip()[:6].upper() == "SELECT" and (cursor.rowcount or 0) > 0:
            self.rows += cursor.rowcount

    def close(self):
        event.remove(self.engine, "after_cursor_execute", self._count)


# ─── The registry ────────────────────────────────────────────────────────────

def mirror_registry(db) -> dict:
    """Mirror backend/data/ats_companies.json into companies, one row per
    (ats_platform, board_slug), the first entry when one is listed twice:
    name, domain (from the curated logo URL), country (hq_country, else the
    board's one country), sector, careers_url, config and enabled. Only
    changed rows are written; a board no longer in the registry is disabled,
    never deleted (scraped_jobs.company_id points at it). Commits."""
    from backend.data import company_registry
    from backend.db.models import Company
    from backend.services.logo_resolver import domain_from_logo_url

    wanted: dict[tuple[str, str], dict] = {}
    for entry in company_registry._load_raw():
        try:
            platform = (entry.get("ats_platform") or "").strip().lower()
            slug = (entry.get("board_slug") or "").strip()
            name = (entry.get("company_name") or "").strip()
        except AttributeError:
            continue
        if not (platform and slug and name) or (platform, slug) in wanted:
            continue
        config = entry.get("config")
        country = (entry.get("hq_country") or entry.get("country") or "").strip().upper()
        wanted[(platform, slug)] = {
            "name": name,
            "domain": domain_from_logo_url(entry.get("company_logo_url") or "") or None,
            "country": country or None,
            "sector": (entry.get("sector") or "").strip() or None,
            "careers_url": (entry.get("careers_url") or "").strip() or None,
            "config": config if isinstance(config, dict) else None,
            "enabled": bool(entry.get("enabled", True)),
        }

    stats = {"inserted": 0, "updated": 0, "disabled": 0}
    existing = {(row.ats, row.ats_token): row for row in db.query(Company).all()}
    for key, values in wanted.items():
        row = existing.get(key)
        if row is None:
            db.add(Company(ats=key[0], ats_token=key[1], **values))
            stats["inserted"] += 1
        elif any(getattr(row, field) != value for field, value in values.items()):
            for field, value in values.items():
                setattr(row, field, value)
            stats["updated"] += 1
    for key, row in existing.items():
        if key not in wanted and row.enabled:
            row.enabled = False
            stats["disabled"] += 1
    db.commit()
    return stats


def plan_boards(db, args, now: datetime.datetime) -> tuple[list, int, dict]:
    """The boards this run crawls: enabled registry boards the scraper can
    fetch (company_registry.load_companies), narrowed by ``--platforms`` and
    ``--boards``, minus those in breaker cooldown, least recently crawled
    first, at most ``--limit``. Returns (boards, skipped for cooldown,
    health rows)."""
    from backend.data import company_registry
    from backend.ingest.core import pipeline
    from backend.services import source_health

    boards = company_registry.load_companies()
    if args.platforms:
        wanted = {p.strip().lower() for p in args.platforms.split(",") if p.strip()}
        boards = [board for board in boards if board[0] in wanted]
    if args.boards:
        wanted = {b.strip() for b in args.boards.split(",") if b.strip()}
        boards = [board for board in boards
                  if f"{board[0]}:{board[1]}" in wanted or board[1] in wanted]
    health = source_health.get_health_map(db, [f"{p}:{s}" for p, s, _ in boards])
    runnable = [board for board in boards
                if not pipeline.in_cooldown(health.get(f"{board[0]}:{board[1]}"), now)]
    skipped = len(boards) - len(runnable)
    runnable.sort(key=lambda board: pipeline.crawled_at(health.get(f"{board[0]}:{board[1]}")))
    if args.limit:
        runnable = runnable[:args.limit]
    return runnable, skipped, health


# ─── The crawl ───────────────────────────────────────────────────────────────

def _platform_stats(stats: dict, platform: str) -> dict:
    return stats.setdefault(platform, {"boards": 0, "ok": 0, "failed": 0, "found": 0,
                                       "inserted": 0, "removed": 0})


async def _crawl(db, client, run_id: int, args, ctx, scraper, counter) -> dict:
    """Claim the run's list tasks and crawl them, round after round (a
    failed board goes back to the queue and is retried after the others),
    until none is left or the budget is spent. Returns the crawl's counts."""
    from backend.ingest.core import pipeline, queue

    out = {"ok": 0, "failed": 0, "deferred": 0, "platforms": {}}
    while not scraper._out_of_time():
        tasks = queue.claim(db, run_id, limit=10_000, lease_seconds=LIST_LEASE_SECONDS)
        if not tasks:
            break
        by_board = {(task.payload["platform"], task.payload["slug"]): task for task in tasks}
        boards = [(task.payload["platform"], task.payload["slug"], task.payload["company"])
                  for task in tasks]
        deferred: list = []
        async with aclosing(pipeline.crawl_boards(scraper, client, boards, args.concurrency,
                                                  per_host=args.per_host,
                                                  deferred=deferred)) as crawl:
            async for (platform, slug, _name), snapshot, error in crawl:
                received = time.monotonic()
                task = by_board[(platform, slug)]
                board_key = f"{platform}:{slug}"
                platform_stats = _platform_stats(out["platforms"], platform)
                rows_before = counter.rows
                line = {"run_id": run_id, "task_id": task.id, "board": board_key,
                        "attempt": task.attempts}
                if error is None:
                    try:
                        line.update(await pipeline.ingest_snapshot(db, client, snapshot, ctx))
                    except Exception as e:  # a write that failed is this board's failure
                        db.rollback()
                        error = e
                if error is None:
                    queue.finish(task, ok=True)
                    out["ok"] += 1
                    platform_stats["boards"] += 1
                    platform_stats["ok"] += 1
                    platform_stats["found"] += line.get("found", 0)
                    platform_stats["inserted"] += line.get("inserted", 0)
                    platform_stats["removed"] += line.get("removed", 0)
                    line["status"] = "ok"
                else:
                    message = pipeline.safe_error(error)
                    status = queue.finish(task, ok=False, error=message)
                    line.update(status="failed" if status == queue.FAILED else "retry",
                                error=message)
                    if status == queue.FAILED:
                        # One failure per run, whatever the attempts: the
                        # breaker counts runs a board failed.
                        pipeline.record_board_failure(db, board_key, platform, slug, message)
                        out["failed"] += 1
                        platform_stats["boards"] += 1
                        platform_stats["failed"] += 1
                line["rows_read"] = counter.rows - rows_before
                line["ingest_seconds"] = round(time.monotonic() - received, 2)
                log_event("task", **line)
        for platform, slug, _name in deferred:
            log_event("task", run_id=run_id, task_id=by_board[(platform, slug)].id,
                      board=f"{platform}:{slug}", status="deferred")
        out["deferred"] += len(deferred)
    return out


# ─── The sweeps ──────────────────────────────────────────────────────────────

async def run_sweeps(db) -> dict:
    """The lifecycle sweeps cron-freshness runs (routers/jobs.py), in its
    order: board-key adoption, stale, aggregator expiry, the two retire
    sweeps, the platform checks in one shared time box, terminal expiry and
    ghost scoring. Kept in step with cron_freshness by hand; the web router
    is not imported here."""
    from backend.services import listing_freshness as lf
    from backend.services.platform_liveness import make_client

    out: dict = {
        "board_keys_adopted": lf.backfill_board_keys(db),
        "marked_stale": lf.sweep_stale(db),
        "expired": lf.sweep_aggregator_expiry(db),
    }
    for name, sweep in (("orphans_off_target", lf.retire_unreconcilable_off_target),
                        ("aggregator_senior_off_target", lf.retire_senior_aggregator_rows)):
        try:
            out[name] = sweep(db)
        except Exception:
            db.rollback()
            logger.exception("ingest sweep %s failed", name)
            out[name] = {"error": True}
    started = time.monotonic()
    box = lf.VERIFY_TIME_BOX_SECONDS
    cache: dict = {}
    async with make_client() as client:
        out["stale_verified"] = await lf.verify_stale_listings(
            db, client, limit=lf.STALE_VERIFY_BUDGET, deadline=started + box * 0.6, cache=cache)
        out["unconfirmed_verified"] = await lf.verify_unconfirmed_active_listings(
            db, client, limit=lf.UNCONFIRMED_VERIFY_BUDGET, deadline=started + box * 0.8,
            cache=cache)
        out["recent_verified"] = await lf.verify_recent_aggregator_listings(
            db, client, limit=lf.RECENT_VERIFY_BUDGET, deadline=started + box, cache=cache)
    out["verify_seconds"] = round(time.monotonic() - started, 1)
    out["terminal_expired"] = lf.sweep_terminal_expiry(db)
    out["ghost_scoring"] = lf.score_ghost_risk(db)
    return out


def _sweep_closures(sweeps: dict) -> int:
    """Rows the sweeps closed (removed or expired)."""
    closed = int(sweeps.get("expired") or 0)
    closed += sum(int(v) for v in (sweeps.get("terminal_expired") or {}).values())
    for name in ("stale_verified", "unconfirmed_verified", "recent_verified"):
        closed += int((sweeps.get(name) or {}).get("removed") or 0)
    return closed


# ─── Alerts ──────────────────────────────────────────────────────────────────

def post_alert(warnings: list, run_id: int) -> bool:
    """Post the run's yield warnings to ALERT_WEBHOOK_URL (a Slack-style
    {"text": ...} body) when it is set. The URL is a secret and never
    logged. Returns whether a post was made and accepted."""
    url = os.environ.get("ALERT_WEBHOOK_URL", "").strip()
    if not url or not warnings:
        return False
    import httpx

    from backend.ingest.core.http import USER_AGENT

    lines = [f"Tailrd ingest run {run_id}: {len(warnings)} yield warning(s)"]
    lines += [f"- {w['board']}: {w['message']}" for w in warnings[:50]]
    try:
        response = httpx.post(url, json={"text": "\n".join(lines)}, timeout=10,
                              headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        return True
    except Exception as e:
        logger.warning("alert webhook failed: %s", type(e).__name__)
        return False


# ─── Dry run ─────────────────────────────────────────────────────────────────

def preview_snapshot(db, snapshot) -> dict:
    """What ingesting ``snapshot`` would change, read-only and column-only:
    listings already stored, new ones, and (for a complete listing) the
    board's open rows it no longer lists."""
    from backend.db.models import ScrapedJob
    from backend.services.listing_freshness import CLOSED_LISTING_STATUSES

    urls = sorted({job.url for job in snapshot.jobs if job.url})
    known: set[str] = set()
    for i in range(0, len(urls), _IN_CHUNK):
        known.update(url for (url,) in db.query(ScrapedJob.url)
                     .filter(ScrapedJob.url.in_(urls[i:i + _IN_CHUNK])))
    would_remove = None
    if snapshot.complete:
        open_urls = [url for (url,) in db.query(ScrapedJob.url).filter(
            ScrapedJob.board_key == snapshot.board_key,
            ScrapedJob.listing_status.notin_(CLOSED_LISTING_STATUSES))]
        would_remove = sum(1 for url in open_urls if url not in snapshot.all_urls)
    db.rollback()
    return {"listed": len(snapshot.all_urls), "found": len(snapshot.jobs),
            "complete": snapshot.complete, "known": len(known),
            "new": len(urls) - len(known), "would_remove": would_remove}


async def _dry_run(db, args, deadline: float, counter) -> dict:
    from backend.ingest.core import http, pipeline
    from backend.services.ats_scraper import ATSScraper

    boards, cooldown, _health = plan_boards(db, args, _utcnow())
    log_event("dry_run_started", boards=len(boards), skipped_cooldown=cooldown)
    scraper = ATSScraper(filter_entry_level=False, filter_north_america=True, deadline=deadline)
    totals = {"boards": len(boards), "ok": 0, "failed": 0, "found": 0, "new": 0,
              "would_remove": 0}
    async with http.make_client(follow_redirects=False) as client, aclosing(
            pipeline.crawl_boards(scraper, client, boards, args.concurrency,
                                  per_host=args.per_host)) as crawl:
        async for (platform, slug, _name), snapshot, error in crawl:
            line = {"board": f"{platform}:{slug}"}
            rows_before = counter.rows
            if error is not None:
                totals["failed"] += 1
                line.update(status="failed", error=pipeline.safe_error(error))
            else:
                preview = preview_snapshot(db, snapshot)
                totals["ok"] += 1
                totals["found"] += preview["found"]
                totals["new"] += preview["new"]
                totals["would_remove"] += preview["would_remove"] or 0
                line.update(status="ok", **preview)
            line["rows_read"] = counter.rows - rows_before
            log_event("dry_run_board", **line)
    log_event("dry_run_finished", **totals)
    return {"status": "dry_run", **totals}


# ─── The run ─────────────────────────────────────────────────────────────────

def _abandon_dead_runs(db, now: datetime.datetime) -> int:
    """Runs still marked ``running`` when a run starts (it holds the lock,
    so they are dead): abandoned, their unfinished tasks failed."""
    from backend.db.models import ScrapeRun
    from backend.ingest.core import queue

    dead = db.query(ScrapeRun).filter(ScrapeRun.status == "running").all()
    for run in dead:
        run.status = "abandoned"
        run.finished_at = run.finished_at or now
        db.commit()
        queue.fail_unfinished(db, run.id, "run abandoned", now=now)
    return len(dead)


async def run(args, *, session_factory, lock_url: Optional[str], ensure: bool = True,
              engine=None) -> dict:
    """One run (the module docstring). ``session_factory`` makes the
    session; ``engine`` is the schema step's engine (the app's by default).
    Returns the summary."""
    from backend.db.models import ScrapeRun
    from backend.ingest.core import http, pipeline, queue
    from backend.services.ats_scraper import ATSScraper

    started = time.monotonic()
    deadline = started + args.budget_minutes * 60
    with run_lock(lock_url) as locked:
        if not locked:
            log_event("skipped", reason="another ingest run holds the lock")
            return {"status": "skipped"}
        if ensure:
            if engine is None:
                from backend.db.database import engine as app_engine
                engine = app_engine
            ensure_schema(engine)
        db = session_factory()
        counter = _RowCounter(db.get_bind())
        try:
            if args.dry_run:
                return await _dry_run(db, args, deadline, counter)

            now = _utcnow()
            deleted = queue.delete_finished(db, now=now)
            abandoned = _abandon_dead_runs(db, now)
            scrape_run = ScrapeRun(trigger=args.trigger, status="running", started_at=now)
            db.add(scrape_run)
            db.commit()
            run_id = scrape_run.id
            log_event("run_started", run_id=run_id, trigger=args.trigger,
                      tasks_deleted=deleted, runs_abandoned=abandoned)
            try:
                mirror = mirror_registry(db)
                boards, cooldown, _health = plan_boards(db, args, now)
                queue.enqueue(db, run_id, "list", [
                    {"platform": platform, "slug": slug, "company": company}
                    for platform, slug, company in boards
                ])
                scrape_run.tasks_total = len(boards)
                db.commit()
                ctx = pipeline.new_context(
                    db, [company for _p, _s, company in boards],
                    detail_per_board=DETAIL_PER_BOARD, detail_concurrency=args.per_host,
                    insert_deadline=deadline,
                )
                ctx.totals["boards_skipped_cooldown"] = cooldown
                scraper = ATSScraper(filter_entry_level=False, filter_north_america=True,
                                     deadline=deadline)
                # The TailrdBot user agent on every request; redirects as
                # cron-ats's client takes them, so a board answers the same.
                async with http.make_client(follow_redirects=False) as client:
                    crawl = await _crawl(db, client, run_id, args, ctx, scraper, counter)
                ctx.totals["boards_failed"] = crawl["failed"]
                ctx.totals["boards_deferred"] = crawl["deferred"]
                queue.fail_unfinished(db, run_id, "the run's budget ran out first")

                sweeps = await run_sweeps(db)
                alerted = post_alert(ctx.warnings, run_id)
            except BaseException as e:
                db.rollback()
                scrape_run = db.get(ScrapeRun, run_id)
                scrape_run.status = "failed"
                scrape_run.finished_at = _utcnow()
                scrape_run.notes = f"failed: {pipeline.safe_error(e)}"
                db.commit()
                queue.fail_unfinished(db, run_id, "the run failed")
                log_event("run_failed", run_id=run_id, error=pipeline.safe_error(e))
                raise

            totals, caps = ctx.totals, {**ctx.caps, "inserting_stopped": ctx.stopped or None}
            notes = [f"{w['board']}: {w['message']}" for w in ctx.warnings]
            if ctx.stopped:
                notes.append(f"inserting stopped: {ctx.stopped} after {ctx.inserted} new rows")
            if crawl["deferred"]:
                notes.append(f"{crawl['deferred']} boards deferred: the budget ran out")
            scrape_run.status = "completed"
            scrape_run.finished_at = _utcnow()
            scrape_run.tasks_ok = crawl["ok"]
            scrape_run.tasks_failed = crawl["failed"]
            scrape_run.jobs_seen = totals["total_found"]
            scrape_run.jobs_new = totals["new_jobs"]
            scrape_run.jobs_closed = totals["removed"] + _sweep_closures(sweeps)
            scrape_run.notes = "\n".join(notes) or None
            summary = {
                "run_id": run_id, "trigger": args.trigger,
                "seconds": round(time.monotonic() - started, 1),
                "boards": len(boards), "boards_ok": crawl["ok"],
                "boards_failed": crawl["failed"], "boards_deferred": crawl["deferred"],
                "boards_skipped_cooldown": cooldown,
                "totals": totals, "caps": caps, "platforms": crawl["platforms"],
                "companies": mirror, "yield_warnings": len(ctx.warnings),
                "alert_posted": alerted, "database_mb": ctx.size_mb,
                "rows_read": counter.rows, "sweeps": sweeps,
            }
            scrape_run.stats = json.loads(json.dumps(summary, default=str))
            db.commit()
            log_event("run_finished", **summary)
            return {"status": "completed", **summary}
        finally:
            counter.close()
            db.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m backend.ingest.run",
        description="Crawl the registry's boards into scraped_jobs (scraper v2).")
    parser.add_argument("--boards", default="",
                        help="comma-separated boards to crawl: platform:slug or slug")
    parser.add_argument("--platforms", default="", help="comma-separated platforms to crawl")
    parser.add_argument("--limit", type=int, default=0,
                        help="crawl at most this many boards (least recently crawled first)")
    parser.add_argument("--concurrency", type=int, default=20, help="boards crawled at once")
    parser.add_argument("--per-host", type=int, default=2,
                        help="boards crawled at once on one API host")
    parser.add_argument("--budget-minutes", type=float, default=170,
                        help="no board starts and no row is inserted after this")
    parser.add_argument("--dry-run", action="store_true",
                        help="crawl and report what would change, writing nothing")
    parser.add_argument("--trigger", default="manual", help="who started the run (cron, manual)")
    args = parser.parse_args(argv)
    args.concurrency = max(1, args.concurrency)
    args.per_host = max(1, args.per_host)
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr,
                        format="%(levelname)s %(name)s: %(message)s")
    from backend.db.database import SessionLocal

    lock_url = os.environ.get("DATABASE_URL_DIRECT") or os.environ.get("DATABASE_URL", "")
    try:
        summary = asyncio.run(run(args, session_factory=SessionLocal, lock_url=lock_url))
    except Exception as e:
        from backend.ingest.core.pipeline import safe_error

        logger.error("ingest run failed: %s", safe_error(e))
        return 1
    return 0 if summary.get("status") in ("completed", "skipped", "dry_run") else 1


if __name__ == "__main__":
    sys.exit(main())
