"""
Job listing endpoints (data only, no bot automation).

GET  /jobs, list scraped jobs with filters
GET  /jobs/{id}, get a single job (a hidden duplicate answers with its visible twin)
GET  /jobs/stats, aggregate stats
GET  /jobs/sidebar, the Jobs page right rail (progress, match snapshot, ...)
GET  /jobs/logo/{sha}.png, a self-hosted company logo (public, immutable)
POST /jobs/{id}/check-live, re-verify a listing when a user opens it
POST /jobs/{id}/save, save a job
POST /jobs/{id}/unsave, unsave a job
"""

import asyncio
import datetime
import html
import ipaddress
import logging
import re
import socket
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session
from sqlalchemy import and_, func, or_

from backend.db.database import get_db
from backend.db.models import ScrapedJob, JobStatus, ApplicationRecord, UserSavedJob
from backend.auth.dependencies import (
    get_verified_user_id,
    get_optional_user_id,
    get_admin_user_id,
    verify_cron_secret,
)
from backend.schemas.jobs import ScrapedJobOut, IngestBatchIn
from backend.schemas.application import ApplicationOut
from backend.services.description_extractor import (
    BROWSER_HEADERS,
    extract_description_from_html,
    extract_description_from_url,
)
from backend.services.location_parser import location_fields
from backend.services.na_location import job_country
from backend.services.logo_cache import (
    brand,
    company_key,
    load_branding,
    logo_quality,
    lookup_logo,
)
from backend.services.logo_resolver import company_website_url, resolve_logo
from backend.services.cross_source_dedup import (
    canonical_url,
    has_direct_twin,
    normalize_title,
    stands_in_for_twins,
)
from backend.services import jobs_sidebar, legacy_urls, platform_liveness
from backend.services.listing_freshness import (
    CLOSED_LISTING_STATUSES,
    HIDDEN_LISTING_STATUSES,
    LISTING_ACTIVE,
    _UNRECONCILABLE_BOARD_KEYS,
    record_liveness,
)
from backend.services.platform_liveness import ALIVE, DEAD, UNKNOWN, LivenessResult

logger = logging.getLogger(__name__)
router = APIRouter()


def _overlay_saved(db: Session, jobs: list[ScrapedJob], user_id: Optional[int]) -> list[ScrapedJobOut]:
    """Attach the requesting user's saved/liked status (UserSavedJob is per-user, not global).

    ``url`` is the apply link the client opens: a legacy SmartRecruiters row
    cron-backfill hasn't migrated yet answers with its posting page
    (legacy_urls.apply_url), never the careers-home redirect it stores."""
    saved_ids: set[int] = set()
    if user_id is not None and jobs:
        rows = (
            db.query(UserSavedJob.job_id)
            .filter(UserSavedJob.user_id == user_id, UserSavedJob.job_id.in_([j.id for j in jobs]))
            .all()
        )
        saved_ids = {row[0] for row in rows}
    # The global match_score column is a single-user leftover (0 on every row);
    # the real results are per user, banked by the fit engine (D-83).
    scores = jobs_sidebar.user_match_scores(db, user_id, [j.id for j in jobs])
    results = []
    for job in jobs:
        out = ScrapedJobOut.model_validate(job)
        out.saved = 1 if job.id in saved_ids else 0
        # A card carries engine data only: the AI-era per-job values still on
        # scraped_jobs (written from one user's résumé by the deleted AI paths)
        # are never served (D-109).
        out.match_score, out.match_band, out.match_outcome = 0, None, None
        out.experience_score = out.skill_score = out.industry_score = 0
        out.requirements_met = out.requirements_total = 0
        out.requirements_detail, out.match_summary, out.match_label = [], "", ""
        if job.id in scores:
            value, band, outcome = scores[job.id]
            # A number only for SCORED (D-86); the band alone for low confidence.
            out.match_score = value if outcome == "SCORED" and value is not None else 0
            out.match_band, out.match_outcome = band, outcome
            out.match_label = band or ""
        out.url = legacy_urls.apply_url(out.url)
        results.append(out)
    return results


def _escape_like(term: str) -> str:
    """Escape SQL LIKE wildcards to prevent DoS via expensive patterns."""
    return re.sub(r'([%_])', r'\\\1', term)


def _sanitize_description(text: str) -> str:
    """Sanitize HTML from job descriptions to prevent stored XSS."""
    import nh3
    # Strip all HTML tags, keeping only safe text content
    return nh3.clean(text, tags=set())


def _ip_is_internal(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    # not is_global catches what the named checks miss, carrier-grade NAT
    # (100.64.0.0/10) among them.
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        or not ip.is_global
    )


def _is_url_allowed(url: str) -> bool:
    """SSRF guard for a fetch made on a user's behalf: http(s) only, and the
    host (an IP literal or every address it resolves to) must be public.
    Resolves DNS, which blocks, so async callers run it in a thread."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return not _ip_is_internal(host)
        except ValueError:
            pass
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return False
        if not infos:
            return False
        return not any(_ip_is_internal(info[4][0]) for info in infos)
    except Exception:
        return False


@router.get("", response_model=list[ScrapedJobOut])
def list_jobs(
    status: Optional[JobStatus] = None,
    min_score: int = Query(0, ge=0),
    source: Optional[str] = None,
    saved: Optional[int] = None,
    search: Optional[str] = None,
    location: Optional[str] = None,
    country: Optional[str] = None,
    work_type: Optional[str] = None,
    role_category: Optional[str] = None,
    experience_level: Optional[str] = None,
    date_posted: Optional[str] = None,
    sort: Optional[str] = None,
    strong: Optional[int] = None,
    since: Optional[datetime.datetime] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user_id: Optional[int] = Depends(get_optional_user_id),
    db: Session = Depends(get_db),
):
    """List scraped jobs, optionally filtered by status, match score, source, country, work_type, etc.

    ``strong=1`` keeps only the user's strong matches (their own banked score,
    best first); ``since`` keeps jobs first seen at or after that time."""
    from backend.services.job_filters import (
        date_posted_cutoff,
        expand_experience_filter_values,
    )

    # min_score is applied below to the user's own engine score; the global
    # scraped_jobs.match_score is an AI-era value and is never read (D-109).
    q = db.query(ScrapedJob)
    q = q.filter(
        ScrapedJob.company.isnot(None),
        func.trim(ScrapedJob.company) != "",
        ScrapedJob.company != "Unknown",
    )

    if status:
        q = q.filter(ScrapedJob.status == status)
    if source:
        q = q.filter(ScrapedJob.source_platform == source)
    if saved:
        # "Liked" jobs are per-user (UserSavedJob), not a global flag on the job.
        # Hidden cross-source duplicates STAY visible here, a bookmark the
        # user made must not vanish because its twin arrived later.
        if user_id is None:
            return []
        q = q.join(UserSavedJob, UserSavedJob.job_id == ScrapedJob.id).filter(
            UserSavedJob.user_id == user_id
        )
    else:
        q = q.filter(ScrapedJob.duplicate_of.is_(None))
        # Freshness: listings the source took down (or that aged out) leave
        # the catalogue. `stale` stays visible, usually crawl lag, not death.
        q = q.filter(
            or_(
                ScrapedJob.listing_status.is_(None),
                ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES),
            )
        )
    if search:
        search_term = _escape_like(search.strip())
        if search_term:
            q = q.filter(
                or_(
                    ScrapedJob.title.ilike(f"%{search_term}%"),
                    ScrapedJob.company.ilike(f"%{search_term}%"),
                )
            )
    if location:
        from backend.services.location_parser import fold, location_tag_tokens

        # Tags arrive ";"-joined (a single tag may contain a comma, e.g.
        # "Ottawa, ON"); legacy clients joined plain city names with ",".
        tags = location.split(";") if ";" in location else location.split(",")
        tag_conditions = []
        for tag in tags:
            tag = tag.strip()
            if not tag:
                continue
            if fold(tag) == "remote":
                tag_conditions.append(
                    or_(
                        ScrapedJob.work_type == "remote",
                        ScrapedJob.location_search.like("%|remote|%"),
                        ScrapedJob.location.ilike("%remote%"),
                    )
                )
                continue
            tokens = location_tag_tokens(tag)
            if not tokens:
                continue
            # Exact token-boundary match ("|ottawa|" can't hit Toronto), with
            # a substring fallback for rows the backfill hasn't parsed yet.
            token_match = and_(
                *[ScrapedJob.location_search.like(f"%|{t}|%") for t in tokens]
            )
            legacy_fallback = and_(
                or_(
                    ScrapedJob.location_search.is_(None),
                    ScrapedJob.location_search == "",
                ),
                ScrapedJob.location.ilike(f"%{_escape_like(tokens[0])}%"),
            )
            tag_conditions.append(or_(token_match, legacy_fallback))
        if tag_conditions:
            q = q.filter(or_(*tag_conditions))

    if country:
        country_values = [c.strip().upper() for c in country.split(",") if c.strip()]
        if country_values:
            q = q.filter(ScrapedJob.country.in_(country_values))
    if work_type:
        work_type_values = [w.strip().lower() for w in work_type.split(",") if w.strip()]
        if work_type_values:
            q = q.filter(ScrapedJob.work_type.in_(work_type_values))
    if role_category:
        category_values = [c.strip() for c in role_category.split(",") if c.strip()]
        if category_values:
            from backend.services.role_classifier import expand_filter_values
            q = q.filter(ScrapedJob.role_category.in_(expand_filter_values(category_values)))
    if experience_level:
        level_values = [l.strip() for l in experience_level.split(",") if l.strip()]
        if level_values:
            q = q.filter(
                ScrapedJob.experience_level.in_(expand_experience_filter_values(level_values))
            )

    if since is not None:
        if since.tzinfo is not None:
            since = since.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        q = q.filter(func.coalesce(ScrapedJob.first_seen_at, ScrapedJob.scraped_at) >= since)

    # The user's own fit-engine result, for strong=1 and for sort=match. The
    # global scraped_jobs.match_score is 0 on every row. Banked rows only: this
    # request never builds the engine (fit_reads, D-107).
    from backend.db.models import JobMatchScore
    from backend.services import fit_reads

    user_score = None
    profile = (
        jobs_sidebar.scoring_resume(db, user_id)
        if user_id is not None and (strong or sort == "match" or min_score > 0)
        else None
    )
    fingerprints = jobs_sidebar.serving_fingerprints(db, user_id, profile) if profile is not None else []
    if strong or min_score > 0:
        # strong=1: a confident Strong match (outcome SCORED, band Strong, D-85).
        # min_score: the user's own SCORED engine score at or above it; a NULL
        # score (not scored, low confidence, gate fail, review) never passes.
        if profile is None or not fingerprints:
            return []
        q = q.join(JobMatchScore, JobMatchScore.job_id == ScrapedJob.id).filter(
            JobMatchScore.user_id == user_id,
            JobMatchScore.resume_fingerprint.in_(fingerprints),
        )
        if strong:
            q = q.filter(fit_reads.strong_match())
        if min_score > 0:
            q = q.filter(
                JobMatchScore.outcome == fit_reads.OUTCOME_SCORED,
                JobMatchScore.score.isnot(None),
                JobMatchScore.score >= min_score,
            )
        user_score = JobMatchScore.score
    elif profile is not None:
        q = q.outerjoin(
            JobMatchScore,
            and_(
                JobMatchScore.job_id == ScrapedJob.id,
                JobMatchScore.user_id == user_id,
                JobMatchScore.resume_fingerprint.in_(fingerprints),
            ),
        )
        user_score = JobMatchScore.score

    effective_date = func.coalesce(ScrapedJob.posted_date, ScrapedJob.scraped_at)
    cutoff = date_posted_cutoff(date_posted or "")
    if cutoff is not None:
        q = q.filter(effective_date >= cutoff)

    # id tiebreaker: bulk inserts share timestamps, and ties without a total
    # order make pagination unstable (the same job shows up on two pages).
    if user_score is not None and (strong or sort == "match"):
        # NULL scores (not scored, low confidence, gate fail, needs review) after
        # every scored job, never as a low score (D-86); ties newest first.
        q = q.order_by(
            *fit_reads.score_order(user_score),
            effective_date.desc().nullslast(),
            ScrapedJob.id.desc(),
        )
    else:
        # Also sort=match without a résumé: there is no score of this user's to
        # sort by, so newest first (never the AI-era global column).
        q = q.order_by(effective_date.desc().nullslast(), ScrapedJob.id.desc())

    q = q.offset((page - 1) * page_size).limit(page_size)
    return _overlay_saved(db, q.all(), user_id)


@router.post("/create")
def create_job(
    title: str,
    company: str,
    location: str,
    url: str,
    source_platform: str = "linkedin",
    experience_level: str = "new_grad",
    work_type: str = "onsite",
    country: str = "CA",
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Create a new job listing (admin only, used by scrapers to push jobs)."""
    # Dedup by URL. Query the id, not the entity: loading the row would pull its
    # ~1.9 KB description over the wire just to read back an id. Scrapers call
    # this once per job, hourly, and nearly every call is a duplicate.
    existing = db.query(ScrapedJob.id).filter(ScrapedJob.url == url).first()
    if existing:
        return {"status": "duplicate", "id": existing.id}

    resolved_logo, resolved_domain = resolve_logo(company)
    job = ScrapedJob(
        title=title,
        company=company,
        location=location,
        url=url,
        description="",
        source_platform=source_platform,
        easy_apply=0,
        work_type=work_type,
        role_category="",
        country=country,
        experience_level=experience_level,
        company_logo=resolved_logo,
        company_domain=resolved_domain,
        title_norm=normalize_title(title),
        **location_fields(location),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"status": "created", "id": job.id}


