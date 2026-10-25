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
from backend.auth.dependencies import get_admin_user_id, verify_cron_secret
from backend.ingest.core import pipeline

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

# The crawl scheduler and the per-board write path live in
# backend/ingest/core/pipeline.py, shared with the v2 runner. The old names
# stay here for the callers and tests that import them from this module.
_confirm_listed = pipeline.confirm_listed
_adopt_site_rows = pipeline.adopt_site_rows
_migrate_smartrecruiters_urls = pipeline.migrate_smartrecruiters_urls


def _crawl_boards(scraper, client, boards: list[tuple[str, str, str]],
                  concurrency: int = CRON_ATS_CONCURRENCY,
                  deferred: Optional[list] = None):
    """pipeline.crawl_boards with cron-ats's defaults: CRON_ATS_CONCURRENCY
    boards at once, one per API host."""
    return pipeline.crawl_boards(scraper, client, boards, concurrency, deferred=deferred)


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
        from backend.services.ats_scraper import ATSScraper
        from backend.services import source_health
        from backend.data import company_registry

        scraper = ATSScraper(
            filter_entry_level=True, filter_north_america=True,
            deadline=time.monotonic() + CRON_ATS_LIST_BUDGET_SECONDS,
        )

        # The workflow's "hourly" schedule really fires ~6x a day at uneven
        # gaps, so hour % shard_count can hand the same shard several runs in
        # a row. Crawl whichever shard has waited longest instead.
        shard_index, shard_count, companies = company_registry.pick_shard(
            company_registry.load_companies(), source_health.last_success_times(db)
        )

        import httpx

        health_map = source_health.get_health_map(
            db, [f"{p}:{s}" for p, s, _ in companies]
        )
        ctx = pipeline.new_context(
            db, [name for _, _, name in companies],
            detail_budget=WORKDAY_DETAIL_BUDGET, detail_per_board=WORKDAY_DETAIL_PER_BOARD,
        )
        totals = ctx.totals

        runnable = []
        for platform, slug, company_name in companies:
            if source_health.in_cooldown(health_map.get(f"{platform}:{slug}")):
                totals["boards_skipped_cooldown"] += 1
            else:
                runnable.append((platform, slug, company_name))
        # Least recently crawled first (the launch sort keeps this order within
        # a platform), so boards a spent budget deferred last run start first
        # instead of being the same deferred tail every time.
        runnable.sort(key=lambda board: pipeline.crawled_at(
            health_map.get(f"{board[0]}:{board[1]}")))
        deferred: list[tuple[str, str, str]] = []

        async with httpx.AsyncClient(timeout=30) as client, aclosing(
            _crawl_boards(scraper, client, runnable, deferred=deferred)
        ) as crawl:
            async for (platform, slug, company_name), snapshot, error in crawl:
                if error is not None:
                    totals["boards_failed"] += 1
                    source_health.record_failure(
                        db, f"{platform}:{slug}", platform, slug, repr(error))
                    continue
                await pipeline.ingest_snapshot(db, client, snapshot, ctx)

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
