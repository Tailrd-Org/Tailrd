"""
The write path every board crawl shares: one BoardSnapshot in, the catalogue
brought in line with it.

cron-ats (routers/github_sources.py) and the v2 runner (backend/ingest/run.py)
both crawl boards with ATSScraper.scrape_board and hand each snapshot to
ingest_snapshot(), so a posting is stored, refreshed, reconciled and retired by
the same rules whichever of them crawled it:

  1. legacy rows move onto the key and URL the crawl uses (shared Workday
     tenants, old SmartRecruiters URLs), so they are matched, not duplicated
  2. known listings are refreshed (listing_freshness.refresh_known_listings:
     last_seen_at, edits, the derived fields); the rest are new
  3. new listings are stored with a description (one detail request each on
     platforms whose list payload carries none, within a budget), the derived
     fields, a logo and the structured extraction; each hides the
     LinkedIn/Indeed copies of itself that arrived first
  4. the board is reconciled: a complete listing removes what it no longer
     lists, a partial one only confirms what it does list
  5. source_health records the board's success

crawl_boards() is the crawl side both use: boards crawled concurrently, never
more than ``per_host`` at once on one API host.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import logging
import math
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

_IN_CHUNK = 400  # keep IN () lists comfortably under driver parameter limits

# Launch order: the shared-host chains first. Greenhouse, Ashby and Lever
# (and SmartRecruiters) boards each share one API host, so a platform crawls
# one board at a time and its chain (~70 Greenhouse boards a shard) is the
# run's critical path. Workday tenants are a host each and fill the slots the
# chains leave; launched first, they held every slot for the first ~35 s.
LAUNCH_ORDER = {"greenhouse": 0, "ashby": 0, "lever": 0, "smartrecruiters": 1, "workday": 2}


# ─── Crawl scheduling ────────────────────────────────────────────────────────

def crawled_at(health) -> datetime.datetime:
    """A board's last successful crawl as naive UTC; never crawled sorts first."""
    stamp = getattr(health, "last_success_at", None)
    if stamp is None:
        return datetime.datetime.min
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return stamp


async def crawl_boards(scraper, client, boards: list[tuple[str, str, str]],
                       concurrency: int, *, per_host: int = 1,
                       deferred: Optional[list] = None):
    """Crawl boards concurrently, yielding ((platform, slug, name), snapshot,
    error) as each one finishes. At most ``concurrency`` crawls are in flight
    and at most ``per_host`` on one API host (ats_scraper.board_host). A
    failed crawl yields its exception in place of a snapshot, so one bad board
    never sinks the run.

    A slot is handed on the moment its crawl finishes, not when the consumer
    next asks for a result, so the seconds spent processing a snapshot never
    leave slots idle. Once the scraper's deadline has passed no board starts:
    the rest of the queue goes to ``deferred`` for the next run."""
    from backend.services.ats_scraper import board_host

    queue = [
        (board, board_host(board[0], board[1]))
        for board in sorted(boards, key=lambda board: LAUNCH_ORDER.get(board[0], 2))
    ]
    running: dict[asyncio.Future, tuple[tuple[str, str, str], str]] = {}
    finished: asyncio.Queue = asyncio.Queue()
    closing = False

    async def crawl(board):
        try:
            return await scraper.scrape_board(client, *board), None
        except Exception as e:
            return None, e

    def launch():
        if closing:
            return
        if queue and scraper._out_of_time():
            if deferred is not None:
                deferred.extend(board for board, _host in queue)
            queue.clear()
            return
        busy = Counter(host for _board, host in running.values())
        i = 0
        while i < len(queue) and len(running) < concurrency:
            board, host = queue[i]
            if busy[host] >= per_host:
                i += 1
                continue
            queue.pop(i)
            busy[host] += 1
            task = asyncio.ensure_future(crawl(board))
            running[task] = (board, host)
            task.add_done_callback(finish)

    def finish(task):
        board, _host = running.pop(task)
        if task.cancelled():
            return
        finished.put_nowait((board, *task.result()))
        launch()

    try:
        launch()
        while running or not finished.empty():
            yield await finished.get()
    finally:
        closing = True
        tasks = list(running)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ─── Per-run state ───────────────────────────────────────────────────────────

def new_totals() -> dict:
    """The run counters cron-ats reports (and the runner keeps per run)."""
    return {
        "total_found": 0, "new_jobs": 0, "refreshed": 0, "edited": 0,
        "removed": 0, "revived": 0, "cross_source_twins_hidden": 0,
        "boards_failed": 0, "boards_skipped_cooldown": 0,
        "boards_partial": 0, "partial_confirmed": 0, "urls_migrated": 0,
        "boards_deferred": 0, "off_target": 0, "reparsed": 0, "recountried": 0,
        "retitled": 0, "relabeled": 0,
    }