@router.post("/ingest-batch")
def ingest_batch(
    batch: IngestBatchIn,
    _cron: None = Depends(verify_cron_secret),
    db: Session = Depends(get_db),
):
    """Bulk-ingest scraped jobs (cron-secret auth, for the JobSpy/LinkedIn
    scraper scripts).

    The per-job /jobs/create path costs one request + one query per job and is
    admin-JWT-only, which the scripts can't send, every call 401'd since
    809c80f. This dedupes the whole batch with ONE url query and bulk-inserts
    the rest.

    A plainly senior title (ats_scraper.HARD_SENIOR: "Senior HR Specialist",
    "Director of Engineering", "Software Engineer (L5)") is not stored and
    counts as ``senior_skipped``. Only the hard markers: the scripts'
    searches are already scoped to entry level, so the crawler's weak-tier
    and frontline rules don't apply to these titles.
    """
    from sqlalchemy.exc import IntegrityError
    from backend.services.ats_scraper import HARD_SENIOR
    from backend.services.role_classifier import classify as classify_role
    from backend.services.structured_extraction import detect_employment_type

    received = len(batch.jobs)
    skipped = 0
    senior_skipped = 0
    duplicates = 0
    unique = {}
    for job in batch.jobs:
        url = canonical_url((job.url or "").strip())
        if not url:
            skipped += 1
            continue
        if HARD_SENIOR.search(job.title or ""):
            senior_skipped += 1
            continue
        if url in unique:
            duplicates += 1
            continue
        unique[url] = job

    existing: set[str] = set()
    if unique:
        rows = db.query(ScrapedJob.url).filter(ScrapedJob.url.in_(unique.keys())).all()
        existing = {row[0] for row in rows}

    # Self-hosted logos + verified domains for the whole batch, one query.
    branding = load_branding(
        db, {job.company for url, job in unique.items() if url not in existing}
    )

    to_insert = []
    twins_skipped = 0
    for url, job in unique.items():
        if url in existing:
            duplicates += 1
            continue

        posted_date = None
        if job.posted_date:
            try:
                posted_date = datetime.datetime.fromisoformat(job.posted_date)
            except (ValueError, TypeError):
                posted_date = None

        # The employer website the scraper saw (Indeed's corporateWebsite via
        # JobSpy) beats the name guess, which is wrong for most employers.
        # (The job URL itself is the LinkedIn/Indeed page, never a hint.)
        company_url = company_website_url(job.company_url)
        resolved_logo, resolved_domain = resolve_logo(job.company, company_url)
        # Prefer a real logo/domain the scraper captured (e.g. LinkedIn's
        # media.licdn.com company image) over the name-guessed favicon, which
        # frequently resolves to a wrong domain and renders as a letter avatar.
        supplied_logo = (job.company_logo or "").strip()
        if not (supplied_logo.lower().startswith(("https://", "http://"))
                and logo_quality(supplied_logo) > 0):
            supplied_logo = ""
        company_logo = supplied_logo or resolved_logo
        company_domain = (job.company_domain or "").strip().lower() or resolved_domain
        company_logo, company_domain = brand(branding, job.company, company_logo, company_domain)
        fields = location_fields(job.location)
        # The country is derived here, not trusted from the client: the
        # LinkedIn script's ", ca" test matched ", canada" and sent "US" for
        # 653 visible Canadian rows. The client's value only stands when the
        # location says nothing either way, or names both countries, or
        # rests on a bare "CA" (JobSpy's "Remote, CA" is ISO Canada).
        sent = (job.country or "").strip().upper()
        country = job_country(job.location or "", current=sent, fallback=sent)

        # A direct (ats/github) row for this employer+title+city already in
        # the catalogue makes this aggregator copy redundant, skip it.
        if job.source_platform in ("linkedin", "indeed") and has_direct_twin(
            db,
            company=job.company,
            company_domain=company_domain,
            title=job.title,
            city=fields["city"],
            country=country,
        ):
            twins_skipped += 1
            duplicates += 1
            continue

        ingested_at = datetime.datetime.utcnow()
        to_insert.append(
            ScrapedJob(
                title=job.title,
                company=job.company,
                location=job.location,
                url=url,
                description="",
                source_platform=job.source_platform,
                posted_date=posted_date,
                easy_apply=0,
                work_type=job.work_type,
                role_category=classify_role(job.title),
                country=country,
                experience_level=job.experience_level,
                company_logo=company_logo,
                company_domain=company_domain,
                company_url=company_url,
                title_norm=normalize_title(job.title),
                # Aggregator rows: nobody re-confirms them, so they enter as
                # low-trust and age out via sweep_aggregator_expiry. Rich
                # extraction happens in cron-backfill once a description lands.
                first_seen_at=ingested_at,
                last_seen_at=ingested_at,
                source_trust="low" if job.source_platform in ("linkedin", "indeed") else "medium",
                employment_type=detect_employment_type(job.title),
                **fields,
            )
        )

    created = 0
    if to_insert:
        db.add_all(to_insert)
        try:
            db.commit()
            created = len(to_insert)
        except IntegrityError:
            # Race: another writer landed one of these URLs between our dedup
            # query and the commit. Retry row by row so the rest still insert.
            db.rollback()
            for row in to_insert:
                db.add(row)
                try:
                    db.commit()
                    created += 1
                except IntegrityError:
                    db.rollback()
                    duplicates += 1

    return {
        "received": received,
        "created": created,
        "duplicates": duplicates,
        "cross_source_twins_skipped": twins_skipped,
        "skipped": skipped,
        "senior_skipped": senior_skipped,
    }


