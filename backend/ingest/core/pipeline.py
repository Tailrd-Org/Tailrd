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
import logging
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


@dataclass
class IngestContext:
    """What ingest_snapshot reads and updates across one run's boards: the
    registry lookups and logo branding loaded once, the detail-request
    budgets, and the run's counters (``totals``)."""
    work_type_classifier: object
    logo_map: dict
    board_countries: dict
    branding: dict
    retire_off_target: bool
    # Detail requests for new rows whose list payload has no description
    # (Workday): at most ``detail_budget`` a run and ``detail_per_board`` a
    # board; None is no cap. Rows past the cap are not inserted this run and
    # surface as new again on the board's next crawl.
    detail_budget: Optional[int] = None
    detail_per_board: Optional[int] = None
    totals: dict = field(default_factory=new_totals)


def new_context(db: Session, company_names: list[str], *,
                detail_budget: Optional[int] = None,
                detail_per_board: Optional[int] = None) -> IngestContext:
    """An IngestContext for a run over boards of ``company_names``."""
    from backend.data import company_registry
    from backend.services import listing_freshness, logo_cache
    from backend.services.work_type_classifier import WorkTypeClassifier

    return IngestContext(
        work_type_classifier=WorkTypeClassifier(),
        logo_map=company_registry.load_logo_map(),
        board_countries=company_registry.load_board_countries(),
        branding=logo_cache.load_branding(db, company_names),
        retire_off_target=listing_freshness.retire_off_target_enabled(),
        detail_budget=detail_budget,
        detail_per_board=detail_per_board,
    )


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
                db.query(ScrapedJob).filter(ScrapedJob.id.in_(ids)).update(
                    {"listing_status": status, "listing_status_changed_at": now},
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

async def ingest_snapshot(db: Session, client, snapshot, ctx: IngestContext) -> dict:
    """Bring the catalogue in line with one board crawl (the steps in the
    module docstring). ``client`` makes the detail requests; ``ctx`` is the
    run's state, whose ``totals`` this adds to. Commits as it goes. Returns
    this board's counts."""
    from backend.db.models import ScrapedJob
    from backend.services import ats_scraper, listing_freshness, logo_cache, source_health
    from backend.services.cross_source_dedup import mark_inferior_twins
    from backend.services.description_extractor import (
        extract_smartrecruiters_from_url,
        sanitize_description,
    )
    from backend.services.logo_resolver import domain_from_logo_url, resolve_logo

    platform, slug = snapshot.platform, snapshot.slug
    board_key = snapshot.board_key
    totals = ctx.totals
    board = {"found": len(snapshot.jobs), "new_jobs": 0, "removed": 0}

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
    )
    for key in ("refreshed", "edited", "reparsed", "recountried", "retitled", "relabeled"):
        totals[key] += refresh_stats[key]

    board_detail_budget = ctx.detail_per_board
    for job in new_jobs:
        # Board APIs carry descriptions for GH/Lever/Ashby;
        # SmartRecruiters/Workday need one extra call per NEW job only.
        description = (job.description or "").strip()
        if not description and platform == "smartrecruiters":
            try:
                description = await extract_smartrecruiters_from_url(client, job.url)
            except Exception:
                description = ""
        elif not description and platform == "workday":
            if ctx.detail_budget is not None and ctx.detail_budget <= 0:
                continue  # re-surfaces as new on the next pass
            if board_detail_budget is not None and board_detail_budget <= 0:
                continue
            if ctx.detail_budget is not None:
                ctx.detail_budget -= 1
            if board_detail_budget is not None:
                board_detail_budget -= 1
            try:
                detail = await ats_scraper.fetch_workday_detail(client, slug, job.detail_ref)
                description = detail.get("description", "")
                if detail.get("employment_type") and not job.employment_type:
                    job.employment_type = detail["employment_type"]
            except Exception:
                description = ""
        description = sanitize_description(description) if description else ""
        job.description = description

        # Country + parsed location, and the title-derived fields, by the
        # same helpers the refresh of known rows uses. A one-country board
        # says which country it is (BDO's bare "London" is Ontario);
        # otherwise the location does, with the classifier the NA filter used
        # ("Toronto" is CA, where CountryFilter found nothing and "US" was the
        # default).
        derived = listing_freshness.location_derived_fields(
            job.location, board_country, hint=job.location_hint,
        )

        work_type = job.work_type or ctx.work_type_classifier.classify(job.location)

        # Resolve an accurate logo: a self-hosted one from the logo store,
        # else the curated registry logo, else one derived from the company
        # domain. The registry's favicon URL carries the curated domain
        # (toasttab.com for Toast, notion.so for Notion), which beats the name
        # guess; an employer-hosted apply link (gh_jid on carvana.com) is next
        # best.
        registry_logo = ctx.logo_map.get(job.company.strip().lower()) or ""
        resolved_logo, resolved_domain = resolve_logo(
            job.company,
            known_domain=domain_from_logo_url(registry_logo),
            apply_url=job.url,
        )
        company_logo, resolved_domain = logo_cache.brand(
            ctx.branding, job.company, registry_logo or resolved_logo, resolved_domain
        )

        scraped_job = ScrapedJob(
            title=job.title,
            company=job.company,
            location=job.location,
            url=job.url,
            description=description,
            source_platform="ats",
            **listing_freshness.title_fields(
                job.title, job.department or "", job.employment_type or ""),
            **derived,
            posted_date=job.posted_date,
            easy_apply=0,
            work_type=work_type,
            company_logo=company_logo,
            company_domain=resolved_domain,
            **listing_freshness.build_new_row_fields(job, board_key),
        )
        db.add(scraped_job)
        try:
            db.commit()
            totals["new_jobs"] += 1
            board["new_jobs"] += 1
        except Exception:
            db.rollback()
            continue

        # This direct row supersedes any LinkedIn/Indeed copies of the same
        # posting that arrived first.
        try:
            totals["cross_source_twins_hidden"] += mark_inferior_twins(db, scraped_job)
        except Exception:
            db.rollback()

    # Reconcile the board's stored rows against what it just listed. Only a
    # complete listing votes on removals; a partial one (a board past
    # Workday's ceiling, a spent crawl budget) still proves every URL it
    # listed is live. Either way a listed row whose listing fails the filters
    # goes off_target, unless the kill switch is set (then listed means
    # active, as before).
    rejected = snapshot.rejected if ctx.retire_off_target else None
    if snapshot.complete:
        rec = listing_freshness.reconcile_board(
            db, board_key, snapshot.all_urls, rejected=rejected)
        totals["removed"] += rec["removed"]
        totals["revived"] += rec["revived"]
        totals["off_target"] += rec["off_target"]
        board["removed"] = rec["removed"]
    else:
        totals["boards_partial"] += 1
        seen = confirm_listed(db, board_key, snapshot.all_urls, rejected=rejected)
        totals["partial_confirmed"] += seen["confirmed"]
        totals["revived"] += seen["revived"]
        totals["off_target"] += seen["off_target"]

    source_health.record_success(db, board_key, platform, slug, len(snapshot.jobs))
    return board