def new_caps() -> dict:
    """What the storage caps (limits.json) and the detail budgets held back
    in a run: postings not stored, by reason, and descriptions cut."""
    return {"too_old": 0, "company_cap": 0, "run_cap": 0, "storage": 0, "deadline": 0,
            "detail_budget": 0, "descriptions_cut": 0}


@dataclass
class Limits:
    """backend/ingest/data/limits.json. None is no cap."""
    max_live_per_company: Optional[int] = None
    skip_posted_older_than_days: Optional[int] = None
    description_max_chars: Optional[int] = None
    max_new_rows_per_run: Optional[int] = None
    stop_inserting_at_mb: Optional[float] = None


LIMITS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "data", "limits.json")
# pg_database_size is read before a run's first insert and then every this many.
SIZE_CHECK_EVERY = 500
# The statuses that hold a place against max_live_per_company.
_LIVE = ("active", "stale")


def load_limits(path: str = LIMITS_PATH) -> Limits:
    """The caps in limits.json; a key that is absent, 0 or not a positive
    number is no cap. An unreadable file raises: a broken data file must
    not quietly switch off the storage guard."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    values = {}
    for name in Limits.__dataclass_fields__:
        value = raw.get(name)
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
        values[name] = value if ok else None
    return Limits(**values)


@dataclass
class IngestContext:
    """What ingest_snapshot reads and updates across one run's boards: the
    registry lookups and logo branding loaded once, the caps and budgets,
    and the run's counters (``totals``, ``caps``)."""
    work_type_classifier: object
    logo_map: dict
    board_countries: dict
    branding: dict
    retire_off_target: bool
    limits: Limits = field(default_factory=Limits)
    # Detail requests for new rows whose list payload has no description
    # (SmartRecruiters, Workday, adapters with fetch_job): at most
    # ``detail_budget`` a run and ``detail_per_board`` a board; None is no
    # cap. Rows past it are not inserted this run and surface as new again
    # on the board's next crawl. ``detail_concurrency`` requests at once per
    # board (one board's are all on one host).
    detail_budget: Optional[int] = None
    detail_per_board: Optional[int] = None
    detail_concurrency: int = 1
    # time.monotonic() past which the run inserts nothing more; refresh and
    # reconciliation go on.
    insert_deadline: Optional[float] = None
    totals: dict = field(default_factory=new_totals)
    caps: dict = field(default_factory=new_caps)
    # Yield warnings raised this run (record_board_success).
    warnings: list = field(default_factory=list)
    inserted: int = 0
    # Why inserting stopped for the rest of the run: "run_cap", "storage" or
    # "deadline"; "" while it goes on.
    stopped: str = ""
    size_mb: Optional[float] = None
    next_size_check: int = 0
    live_counts: dict = field(default_factory=dict)
    company_ids: Optional[dict] = None


def new_context(db: Session, company_names: list[str], *,
                detail_budget: Optional[int] = None,
                detail_per_board: Optional[int] = None,
                detail_concurrency: int = 1,
                insert_deadline: Optional[float] = None,
                limits: Optional[Limits] = None) -> IngestContext:
    """An IngestContext for a run over boards of ``company_names``: their
    logo branding and, under max_live_per_company, their live row counts,
    each in one query."""
    from backend.data import company_registry
    from backend.services import listing_freshness, logo_cache
    from backend.services.work_type_classifier import WorkTypeClassifier

    ctx = IngestContext(
        work_type_classifier=WorkTypeClassifier(),
        logo_map=company_registry.load_logo_map(),
        board_countries=company_registry.load_board_countries(),
        branding=logo_cache.load_branding(db, company_names),
        retire_off_target=listing_freshness.retire_off_target_enabled(),
        limits=limits if limits is not None else load_limits(),
        detail_budget=detail_budget,
        detail_per_board=detail_per_board,
        detail_concurrency=max(1, detail_concurrency),
        insert_deadline=insert_deadline,
    )
    if ctx.limits.max_live_per_company:
        _load_live_counts(db, ctx, company_names)
    return ctx