# cron-backfill's wall clock. Vercel kills the function at 300 s, the moment
# the workflow's curl gives up too, so the whole pass is planned to finish by
# BACKFILL_BUDGET_S and leave room for a cold start. Descriptions get the
# first DESCRIPTION_BUDGET_S; the logo harvest gets what is left (at most
# logo_cache.HARVEST_BUDGET_S), minus POST_HARVEST_RESERVE_S for the steps
# after its network budget that have no clock of their own. Measured on prod
# (2026-09-28): a last bogus-domain probe (<=15 s over), the harvest's writes
# (~15 s for 150 employers), the twin sweep, the remaining count and the
# response (~13 s). The rest covers the twin sweep's burst when many winners
# close at once (release_from_closed_winners has no cap: ~35 s for 440 rows).
BACKFILL_BUDGET_S = 240.0
DESCRIPTION_BUDGET_S = 75.0
POST_HARVEST_RESERVE_S = 75.0
# Per job, all its requests together (the page, redirects, up to three ATS
# API calls). httpx's 12 s applies to each connect/read, not to the whole.
DESCRIPTION_FETCH_TIMEOUT_S = 30.0


@router.post("/cron-backfill")
async def cron_backfill(
    batch_size: int = Query(100, ge=1, le=150),
    _cron: None = Depends(verify_cron_secret),
    db: Session = Depends(get_db),
):
    """Bounded repair pass: fetch missing descriptions (<=3 attempts/job,
    direct-URL rows before login-walled LinkedIn/Indeed ones), fill structured
    location + company_domain, and harvest self-hosted logos for employers
    that have none yet (services/logo_cache.py). The network phases are
    bounded, and the pass is planned to finish inside BACKFILL_BUDGET_S.
    DB-only steps also heal visible rows whose country contradicts their
    location (listing_freshness.repair_country) and move legacy
    SmartRecruiters rows to their posting URL
    (legacy_urls.migrate_legacy_smartrecruiters)."""
    import httpx
    from backend.services import listing_freshness, logo_cache

    loop = asyncio.get_running_loop()
    started = loop.time()

    needs_description = or_(
        ScrapedJob.description.is_(None),
        func.length(func.trim(ScrapedJob.description)) < 50,
    )
    # LinkedIn/Indeed pages are login-walled og-snippets at best; spend the
    # batch on direct URLs first. false < true in both SQLite and Postgres.
    is_aggregator = or_(
        ScrapedJob.url.ilike("%linkedin.com%"),
        ScrapedJob.url.ilike("%indeed.com%"),
    )
    jobs = (
        db.query(ScrapedJob)
        .filter(
            needs_description,
            func.coalesce(ScrapedJob.desc_fetch_attempts, 0) < 3,
            ScrapedJob.duplicate_of.is_(None),  # hidden twins aren't worth fetches
        )
        .order_by(is_aggregator.asc(), ScrapedJob.id.desc())
        .limit(batch_size)
        .all()
    )

    descriptions_fixed = locations_fixed = domains_fixed = 0
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=12, headers=BROWSER_HEADERS
    ) as client:
        # Phase 1: concurrent HTTP only, the Session is not thread/task safe,
        # so every DB mutation happens sequentially in phase 2.
        semaphore = asyncio.Semaphore(6)
        description_deadline = started + DESCRIPTION_BUDGET_S

        async def fetch(job_id: int, url: str) -> tuple[int, str | None]:
            # None: the budget ran out before this job had a fair try. It
            # costs no attempt and keeps its place in the queue.
            async with semaphore:
                remaining = description_deadline - loop.time()
                if remaining <= 1:
                    return job_id, None
                cut_by_budget = remaining < DESCRIPTION_FETCH_TIMEOUT_S
                try:
                    return job_id, await asyncio.wait_for(
                        extract_description_from_url(client, url),
                        timeout=min(remaining, DESCRIPTION_FETCH_TIMEOUT_S),
                    )
                except asyncio.TimeoutError:
                    # The pass ran out, or the host was too slow (an attempt).
                    return job_id, None if cut_by_budget else ""
                except Exception:
                    return job_id, ""

        results = await asyncio.gather(
            *[fetch(job.id, job.url) for job in jobs if job.url]
        )
        fetched = dict(results)
        descriptions_deferred = sum(1 for text in fetched.values() if text is None)

        from backend.services.structured_extraction import (
            compute_raw_hash,
            detect_employment_type,
            detect_visa_sponsorship,
            extract_skills,
            parse_salary,
        )

        # The logo store knows verified domains, and guesses it proved bogus
        # that the repair below must not plant again.
        branding = load_branding(
            db, {job.company for job in jobs if not (job.company_domain or "")}
        )

        for job in jobs:
            # A row with no URL has nothing to fetch: it spends its attempts.
            text = fetched.get(job.id, "")
            if text is not None:
                job.desc_fetch_attempts = (job.desc_fetch_attempts or 0) + 1
            if text:
                job.description = _sanitize_description(text)
                job.description_sections = None
                descriptions_fixed += 1
                # A description just landed: the structured fields it feeds
                # (visa/skills/salary/type) can finally be extracted. A visa
                # answer the row already has (a list's no-sponsorship mark)
                # stays: the description only fills an unknown.
                if (job.visa_sponsorship or "unknown") == "unknown":
                    job.visa_sponsorship = detect_visa_sponsorship(job.description)
                job.skills = extract_skills(job.title, job.description) or None
                if not job.employment_type:
                    job.employment_type = detect_employment_type(job.title, job.description)
                if not job.salary_min:
                    parsed = parse_salary(job.salary_range or "") or parse_salary(job.description)
                    if parsed:
                        (job.salary_min, job.salary_max,
                         job.salary_currency, job.salary_period) = parsed
                job.raw_hash = compute_raw_hash(job.title, job.location or "",
                                                job.description, job.salary_range or "")
            if not (job.location_search or "") and (job.location or ""):
                for key, value in location_fields(job.location).items():
                    setattr(job, key, value)
                locations_fixed += 1
            if not (job.company_domain or ""):
                logo, domain = brand(
                    branding, job.company, *resolve_logo(job.company, job.company_url)
                )
                if domain:
                    job.company_domain = domain
                    if not (job.company_logo or "") or "icon.horse" in (job.company_logo or ""):
                        job.company_logo = logo
                    domains_fixed += 1
        db.commit()

        # Country repair: visible rows whose stored country contradicts their
        # own location (653 Canadian LinkedIn rows stored "US", bare
        # Canadian cities cron-ats defaulted to "US"). DB-only, one bounded
        # column-only SELECT, so it takes well under a second; skipped when
        # the pass is already past its harvest mark, and it can only shorten
        # the harvest below (whose deadline is fixed from ``started``), never
        # push the pass past BACKFILL_BUDGET_S.
        harvest_deadline = started + BACKFILL_BUDGET_S - POST_HARVEST_RESERVE_S
        country_stats: dict = {"skipped": True}
        if loop.time() < harvest_deadline:
            try:
                country_stats = listing_freshness.repair_country(
                    db, limit=listing_freshness.REPAIR_COUNTRY_LIMIT,
                )
            except Exception:
                db.rollback()
                logger.exception("cron-backfill country repair failed")
                country_stats = {"error": True}

        # Legacy SmartRecruiters URLs (careers.smartrecruiters.com redirects
        # to the employer's careers home) move to the posting page, or hide
        # behind the row that already holds it. cron-ats only migrates rows
        # its crawl lists, and a page-capped board like BoschGroup never
        # lists most of them. DB-only and bounded like the country repair,
        # under the same harvest mark.
        legacy_sr_stats: dict = {"skipped": True}
        if loop.time() < harvest_deadline:
            try:
                legacy_sr_stats = legacy_urls.migrate_legacy_smartrecruiters(
                    db, limit=legacy_urls.MIGRATE_LEGACY_SR_LIMIT,
                )
            except Exception:
                db.rollback()
                logger.exception("cron-backfill legacy SmartRecruiters migration failed")
                legacy_sr_stats = {"error": True}

        # Phase 3: self-hosted logos. Rows of employers whose logo is already
        # stored get re-pointed at it; employers with none are harvested
        # (busiest first, misses on a 14d-per-attempt backoff) inside a
        # wall-clock budget, and a hit is propagated to all their rows. The
        # budget is whatever the pass has left, so a slow description phase
        # shortens the harvest instead of pushing the pass past Vercel's cap.
        try:
            logo_stats = await logo_cache.harvest_missing_logos(
                db, client, deadline=harvest_deadline,
            )
        except Exception:
            db.rollback()
            logger.exception("cron-backfill logo harvest failed")
            logo_stats = {"error": True}

    # New LinkedIn/Indeed rows that duplicate an existing better posting keep
    # arriving between sweeps; absorb them incrementally.
    from backend.services.cross_source_dedup import absorb_new_aggregator_rows
    try:
        twins_absorbed = absorb_new_aggregator_rows(db)
    except Exception:
        db.rollback()
        twins_absorbed = 0

    remaining = (
        db.query(ScrapedJob)
        .filter(
            needs_description,
            func.coalesce(ScrapedJob.desc_fetch_attempts, 0) < 3,
            ScrapedJob.duplicate_of.is_(None),
        )
        .count()
    )
    return {
        "processed": len(jobs),
        "descriptions_fixed": descriptions_fixed,
        "descriptions_deferred": descriptions_deferred,
        "locations_fixed": locations_fixed,
        "domains_fixed": domains_fixed,
        "country_repair": country_stats,
        "legacy_smartrecruiters": legacy_sr_stats,
        # Back-compat names: companies harvested, logos stored.
        "logo_domains_probed": logo_stats.get("companies_attempted", 0),
        "logos_harvested": logo_stats.get("stored", 0),
        "logo_harvest": logo_stats,
        "twins_absorbed": twins_absorbed,
        "remaining": remaining,
        "elapsed_s": round(loop.time() - started, 1),
    }


