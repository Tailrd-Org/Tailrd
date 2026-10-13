"""
GitHub source management endpoints.

GET    /github-sources           → list[GitHubSourceOut]
POST   /github-sources           → GitHubSourceOut
POST   /github-sources/seed      → SeedResult
PUT    /github-sources/{id}      → GitHubSourceOut
DELETE /github-sources/{id}      → None
POST   /github-sources/{id}/poll → PollResult
"""

import asyncio
import os
import re
import datetime
import logging
import time
import traceback
from contextlib import aclosing

from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
from sqlalchemy.orm import Session

from backend.db.database import get_db
from backend.db.models import GitHubSource
from backend.schemas.github_source import GitHubSourceOut, GitHubSourceCreate
from backend.services.github_scraper import validate_github_repo_url
from backend.services.role_classifier import classify as classify_role
from backend.services.location_parser import location_fields
from backend.services.cross_source_dedup import mark_inferior_twins
from backend.auth.dependencies import get_admin_user_id, verify_cron_secret

logger = logging.getLogger(__name__)
router = APIRouter()


def _parse_github_url(url: str) -> tuple[str, str]:
    """Extract owner and repo name from a GitHub URL."""
    match = re.match(r'https://github\.com/([^/]+)/([^/]+)/?$', url)
    if not match:
        raise HTTPException(status_code=422, detail="Invalid GitHub repository URL.")
    return match.group(1), match.group(2)


# A source's URL (aggregator.source_url): the repo for its README, or
# '<repo>/blob/HEAD/<file>' for a list kept in its own file (speedyapply's
# NEW_GRAD_USA.md, *_INTL.md).
_FILE_SOURCE_URL_RE = re.compile(
    r"^(https://github\.com/[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)/blob/HEAD/([\w.\-/]+)$")


def _source_owner_repo(repo_url: str, file_path: str) -> tuple[str, str]:
    """Owner and repo name of a source URL in either form. A file URL must
    be the one seed_sources stores for the source's own ``file_path`` (a
    README source is the bare repo). 422 otherwise."""
    from backend.services.aggregator import source_url

    match = _FILE_SOURCE_URL_RE.match(repo_url or "")
    if match:
        repo, url_file = match.groups()
        if ".." in url_file.split("/") or source_url(repo, file_path) != repo_url:
            raise HTTPException(status_code=422,
                                detail="The URL's file must be the source's file_path.")
        return _parse_github_url(repo)
    if not validate_github_repo_url(repo_url):
        raise HTTPException(status_code=422, detail="Invalid GitHub repository URL.")
    return _parse_github_url(repo_url)