def _load_live_counts(db: Session, ctx: IngestContext, companies) -> None:
    """Live rows per employer name, for the names ctx has no count for yet:
    one grouped, column-only query per chunk of names."""
    from sqlalchemy import func

    from backend.db.models import ScrapedJob

    names = sorted({name for name in companies if name and name not in ctx.live_counts})
    for i in range(0, len(names), _IN_CHUNK):
        chunk = names[i:i + _IN_CHUNK]
        counts = dict(
            db.query(ScrapedJob.company, func.count(ScrapedJob.id))
            .filter(ScrapedJob.listing_status.in_(_LIVE), ScrapedJob.company.in_(chunk))
            .group_by(ScrapedJob.company)
            .all()
        )
        for name in chunk:
            ctx.live_counts[name] = int(counts.get(name) or 0)


def _company_id(db: Session, ctx: IngestContext, platform: str, slug: str) -> Optional[int]:
    """companies.id of the board's employer (the runner mirrors the registry
    there), None when the table does not have it. The whole table is read
    once a run: one row per registry board."""
    if ctx.company_ids is None:
        from backend.db.models import Company

        ctx.company_ids = {
            (ats, token): company_id
            for company_id, ats, token in db.query(Company.id, Company.ats, Company.ats_token)
        }
    return ctx.company_ids.get((platform, slug))


def database_size_mb(db: Session) -> Optional[float]:
    """pg_database_size of the current database in MB; None off Postgres."""
    from sqlalchemy import text

    if db.get_bind().dialect.name != "postgresql":
        return None
    size = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
    return round(int(size) / (1024 * 1024), 1)


def _may_insert(db: Session, ctx: IngestContext) -> bool:
    """Whether the run may insert one more row: not past its deadline or
    max_new_rows_per_run, and the database under stop_inserting_at_mb
    (read before the first insert and every SIZE_CHECK_EVERY after). Once
    one says no, ``ctx.stopped`` holds the reason for the rest of the run."""
    limits = ctx.limits
    if ctx.stopped:
        return False
    if ctx.insert_deadline is not None and time.monotonic() >= ctx.insert_deadline:
        ctx.stopped = "deadline"
    elif limits.max_new_rows_per_run and ctx.inserted >= limits.max_new_rows_per_run:
        ctx.stopped = "run_cap"
    elif limits.stop_inserting_at_mb and ctx.inserted >= ctx.next_size_check:
        ctx.next_size_check = ctx.inserted + SIZE_CHECK_EVERY
        ctx.size_mb = database_size_mb(db)
        if ctx.size_mb is not None and ctx.size_mb >= limits.stop_inserting_at_mb:
            ctx.stopped = "storage"
    if ctx.stopped:
        logger.warning("ingest: inserting stopped for this run (%s) after %d new rows%s",
                       ctx.stopped, ctx.inserted,
                       f", database at {ctx.size_mb} MB" if ctx.stopped == "storage" else "")
        return False
    return True


# ─── Fingerprint ─────────────────────────────────────────────────────────────

def fingerprint(company_domain: str, company: str, title: str,
                locations: Optional[list], location: str, employment_type: str) -> str:
    """sha256 of (company domain | normalized title | sorted normalized
    locations | employment type): a match key for one posting across
    sources, never unique (one employer posts the same title as separate
    requisitions). The employer is its domain, else its folded name; the
    places are the parsed locations (city, region, country), else the
    folded location text."""
    from backend.services.cross_source_dedup import normalize_company, normalize_title

    employer = (company_domain or "").strip().lower()
    if employer.startswith("www."):
        employer = employer[4:]
    employer = employer or normalize_company(company or "")
    places = sorted({
        ",".join(str(entry.get(key) or "").strip().lower() for key in ("city", "region", "country"))
        for entry in (locations or []) if isinstance(entry, dict)
    } - {",,"})
    if not places and (location or "").strip():
        places = [re.sub(r"[^a-z0-9]+", " ", location.lower()).strip()]
    key = "|".join((employer, normalize_title(title or ""), ";".join(places),
                    (employment_type or "").strip().lower()))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ─── Errors fit to publish ───────────────────────────────────────────────────

_QUERY_STRING = re.compile(r"(https?://[^\s?#'\"<>]+)\?[^\s#'\"<>]*")
_URL_SECRETS = ("DATABASE_URL", "DATABASE_URL_DIRECT", "ALERT_WEBHOOK_URL")