@router.post("/cron-freshness")
async def cron_freshness(
    _cron: None = Depends(verify_cron_secret),
    db: Session = Depends(get_db),
):
    """Hourly lifecycle sweep, the half of freshness that board crawls can't
    do: age out rows nothing re-confirms, check unconfirmed URLs against each
    platform's own API, keep ghost-risk scores current, and adopt legacy rows
    into board reconciliation."""
    import time

    from backend.services import listing_freshness
    from backend.services.platform_liveness import make_client

    adopted = listing_freshness.backfill_board_keys(db)
    stale = listing_freshness.sweep_stale(db)
    expired = listing_freshness.sweep_aggregator_expiry(db)
    # Before the checks: crawler rows no board reconciles ('' / 'unknown')
    # whose own title or location fails the filters leave the feed here,
    # and spend no probe. Bounded, column-only, no network.
    try:
        orphans_retired = listing_freshness.retire_unreconcilable_off_target(db)
    except Exception:
        db.rollback()
        logger.exception("cron-freshness orphan retire failed")
        orphans_retired = {"error": True}
    # LinkedIn/Indeed rows whose title is plainly senior (ingested before
    # ingest-batch vetoed them): same shape, no network.
    try:
        senior_retired = listing_freshness.retire_senior_aggregator_rows(db)
    except Exception:
        db.rollback()
        logger.exception("cron-freshness senior aggregator retire failed")
        senior_retired = {"error": True}

    # The probes share one wall-clock box (the "hourly" schedule really fires
    # ~6x/day, so budgets are big and hosts can hang). Each phase stops
    # starting checks at its mark; time a phase doesn't use flows to the next.
    # One liveness state for the whole run: host breakers, LinkedIn's
    # per-run budget and Ashby boards carry across the phases.
    started = time.monotonic()
    box = listing_freshness.VERIFY_TIME_BOX_SECONDS
    run_cache: dict = {}
    async with make_client() as client:
        verified = await listing_freshness.verify_stale_listings(
            db, client, limit=listing_freshness.STALE_VERIFY_BUDGET,
            deadline=started + box * 0.6, cache=run_cache,
        )
        # Active rows past a partial crawl's page cap (big Workday/SR boards)
        # or on boards we don't crawl: kill the dead ones before the 72h TTL.
        unconfirmed = await listing_freshness.verify_unconfirmed_active_listings(
            db, client, limit=listing_freshness.UNCONFIRMED_VERIFY_BUDGET,
            deadline=started + box * 0.8, cache=run_cache,
        )
        # GitHub lists re-publish closed postings and aged LinkedIn rows go
        # soft-dead (200 + "no longer accepting applications"); rotate through
        # them so dead apply links leave the catalogue instead of collecting
        # 404 complaints.
        recent = await listing_freshness.verify_recent_aggregator_listings(
            db, client, limit=listing_freshness.RECENT_VERIFY_BUDGET,
            deadline=started + box, cache=run_cache,
        )
    verify_seconds = round(time.monotonic() - started, 1)

    # After the checks, so this run's probes count: terminal expiry only ends
    # a row a check has reached since its last positive evidence.
    terminal = listing_freshness.sweep_terminal_expiry(db)

    ghost = listing_freshness.score_ghost_risk(db)

    return {
        "board_keys_adopted": adopted,
        "marked_stale": stale,
        "expired": expired,
        "orphans_off_target": orphans_retired,
        "aggregator_senior_off_target": senior_retired,
        "terminal_expired": terminal,
        "stale_verified": verified,
        "unconfirmed_verified": unconfirmed,
        "recent_verified": recent,
        "verify_seconds": verify_seconds,
        "ghost_scoring": ghost,
    }


@router.get("/ingest-metrics")
def ingest_metrics(
    _cron: None = Depends(verify_cron_secret),
    db: Session = Depends(get_db),
):
    """Pipeline health snapshot: catalogue freshness, ingest volume, dedup
    rate, ghost flags, and the currently-broken boards (dead-letter view).
    Cron-secret auth so the workflow can log it every run."""
    from backend.db.models import SourceHealth
    from backend.services.listing_freshness import LISTING_ACTIVE, LISTING_OFF_TARGET
    from backend.services.source_health import FAILURE_THRESHOLD

    now = datetime.datetime.utcnow()
    day_ago = now - datetime.timedelta(days=1)
    week_ago = now - datetime.timedelta(days=7)

    by_status = dict(
        db.query(ScrapedJob.listing_status, func.count(ScrapedJob.id))
        .group_by(ScrapedJob.listing_status)
        .all()
    )
    by_trust = dict(
        db.query(ScrapedJob.source_trust, func.count(ScrapedJob.id))
        .filter(ScrapedJob.listing_status == LISTING_ACTIVE)
        .group_by(ScrapedJob.source_trust)
        .all()
    )
    ingested_24h = (
        db.query(ScrapedJob).filter(ScrapedJob.first_seen_at >= day_ago).count()
    )
    ingested_7d = (
        db.query(ScrapedJob).filter(ScrapedJob.first_seen_at >= week_ago).count()
    )
    removed_24h = (
        db.query(ScrapedJob)
        .filter(ScrapedJob.listing_status == "removed",
                ScrapedJob.listing_status_changed_at >= day_ago)
        .count()
    )
    # Rows the crawl (or the orphan sweep) retired because their own listing
    # fails the filters; by_listing_status carries the running total.
    off_target_24h = (
        db.query(ScrapedJob)
        .filter(ScrapedJob.listing_status == LISTING_OFF_TARGET,
                ScrapedJob.listing_status_changed_at >= day_ago)
        .count()
    )
    hidden_duplicates = (
        db.query(ScrapedJob).filter(ScrapedJob.duplicate_of.isnot(None)).count()
    )
    total_rows = db.query(ScrapedJob).count()

    active_q = db.query(ScrapedJob).filter(
        ScrapedJob.listing_status == LISTING_ACTIVE,
        ScrapedJob.duplicate_of.is_(None),
    )
    active_total = active_q.count()
    ghost_flagged = active_q.filter(ScrapedJob.ghost_risk_score >= 50).count()

    # Median active listing age without a percentile function (SQLite + PG).
    median_age_days = None
    if active_total:
        midpoint_first_seen = (
            db.query(ScrapedJob.first_seen_at)
            .filter(ScrapedJob.listing_status == LISTING_ACTIVE,
                    ScrapedJob.duplicate_of.is_(None),
                    ScrapedJob.first_seen_at.isnot(None))
            .order_by(ScrapedJob.first_seen_at.desc())
            .offset(active_total // 2)
            .limit(1)
            .scalar()
        )
        if midpoint_first_seen:
            median_age_days = (now - midpoint_first_seen).days

    # Only boards cron-ats still crawls. A board disabled or removed from the
    # registry keeps its source_health row (history, if it comes back) but no
    # crawl ever updates it again, so it would read as broken forever.
    from backend.data import company_registry

    crawled = {
        f"{platform}:{slug}" for platform, slug, _ in company_registry.load_companies()
    }
    failing = [
        row
        for row in (
            db.query(SourceHealth)
            .filter(SourceHealth.consecutive_failures > 0)
            .order_by(SourceHealth.consecutive_failures.desc())
            .all()
        )
        if row.board_key in crawled
    ]
    failing_boards = [
        {
            "board_key": row.board_key,
            "consecutive_failures": row.consecutive_failures,
            "last_error": row.last_error,
            "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
        }
        for row in failing[:20]
    ]
    boards_in_cooldown = sum(
        1 for row in failing if row.consecutive_failures >= FAILURE_THRESHOLD
    )

    return {
        "by_listing_status": by_status,
        "active_by_trust": by_trust,
        "ingested_24h": ingested_24h,
        "ingested_7d": ingested_7d,
        "removed_24h": removed_24h,
        "off_target_24h": off_target_24h,
        "hidden_duplicates": hidden_duplicates,
        "dedup_rate": round(hidden_duplicates / total_rows, 4) if total_rows else 0.0,
        "active_total": active_total,
        "ghost_flagged": ghost_flagged,
        "ghost_rate": round(ghost_flagged / active_total, 4) if active_total else 0.0,
        "median_active_age_days": median_age_days,
        "failing_boards": failing_boards,
        "boards_in_cooldown": boards_in_cooldown,
    }


@router.get("/stats")
def job_stats(
    user_id: Optional[int] = Depends(get_optional_user_id),
    db: Session = Depends(get_db),
):
    """Return aggregate job stats with breakdowns by country, work_type, role_category, experience_level."""
    # Exclude blank-company jobs, hidden duplicates, and dead listings to
    # match the listing query.
    _has_company = (
        ScrapedJob.company.isnot(None)
        & (func.trim(ScrapedJob.company) != "")
        & (ScrapedJob.company != "Unknown")
        & ScrapedJob.duplicate_of.is_(None)
        & or_(
            ScrapedJob.listing_status.is_(None),
            ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES),
        )
    )
    total = db.query(ScrapedJob).filter(_has_company).count()
    applied = db.query(ScrapedJob).filter(_has_company, ScrapedJob.status == JobStatus.APPLIED).count()
    new = db.query(ScrapedJob).filter(_has_company, ScrapedJob.status == JobStatus.NEW).count()
    saved_count = 0
    if user_id is not None:
        saved_count = (
            db.query(UserSavedJob)
            .join(ScrapedJob, ScrapedJob.id == UserSavedJob.job_id)
            .filter(UserSavedJob.user_id == user_id, _has_company)
            .count()
        )

    # The user's own SCORED engine scores on visible jobs; None when there are
    # none (anonymous, no résumé, nothing scored yet). Never the AI-era column.
    avg_match_score = None
    if user_id is not None:
        profile = jobs_sidebar.scoring_resume(db, user_id)
        fingerprints = jobs_sidebar.serving_fingerprints(db, user_id, profile)
        if fingerprints:
            from backend.db.models import JobMatchScore
            from backend.services import fit_reads

            avg_score = (
                db.query(func.avg(JobMatchScore.score))
                .join(ScrapedJob, ScrapedJob.id == JobMatchScore.job_id)
                .filter(
                    _has_company,
                    JobMatchScore.user_id == user_id,
                    JobMatchScore.resume_fingerprint.in_(fingerprints),
                    JobMatchScore.outcome == fit_reads.OUTCOME_SCORED,
                    JobMatchScore.score.isnot(None),
                )
                .scalar()
            )
            avg_match_score = round(avg_score) if avg_score is not None else None

    # Breakdown by country
    by_country = {}
    country_counts = (
        db.query(ScrapedJob.country, func.count(ScrapedJob.id))
        .filter(ScrapedJob.country != "")
        .group_by(ScrapedJob.country)
        .all()
    )
    for country, count in country_counts:
        by_country[country] = count

    # Breakdown by work_type
    by_work_type = {}
    work_type_counts = (
        db.query(ScrapedJob.work_type, func.count(ScrapedJob.id))
        .filter(ScrapedJob.work_type != "")
        .group_by(ScrapedJob.work_type)
        .all()
    )
    for wt, count in work_type_counts:
        by_work_type[wt] = count

    # Breakdown by role_category
    by_role_category = {}
    category_counts = (
        db.query(ScrapedJob.role_category, func.count(ScrapedJob.id))
        .filter(ScrapedJob.role_category != "")
        .group_by(ScrapedJob.role_category)
        .all()
    )
    for cat, count in category_counts:
        by_role_category[cat] = count

    # Breakdown by experience_level
    by_experience_level = {}
    level_counts = (
        db.query(ScrapedJob.experience_level, func.count(ScrapedJob.id))
        .filter(ScrapedJob.experience_level != "")
        .group_by(ScrapedJob.experience_level)
        .all()
    )
    for level, count in level_counts:
        by_experience_level[level] = count

    return {
        "total": total,
        "applied": applied,
        "new": new,
        "saved_count": saved_count,
        "avg_match_score": avg_match_score,
        "by_country": by_country,
        "by_work_type": by_work_type,
        "by_role_category": by_role_category,
        "by_experience_level": by_experience_level,
    }