@router.get("", response_model=list[GitHubSourceOut])
def list_sources(
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """List all configured GitHub sources."""
    return db.query(GitHubSource).all()


@router.post("", response_model=GitHubSourceOut)
def create_source(
    source: GitHubSourceCreate,
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Add a new GitHub repository source."""
    owner, repo_name = _source_owner_repo(source.repo_url, source.file_path)

    # Check for duplicate
    existing = db.query(GitHubSource).filter(GitHubSource.repo_url == source.repo_url).first()
    if existing:
        raise HTTPException(status_code=409, detail="This repository is already configured.")

    db_source = GitHubSource(
        repo_url=source.repo_url,
        repo_owner=owner,
        repo_name=repo_name,
        file_path=source.file_path,
        poll_interval_minutes=source.poll_interval_minutes,
    )
    db.add(db_source)
    db.commit()
    db.refresh(db_source)
    return db_source


@router.post("/seed")
async def seed_sources(
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Seed all jobright-ai repositories. Idempotent.

    Creates GitHubSource records for all configured repositories.
    Skips any that already exist. Returns counts of created vs existing.
    """
    try:
        from backend.services.aggregator import AggregatorService
        aggregator = AggregatorService(db)
        result = await aggregator.seed_sources()
        return {
            "status": "seeded",
            "created": result["created"],
            "existing": result["existing"],
            "total": result["created"] + result["existing"],
        }
    except Exception:
        logger.error(f"Seed failed: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/cleanup-blank-companies")
def cleanup_blank_companies(
    dry_run: bool = True,
    _cron: None = Depends(verify_cron_secret),
    db: Session = Depends(get_db),
):
    """Remove jobs with empty/placeholder company names.

    These render as blank cards with no logo on the dashboard (mostly old
    LinkedIn rows whose company failed to parse). The scraper now rejects
    these at write time and the listing API hides them; this permanently
    removes the historical ones. Authenticated via cron secret.

    Defaults to dry_run=True (counts only). Pass ?dry_run=false to delete.
    """
    from backend.db.models import ScrapedJob as SJ
    from sqlalchemy import or_, func

    blank_filter = or_(
        SJ.company.is_(None),
        func.trim(SJ.company) == "",
        SJ.company == "Unknown",
    )
    q = db.query(SJ).filter(blank_filter)
    count = q.count()
    if dry_run:
        return {"dry_run": True, "would_delete": count}

    deleted = q.delete(synchronize_session=False)
    db.commit()
    return {"dry_run": False, "deleted_jobs": deleted}


# New Workday rows need one detail request each for their description (the
# list payload has none, and the public page is JS-rendered so cron-backfill
# can't recover it later). Cap per run; jobs past the cap simply stay
# un-inserted and surface as "new" again on the board's next shard pass.
# ~0.4 s a request, so the run's 40 add ~16 s against the 300 s limit.
WORKDAY_DETAIL_BUDGET = 40
# ...and per board: the budget goes to boards in the order their lists
# finish, so a big new board (PwC's 212 postings) took all 40 for days while
# fresh postings on BMO, CIBC or RBC's early-talent site waited behind it.
WORKDAY_DETAIL_PER_BOARD = 10

# Wall-clock budget, from the start of the run, for paging big boards past
# their newest-first head. Full lists are what let Workday boards reconcile
# (BMO is ~50 list POSTs, Parsons ~100), but the whole run shares one request
# with the workflow's 300 s curl. Boards still paging at the deadline finish
# partial: they confirm what they listed and remove nothing. No board starts
# after it either; those wait for the shard's next run.
CRON_ATS_LIST_BUDGET_SECONDS = float(os.getenv("CRON_ATS_LIST_BUDGET_SECONDS", "150"))

# Boards crawled at once. Never two on one API host, so Greenhouse, Lever,
# Ashby and SmartRecruiters boards still go one after another at the per-host
# pace; the parallelism is mostly Workday tenants, each its own host.
CRON_ATS_CONCURRENCY = max(1, int(os.getenv("CRON_ATS_CONCURRENCY", "6")))

_IN_CHUNK = 400  # keep IN () lists comfortably under driver parameter limits

# Launch order: the shared-host chains first. Greenhouse, Ashby and Lever
# (and SmartRecruiters) boards each share one API host, so a platform crawls
# one board at a time and its chain (~70 Greenhouse boards a shard) is the
# run's critical path. Workday tenants are a host each and fill the slots the
# chains leave; launched first, they held every slot for the first ~35 s.
_LAUNCH_ORDER = {"greenhouse": 0, "ashby": 0, "lever": 0, "smartrecruiters": 1, "workday": 2}


def _crawled_at(health) -> datetime.datetime:
    """A board's last successful crawl as naive UTC; never crawled sorts first."""
    stamp = getattr(health, "last_success_at", None)
    if stamp is None:
        return datetime.datetime.min
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return stamp


async def _crawl_boards(scraper, client, boards: list[tuple[str, str, str]],
                        concurrency: int = CRON_ATS_CONCURRENCY,
                        deferred: Optional[list] = None):
    """Crawl boards concurrently, yielding ((platform, slug, name), snapshot,
    error) as each one finishes. At most ``concurrency`` crawls are in flight
    and never two on the same API host. A failed crawl yields its exception
    in place of a snapshot, so one bad board never sinks the run.

    A slot is handed on the moment its crawl finishes, not when the consumer
    next asks for a result, so the seconds spent processing a snapshot never
    leave slots idle. Once the scraper's deadline has passed no board starts:
    the rest of the queue goes to ``deferred`` for the shard's next run."""
    from backend.services.ats_scraper import board_host

    queue = [
        (board, board_host(board[0], board[1]))
        for board in sorted(boards, key=lambda board: _LAUNCH_ORDER.get(board[0], 2))
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
        busy = {host for _board, host in running.values()}
        i = 0
        while i < len(queue) and len(running) < concurrency:
            board, host = queue[i]
            if host in busy:
                i += 1
                continue
            queue.pop(i)
            busy.add(host)
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


def _confirm_listed(db: Session, board_key: str, urls: set[str],
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


def _adopt_site_rows(db: Session, platform: str, slug: str, board_key: str) -> int:
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


def _migrate_smartrecruiters_urls(db: Session, snapshot) -> int:
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


@router.post("/cron-ats")
async def cron_ats(
    _cron: None = Depends(verify_cron_secret),
    db: Session = Depends(get_db),
):
    """Crawl the least-recently-crawled shard of ATS boards: ingest new
    listings, re-confirm known ones (last_seen_at), and reconcile each board
    so vanished postings are marked removed the same run, the freshness edge
    over aggregators.

    Per-board failures are isolated and recorded in source_health; a board
    failing repeatedly is skipped for a cooldown (circuit breaker) instead of
    burning the run's budget. Filters to entry-level + US/Canada only, and a
    stored row whose own listing now fails those filters (a London, UK row
    from before the NA filter knew better) is retired as off_target instead
    of confirmed; CRON_ATS_RETIRE_OFF_TARGET=0 switches that off.
    """
    try:
        from backend.db.models import ScrapedJob
        from backend.services.ats_scraper import ATSScraper, fetch_workday_detail
        from backend.services.work_type_classifier import WorkTypeClassifier
        from backend.services.logo_resolver import domain_from_logo_url, resolve_logo
        from backend.services import listing_freshness, logo_cache, source_health
        from backend.data import company_registry

        scraper = ATSScraper(
            filter_entry_level=True, filter_north_america=True,
            deadline=time.monotonic() + CRON_ATS_LIST_BUDGET_SECONDS,
        )
        work_type_classifier = WorkTypeClassifier()
        logo_map = company_registry.load_logo_map()
        board_countries = company_registry.load_board_countries()
        retire_off_target = listing_freshness.retire_off_target_enabled()

        # The workflow's "hourly" schedule really fires ~6x a day at uneven
        # gaps, so hour % shard_count can hand the same shard several runs in
        # a row. Crawl whichever shard has waited longest instead.
        shard_index, shard_count, companies = company_registry.pick_shard(
            company_registry.load_companies(), source_health.last_success_times(db)
        )

        import httpx
        from backend.services.description_extractor import (
            extract_smartrecruiters_from_url,
            sanitize_description,
        )

        health_map = source_health.get_health_map(
            db, [f"{p}:{s}" for p, s, _ in companies]
        )
        branding = logo_cache.load_branding(db, [name for _, _, name in companies])

        totals = {
            "total_found": 0, "new_jobs": 0, "refreshed": 0, "edited": 0,
            "removed": 0, "revived": 0, "cross_source_twins_hidden": 0,
            "boards_failed": 0, "boards_skipped_cooldown": 0,
            "boards_partial": 0, "partial_confirmed": 0, "urls_migrated": 0,
            "boards_deferred": 0, "off_target": 0, "reparsed": 0, "recountried": 0,
            "retitled": 0, "relabeled": 0,
        }
        workday_detail_budget = WORKDAY_DETAIL_BUDGET

        runnable = []
        for platform, slug, company_name in companies:
            if source_health.in_cooldown(health_map.get(f"{platform}:{slug}")):
                totals["boards_skipped_cooldown"] += 1
            else:
                runnable.append((platform, slug, company_name))
        # Least recently crawled first (the launch sort keeps this order within
        # a platform), so boards a spent budget deferred last run start first
        # instead of being the same deferred tail every time.
        runnable.sort(key=lambda board: _crawled_at(health_map.get(f"{board[0]}:{board[1]}")))
        deferred: list[tuple[str, str, str]] = []

        async with httpx.AsyncClient(timeout=30) as client, aclosing(
            _crawl_boards(scraper, client, runnable, deferred=deferred)
        ) as crawl:
            async for (platform, slug, company_name), snapshot, error in crawl:
                board_key = f"{platform}:{slug}"

                if error is not None:
                    totals["boards_failed"] += 1
                    source_health.record_failure(db, board_key, platform, slug, repr(error))
                    continue

                totals["total_found"] += len(snapshot.jobs)

                # Pull legacy rows onto the key and URL this crawl uses, so
                # they are matched below rather than duplicated or removed.
                _adopt_site_rows(db, platform, slug, board_key)
                totals["urls_migrated"] += _migrate_smartrecruiters_urls(db, snapshot)

                # Re-confirm known listings (and detect edits, re-deriving the
                # fields an edit moves); get the new ones.
                board_country = board_countries.get(board_key, "")
                new_jobs, refresh_stats = listing_freshness.refresh_known_listings(
                    db, board_key, snapshot.jobs, board_country=board_country,
                )
                for key in ("refreshed", "edited", "reparsed", "recountried", "retitled",
                            "relabeled"):
                    totals[key] += refresh_stats[key]

                board_detail_budget = WORKDAY_DETAIL_PER_BOARD
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
                        if workday_detail_budget <= 0 or board_detail_budget <= 0:
                            continue  # re-surfaces as new on the next pass
                        workday_detail_budget -= 1
                        board_detail_budget -= 1
                        try:
                            detail = await fetch_workday_detail(client, slug, job.detail_ref)
                            description = detail.get("description", "")
                            if detail.get("employment_type") and not job.employment_type:
                                job.employment_type = detail["employment_type"]
                        except Exception:
                            description = ""
                    description = sanitize_description(description) if description else ""
                    job.description = description

                    # Country + parsed location, and the title-derived fields,
                    # by the same helpers the refresh of known rows uses. A
                    # one-country board says which country it is (BDO's bare
                    # "London" is Ontario); otherwise the location does, with
                    # the classifier the NA filter used ("Toronto" is CA, where
                    # CountryFilter found nothing and "US" was the default).
                    derived = listing_freshness.location_derived_fields(
                        job.location, board_country, hint=job.location_hint,
                    )

                    # Classify work type
                    work_type = job.work_type or work_type_classifier.classify(job.location)

                    # Resolve an accurate logo: a self-hosted one from the logo
                    # store, else the curated registry logo, else one derived
                    # from the company domain. The registry's favicon URL
                    # carries the curated domain (toasttab.com for Toast,
                    # notion.so for Notion), which beats the name guess; an
                    # employer-hosted apply link (gh_jid on carvana.com) is
                    # next best.
                    registry_logo = logo_map.get(job.company.strip().lower()) or ""
                    resolved_logo, resolved_domain = resolve_logo(
                        job.company,
                        known_domain=domain_from_logo_url(registry_logo),
                        apply_url=job.url,
                    )
                    company_logo, resolved_domain = logo_cache.brand(
                        branding, job.company, registry_logo or resolved_logo, resolved_domain
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
                    except Exception:
                        db.rollback()
                        continue

                    # This direct row supersedes any LinkedIn/Indeed copies of the
                    # same posting that arrived first.
                    try:
                        totals["cross_source_twins_hidden"] += mark_inferior_twins(db, scraped_job)
                    except Exception:
                        db.rollback()

                # Reconcile the board's stored rows against what it just listed.
                # Only a complete listing votes on removals; a partial one (a
                # board past Workday's ceiling, a spent crawl budget) still
                # proves every URL it listed is live. Either way a listed row
                # whose listing fails the filters goes off_target, unless the
                # kill switch is set (then listed means active, as before).
                rejected = snapshot.rejected if retire_off_target else None
                if snapshot.complete:
                    rec = listing_freshness.reconcile_board(
                        db, board_key, snapshot.all_urls, rejected=rejected)
                    totals["removed"] += rec["removed"]
                    totals["revived"] += rec["revived"]
                    totals["off_target"] += rec["off_target"]
                else:
                    totals["boards_partial"] += 1
                    seen = _confirm_listed(db, board_key, snapshot.all_urls, rejected=rejected)
                    totals["partial_confirmed"] += seen["confirmed"]
                    totals["revived"] += seen["revived"]
                    totals["off_target"] += seen["off_target"]

                source_health.record_success(db, board_key, platform, slug, len(snapshot.jobs))

        # Never started: no success stamp, and first in line next time.
        totals["boards_deferred"] = len(deferred)
        if deferred:
            logger.warning(
                "cron-ats: crawl budget spent, deferred %d boards to the next run: %s",
                len(deferred), ", ".join(f"{p}:{s}" for p, s, _ in deferred),
            )

        return {
            "status": "completed",
            **totals,
            # Back-compat alias: known listings are refreshed now, not skipped.
            "duplicates_skipped": totals["refreshed"],
            "shard": {
                "index": shard_index,
                "count": shard_count,
                "companies": len(companies),
            },
        }
    except Exception:
        logger.error(f"ATS cron failed: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/scrape-linkedin")
async def scrape_linkedin_jobs(
    city: Optional[str] = None,
    query: Optional[str] = None,
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Scrape LinkedIn public job search for intern/new-grad/co-op positions.

    Pass ?city=Ottawa&query=intern to scrape a single query for a single city (fast).
    Without params, scrapes all cities and queries (may timeout on serverless).
    """
    try:
        from backend.db.models import ScrapedJob
        from backend.services.ats_scraper import experience_level_for
        from backend.services.linkedin_scraper import LinkedInScraper, CITIES, QUERIES
        from backend.services.na_location import job_country
        from backend.services.work_type_classifier import WorkTypeClassifier

        scraper = LinkedInScraper(request_delay=2.0)
        work_type_classifier = WorkTypeClassifier()

        if city and query:
            # Single query + single city (fastest, fits serverless timeout)
            city_match = next(
                ((c, p) for c, p in CITIES if c.lower() == city.lower()),
                None
            )
            if not city_match:
                return {"error": f"City '{city}' not found. Available: {[c for c, _ in CITIES]}"}
            jobs = await scraper.scrape_single(query, city_match[0], city_match[1])
            # Return immediately with parsed results for debugging
            return {
                "status": "completed",
                "total_found": len(jobs),
                "jobs_preview": [{"title": j.title, "company": j.company, "location": j.location, "url": j.url} for j in jobs[:5]],
            }
        elif city:
            # All queries for one city
            city_match = next(
                ((c, p) for c, p in CITIES if c.lower() == city.lower()),
                None
            )
            if not city_match:
                return {"error": f"City '{city}' not found. Available: {[c for c, _ in CITIES]}"}
            jobs = await scraper.scrape_city(city_match[0], city_match[1])
        else:
            jobs = await scraper.scrape_all()

        new_count = 0
        skipped_dupe = 0
        for job in jobs:
            # Dedup by URL. Query the column, not the entity: loading the row
            # would pull its ~1.9 KB description across the wire for a boolean.
            existing = db.query(ScrapedJob.url).filter(ScrapedJob.url == job.url).first()
            if existing:
                skipped_dupe += 1
                continue

            # Classify country: the classifier every ingest path shares; a
            # location it can't place stays "CA" (every search is Canadian).
            country = job_country(job.location, fallback="CA")

            # Classify work type
            work_type = work_type_classifier.classify(job.location)

            # Word-bounded: "Internal Audit Analyst" is not an internship.
            experience_level = experience_level_for(job.title)

            # Resolve logo from the company domain; a self-hosted logo (and
            # verified domain) from the logo store wins.
            from backend.services.logo_resolver import resolve_logo as _resolve_logo
            from backend.services.logo_cache import brand, load_branding
            company_logo, company_domain = brand(
                load_branding(db, [job.company]), job.company, *_resolve_logo(job.company)
            )

            # Parse the card's posted date (ISO "YYYY-MM-DD") when present.
            posted_date = None
            if job.posted_date:
                try:
                    posted_date = datetime.datetime.fromisoformat(job.posted_date)
                except (ValueError, TypeError):
                    posted_date = None

            scraped_job = ScrapedJob(
                title=job.title,
                company=job.company,
                location=job.location,
                url=job.url,
                description="",
                source_platform="linkedin",
                posted_date=posted_date,
                easy_apply=0,
                work_type=work_type,
                role_category=classify_role(job.title),
                country=country,
                experience_level=experience_level,
                company_logo=company_logo,
                company_domain=company_domain,
                **location_fields(job.location),
            )
            db.add(scraped_job)
            try:
                db.commit()
                new_count += 1
            except Exception:
                db.rollback()
                skipped_dupe += 1

        return {
            "status": "completed",
            "total_found": len(jobs),
            "new_jobs": new_count,
            "duplicates_skipped": skipped_dupe,
        }
    except Exception:
        logger.error(f"LinkedIn scrape failed: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Internal server error")


# cron-poll shares one request with the workflow's `curl --max-time 300` and
# Vercel's 300 s function limit, so every phase is time-boxed from the start
# of the request (a normal run takes 13-27 s, the slowest of 30 in late
# September 127 s):
#   - no new source starts after CRON_POLL_BUDGET_SECONDS, and a source still
#     polling at CRON_POLL_HARD_STOP_SECONDS is cut off (a hung README or
#     probe batch; it is due again next run, the rows it stored are kept);
#   - no description fetch starts after CRON_POLL_ENRICH_UNTIL_SECONDS (each
#     can take its whole 15 s timeout; cron-backfill picks up the rest);
#   - the match-alert sweep is cut off at CRON_POLL_WALL_SECONDS. It scores
#     with the deterministic fit engine (no LLM, Task 9.2) in chunks of at
#     most 500 jobs, each committed on its own, so a cut sweep loses nothing
#     it scored. It yields to the event loop only between chunks, so it can
#     only be cut there, never between an email send and the record of it.
#     Its summary ("match_alerts" below) carries jobs_scored, chunks_skipped,
#     scoring_budget_spent, fit_trace_salt_missing (true: FIT_TRACE_SALT is
#     unset and nothing could be scored), calibration and drift.
# A cut lands only at an await, so synchronous work runs past it:
#   - a source's insert loop (poll_source stores rows with no await once
#     its probes are done). A source whose probes end just before the hard
#     stop stores its whole batch after it, ~20 s for a 375-row file at
#     prod's insert rate, and then finishes its poll: past
#     CRON_POLL_ENRICH_UNTIL_SECONDS its description fetch starts nothing,
#     so no await is left to cut it at.
#   - the list dedup passes below (column-only: one read of the visible list
#     rows, a few thousand, plus an UPDATE per hidden group; 0.2 s over
#     1,824 rows locally, a few Neon round trips in prod).
#   - one scoring chunk: the first in a cold process builds the engine
#     (about 10 s), and a chunk of 500 never-parsed postings parses them
#     (about 20 ms each) before scoring them (about 3 ms each), so one chunk
#     can run about 20 s past the cut; later chunks take a second or two.
#   - the rest of the sweep once it has stopped scoring (fit_trace_salt_missing,
#     or its scoring budget spent): no await is left, so every remaining
#     user's queries and banked-match email sends run to completion, each
#     send up to the Resend SDK's 30 s timeout, followed by the calibration
#     refresh and the drift report (a few queries).
# At today's scale (5 eligible users, a 25-user cap) the worst run ends
# around 245-285 s, under the 300 s ceiling. Only sends that hang to their
# timeout, or many more alert users while scoring is off, could pass it:
# the wall clock cannot cut either.
CRON_POLL_MAX_SOURCES = int(os.getenv("CRON_POLL_MAX_SOURCES", "12"))
CRON_POLL_BUDGET_SECONDS = float(os.getenv("CRON_POLL_BUDGET_SECONDS", "120"))
CRON_POLL_HARD_STOP_SECONDS = float(os.getenv("CRON_POLL_HARD_STOP_SECONDS", "200"))
CRON_POLL_ENRICH_UNTIL_SECONDS = float(os.getenv("CRON_POLL_ENRICH_UNTIL_SECONDS", "170"))
CRON_POLL_WALL_SECONDS = float(os.getenv("CRON_POLL_WALL_SECONDS", "240"))


async def _sweep_match_alerts_boxed(db: Session, seconds_left: float) -> dict:
    """The match-alert sweep, cut off after ``seconds_left``. Best-effort: a
    failure or a cut-off never fails the poll."""
    if seconds_left <= 0:
        logger.warning("Match-alert sweep skipped: cron-poll wall clock spent")
        return {"status": "skipped", "reason": "cron-poll wall clock spent"}
    try:
        from backend.services.match_notifier import sweep_match_alerts
        return await asyncio.wait_for(sweep_match_alerts(db), timeout=seconds_left)
    except asyncio.TimeoutError:
        db.rollback()
        logger.warning("Match-alert sweep cut off after %.0f s (cron-poll wall clock)",
                       seconds_left)
        return {"status": "timed_out", "seconds": round(seconds_left, 1)}
    except Exception:
        db.rollback()
        logger.error(f"Match-alert sweep failed: {traceback.format_exc()}")
        return {"status": "failed"}


@router.post("/cron-poll")
async def cron_poll(
    _cron: None = Depends(verify_cron_secret),
    db: Session = Depends(get_db),
):
    """Seed sources (if needed), poll the GitHub lists that are due, and run
    the match-alert sweep.

    Due means: a list that committed this week hourly, a quiet one daily, a
    parked one (nothing but list-vendor links, or no job table) weekly, and a
    source parked in 'error' by a transient failure (5xx, timeout, a rename
    recorded before redirects were followed) after a cooldown. The sweep runs
    whether or not any list was due: it used to sit behind an early return,
    so retiring the lists would have silently stopped every alert email.
    """
    started = time.monotonic()
    timings: dict[str, float] = {}

    def elapsed() -> float:
        return time.monotonic() - started

    try:
        from backend.services.aggregator import AggregatorService
        aggregator = AggregatorService(db, deadline=started + CRON_POLL_ENRICH_UNTIL_SECONDS)

        seed_result = await aggregator.seed_sources()

        due = aggregator.sources_due(limit=10_000)
        sources = due[:CRON_POLL_MAX_SOURCES]
        timings["seed"] = round(elapsed(), 1)

        polled: list[dict] = []
        total_new = 0
        total_enriched = 0
        for source in sources:
            # Sources left over stay the most overdue: first in line next run.
            if polled and elapsed() > CRON_POLL_BUDGET_SECONDS:
                break
            repo_name = source.repo_name
            source_started = time.monotonic()
            try:
                new_count = await asyncio.wait_for(
                    aggregator.poll_source(source),
                    timeout=max(1.0, CRON_POLL_HARD_STOP_SECONDS - elapsed()),
                )
            except asyncio.TimeoutError:
                # Its rows so far are committed row by row; the stamp that
                # says the README was read is not, so the next poll re-reads it.
                db.rollback()
                source.error_message = "Timeout: poll cut off by the cron-poll budget"
                source.last_polled_at = datetime.datetime.utcnow()
                db.commit()
                logger.warning("cron-poll: %s cut off at %.0f s", repo_name, elapsed())
                polled.append({"source": repo_name, "file": source.file_path or "README.md",
                               "new_jobs": 0, "timed_out": True,
                               "seconds": round(time.monotonic() - source_started, 1)})
                break
            enriched = await aggregator._enrich_missing_descriptions(source.id, limit=3)
            total_new += new_count
            total_enriched += enriched
            polled.append(
                {
                    "source": repo_name,
                    "file": source.file_path or "README.md",
                    "new_jobs": new_count,
                    "descriptions_enriched": enriched,
                    "seconds": round(time.monotonic() - source_started, 1),
                }
            )
        timings["poll"] = round(elapsed(), 1)

        global_enriched = await aggregator._enrich_missing_descriptions(None, limit=40)
        timings["enrich"] = round(elapsed(), 1)

        # Hide list rows the board crawl also carries under another spelling
        # ('/en-US/marvellcareers/...' vs '/MarvellCareers/...'), and list
        # rows repeating a posting another list row carries ('jobsathpe' vs
        # 'wfmathpe'), after giving back the ones whose board row or list row
        # has since left the feed. Column-only, a few thousand rows.
        list_copies_hidden = list_copies_released = 0
        list_repeats_hidden = list_repeats_released = 0
        try:
            from backend.services.cross_source_dedup import (
                hide_list_copies_of_board_rows,
                hide_repeated_list_rows,
                release_list_copies_of_lapsed_board_rows,
                release_repeated_list_rows,
            )
            list_copies_released = release_list_copies_of_lapsed_board_rows(db)
            list_repeats_released = release_repeated_list_rows(db)
            list_copies_hidden = hide_list_copies_of_board_rows(db)
            list_repeats_hidden = hide_repeated_list_rows(db)
        except Exception:
            db.rollback()
            logger.error(f"List-copy dedup failed: {traceback.format_exc()}")
        timings["dedup"] = round(elapsed(), 1)

        # Email users about new strong matches. Folded in here (rather than a
        # separate cron) so the app stays within Vercel's 2-cron Hobby limit.
        match_alerts = await _sweep_match_alerts_boxed(db, CRON_POLL_WALL_SECONDS - elapsed())
        timings["total"] = round(elapsed(), 1)

        return {
            "status": "completed" if sources else "no_sources",
            "sources_seeded": seed_result["created"],
            "sources_polled": len(polled),
            "sources_due": len(due),
            "new_jobs": total_new,
            "descriptions_enriched": total_enriched,
            "global_descriptions_enriched": global_enriched,
            "list_copies_hidden": list_copies_hidden,
            "list_copies_released": list_copies_released,
            "list_repeats_hidden": list_repeats_hidden,
            "list_repeats_released": list_repeats_released,
            "polled": polled,
            "match_alerts": match_alerts,
            "timings": timings,
        }
    except Exception:
        logger.error(f"Cron poll failed: {traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/{source_id}", response_model=GitHubSourceOut)
def update_source(
    source_id: int,
    source: GitHubSourceCreate,
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Update a GitHub source configuration. A file source keeps its
    '<repo>/blob/HEAD/<file>' URL (every edit of one used to 422). An
    interval other than the automatic hourly/daily cadence sticks
    (AggregatorService.poll_source)."""
    db_source = db.query(GitHubSource).filter(GitHubSource.id == source_id).first()
    if not db_source:
        raise HTTPException(status_code=404, detail="GitHub source not found.")

    owner, repo_name = _source_owner_repo(source.repo_url, source.file_path)

    db_source.repo_url = source.repo_url
    db_source.repo_owner = owner
    db_source.repo_name = repo_name
    db_source.file_path = source.file_path
    db_source.poll_interval_minutes = source.poll_interval_minutes
    db.commit()
    db.refresh(db_source)
    return db_source


@router.delete("/{source_id}")
def delete_source(
    source_id: int,
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Remove a GitHub source."""
    db_source = db.query(GitHubSource).filter(GitHubSource.id == source_id).first()
    if not db_source:
        raise HTTPException(status_code=404, detail="GitHub source not found.")

    db.delete(db_source)
    db.commit()
    return {"status": "deleted"}


@router.post("/{source_id}/poll")
async def poll_source(
    source_id: int,
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Trigger an immediate poll of a GitHub source, through the same pipeline
    as cron-poll. It used to run the old GitHubScraper, which stored every
    parsed row as an active listing with no vendor-link, country, max-age or
    dead-link filter and no canonical URL: one poll of a jobright list would
    have put hundreds of jobright.ai redirects in the feed."""
    db_source = db.query(GitHubSource).filter(GitHubSource.id == source_id).first()
    if not db_source:
        raise HTTPException(status_code=404, detail="GitHub source not found.")

    from backend.services.aggregator import AggregatorService
    try:
        new_count = await AggregatorService(db).poll_source(db_source)
    except Exception:
        logger.error(f"Poll failed for source {source_id}: {traceback.format_exc()}")
        raise HTTPException(status_code=502, detail="Internal server error")
    return {"status": db_source.status, "new_jobs": new_count,
            "error_message": db_source.error_message or ""}


@router.post("/backfill-role-categories")
def backfill_role_categories(
    apply: bool = False,
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Remap existing scraped_jobs.role_category to the canonical taxonomy.

    Dry-run by default (returns the counts that would change). Pass ?apply=true
    to write the changes. Rows already canonical are left untouched; known
    legacy aliases are mapped; empty/free-text values are reclassified by title.
    """
    from backend.db.models import ScrapedJob
    from backend.services.role_classifier import (
        CANONICAL_CATEGORIES, classify, normalize_category,
    )

    def target(title: str, current: str) -> str:
        cur = (current or "").strip()
        if cur in CANONICAL_CATEGORIES:
            return cur
        mapped = normalize_category(cur)
        if mapped:
            return mapped
        return classify(title or "", cur)

    rows = db.query(ScrapedJob).all()
    changes: dict[int, str] = {}
    for r in rows:
        new = target(r.title, r.role_category)
        if new != (r.role_category or ""):
            changes[r.id] = new

    if apply:
        for r in rows:
            if r.id in changes:
                r.role_category = changes[r.id]
        db.commit()

    return {
        "total_rows": len(rows),
        "changed": len(changes),
        "applied": apply,
    }