def _secret_values() -> list[str]:
    """Values that must never reach a log line or source_health: every
    adapter's REQUIRED_ENV value (Jooble's key sits in its URL path), the
    connection strings and their passwords, and the alert webhook."""
    from urllib.parse import urlsplit

    from backend.ingest import adapters

    names = set(_URL_SECRETS)
    for platform in adapters.adapter_platforms():
        required = getattr(adapters.get_adapter(platform), "REQUIRED_ENV", ()) or ()
        names.update([required] if isinstance(required, str) else required)
    values = set()
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if len(value) >= 4:
            values.add(value)
        if name in _URL_SECRETS and value:
            try:
                password = urlsplit(value).password
            except ValueError:
                password = None
            if password and len(password) >= 4:
                values.add(password)
    return sorted(values, key=len, reverse=True)


def safe_error(error) -> str:
    """An error as text fit for the public workflow log and for
    source_health.last_error: URLs lose their query strings (API keys ride
    in them) and every secret value is masked. At most 500 characters."""
    message = repr(error) if isinstance(error, BaseException) else str(error)
    message = _QUERY_STRING.sub(r"\1?[redacted]", message)
    for value in _secret_values():
        message = message.replace(value, "[redacted]")
    return message[:500]


# ─── Source health ───────────────────────────────────────────────────────────

# A board whose listing count falls more than this far below its 7-day
# average gets a yield warning, once the average is at least YIELD_MIN_AVG
# (a board going from 2 postings to 0 is no signal).
YIELD_DROP = 0.6
YIELD_MIN_AVG = 5.0
YIELD_WINDOW_DAYS = 7.0


def in_cooldown(health, now: Optional[datetime.datetime] = None) -> bool:
    """The board's breaker is open (source_health.in_cooldown), or its
    disabled_until is still ahead: stamped when the breaker opens, and a
    way to park a board by hand."""
    from backend.services import source_health

    now = now or datetime.datetime.utcnow()
    if source_health.in_cooldown(health, now):
        return True
    until = getattr(health, "disabled_until", None)
    return until is not None and until > now


def record_board_failure(db: Session, board_key: str, platform: str, slug: str,
                         error: str, now: Optional[datetime.datetime] = None) -> bool:
    """source_health.record_failure, plus the breaker's end in
    disabled_until when this failure opens it. Returns whether it is open.
    Commits."""
    from backend.db.models import SourceHealth
    from backend.services import source_health

    now = now or datetime.datetime.utcnow()
    source_health.record_failure(db, board_key, platform, slug, error, now=now)
    health = db.query(SourceHealth).filter(SourceHealth.board_key == board_key).first()
    if health is None or (health.consecutive_failures or 0) < source_health.FAILURE_THRESHOLD:
        return False
    health.disabled_until = now + datetime.timedelta(hours=source_health.COOLDOWN_HOURS)
    db.commit()
    return True


def record_board_success(db: Session, snapshot, ctx: Optional[IngestContext] = None,
                         now: Optional[datetime.datetime] = None) -> Optional[str]:
    """source_health.record_success, then the board's 7-day yield: a
    complete listing's count moves jobs_7d_avg (an average weighted by the
    time since the last crawl, so it spans about a week however often the
    board is crawled), and a count more than YIELD_DROP below the average
    is a warning, kept in last_error until the next success and in
    ``ctx.warnings``. A partial listing's count says nothing about the
    board's yield. Returns the warning. Commits."""
    from backend.db.models import SourceHealth
    from backend.services import source_health

    now = now or datetime.datetime.utcnow()
    board_key = snapshot.board_key
    count = len(snapshot.jobs)
    prior = (
        db.query(SourceHealth.jobs_7d_avg, SourceHealth.last_success_at)
        .filter(SourceHealth.board_key == board_key)
        .first()
    )
    source_health.record_success(db, board_key, snapshot.platform, snapshot.slug, count, now=now)
    if not snapshot.complete:
        return None
    average, last_success = prior if prior else (None, None)
    warning = None
    if average is not None and average >= YIELD_MIN_AVG and count < average * (1 - YIELD_DROP):
        warning = (f"yield warning: {count} jobs listed against a 7-day average of "
                   f"{average:.1f}")
    if average is None or last_success is None:
        updated = float(count)
    else:
        days = max(0.0, (now - last_success).total_seconds() / 86400)
        updated = average + (count - average) * (1 - math.exp(-days / YIELD_WINDOW_DAYS))
    values: dict = {"jobs_7d_avg": round(updated, 2)}
    if warning:
        values["last_error"] = warning
    db.query(SourceHealth).filter(SourceHealth.board_key == board_key).update(
        values, synchronize_session=False)
    db.commit()
    if warning:
        logger.warning("%s: %s", board_key, warning)
        if ctx is not None:
            ctx.warnings.append({"board": board_key, "jobs": count,
                                 "jobs_7d_avg": round(average, 2), "message": warning})
    return warning