@router.get("/sidebar")
def jobs_sidebar_data(
    since: Optional[datetime.datetime] = None,
    user_id: Optional[int] = Depends(get_optional_user_id),
    db: Session = Depends(get_db),
):
    """Everything the Jobs page's right rail shows: resume match snapshot,
    weekly progress, saved jobs closing soon, feed summary, top companies,
    autofill totals and the match-alert setting. Read-only, no LLM calls."""
    if since is not None and since.tzinfo is not None:
        since = since.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return jobs_sidebar.build_sidebar(db, user_id, since=since)


@router.get("/applications", response_model=list[ApplicationOut])
def list_applications(
    user_id: int = Depends(get_verified_user_id),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """List the current user's application records."""
    rows = (
        db.query(ApplicationRecord, ScrapedJob)
        .outerjoin(ScrapedJob, ScrapedJob.id == ApplicationRecord.job_id)
        .filter(ApplicationRecord.user_id == user_id)
        .order_by(ApplicationRecord.applied_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    results = []
    for record, job in rows:
        out = ApplicationOut.model_validate(record)
        if job:
            out.company_logo = job.company_logo
            out.company_domain = job.company_domain
            out.company_url = job.company_url
            out.listing_status = job.listing_status
        results.append(out)
    return results


@router.get("/cities")
def list_cities(
    country: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(12, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Distinct parsed cities (with counts) for filter autocomplete."""
    from backend.services.location_parser import fold

    query = (
        db.query(ScrapedJob.city, func.count(ScrapedJob.id))
        .filter(
            ScrapedJob.city.isnot(None),
            ScrapedJob.city != "",
            ScrapedJob.duplicate_of.is_(None),
            or_(
                ScrapedJob.listing_status.is_(None),
                ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES),
            ),
        )
    )
    if country:
        query = query.filter(ScrapedJob.country == country.strip().upper())
    if q and q.strip():
        query = query.filter(ScrapedJob.city.like(f"{fold(q)}%"))
    rows = (
        query.group_by(ScrapedJob.city)
        .order_by(func.count(ScrapedJob.id).desc())
        .limit(limit)
        .all()
    )
    return [
        {"city": " ".join(w.capitalize() for w in city.split(" ")), "count": count}
        for city, count in rows
    ]


_LOGO_FILE = re.compile(r"^([0-9a-f]{40})\.(png|svg)$")
_LOGO_MEDIA_TYPES = {"png": "image/png", "svg": "image/svg+xml"}


@router.get("/logo/{name}")
def serve_company_logo(name: str, db: Session = Depends(get_db)):
    """A self-hosted company logo by content hash (services/logo_cache.py).

    Public (feed cards load it as a plain <img>) and immutable: the hash
    changes whenever the image does, so browsers and the CDN keep it for a
    year and Neon serves each logo about once per edge. SVGs are sanitized at
    harvest and additionally sandboxed here.
    """
    from fastapi import Response
    from backend.db.models import CompanyLogo

    match = _LOGO_FILE.match(name or "")
    if not match:
        raise HTTPException(status_code=404, detail="Logo not found.")
    sha, ext = match.groups()
    row = (
        db.query(CompanyLogo.data, CompanyLogo.fmt)
        .filter(CompanyLogo.sha == sha, CompanyLogo.status == "ok")
        .first()
    )
    if row is None or not row.data or (row.fmt or "png") != ext:
        raise HTTPException(status_code=404, detail="Logo not found.")
    headers = {
        "Cache-Control": "public, max-age=31536000, s-maxage=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
    }
    if ext == "svg":
        headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return Response(content=bytes(row.data), media_type=_LOGO_MEDIA_TYPES[ext], headers=headers)


# duplicate_of points straight at the surviving row by construction; the cap
# only stops a corrupted chain or cycle from looping.
_MAX_TWIN_HOPS = 3


def _canonical_twin_id(db: Session, job_id: int, duplicate_of: Optional[int]) -> int:
    """The visible row a hidden cross-source duplicate points at, following
    duplicate_of column-only, or ``job_id`` itself when it isn't a duplicate,
    its twin is gone, or its twin has closed and can't answer for it
    (cross_source_dedup.stands_in_for_twins): a live LinkedIn repost hidden
    behind an older, since-expired copy must open as itself, not as the
    closed row."""
    seen = {job_id}
    target, next_id = job_id, duplicate_of
    status, source, url = None, "", ""
    for _ in range(_MAX_TWIN_HOPS):
        if not next_id or next_id in seen:
            break
        hop = (
            db.query(ScrapedJob.id, ScrapedJob.duplicate_of, ScrapedJob.listing_status,
                     ScrapedJob.source_platform, ScrapedJob.url)
            .filter(ScrapedJob.id == next_id)
            .first()
        )
        if hop is None:
            break
        seen.add(hop.id)
        target, next_id = hop.id, hop.duplicate_of
        status, source, url = hop.listing_status, hop.source_platform, hop.url
    if target != job_id and not stands_in_for_twins(status, source or "", url or ""):
        return job_id
    return target


@router.get("/{job_id}", response_model=ScrapedJobOut)
def get_job(
    job_id: int,
    user_id: Optional[int] = Depends(get_optional_user_id),
    db: Session = Depends(get_db),
):
    """Get a single job by ID.

    A hidden cross-source duplicate answers with its visible twin (a match
    email or a shared link to the LinkedIn copy lands on the row the feed
    shows, not on a hidden one), so the returned id can differ from the one
    asked for. A twin that has closed answers only when it still speaks for
    the duplicate (a direct row its board removed); otherwise the duplicate
    answers as itself, with its own status. listing_status comes back as
    stored, closed rows included, so the client can show the closed state.
    """
    job = db.query(ScrapedJob).filter(ScrapedJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.duplicate_of:
        canonical_id = _canonical_twin_id(db, job.id, job.duplicate_of)
        if canonical_id != job.id:
            canonical = db.query(ScrapedJob).filter(ScrapedJob.id == canonical_id).first()
            if canonical is not None:
                job = canonical
    return _overlay_saved(db, [job], user_id)[0]


# ─── Click-time liveness ─────────────────────────────────────────────────────

# The database answers without a probe only when what it holds is
# conclusive: a closed row (dead), or an active row its board (or the
# platform's API) vouched for this recently (alive; see _vouched_for). A
# recent probe that learned nothing is not an answer, so it doesn't stop a
# new one.
CHECK_LIVE_SEEN_FRESH = datetime.timedelta(hours=24)
# Per request. The detail panel never waits on this, but it must still end.
CHECK_LIVE_TIMEOUT_S = 8.0
# Per user, real probes only. The feed asks once per job per session, so a
# person reading it never gets close; a script walking job ids does.
CHECK_LIVE_PER_MINUTE = 30
CHECK_LIVE_PER_DAY = 500


def _naive_utc(value: Optional[datetime.datetime]) -> Optional[datetime.datetime]:
    """Stored timestamps are naive UTC; tolerate an aware one from a driver."""
    if value is not None and value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


def _vouched_for(row, now: datetime.datetime) -> bool:
    """Whether the row's last_seen_at is a confirmation from the last
    CHECK_LIVE_SEEN_FRESH, not just a timestamp.

    It is one on a row its board reconciles (the crawl bumps it only while
    the board lists the URL) and on a row whose latest probe was a platform
    confirmation (record_liveness and the verify sweeps stamp last_seen_at
    and last_probed_at with the same `now` on an authoritative alive).
    Aggregator rows (LinkedIn, Indeed, GitHub lists) get last_seen_at at
    insert and no board ever moves it; a later probe that learned nothing
    stamps only last_probed_at, so it leaves last_seen_at behind it."""
    seen_at = _naive_utc(row.last_seen_at)
    if seen_at is None or now - seen_at >= CHECK_LIVE_SEEN_FRESH:
        return False
    if (row.board_key or "") not in _UNRECONCILABLE_BOARD_KEYS:
        return True
    probed_at = _naive_utc(row.last_probed_at)
    return probed_at is not None and seen_at >= probed_at


def _live_answer(job_id: int, listing_status: str, verdict: str) -> dict:
    return {"id": job_id, "listing_status": listing_status, "verdict": verdict}


def _enforce_check_live_limits(db: Session, request: Request, user_id: int) -> None:
    """Per-user caps on click-time probes, on the same database-backed
    counters the AI routes use (an in-memory count would reset per serverless
    instance). Answers served from stored state are free and never counted."""
    from backend.services import usage_limiter

    if not usage_limiter._enabled():
        return
    identity = usage_limiter.client_identity(request, user_id)
    for name, limit, window in (
        ("live_min", CHECK_LIVE_PER_MINUTE, 60),
        ("live_day", CHECK_LIVE_PER_DAY, 86_400),
    ):
        retry_after = usage_limiter._hit(db, name, identity, limit, window)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )


def _probe_allowed(url: str) -> bool:
    """SSRF guard for a probe a user triggers: public http(s) hosts only. A
    URL with no host is left to the liveness check, which calls it dead
    without making a request."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return True
    return _is_url_allowed(url)


async def _probe_listing(url: str, board_key: str = "") -> LivenessResult:
    if not await asyncio.to_thread(_probe_allowed, url):
        return LivenessResult(UNKNOWN, "url_not_allowed")
    # make_client refuses every hop (redirects included) to a non-public
    # address; the check above only saw the first URL.
    async with platform_liveness.make_client() as client:
        return await platform_liveness.check_listing(client, url, board_key=board_key)


def _learned_nothing(result: LivenessResult) -> bool:
    """A click-time probe that got no answer: rate-limited, a network miss,
    or never sent. Recording it would only push the row back in the sweeps'
    queue as if it had been checked."""
    return platform_liveness.is_miss(result) or result.reason == "url_not_allowed"


@router.post("/{job_id}/check-live")
async def check_job_live(
    job_id: int,
    request: Request,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Re-verify one listing when a user opens it.

    Asks the posting's own platform (services/platform_liveness.py) whether
    it is still open and applies the answer by the sweeps' rules
    (listing_freshness.record_liveness): dead → removed, an authoritative
    alive → last_seen_at (and a stale row revives), anything else only
    stamps last_probed_at. The database answers without a request only when
    it is conclusive: a closed row is dead, an active row its board or the
    platform confirmed within CHECK_LIVE_SEEN_FRESH is alive (a fresh
    aggregator row nothing has confirmed is probed). A probe that got no
    answer (rate limit, network miss, timeout past CHECK_LIVE_TIMEOUT_S) is
    verdict unknown, never an error, and is not recorded.

    Returns {"id", "listing_status", "verdict"} (alive | dead | unknown).
    """
    row = (
        db.query(
            ScrapedJob.id, ScrapedJob.url, ScrapedJob.listing_status,
            ScrapedJob.last_seen_at, ScrapedJob.last_probed_at, ScrapedJob.board_key,
        )
        .filter(ScrapedJob.id == job_id)
        .first()
    )
    if row is None:
        # Not FastAPI's bare "Not Found": the client reads that as "this
        # endpoint doesn't exist" and stops asking for the session.
        raise HTTPException(status_code=404, detail="Job not found.")

    status = row.listing_status or LISTING_ACTIVE
    # Closed only: an off_target row is hidden from the feed but still open
    # on its board (a saved or deep-linked one), so it is probed like any row
    # instead of being called dead, which the client shows as "removed".
    if status in CLOSED_LISTING_STATUSES:
        return _live_answer(row.id, status, DEAD)

    if status == LISTING_ACTIVE and _vouched_for(row, datetime.datetime.utcnow()):
        return _live_answer(row.id, status, ALIVE)

    _enforce_check_live_limits(db, request, user_id)

    try:
        result = await asyncio.wait_for(_probe_listing(row.url or "", row.board_key or ""),
                                        CHECK_LIVE_TIMEOUT_S)
    except asyncio.TimeoutError:
        result = LivenessResult(UNKNOWN, "timeout")
    except Exception as exc:
        logger.warning("check-live probe failed for job %s: %s", job_id, exc)
        result = LivenessResult(UNKNOWN, "error")

    if not _learned_nothing(result):
        try:
            status = record_liveness(db, row.id, status, result)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("check-live could not record job %s: %s", job_id, exc)
    logger.info("check-live job %s: %s (%s), now %s", job_id, result.verdict, result.reason, status)
    return _live_answer(row.id, status, result.verdict)


async def _refuse_non_public_request(request) -> None:
    """Request hook on fetch-details' client. httpx runs it for every request
    the client sends: each redirect hop, and the follow-ups the description
    extractor and the platform check make on the same client. Checking only
    the stored URL let a public page 302 the server into loopback, a private
    network or cloud metadata, and the landing page's text was then saved as
    the description."""
    import httpx

    if not await asyncio.to_thread(_is_url_allowed, str(request.url)):
        raise httpx.RequestError(
            f"blocked request to a non-public address: {request.url.host}", request=request,
        )


def _details_client(transport=None):
    """The page fetch fetch-details makes (tests pass ``transport``). Every
    request it sends, redirect hops included, passes the SSRF guard."""
    import httpx

    options: dict = {
        "follow_redirects": True,
        "timeout": 15,
        "headers": BROWSER_HEADERS,
        "event_hooks": {"request": [_refuse_non_public_request]},
    }
    if transport is not None:
        options["transport"] = transport
    return httpx.AsyncClient(**options)


# LinkedIn's own company image on a guest job page. og:image is never used as
# a logo: on most job pages it is a banner, a job card or the ATS vendor's art.
_LINKEDIN_LOGO_TAG_RE = re.compile(r"<img\b[^>]*\bartdeco-entity-image\b[^>]*>", re.IGNORECASE)
_IMG_URL_ATTR_RE = re.compile(r'\b(?:data-delayed-url|src)="([^"]+)"', re.IGNORECASE)
_IMG_ALT_RE = re.compile(r'\balt="([^"]*)"', re.IGNORECASE)


def _linkedin_company_logo(page: str, company: str) -> str:
    """The posting's own company logo from a LinkedIn guest job page. The
    top card's images carry the company name as alt text; any other page
    shape (a search list after an expired-job redirect shows dozens of other
    employers' logos) must not lend its first image to this company."""
    wanted = company_key(company)
    if not wanted:
        # No usable name (blank or a placeholder like 'nan'): nothing to
        # match the alt text against, so no image can be proven ours.
        return ""
    for tag in _LINKEDIN_LOGO_TAG_RE.findall(page):
        alt = _IMG_ALT_RE.search(tag)
        if company_key(html.unescape(alt.group(1)) if alt else "") != wanted:
            continue
        for raw in _IMG_URL_ATTR_RE.findall(tag):
            url = html.unescape(raw)
            # A trusted licdn company logo, never a poster's profile photo
            # or the ghost placeholder.
            if "company-logo" in url and logo_quality(url) >= 2:
                return url
    return ""


def _page_death_reason(status: int, final_url: str, text: str) -> str:
    """Why a fetched job page shows the posting is gone, or "" when it
    doesn't: 404/410, a landing on an error page (error=404, errortype=404,
    /errorpage), or a closed notice (LinkedIn's markers, or a dead phrase in
    the visible text). Bot walls (401/403/429/999) prove nothing, and Indeed,
    which walls every non-browser fetch, is never judged (as in
    platform_liveness)."""
    host = (urlparse(final_url).hostname or "").lower()
    if host == "indeed.com" or host.endswith(".indeed.com"):
        return ""
    if status in platform_liveness.DEAD_HTTP_STATUSES:
        return f"http_{status}"
    if status != 200:
        return ""
    if platform_liveness._ERROR_URL_RE.search(final_url):
        return "error_redirect"
    if host.endswith("linkedin.com") and (
        "expired_jd_redirect" in final_url
        or any(marker in text for marker in platform_liveness._LINKEDIN_DEAD_MARKERS)
    ):
        return "linkedin_closed"
    if platform_liveness.says_dead(text):
        return "body_closed"
    return ""


def _left_posting(original_url: str, final_url: str) -> bool:
    """A redirect that dropped the posting's own id: it landed on a careers
    home or a job list, not on the same job at a new address."""
    if final_url == original_url:
        return False
    token = platform_liveness._job_token(urlparse(original_url))
    return not token or token.lower() not in final_url.lower()


@router.post("/{job_id}/fetch-details")
async def fetch_job_details(
    job_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Fetch job description from the apply URL on-demand and cache it.

    The page fetch doubles as a liveness read. A posting whose page 404s,
    lands on an error page or shows a closed notice, or that redirected away
    from its own id to where the platform check (services/platform_liveness.py)
    calls it dead, is marked removed, and the answer carries ``dead: true``
    with the original apply URL. Handing back the redirect target instead is
    what turned Apply into a careers-homepage link; a redirect is only adopted
    as the apply URL when it kept the posting's id. A legacy SmartRecruiters
    URL is fetched and answered as its posting page (legacy_urls.apply_url).

    Logos: a missing or generated one is filled only from the self-hosted
    store (services/logo_cache.py) or LinkedIn's own company image. Job-page
    og:image and name-guessed logo services are never written; the harvester
    (cron-backfill Phase 3) covers the rest.
    """
    import json

    job = db.query(ScrapedJob).filter(ScrapedJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    def _listing_state() -> dict:
        status = job.listing_status or LISTING_ACTIVE
        # off_target is hidden, not closed: its apply link still works.
        return {"listing_status": status, "dead": status in CLOSED_LISTING_STATUSES}

    # A legacy SmartRecruiters row is read, and handed out, at its posting
    # page: the careers.smartrecruiters.com URL it stores bounces to the
    # employer's careers home. The stored URL is left to cron-backfill.
    url = legacy_urls.apply_url(job.url or "")
    if not url or not await asyncio.to_thread(_is_url_allowed, url):
        return {
            "id": job.id,
            "description": job.description or "",
            "apply_url": url,
            "company_logo": job.company_logo or "",
            **_listing_state(),
        }

    if job.description and len(job.description) > 50:
        if "This button displays the currently selected search type" not in job.description:
            return {
                "id": job.id,
                "description": job.description,
                "apply_url": url,
                "company_logo": job.company_logo,
                **_listing_state(),
            }
        job.description = ""
        db.commit()

    # Count this as a fetch attempt so the backfill cron stops retrying URLs
    # that fail here too.
    job.desc_fetch_attempts = (job.desc_fetch_attempts or 0) + 1
    db.commit()

    try:
        async with _details_client() as client:
            response = await client.get(url)
            text = response.text
            final_url = str(response.url)
            linkedin_url = "linkedin.com/jobs" in url or "linkedin.com/jobs" in final_url
            left_posting = _left_posting(url, final_url)

            reason = _page_death_reason(response.status_code, final_url, text)
            if not reason and left_posting and response.status_code == 200 and not linkedin_url:
                # Bounced off its own id: a closed posting's careers-home
                # landing, or (SmartRecruiters) a live one's. Only the
                # platform can tell which.
                try:
                    verdict = await asyncio.wait_for(
                        platform_liveness.check_listing(client, url), CHECK_LIVE_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    verdict = LivenessResult(UNKNOWN, "timeout")
                if verdict.verdict == DEAD:
                    reason = verdict.reason
            if reason:
                status = record_liveness(
                    db, job.id, job.listing_status or LISTING_ACTIVE, LivenessResult(DEAD, reason),
                )
                db.commit()
                logger.info("fetch-details: job %s is gone (%s)", job_id, reason)
                return {
                    "id": job.id,
                    "description": job.description or "",
                    "apply_url": url,
                    "company_logo": job.company_logo or "",
                    "listing_status": status,
                    "dead": True,
                }

            description = await extract_description_from_html(client, url, text, final_url)
            apply_url = url if (linkedin_url or left_posting) else final_url

            if linkedin_url:
                if not job.company or job.company.strip() == "":
                    og_title_match = re.search(
                        r'<meta\s+property="og:title"\s+content="([^"]*)"',
                        text, re.IGNORECASE,
                    )
                    if og_title_match:
                        og_title = og_title_match.group(1)
                        at_match = re.search(r'\s+at\s+(.+?)(?:\s*\||\s*-|\s*$)', og_title)
                        hiring_match = re.search(r'^(.+?)\s+hiring\s+', og_title)
                        if at_match:
                            job.company = at_match.group(1).strip()
                        elif hiring_match:
                            job.company = hiring_match.group(1).strip()

            if logo_quality(job.company_logo) == 0:
                logo = (lookup_logo(db, job.company) if job.company else None) or ""
                if not logo and linkedin_url:
                    logo = _linkedin_company_logo(text, job.company or "")
                if logo:
                    job.company_logo = logo

            next_match = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
                text, re.DOTALL,
            )
            if next_match:
                try:
                    next_data = json.loads(next_match.group(1))
                    job_result = (
                        next_data.get("props", {})
                        .get("pageProps", {})
                        .get("dataSource", {})
                        .get("jobResult", {})
                    ) or {}
                    if job_result:
                        logo = job_result.get("jdLogo", "")
                        if (isinstance(logo, str) and logo_quality(logo) > 0
                                and logo_quality(job.company_logo) == 0):
                            job.company_logo = logo
                        salary = job_result.get("salaryDesc", "")
                        if salary and not job.salary_range:
                            job.salary_range = salary[:255]
                        applicants = job_result.get("applicantsCount")
                        if isinstance(applicants, int) and applicants >= 0 and job.applicant_count is None:
                            job.applicant_count = applicants
                        work_model = (job_result.get("workModel") or "").lower()
                        if work_model:
                            if "remote" in work_model:
                                job.work_type = "remote"
                            elif "hybrid" in work_model:
                                job.work_type = "hybrid"
                            elif "site" in work_model or "office" in work_model:
                                job.work_type = "onsite"
                except (json.JSONDecodeError, KeyError, TypeError):
                    pass

            if description:
                job.description = _sanitize_description(description)
                job.description_sections = None  # re-structure the new text

            db.commit()

            return {
                "id": job.id,
                "description": job.description or "",
                "apply_url": apply_url,
                "company_logo": job.company_logo or "",
                "company": job.company or "",
                "company_domain": job.company_domain or "",
                "salary_range": job.salary_range or "",
                "applicant_count": job.applicant_count,
                "work_type": job.work_type or "",
                **_listing_state(),
            }
    except Exception as e:
        logger.warning(f"Failed to fetch details for job {job_id}: {e}")
        db.rollback()
        return {
            "id": job.id,
            "description": job.description or "",
            "apply_url": url,
            "company_logo": job.company_logo or "",
            **_listing_state(),
        }


@router.post("/{job_id}/structure-description")
async def structure_description(
    job_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Structured sections for a posting: the cached parse if one exists, else
    an LLM parse when JOB_STRUCTURE_AI is on (off by default: zero cost).

    Layout only (headings and bullets). No AI in job scoring or match signals (final
    review I-4): no skill list is asked for, stored or served, and a ``skills`` key in a
    row cached under the old prompt is dropped. The engine's breakdown is the detail
    page's only match signal."""
    import json
    from backend.services.llm import get_llm_service

    job = db.query(ScrapedJob).filter(ScrapedJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    if not job.description or len(job.description) < 50:
        return {"sections": [], "error": "No description available"}

    # Cache: proper column first, then the legacy company_description JSON hack
    # (rows structured before description_sections existed).
    if isinstance(job.description_sections, dict) and job.description_sections.get("sections"):
        return _layout_only(job.description_sections)
    if job.company_description and job.company_description.startswith("{"):
        try:
            cached = json.loads(job.company_description)
            if cached.get("sections"):
                cached = _layout_only(cached)
                job.description_sections = cached
                db.commit()
                return cached
        except (json.JSONDecodeError, TypeError):
            pass

    # The web app already parses the posting client-side the moment it opens;
    # this LLM pass only polishes that, at a paid call per job. Off unless
    # JOB_STRUCTURE_AI is set, and an empty answer tells the client to keep its
    # own parse.
    import os

    if (os.getenv("JOB_STRUCTURE_AI") or "").strip().lower() not in ("1", "true", "yes", "on"):
        return {"sections": [], "source": "client"}

    llm = get_llm_service()

    prompt = f"""Parse this job description into structured JSON sections. Return ONLY a JSON object:
{{
  "sections": [
    {{"title": "Responsibilities", "icon": "clipboard-list", "items": ["..."]}},
    {{"title": "Qualifications", "icon": "graduation-cap", "subsections": [
      {{"title": "Required", "items": ["..."]}},
      {{"title": "Preferred", "items": ["..."]}}
    ]}},
    {{"title": "Benefits", "icon": "gift", "items": ["..."]}},
    {{"title": "About the Company", "icon": "building", "items": ["..."]}}
  ],
  "experience_years": "2-4",
  "education": "BS in Computer Science"
}}

Rules:
- Preserve every bullet from the posting in the matching section; do not invent content.
- Qualifications MUST use Required/Preferred subsections when the posting distinguishes them; otherwise put everything under Required.
- Omit sections the posting does not contain. Keep items to one sentence.

Job Description:
{job.description[:6000]}"""

    try:
        response = await llm._generate(prompt, model="gpt-4o-mini", json_mode=True, op="jobs.structure_description")
        data = _layout_only(json.loads(response))
        if data.get("sections"):
            job.description_sections = data
            db.commit()
        return data
    except Exception as e:
        return {"sections": [], "error": str(e)}


def _layout_only(data: dict) -> dict:
    """A structured description without any LLM-made skill list (I-4)."""
    return {key: value for key, value in data.items() if key != "skills"}


@router.post("/{job_id}/save", response_model=ScrapedJobOut)
def save_job(
    job_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Save a job (bookmark it) for the current user."""
    job = db.query(ScrapedJob).filter(ScrapedJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    # Check if already saved
    existing = db.query(UserSavedJob).filter(
        UserSavedJob.user_id == user_id,
        UserSavedJob.job_id == job_id,
    ).first()
    if not existing:
        saved_entry = UserSavedJob(user_id=user_id, job_id=job_id)
        db.add(saved_entry)
        db.commit()
    return _overlay_saved(db, [job], user_id)[0]


@router.post("/{job_id}/unsave", response_model=ScrapedJobOut)
def unsave_job(
    job_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Unsave a job (remove bookmark) for the current user."""
    job = db.query(ScrapedJob).filter(ScrapedJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    db.query(UserSavedJob).filter(
        UserSavedJob.user_id == user_id,
        UserSavedJob.job_id == job_id,
    ).delete()
    db.commit()
    return _overlay_saved(db, [job], user_id)[0]


@router.post("/{job_id}/mark-applied", response_model=ApplicationOut)
def mark_applied(
    job_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Record that the current user applied to a job (manual apply confirmation)."""
    job = db.query(ScrapedJob).filter(ScrapedJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    job.status = JobStatus.APPLIED

    record = (
        db.query(ApplicationRecord)
        .filter(ApplicationRecord.user_id == user_id, ApplicationRecord.job_id == job_id)
        .first()
    )
    if record:
        record.applied_at = datetime.datetime.utcnow()
    else:
        record = ApplicationRecord(
            user_id=user_id,
            job_id=job_id,
            platform=job.source_platform or "linkedin",
            company=job.company,
            role=job.title,
            url=job.url,
            applied_at=datetime.datetime.utcnow(),
        )
        db.add(record)

    db.commit()
    db.refresh(record)
    return record


@router.post("/fix-empty-companies")
async def fix_empty_companies(
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Fix jobs with empty company names by extracting from LinkedIn or other sources."""
    import re
    import httpx

    # Columns only: whole rows would drag 50 descriptions over the wire just
    # to read a URL.
    jobs_with_empty_company = (
        db.query(ScrapedJob.id, ScrapedJob.url)
        .filter(ScrapedJob.company == "")
        .limit(50)
        .all()
    )

    fixed = 0
    for job in jobs_with_empty_company:
        company_name = ""

        # Try to extract company from LinkedIn job URL
        if "linkedin.com/jobs/view" in (job.url or ""):
            try:
                async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
                    resp = await client.get(job.url)
                    text = resp.text
                    # LinkedIn og:title format: "Company hiring Title in Location | LinkedIn"
                    og_match = re.search(r'property="og:title"[^>]*content="([^"]*)"', text)
                    if og_match:
                        og_title = og_match.group(1)
                        # Format: "Company hiring Job Title in Location | LinkedIn"
                        if " hiring " in og_title:
                            company_name = og_title.split(" hiring ")[0].strip()
                        elif " at " in og_title:
                            # Alternate format: "Job Title at Company | LinkedIn"
                            company_name = og_title.split(" at ")[1].split("|")[0].strip()
                    if not company_name:
                        # Try title tag: "Company hiring Title..."
                        title_match = re.search(r'<title>([^<]*)</title>', text)
                        if title_match:
                            title_text = title_match.group(1)
                            if " hiring " in title_text:
                                company_name = title_text.split(" hiring ")[0].strip()
            except Exception:
                pass

        if company_name:
            # The stored self-hosted logo and verified domain win over the
            # name-guessed favicon, the same rule every insert path follows.
            logo, domain = brand(
                load_branding(db, [company_name]), company_name, *resolve_logo(company_name)
            )
            db.query(ScrapedJob).filter(ScrapedJob.id == job.id).update(
                {"company": company_name, "company_logo": logo, "company_domain": domain},
                synchronize_session=False,
            )
            db.commit()
            fixed += 1

    return {"total_empty": len(jobs_with_empty_company), "fixed": fixed}


@router.post("/batch-fix-descriptions")
async def batch_fix_descriptions(
    batch_size: int = Query(20, ge=1, le=50),
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Batch fix jobs with missing or garbage descriptions.

    Processes LinkedIn and Greenhouse/Lever jobs that have empty or garbage descriptions.
    Prioritizes LinkedIn jobs (most common source for missing descriptions).
    """
    import re
    import json
    import httpx

    GARBAGE_PATTERNS = [
        "This button displays the currently selected search type",
        "Sign in to view more",
        "Join now to see",
    ]

    # Find jobs needing description fixes
    jobs_to_fix = (
        db.query(ScrapedJob)
        .filter(
            or_(
                ScrapedJob.description == "",
                ScrapedJob.description == None,
                ScrapedJob.description.ilike("%This button displays%"),
            )
        )
        .limit(batch_size)
        .all()
    )

    fixed = 0
    failed = 0
    results = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=15, headers=BROWSER_HEADERS) as client:
        for job in jobs_to_fix:
            try:
                description = await extract_description_from_url(client, job.url or "")
                if description:
                    job.description = _sanitize_description(description)
                    job.description_sections = None
                    db.commit()
                    fixed += 1
                    results.append({"id": job.id, "company": job.company, "status": "fixed"})
                else:
                    failed += 1
                    results.append({"id": job.id, "company": job.company, "status": "no_description_found"})
            except Exception as e:
                failed += 1
                results.append({"id": job.id, "company": job.company, "status": f"error: {str(e)[:50]}"})

    return {
        "total_processed": len(jobs_to_fix),
        "fixed": fixed,
        "failed": failed,
        "remaining": db.query(ScrapedJob).filter(
            or_(
                ScrapedJob.description == "",
                ScrapedJob.description == None,
            )
        ).count(),
    }


@router.post("/batch-enrich-salaries")
async def batch_enrich_salaries(
    batch_size: int = Query(50, ge=1, le=200),
    _admin: int = Depends(get_admin_user_id),
    db: Session = Depends(get_db),
):
    """Enrich jobs with salary data from Levels.fyi and known company ranges.

    Uses a mapping of known intern/new-grad salary ranges for major tech companies.
    For companies not in the mapping, attempts to extract from job descriptions.
    """
    import re

    # Known intern/new-grad hourly rates (CAD/USD) from Levels.fyi and public data
    SALARY_MAP = {
        # Big Tech
        "google": "$45-55/hr",
        "amazon": "$40-50/hr",
        "microsoft": "$40-52/hr",
        "apple": "$40-55/hr",
        "meta": "$45-55/hr",
        # Mid-size Tech
        "shopify": "$35-45/hr CAD",
        "databricks": "$45-55/hr",
        "stripe": "$45-55/hr",
        "airbnb": "$45-55/hr",
        "uber": "$42-52/hr",
        "lyft": "$40-50/hr",
        "pinterest": "$40-50/hr",
        "reddit": "$40-50/hr",
        "discord": "$40-50/hr",
        "figma": "$45-55/hr",
        "roblox": "$45-55/hr",
        "robinhood": "$42-52/hr",
        "cloudflare": "$38-48/hr",
        "datadog": "$40-50/hr",
        "mongodb": "$35-45/hr",
        "elastic": "$35-45/hr",
        "twilio": "$38-48/hr",
        "okta": "$35-45/hr",
        "pagerduty": "$35-45/hr",
        "samsara": "$38-48/hr",
        "scale ai": "$45-55/hr",
        "spacex": "$30-38/hr",
        "palantir": "$45-55/hr",
        # Canadian companies
        "ciena": "$25-34/hr CAD",
        "nokia": "$28-38/hr CAD",
        "ericsson": "$28-38/hr CAD",
        "blackberry": "$25-35/hr CAD",
        "kinaxis": "$25-35/hr CAD",
        "ross video": "$22-30/hr CAD",
        "fullscript": "$25-35/hr CAD",
        "solace": "$28-38/hr CAD",
        "fortinet": "$30-40/hr CAD",
        # Finance
        "jane street": "$55-65/hr",
        "citadel": "$55-65/hr",
        "two sigma": "$50-60/hr",
        # Other
        "nvidia": "$42-55/hr",
        "intel": "$30-40/hr",
        "amd": "$30-40/hr",
        "qualcomm": "$32-42/hr",
        "broadcom": "$32-42/hr",
        "cisco": "$30-40/hr",
        "ibm": "$25-35/hr",
        "oracle": "$30-40/hr",
        "salesforce": "$40-50/hr",
        "adobe": "$38-48/hr",
        "vmware": "$35-45/hr",
        "splunk": "$38-48/hr",
        "atlassian": "$40-50/hr",
        "snap": "$42-52/hr",
        "doordash": "$40-50/hr",
        "instacart": "$38-48/hr",
        "coinbase": "$45-55/hr",
        "block": "$40-50/hr",
        "square": "$40-50/hr",
        "affirm": "$42-52/hr",
        "brex": "$40-50/hr",
        "chime": "$38-48/hr",
        "sofi": "$35-45/hr",
        "toast": "$35-45/hr",
        "gusto": "$38-48/hr",
        "vercel": "$35-45/hr",
        "netlify": "$35-45/hr",
        "webflow": "$35-45/hr",
        "duolingo": "$40-50/hr",
        "epic games": "$38-48/hr",
        "riot games": "$38-48/hr",
        "unity": "$35-45/hr",
        "waymo": "$45-55/hr",
        "nuro": "$42-52/hr",
        "zoox": "$42-52/hr",
        "lucid motors": "$35-45/hr",
        "roku": "$38-48/hr",
        "peloton": "$35-45/hr",
        "dropbox": "$40-50/hr",
        "asana": "$40-50/hr",
        "gitlab": "$35-45/hr",
        "new relic": "$35-45/hr",
        "cockroachdb": "$38-48/hr",
        "contentful": "$35-45/hr",
        "flexport": "$38-48/hr",
        "faire": "$38-48/hr",
        "squarespace": "$38-48/hr",
        "wattpad": "$25-35/hr CAD",
        "vanta": "$40-50/hr",
    }

    # Find jobs without salary data (newest first so recent jobs get enriched first)
    jobs_to_enrich = (
        db.query(ScrapedJob)
        .filter(
            or_(
                ScrapedJob.salary_range == "",
                ScrapedJob.salary_range == None,
            ),
            ScrapedJob.experience_level.in_(["internship", "new_grad"]),
        )
        .order_by(ScrapedJob.id.desc())
        .limit(batch_size)
        .all()
    )

    enriched = 0
    for job in jobs_to_enrich:
        company_lower = job.company.lower().strip()

        # Check direct match
        salary = SALARY_MAP.get(company_lower)

        # Check partial match (e.g., "Scale AI" matches "scale ai")
        if not salary:
            for key, val in SALARY_MAP.items():
                if key in company_lower or company_lower in key:
                    salary = val
                    break

        # Try to extract from description
        if not salary and job.description:
            # Look for patterns like "$XX/hr", "$XX-$YY/hr", "$XX,000-$YY,000"
            hr_match = re.search(r'\$(\d+(?:\.\d+)?)\s*[-–]\s*\$?(\d+(?:\.\d+)?)\s*/\s*(?:hr|hour)', job.description, re.IGNORECASE)
            if hr_match:
                salary = f"${hr_match.group(1)}-${hr_match.group(2)}/hr"
            else:
                annual_match = re.search(r'\$(\d{2,3}),?(\d{3})\s*[-–]\s*\$?(\d{2,3}),?(\d{3})', job.description)
                if annual_match:
                    low = int(annual_match.group(1) + annual_match.group(2))
                    high = int(annual_match.group(3) + annual_match.group(4))
                    if low > 10000 and high > 10000:
                        salary = f"${low:,}-${high:,}/yr"

        if salary:
            job.salary_range = salary
            db.commit()
            enriched += 1

    remaining = db.query(ScrapedJob).filter(
        or_(
            ScrapedJob.salary_range == "",
            ScrapedJob.salary_range == None,
        ),
        ScrapedJob.experience_level.in_(["internship", "new_grad"]),
    ).count()

    return {
        "total_processed": len(jobs_to_enrich),
        "enriched": enriched,
        "remaining_without_salary": remaining,
    }