# ─── Matching legacy rows ────────────────────────────────────────────────────

def adopt_site_rows(db: Session, platform: str, slug: str, board_key: str) -> int:
    """Give a shared Workday tenant's rows to the career site that lists them.

    One tenant can host several sites (BlackBerry and QNX both live on tenant
    "bb"), but board_key_from_url() only names the tenant, so legacy rows of
    both sites share "workday:bb". Such sites get their own registry slugs;
    this moves the rows under this site's URL root onto its key before the
    crawl matches or reconciles, so one site's complete crawl can never mark
    the other site's rows removed. A no-op for single-site tenants, whose
    slug already is the tenant. Commits when it moves anything.
    """
    if platform != "workday":
        return 0
    from backend.data.company_registry import load_workday_bases
    from backend.db.models import ScrapedJob
    from backend.services.ats_scraper import workday_public_base
    from backend.services.listing_freshness import board_key_from_url

    site_root = workday_public_base(load_workday_bases().get(slug, ""))
    if not site_root:
        return 0
    site_root += "/"
    tenant_key = board_key_from_url(site_root)
    if not tenant_key or tenant_key == board_key:
        return 0

    moved = (
        db.query(ScrapedJob)
        .filter(ScrapedJob.board_key == tenant_key,
                ScrapedJob.url.startswith(site_root, autoescape=True))
        .update({"board_key": board_key}, synchronize_session=False)
    )
    if moved:
        db.commit()
    return moved


def migrate_smartrecruiters_urls(db: Session, snapshot) -> int:
    """Point legacy SmartRecruiters rows at their real posting page.

    Rows stored before the URL fix carry careers.smartrecruiters.com URLs,
    which redirect to the company careers home. When the board lists the
    posting, rewrite the row to the jobs.smartrecruiters.com URL the crawl now
    builds, so refresh and reconcile match it instead of inserting a twin and
    (on a complete board) removing the original. A row whose new URL another
    row already holds (url is UNIQUE) is left here; cron-backfill's
    legacy_urls.migrate_legacy_smartrecruiters hides it behind that row, and
    moves the rows no crawl lists.
    Commits when it rewrites anything. Returns the count.
    """
    if snapshot.platform != "smartrecruiters":
        return 0
    from backend.db.models import ScrapedJob
    from backend.services.ats_scraper import smartrecruiters_legacy_url

    legacy_to_new = {smartrecruiters_legacy_url(url): url for url in snapshot.all_urls}
    legacy_to_new.pop("", None)
    legacy = sorted(legacy_to_new)

    migrated = 0
    for i in range(0, len(legacy), _IN_CHUNK):
        found = (
            db.query(ScrapedJob.id, ScrapedJob.url)
            .filter(ScrapedJob.url.in_(legacy[i:i + _IN_CHUNK]))
            .all()
        )
        if not found:
            continue
        wanted = [legacy_to_new[url] for _row_id, url in found]
        taken = {
            url for (url,) in
            db.query(ScrapedJob.url).filter(ScrapedJob.url.in_(wanted)).all()
        }
        for row_id, url in found:
            if legacy_to_new[url] in taken:
                continue
            db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(
                {"url": legacy_to_new[url]}, synchronize_session=False,
            )
            migrated += 1
    if migrated:
        db.commit()
    return migrated


# ─── Reconciliation of partial listings ──────────────────────────────────────

def confirm_listed(db: Session, board_key: str, urls: set[str],
                   now: Optional[datetime.datetime] = None, *,
                   rejected: Optional[dict] = None) -> dict:
    """The confirm half of reconciliation, for partial snapshots.

    Every URL a board lists is live, even when the crawl couldn't list the
    whole board: bump those rows' ``last_seen_at`` and move their status by
    listing_freshness.listed_status_change(): stale/removed/expired back to
    active, or off_target when ``rejected`` (url -> ATSScraper.rejection
    reason) says the listing fails the crawler's filters. A listing's own
    title and location are evidence however much of the board was read. Rows
    the partial list didn't mention are left alone, its silence is not
    evidence of removal (reconcile_board's remove half only ever runs on
    complete snapshots). A column-only SELECT (id, url, status, source) per
    chunk, then grouped UPDATEs. Commits. Returns counts.
    """
    from backend.db.models import ScrapedJob
    from backend.services.listing_freshness import (
        LISTING_ACTIVE, LISTING_OFF_TARGET, listed_status_change,
    )

    now = now or datetime.datetime.utcnow()
    rejected = rejected or {}
    stats = {"confirmed": 0, "revived": 0, "off_target": 0}
    listed = sorted(url for url in urls if url)
    for i in range(0, len(listed), _IN_CHUNK):
        rows = (
            db.query(ScrapedJob.id, ScrapedJob.url, ScrapedJob.listing_status,
                     ScrapedJob.source_platform)
            .filter(ScrapedJob.board_key == board_key,
                    ScrapedJob.url.in_(listed[i:i + _IN_CHUNK]))
            .all()
        )
        moves: dict[str, list[int]] = {LISTING_ACTIVE: [], LISTING_OFF_TARGET: []}
        for row_id, url, listing_status, source_platform in rows:
            change = listed_status_change(listing_status, source_platform or "",
                                          rejected.get(url))
            if change in moves:
                moves[change].append(row_id)
        for status, ids in moves.items():
            if ids:
                # Neither status is closed, so a closed reason goes too.
                db.query(ScrapedJob).filter(ScrapedJob.id.in_(ids)).update(
                    {"listing_status": status, "listing_status_changed_at": now,
                     "closed_reason": None},
                    synchronize_session=False)
        if rows:
            db.query(ScrapedJob).filter(
                ScrapedJob.id.in_([row[0] for row in rows])
            ).update({"last_seen_at": now}, synchronize_session=False)
        stats["confirmed"] += len(rows)
        stats["revived"] += len(moves[LISTING_ACTIVE])
        stats["off_target"] += len(moves[LISTING_OFF_TARGET])
    if listed:
        db.commit()
    return stats


# ─── One board ───────────────────────────────────────────────────────────────

# Platforms whose list payload carries no description: each new row costs
# one detail request (SmartRecruiters' posting page, Workday's CxS detail; a
# v2 adapter's fetch_job).
_DETAIL_PLATFORMS = ("smartrecruiters", "workday")


def _newest_first(job) -> tuple:
    """Sort key: dated postings newest first, undated ones after them, each
    group in listing order (the sort is stable)."""
    posted = _naive(getattr(job, "posted_date", None))
    if posted is None:
        return (1, datetime.timedelta(0))
    return (0, datetime.datetime.max - posted)


def _naive(stamp: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """A datetime as naive UTC (sources mix aware and naive stamps)."""
    if stamp is not None and stamp.tzinfo is not None:
        return stamp.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return stamp


def _select_new(db: Session, jobs: list, ctx: IngestContext,
                now: datetime.datetime) -> list:
    """The new jobs this run may store, newest first, within the caps that
    are known before any request: none the source already dated older than
    skip_posted_older_than_days, none of an employer holding
    max_live_per_company live rows, none past max_new_rows_per_run. The rest
    are counted in ``ctx.caps``."""
    limits, caps = ctx.limits, ctx.caps
    jobs = sorted(jobs, key=_newest_first)
    if ctx.stopped:
        caps[ctx.stopped] += len(jobs)
        return []
    if limits.skip_posted_older_than_days:
        cutoff = now - datetime.timedelta(days=limits.skip_posted_older_than_days)
        fresh = [job for job in jobs
                 if job.posted_date is None or _naive(job.posted_date) >= cutoff]
        caps["too_old"] += len(jobs) - len(fresh)
        jobs = fresh
    if limits.max_live_per_company:
        _load_live_counts(db, ctx, {(job.company or "").strip() for job in jobs})
    room = None
    if limits.max_new_rows_per_run:
        room = max(0, limits.max_new_rows_per_run - ctx.inserted)
    selected: list = []
    taken: Counter = Counter()
    for job in jobs:
        if room is not None and len(selected) >= room:
            caps["run_cap"] += 1
            continue
        company = (job.company or "").strip()
        if limits.max_live_per_company:
            if ctx.live_counts.get(company, 0) + taken[company] >= limits.max_live_per_company:
                caps["company_cap"] += 1
                continue
            taken[company] += 1
        selected.append(job)
    return selected


async def _fill_descriptions(client, snapshot, jobs: list, ctx: IngestContext) -> list:
    """Give each job its sanitized description, fetching it for platforms
    whose list payload has none (one request each, ``ctx.detail_concurrency``
    at once, within the detail budgets). Returns the jobs to store: one the
    budget could not cover waits for the board's next crawl, while one whose
    request failed is stored without a description, as cron-ats always did."""
    from backend.data.company_registry import SUPPORTED_PLATFORMS, load_board_config
    from backend.ingest.adapters import adapter_enabled, get_adapter
    from backend.services import ats_scraper
    from backend.services.description_extractor import (
        extract_smartrecruiters_from_url,
        sanitize_description,
    )

    platform, slug = snapshot.platform, snapshot.slug
    adapter = None
    if platform not in SUPPORTED_PLATFORMS:
        candidate = get_adapter(platform)
        if candidate is not None and adapter_enabled(platform) and callable(
                getattr(candidate, "fetch_job", None)):
            adapter = candidate
    fetches = platform in _DETAIL_PLATFORMS or adapter is not None

    keep: list = []
    pending: list = []
    board_budget = ctx.detail_per_board
    for job in jobs:
        if (job.description or "").strip() or not fetches:
            keep.append(job)
            continue
        if ((ctx.detail_budget is not None and ctx.detail_budget <= 0)
                or (board_budget is not None and board_budget <= 0)):
            ctx.caps["detail_budget"] += 1
            continue  # re-surfaces as new on the board's next crawl
        if ctx.detail_budget is not None:
            ctx.detail_budget -= 1
        if board_budget is not None:
            board_budget -= 1
        keep.append(job)
        pending.append(job)

    gate = asyncio.Semaphore(ctx.detail_concurrency)
    config = load_board_config(platform, slug) if adapter is not None else None

    async def fetch(job):
        async with gate:
            try:
                if platform == "smartrecruiters":
                    job.description = await extract_smartrecruiters_from_url(client, job.url)
                    return
                if platform == "workday":
                    detail = await ats_scraper.fetch_workday_detail(client, slug, job.detail_ref)
                else:
                    detail = await adapter.fetch_job(client, slug, job.detail_ref or job.url,
                                                     config=config)
                detail = detail or {}
                job.description = detail.get("description", "") or ""
                if detail.get("employment_type") and not job.employment_type:
                    job.employment_type = detail["employment_type"]
                if detail.get("apply_url") and not job.apply_url:
                    job.apply_url = detail["apply_url"]
            except Exception:
                job.description = ""

    if pending:
        await asyncio.gather(*(fetch(job) for job in pending))
    for job in keep:
        description = (job.description or "").strip()
        job.description = sanitize_description(description) if description else ""
    return keep


def _new_row(db: Session, job, snapshot, ctx: IngestContext, board_country: str):
    """The ScrapedJob for one new listing (its description already
    sanitized onto ``job``)."""
    from backend.db.models import ScrapedJob
    from backend.services import listing_freshness, logo_cache
    from backend.services.logo_resolver import domain_from_logo_url, resolve_logo

    board_key = snapshot.board_key
    description = job.description

    # Country + parsed location, and the title-derived fields, by the same
    # helpers the refresh of known rows uses. A one-country board says which
    # country it is (BDO's bare "London" is Ontario); otherwise the location
    # does, with the classifier the NA filter used ("Toronto" is CA, where
    # CountryFilter found nothing and "US" was the default).
    derived = listing_freshness.location_derived_fields(
        job.location, board_country, hint=job.location_hint,
    )
    work_type = job.work_type or ctx.work_type_classifier.classify(job.location)

    # Resolve an accurate logo: a self-hosted one from the logo store, else
    # the curated registry logo, else one derived from the company domain.
    # The registry's favicon URL carries the curated domain (toasttab.com for
    # Toast, notion.so for Notion), which beats the name guess; an
    # employer-hosted apply link (gh_jid on carvana.com) is next best.
    registry_logo = ctx.logo_map.get(job.company.strip().lower()) or ""
    resolved_logo, resolved_domain = resolve_logo(
        job.company,
        known_domain=domain_from_logo_url(registry_logo),
        apply_url=job.url,
    )
    company_logo, resolved_domain = logo_cache.brand(
        ctx.branding, job.company, registry_logo or resolved_logo, resolved_domain
    )

    # Extraction, hashes and tags read the whole description; only the
    # stored copy is cut at description_max_chars.
    extracted = listing_freshness.build_new_row_fields(job, board_key)
    stored = listing_freshness.cap_description(description, ctx.limits.description_max_chars)
    if len(stored) < len(description):
        ctx.caps["descriptions_cut"] += 1
    return ScrapedJob(
        title=job.title,
        company=job.company,
        location=job.location,
        url=job.url,
        description=stored,
        source_platform="ats",
        **listing_freshness.title_fields(
            job.title, job.department or "", job.employment_type or "", description),
        **derived,
        posted_date=job.posted_date,
        easy_apply=0,
        work_type=work_type,
        company_logo=company_logo,
        company_domain=resolved_domain,
        **extracted,
        fingerprint=fingerprint(resolved_domain, job.company, job.title,
                                derived.get("locations_json"), job.location,
                                extracted.get("employment_type", "")),
        company_id=_company_id(db, ctx, snapshot.platform, snapshot.slug),
        apply_url=job.apply_url or None,
    )


async def ingest_snapshot(db: Session, client, snapshot, ctx: IngestContext) -> dict:
    """Bring the catalogue in line with one board crawl (the steps in the
    module docstring). ``client`` makes the detail requests; ``ctx`` is the
    run's state, whose ``totals`` and ``caps`` this adds to. Commits as it
    goes. Returns this board's counts."""
    from backend.services import listing_freshness
    from backend.services.cross_source_dedup import mark_inferior_twins

    platform, slug = snapshot.platform, snapshot.slug
    board_key = snapshot.board_key
    totals = ctx.totals
    now = datetime.datetime.utcnow()
    board = {"board": board_key, "listed": len(snapshot.all_urls), "found": len(snapshot.jobs),
             "complete": snapshot.complete, "known": 0, "new": 0, "inserted": 0,
             "removed": 0, "revived": 0}

    totals["total_found"] += len(snapshot.jobs)

    # Pull legacy rows onto the key and URL this crawl uses, so they are
    # matched below rather than duplicated or removed.
    adopt_site_rows(db, platform, slug, board_key)
    totals["urls_migrated"] += migrate_smartrecruiters_urls(db, snapshot)

    # Re-confirm known listings (and detect edits, re-deriving the fields an
    # edit moves); get the new ones.
    board_country = ctx.board_countries.get(board_key, "")
    new_jobs, refresh_stats = listing_freshness.refresh_known_listings(
        db, board_key, snapshot.jobs, board_country=board_country,
        description_max_chars=ctx.limits.description_max_chars,
    )
    for key in ("refreshed", "edited", "reparsed", "recountried", "retitled", "relabeled"):
        totals[key] += refresh_stats[key]
    board["known"] = refresh_stats["refreshed"]
    board["new"] = len(new_jobs)

    # Reconcile the board's stored rows against what it just listed, before
    # inserting, so the rows its listing dropped have closed by the time the
    # employer's live rows are counted against max_live_per_company. Only a
    # complete listing votes on removals; a partial one (a board past
    # Workday's ceiling, a spent crawl budget) still proves every URL it
    # listed is live. Either way a listed row whose listing fails the
    # filters goes off_target, unless the kill switch is set (then listed
    # means active, as before).
    rejected = snapshot.rejected if ctx.retire_off_target else None
    if snapshot.complete:
        rec = listing_freshness.reconcile_board(
            db, board_key, snapshot.all_urls, rejected=rejected)
        totals["removed"] += rec["removed"]
        totals["revived"] += rec["revived"]
        totals["off_target"] += rec["off_target"]
        board.update(removed=rec["removed"], revived=rec["revived"])
    else:
        totals["boards_partial"] += 1
        seen = confirm_listed(db, board_key, snapshot.all_urls, rejected=rejected)
        totals["partial_confirmed"] += seen["confirmed"]
        totals["revived"] += seen["revived"]
        totals["off_target"] += seen["off_target"]
        board["revived"] = seen["revived"]
    if board["removed"]:
        # Closed rows free their places: the employer counts are read again.
        for job in new_jobs:
            ctx.live_counts.pop((job.company or "").strip(), None)

    selected = _select_new(db, new_jobs, ctx, now)
    if selected:
        selected = await _fill_descriptions(client, snapshot, selected, ctx)
    for index, job in enumerate(selected):
        if not _may_insert(db, ctx):
            ctx.caps[ctx.stopped] += len(selected) - index
            break
        scraped_job = _new_row(db, job, snapshot, ctx, board_country)
        db.add(scraped_job)
        try:
            db.commit()
        except Exception:
            db.rollback()
            continue
        totals["new_jobs"] += 1
        board["inserted"] += 1
        ctx.inserted += 1
        company = (job.company or "").strip()
        ctx.live_counts[company] = ctx.live_counts.get(company, 0) + 1

        # This direct row supersedes any LinkedIn/Indeed copies of the same
        # posting that arrived first.
        try:
            totals["cross_source_twins_hidden"] += mark_inferior_twins(db, scraped_job)
        except Exception:
            db.rollback()

    record_board_success(db, snapshot, ctx)
    return board
