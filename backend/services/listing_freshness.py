"""
Listing freshness: the lifecycle layer that keeps the catalogue honest.

Aggregator competitors' known weakness is ghost/expired listings, jobs that
died on the employer's board weeks ago but keep ranking. This module makes the
catalogue self-correcting:

  - every board crawl RECONCILES its own rows: still-listed rows get
    ``last_seen_at`` bumped (and revived if previously removed), vanished rows
    are marked ``removed`` the same hour, not on some future full sweep
  - rows a board stopped vouching for (partial crawls, broken boards) go
    ``stale`` after STALE_AFTER_HOURS, and a time-boxed per-run budget of
    rows gets checked against the platform's own API (services/
    platform_liveness.py): dead → removed, alive → revived, anything
    inconclusive only stamps ``last_probed_at``
  - aggregator rows (LinkedIn/Indeed/GitHub lists), which no board will ever
    re-confirm, EXPIRE by age; stale rows nothing vouches for, and rows on
    boards we can't reconcile, age out too
  - active rows carry a ``ghost_risk_score`` heuristic, surfaced as data,
    never silently filtered, so the product decides hide vs badge

``last_seen_at`` is positive evidence only (a board listed the row, or the
platform's API said the posting is open). A probe that learned nothing
stamps ``last_probed_at`` instead, so it can never pass for a confirmation;
a check that got no answer at all (never sent, or rate-limited) stamps
nothing, so the row keeps its place in line.

All states are soft: rows are never deleted (saved-job and application records
reference them), and the row's user-facing ``status`` workflow is untouched.
Everything here is column-query based, descriptions are only read for the
one-time evergreen check, because a whole-row sweep over the catalogue is
exactly the egress mistake that melted the Neon budget once already.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import time
from collections import Counter

from sqlalchemy import case, func, not_, nulls_first, or_
from sqlalchemy.orm import Session

from backend.db.models import ScrapedJob
from backend.services.description_extractor import sanitize_description
from backend.services.platform_liveness import (
    ALIVE,
    DEAD,
    LINKEDIN_RUN_CAP,
    check_listings,
    is_deferred,
    strip_workday_apply,
)
from backend.services.structured_extraction import (
    compute_raw_hash,
    detect_employment_type,
    detect_visa_sponsorship,
    extract_skills,
    looks_evergreen,
    parse_salary,
)

logger = logging.getLogger(__name__)

LISTING_ACTIVE = "active"
LISTING_STALE = "stale"
LISTING_REMOVED = "removed"
LISTING_EXPIRED = "expired"
# Open on its board, but the board's own listing fails the crawler's filters
# (outside the US and Canada; for a crawl that keeps the student catalogue,
# cron-ats while CRON_ATS_V2_CATALOGUE is off, also not entry level): a
# London, UK posting stored before the NA filter knew better.
# Set only from a listing's verdict (a board crawl, or the
# orphan sweep for rows no crawl reaches), and a crawl brings it back to
# active the moment the listing passes again. Never a death verdict: a
# complete crawl that stops listing it marks it removed, like any row. A
# LinkedIn/Indeed row with a plainly senior title goes off_target too
# (retire_senior_aggregator_rows) and stays there while the retire is on: no
# crawl lists it.
LISTING_OFF_TARGET = "off_target"

# Closed: the posting no longer takes applications (or nothing vouches for it).
CLOSED_LISTING_STATUSES = (LISTING_REMOVED, LISTING_EXPIRED)
# Listing statuses hidden from the default catalogue view. ``stale`` (missed a
# listing or failed a check) is hidden too, until a crawl or check vouches again.
HIDDEN_LISTING_STATUSES = CLOSED_LISTING_STATUSES + (LISTING_OFF_TARGET, LISTING_STALE)
# ats_scraper.RETIRABLE_REJECTIONS: the listing verdicts that retire a row.
# Only a crawl that filters by level gives "level" (cron-ats while
# CRON_ATS_V2_CATALOGUE is off); an every-level crawl never does.
_RETIRABLE = frozenset({"level", "location"})

# scraped_jobs.closed_reason, stamped whenever a row is removed or expires and
# cleared when it comes back: the board's complete listing no longer has it,
# the posting answers not-found, it redirects to a careers home or an error
# page, it says it is closed, or it aged out.
CLOSED_DELISTED = "delisted"
CLOSED_404 = "404"
CLOSED_REDIRECT_HOME = "redirect_home"
CLOSED_TEXT = "closed_text"
CLOSED_TTL = "ttl"
CLOSED_REASONS = (CLOSED_DELISTED, CLOSED_404, CLOSED_REDIRECT_HOME, CLOSED_TEXT, CLOSED_TTL)
# platform_liveness dead reasons (LivenessResult.reason) that are not a
# not-found answer. Every other dead reason (http_404, workday_cxs_S22,
# gh_api_404, oracle_items_0, ...) is the platform saying the posting is gone.
_DEAD_REASON_CLOSED = {
    "ashby_posting_null": CLOSED_DELISTED,   # absent from the org's board
    "sr_inactive": CLOSED_TEXT,              # the API's own active: false
    "linkedin_closed": CLOSED_TEXT,          # the closed banner
    "body_closed": CLOSED_TEXT,              # a dead phrase in the page
    "error_redirect": CLOSED_REDIRECT_HOME,
    "offsite_home_redirect": CLOSED_REDIRECT_HOME,
}


def closed_reason_for(dead_reason: str | None) -> str:
    """The closed_reason for a platform_liveness DEAD verdict's reason. An
    adapter's check_alive may already answer in closed_reason terms (spec:
    404 / redirect_home / closed_text); anything unrecognized is a
    not-found."""
    reason = (dead_reason or "").strip()
    if reason in CLOSED_REASONS:
        return reason
    if reason in _DEAD_REASON_CLOSED:
        return _DEAD_REASON_CLOSED[reason]
    lowered = reason.lower()
    if "redirect" in lowered:
        return CLOSED_REDIRECT_HOME
    if "closed" in lowered or "inactive" in lowered or "expired" in lowered:
        return CLOSED_TEXT
    return CLOSED_404


def retire_off_target_enabled() -> bool:
    """CRON_ATS_RETIRE_OFF_TARGET kill switch (default on), read at the start
    of each cron run. On Vercel a changed env var reaches the functions only
    with the next deployment: redeploy after flipping it.

    "0"/"false"/"no"/"off" turns the retire off and rolls it back: crawls go
    back to the old rule (a listed row is active), so each board's next crawl
    brings back the off_target rows it lists, and the orphan sweep and the
    senior aggregator sweep, whose rows no crawl ever re-judges, move the
    rows they retired back themselves (_restore_off_target, bounded per
    run). For an emergency such as a classifier regression hiding real
    rows."""
    value = os.getenv("CRON_ATS_RETIRE_OFF_TARGET", "1").strip().lower()
    return value not in ("0", "false", "no", "off")

# A direct-board row not re-confirmed for this long means its board stopped
# vouching for it (partial Workday crawls, a board that 500s). The full
# registry re-crawls every shard_count (~2-3) hours, so 72h is many misses.
STALE_AFTER_HOURS = 72

# Aggregator rows are never re-confirmed by anyone; past this age they are
# presumed dead. LinkedIn/Indeed postings churn far faster than the curated
# GitHub lists (which re-publish), so they age out sooner.
AGGREGATOR_MAX_AGE_DAYS = 30
AGGREGATOR_FAST_MAX_AGE_DAYS = 21
_FAST_AGGREGATOR_SOURCES = ("linkedin", "indeed")
_AGGREGATOR_SOURCES = _FAST_AGGREGATOR_SOURCES + ("github",)
# LinkedIn/Indeed rows by URL host: the retired external scraper stored its
# LinkedIn cards as source_platform='ats'.
_LINKEDIN_URL_PATTERNS = ("http%://linkedin.com/%", "http%://%.linkedin.com/%")
_FAST_AGGREGATOR_URL_PATTERNS = _LINKEDIN_URL_PATTERNS + (
    "http%://indeed.com/%", "http%://%.indeed.com/%",
)
# Board keys no crawl will ever reconcile.
_UNRECONCILABLE_BOARD_KEYS = ("", "unknown")

# A stale row nothing has vouched for (no board listing, no platform "open")
# in this long is presumed dead, once the verifier has checked it since:
# it had ~6 runs a day to prove otherwise.
STALE_TERMINAL_DAYS = 21
# Direct rows on a board no crawl reconciles age out this long after their
# last positive evidence, again only once a check has come back since.
UNRECONCILABLE_MAX_AGE_DAYS = 30

# Per-run verification budgets, sized for the ~6 runs/day the "hourly"
# GitHub schedule actually delivers, and a wall-clock box so a run of hanging
# hosts can't push the cron past Vercel's 300s limit. Rows are worked
# least-recently-probed first, so the whole backlog rotates.
STALE_VERIFY_BUDGET = 600
RECENT_VERIFY_BUDGET = 200
UNCONFIRMED_VERIFY_BUDGET = 150
VERIFY_TIME_BOX_SECONDS = 150
VERIFY_RECHECK_HOURS = 20
# LinkedIn answers a paced trickle per run (platform_liveness caps it):
# selecting more of its rows would only defer them, and rows that are never
# stamped would crowd the GitHub rows out of every run's budget.
RECENT_LINKEDIN_QUOTA = LINKEDIN_RUN_CAP
# An active direct row no board has re-listed for this long gets checked
# before it goes stale (partial Workday/SmartRecruiters crawls, and rows the
# old verifier revived on a bare 200).
UNCONFIRMED_AFTER_HOURS = 48
_VERIFY_CHUNK = 100  # probe + commit in chunks, a timeout loses one chunk
_VERIFY_CONCURRENCY = 8

GHOST_DAYS_OPEN = 45
CHANGE_LOG_CAP = 20

_IN_CHUNK = 400  # keep IN () lists comfortably under driver parameter limits


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


def _chunks(items: list, size: int = _IN_CHUNK):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ─── New-row field construction ──────────────────────────────────────────────

def stored_external_id(board_key: str, external_id: str) -> str:
    """scraped_jobs.external_id for a crawled listing: "{platform}:{slug}:{the
    source's id}". The five built-in fetchers give the bare source id, so it
    takes the board key in front; a v2 adapter's ATSJob.external_id already
    has that shape (docs/scraper-v2/PLAN.md, constraint 13) and is kept as
    it is, never "workable:financeit:workable:financeit:ABC123"."""
    from backend.data.company_registry import SUPPORTED_PLATFORMS

    if not external_id:
        return ""
    if (board_key or "").split(":", 1)[0] in SUPPORTED_PLATFORMS:
        return f"{board_key}:{external_id}"
    return external_id


def build_new_row_fields(job, board_key: str, source_trust: str = "high") -> dict:
    """Structured-extraction + freshness fields for a brand-new direct-board
    row. The caller merges these into its ScrapedJob(...) constructor kwargs.
    ``job`` is an ats_scraper.ATSJob."""
    now = _utcnow()
    description = job.description or ""
    salary_text = job.salary_text or ""

    salary = parse_salary(salary_text) or parse_salary(description)
    salary_min, salary_max, salary_currency, salary_period = salary if salary else (None, None, "", "")

    fields = {
        "listing_status": LISTING_ACTIVE,
        "first_seen_at": now,
        "last_seen_at": now,
        "board_key": board_key,
        "external_id": stored_external_id(board_key, job.external_id),
        "raw_hash": compute_raw_hash(job.title, job.location, description, salary_text),
        "source_trust": source_trust,
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": salary_currency,
        "salary_period": salary_period,
        "employment_type": detect_employment_type(job.title, description, job.employment_type or ""),
        "visa_sponsorship": detect_visa_sponsorship(description),
        "skills": extract_skills(job.title, description) or None,
    }
    return fields


# ─── Board reconciliation ────────────────────────────────────────────────────

def listed_status_change(listing_status: str | None, source_platform: str,
                         rejection: str | None) -> str | None:
    """The listing_status a row its board LISTS moves to, or None to keep it.

    ``rejection`` is the crawler's verdict on the listing (ats_scraper.
    ATSScraper.rejection, None when it passes). A crawler row ('ats') whose
    listing is off target (_RETIRABLE: "location", and "level" from a crawl
    that filters by level) goes off_target, whatever it was; one that passes
    comes back to active from any hidden or stale state. An "unplaced"
    listing (no location either way) proves only that the posting is open:
    it revives a closed row as before, never an off_target one. Other sources' rows (a GitHub-list row adopted into the
    board, LinkedIn/Indeed copies) keep the old rule, listed means active:
    they passed their own source's filters, which are not the crawler's."""
    status = listing_status or LISTING_ACTIVE
    if source_platform == "ats" and rejection in _RETIRABLE:
        return None if status == LISTING_OFF_TARGET else LISTING_OFF_TARGET
    if status == LISTING_OFF_TARGET and rejection is not None:
        return None
    return LISTING_ACTIVE if status != LISTING_ACTIVE else None


# A complete listing that would close more than MASS_REMOVAL_SHARE of a
# board's open rows, and more than MASS_REMOVAL_MIN of them, is held: a source
# that answers short looks exactly like that, and three adapters were caught
# calling a partial listing complete. The rows it lists are confirmed as
# usual; the rest are left to the stale sweep and the verifier, which close
# the dead ones within days (stale rows are hidden since scraper v2).
MASS_REMOVAL_MIN = 20
MASS_REMOVAL_SHARE = 0.5


def mass_removal(removals: int, open_rows: int) -> bool:
    """Whether a complete listing that would close ``removals`` of a board's
    ``open_rows`` is held instead (MASS_REMOVAL_MIN, MASS_REMOVAL_SHARE)."""
    return removals > MASS_REMOVAL_MIN and removals > MASS_REMOVAL_SHARE * open_rows


def reconcile_board(db: Session, board_key: str, live_urls: set[str],
                    now: datetime.datetime | None = None, *,
                    rejected: dict[str, str] | None = None) -> dict:
    """Sync this board's rows against the URLs the board just listed.

    - rows whose URL is still listed: ``last_seen_at`` = now, and the status
      follows listed_status_change(): removed/stale/expired rows come back
      to ``active`` (reposted or crawl recovered), unless ``rejected`` (url
      -> ATSScraper.rejection reason) says the listing is off target, which
      sends a crawler row ``off_target`` instead. Before, every listed row
      was confirmed whatever its listing said, so a row stored under older
      filters (London, UK; a "Vice President" title) stayed visible for as
      long as its board listed it.
    - rows whose URL vanished: ``removed`` with closed_reason ``delisted``,
      effective immediately. An ``off_target`` row too: hidden either way,
      but only a closed status tells a saved job, an application or a deep
      link that the posting is gone. If it is relisted and still fails,
      listed_status_change sends it back to off_target. Unless the removals
      are a mass removal (mass_removal: more than half of the board's open
      rows, and more than 20): then none goes, a warning is logged, and
      ``held`` counts them.

    A row leaving a closed status loses its closed_reason. Only call with a
    COMPLETE snapshot, a partial crawl's absence is not evidence of removal.
    Commits. Returns counts.
    """
    now = now or _utcnow()
    rejected = rejected or {}
    stats = {"confirmed": 0, "revived": 0, "removed": 0, "off_target": 0, "held": 0}

    # Every open row of the board, but of its closed history (which only
    # grows) just the rows the listing names: a closed row it does not list
    # stays as it is, so reading it back every crawl was transfer for nothing.
    # A Workday '/job/<slug>/apply' row is matched to its listing below, so
    # closed rows with such a link are read too.
    columns = (ScrapedJob.id, ScrapedJob.url, ScrapedJob.listing_status,
               ScrapedJob.source_platform)
    closed = ScrapedJob.listing_status.in_(CLOSED_LISTING_STATUSES)
    rows = (
        db.query(*columns)
        .filter(ScrapedJob.board_key == board_key,
                or_(ScrapedJob.listing_status.is_(None), not_(closed)))
        .all()
    )
    for chunk in _chunks(sorted(url for url in live_urls if url)):
        rows += (
            db.query(*columns)
            .filter(ScrapedJob.board_key == board_key, closed, ScrapedJob.url.in_(chunk))
            .all()
        )
    if live_urls:
        rows += (
            db.query(*columns)
            .filter(ScrapedJob.board_key == board_key, closed,
                    ScrapedJob.url.like("%/apply%"))
            .all()
        )
        rows = list({row[0]: row for row in rows}.values())
    if not rows:
        return stats

    live_ids: list[int] = []
    revive_ids: list[int] = []
    off_target_ids: list[int] = []
    gone_ids: list[int] = []
    for row_id, url, listing_status, source_platform in rows:
        # A stored Workday '/job/<slug>/apply' link is the listed posting.
        listed = url if url in live_urls else strip_workday_apply(url or "")
        if listed in live_urls:
            live_ids.append(row_id)
            change = listed_status_change(listing_status, source_platform or "",
                                          rejected.get(listed))
            if change == LISTING_ACTIVE:
                revive_ids.append(row_id)
            elif change == LISTING_OFF_TARGET:
                off_target_ids.append(row_id)
        elif listing_status in (LISTING_ACTIVE, LISTING_STALE, LISTING_OFF_TARGET):
            gone_ids.append(row_id)

    # A complete-but-empty response on a board that had many live rows is more
    # often an API hiccup than a real mass takedown; degrade to the stale
    # sweep instead of declaring everything removed.
    if not live_urls and len(gone_ids) > 10:
        logger.warning("reconcile %s: empty board with %d active rows, leaving to stale sweep",
                       board_key, len(gone_ids))
        return stats
    open_rows = sum(1 for _id, _url, status, _source in rows
                    if status is None or status not in CLOSED_LISTING_STATUSES)
    if mass_removal(len(gone_ids), open_rows):
        logger.warning("reconcile %s: a complete listing would close %d of %d open rows; "
                       "held as partial", board_key, len(gone_ids), open_rows)
        stats["held"] = len(gone_ids)
        gone_ids = []

    for chunk in _chunks(live_ids):
        db.query(ScrapedJob).filter(ScrapedJob.id.in_(chunk)).update(
            {"last_seen_at": now}, synchronize_session=False,
        )
    for chunk in _chunks(revive_ids):
        db.query(ScrapedJob).filter(ScrapedJob.id.in_(chunk)).update(
            {"listing_status": LISTING_ACTIVE, "listing_status_changed_at": now,
             "closed_reason": None},
            synchronize_session=False,
        )
    for chunk in _chunks(gone_ids):
        db.query(ScrapedJob).filter(ScrapedJob.id.in_(chunk)).update(
            {"listing_status": LISTING_REMOVED, "listing_status_changed_at": now,
             "closed_reason": CLOSED_DELISTED},
            synchronize_session=False,
        )
    for chunk in _chunks(off_target_ids):
        db.query(ScrapedJob).filter(ScrapedJob.id.in_(chunk)).update(
            {"listing_status": LISTING_OFF_TARGET, "listing_status_changed_at": now,
             "closed_reason": None},
            synchronize_session=False,
        )

    db.commit()
    stats.update(confirmed=len(live_ids), revived=len(revive_ids), removed=len(gone_ids),
                 off_target=len(off_target_ids))
    if off_target_ids:
        logger.info("reconcile %s: %d listed rows retired off_target", board_key,
                    len(off_target_ids))
    return stats


# ─── Fields derived from a row's title and location ──────────────────────────
# Inserts (cron-ats) and the crawl's refresh of known rows compute these with
# the same helpers, so an edited row can never end up filed differently from
# a new one with the same title and location.

def title_fields(title: str, department: str = "", employment_type: str = "",
                 description: str = "", years_required: int | None = None) -> dict:
    """Columns derived from a crawled row's title, and from its description
    where the title leaves a gap: the cross-source dedup key, the role
    category (role_classifier, which the fit engine reads) and the scraper v2
    tags of ingest/taxonomy, with the version of the rules that set them.

    The experience level (taxonomy.classify_experience) reads the title, the
    department and the source's commitment ``employment_type``, word-bounded:
    "Internal Audit Analyst" is no internship, and a Lever "Intern"
    commitment behind a bare "RF Validation Associate" is one. A title that
    names no level falls back to the minimum years the description asks for:
    ``years_required`` when the caller already knows it (a refresh whose
    crawl carried no description passes the stored value), else
    taxonomy.years_required(description). That number is stored too
    (experience_years_required). The career families read the title, the
    department and the start of the description."""
    from backend.ingest import taxonomy
    from backend.services.cross_source_dedup import normalize_title
    from backend.services.role_classifier import classify as classify_role

    title, department, employment_type = title or "", department or "", employment_type or ""
    if years_required is None and description:
        years_required = taxonomy.years_required(description)
    family, secondary = taxonomy.classify_family(title, department, description or "")
    return {
        "title_norm": normalize_title(title),
        "role_category": classify_role(title, department),
        "experience_level": taxonomy.classify_experience(
            title, department, employment_type, years_required=years_required),
        "experience_years_required": years_required,
        "career_family": family,
        "career_family_secondary": secondary,
        "enrichment_version": taxonomy.ENRICHMENT_VERSION,
    }


def cap_description(description: str, max_chars: int | None) -> str:
    """The description as stored: cut at ``max_chars`` (limits.json
    description_max_chars) when set. Hashes and extraction read the whole
    text; only the stored copy is cut."""
    if max_chars and len(description or "") > max_chars:
        return description[:max_chars]
    return description


def location_derived_fields(location: str, board_country: str = "", *, hint: str = "",
                            current_country: str = "", fallback: str = "US") -> dict:
    """Columns derived from a crawled row's location: the parsed city/region/
    locations_json/location_search (location_parser) and the ``country``
    (na_location.job_country: the board's registry country, else what the
    location says, else ``fallback``).

    The parse gets the country positive evidence names (the board's, else
    na_location's reading of the location, never the fallback), so PwC's
    "CA-San Francisco" is San Francisco, California. A location that names
    no place (Workday's "3 Locations") takes its city, region and
    location_search from the path ``hint`` ("Toronto-ON"), so the city
    filter finds it."""
    from backend.services.location_parser import hint_location_fields, location_fields
    from backend.services.na_location import CA, US, hint_region, job_country, region_of

    evidence = board_country or region_of(location or "")
    fields = location_fields(location or "", evidence if evidence in (US, CA) else "")
    if not fields["location_search"] and hint:
        fields.update(hint_location_fields(hint, hint_region(hint) or board_country))
    fields["country"] = job_country(location or "", board_country, hint=hint,
                                    current=current_country, fallback=fallback)
    return fields


def _title_edited_before(change_log) -> bool:
    for entry in change_log or []:
        if isinstance(entry, dict) and "title" in (entry.get("changed") or []):
            return True
    return False


def refresh_known_listings(db: Session, board_key: str, jobs: list,
                           now: datetime.datetime | None = None,
                           board_country: str = "", *,
                           description_max_chars: int | None = None,
                           listed_urls: set[str] | None = None) -> tuple[list, dict]:
    """Split a board's filtered jobs into (new, stats) and refresh the ones
    already stored: detect edits (title/location/salary/description) into
    ``change_log``, update the structured fields, adopt legacy rows into
    ``board_key``, and give a crawler row the registry's company name.
    ``jobs`` are ats_scraper.ATSJob. Commits.

    A row is matched by its URL, and failing that by the source's own id
    (external_id) on this board: Recruitee, Teamtailor, SmartRecruiters and
    Workday URLs carry the title, so a retitle moves the URL, and the row
    moves with it ("moved") instead of closing beside a new twin that saved
    jobs and alerts never point at. Only an id one listing and one row carry,
    and only a row whose own URL the board no longer lists (``listed_urls``,
    the whole listing, filtered-out postings included).

    Change detection is explicit column compares plus a description hash,
    a re-crawl that didn't carry the description (SmartRecruiters/Workday
    list payloads) must not read "description became empty" as an edit.
    Nor may Workday's bare "10 Locations" replace a stored location that
    names the place: the path hint only places a row whose text names none.

    The fields derived from the title and location are kept in step, the way
    cron-ats derives them for a new row (location_derived_fields,
    title_fields): an edit used to rewrite only ``location``/``title``, so a
    Toronto job stayed filed under Austin and a retitled one kept its old
    dedup key. Each crawl also heals what earlier code left behind: parsed
    fields that disagree with the stored location ("reparsed"), a country
    that disagrees with what the location or ``board_country`` (the
    registry's country for a one-country board) says ("recountried": a bare
    "Toronto" once defaulted to "US"), a title_norm left stale by an
    earlier title edit ("retitled"), and a crawler row's experience_level
    that disagrees with taxonomy.classify_experience ("relabeled": the old
    substring test filed "Internal Audit Analyst" under internships, and
    scraper v2 splits the old entry tier into new_grad and entry). The level
    falls back on the years the description asks for; a crawl that carries
    no description (SmartRecruiters/Workday list payloads) uses the stored
    experience_years_required, so the label never flips between crawls.
    Only rows that differ are rewritten; a row the crawl only re-confirms
    gets its last_seen_at in one UPDATE per chunk. A changed description is
    stored cut at ``description_max_chars``.
    """
    from backend.ingest import taxonomy
    from backend.services.location_parser import is_location_count, location_fields

    now = now or _utcnow()
    stats = {"refreshed": 0, "edited": 0, "salary_removed": 0,
             "reparsed": 0, "recountried": 0, "retitled": 0, "relabeled": 0, "moved": 0}
    if not jobs:
        return [], stats

    by_url = {job.url: job for job in jobs if job.url}
    existing: dict[str, tuple] = {}
    urls = list(by_url.keys())
    columns = (
        ScrapedJob.id, ScrapedJob.url, ScrapedJob.title,
        ScrapedJob.location, ScrapedJob.salary_min, ScrapedJob.raw_hash,
        ScrapedJob.edit_count, ScrapedJob.change_log,
        ScrapedJob.board_key, ScrapedJob.external_id,
        ScrapedJob.country, ScrapedJob.city, ScrapedJob.region,
        ScrapedJob.location_search, ScrapedJob.title_norm,
        ScrapedJob.experience_level, ScrapedJob.source_platform,
        ScrapedJob.company, ScrapedJob.experience_years_required,
        ScrapedJob.salary_max, ScrapedJob.salary_currency, ScrapedJob.salary_period,
    )
    for chunk in _chunks(urls):
        for row in db.query(*columns).filter(ScrapedJob.url.in_(chunk)).all():
            existing[row[1]] = row

    # The listings no row has by URL, by the id their row would carry. An id
    # two listings share names neither of them.
    unmatched: dict[str, list[str]] = {}
    for url, job in by_url.items():
        if url not in existing and job.external_id:
            unmatched.setdefault(stored_external_id(board_key, job.external_id), []).append(url)
    by_external = {ext: found[0] for ext, found in unmatched.items() if len(found) == 1}
    listed = (set(listed_urls or ()) | set(by_url)) if by_external else set()
    for chunk in _chunks(sorted(by_external)):
        found = (db.query(*columns)
                 .filter(ScrapedJob.board_key == board_key, ScrapedJob.external_id.in_(chunk))
                 .all())
        carriers = Counter(row[9] for row in found)
        for row in found:
            # A row its board still lists under its own URL is another posting.
            if carriers[row[9]] == 1 and row[1] not in listed:
                existing[by_external[row[9]]] = row

    new_jobs = [job for url, job in by_url.items() if url not in existing]

    from backend.services.cross_source_dedup import normalize_title

    seen_only: list[int] = []
    for url, row in existing.items():
        (row_id, old_url, old_title, old_location, old_salary_min, old_hash,
         edit_count, change_log, old_board_key, old_external_id, old_country,
         old_city, old_region, old_search, old_title_norm, old_level,
         source_platform, old_company, old_years, old_salary_max, old_currency,
         old_period) = row
        job = by_url[url]
        # The description as cron-ats stores and hashes it (sanitized), and
        # the years of experience it asks for: the level's fallback.
        raw_description = (job.description or "").strip()
        description = sanitize_description(raw_description) if raw_description else ""
        years = taxonomy.years_required(description) if description else old_years

        updates: dict = {"last_seen_at": now}
        if old_url != url:
            # Found by its external_id: the posting's URL changed shape.
            updates["url"] = url
            stats["moved"] += 1
        if not old_board_key:
            updates["board_key"] = board_key
        if job.external_id and not old_external_id:
            updates["external_id"] = stored_external_id(board_key, job.external_id)
        # A crawler row carries the registry's name for the employer, so a
        # rename ("Notion (Ashby)" -> "Notion") reaches the stored rows. Not
        # an edit of the posting. A list row keeps its list's spelling.
        company = (job.company or "").strip()
        if source_platform == "ats" and company and company != old_company:
            updates["company"] = company

        # Titles and locations compare stripped: padding (Carvana's
        # " Fleet Operations Associate") is no edit, and is healed quietly.
        changes: list[str] = []
        new_title = (job.title or "").strip()
        title = new_title or old_title or ""
        title_changed = bool(new_title) and new_title != (old_title or "").strip()
        if title_changed:
            changes.append("title")
        if new_title and new_title != old_title:
            updates["title"] = new_title
        # Re-derive on an edit, and heal a row an earlier edit left with the
        # old title's dedup key. Only rows with a logged title edit: an
        # unedited row's title_norm is the insert's own, whatever version of
        # normalize_title wrote it.
        if title and (title_changed or (
                _title_edited_before(change_log)
                and (old_title_norm or "") != normalize_title(title))):
            updates.update(title_fields(title, job.department or "", job.employment_type or "",
                                        description, years_required=years))
            stats["retitled"] += 1
        # The crawler's own label only: a GitHub-list row adopted into the
        # board keeps the label its list gave it.
        if title and source_platform == "ats" and "experience_level" not in updates:
            level = taxonomy.classify_experience(title, job.department or "",
                                                 job.employment_type or "",
                                                 years_required=years)
            if level != (old_level or ""):
                updates["experience_level"] = level
                stats["relabeled"] += 1
        if description and years != old_years:
            updates["experience_years_required"] = years

        new_location = (job.location or "").strip()
        if (is_location_count(new_location)
                and location_fields(old_location or "")["location_search"]):
            # Workday's list payload says only "10 Locations"; a stored
            # "REMOTETELETRAVAIL QC CAN (10 Locations)" names the place.
            # Not an edit, and the stored text keeps filing the row.
            new_location = ""
        location = new_location or old_location or ""
        location_changed = bool(new_location) and new_location != (old_location or "").strip()
        # Filling in a location the row never had (Parsons' list rows
        # carried none until the bullet fallback) is not an edit.
        if location_changed and (old_location or "").strip():
            changes.append("location")
        if new_location and new_location != old_location:
            updates["location"] = new_location
        derived = location_derived_fields(
            location, board_country, hint=job.location_hint or "",
            current_country=old_country or "", fallback=old_country or "US",
        )
        parsed_place = (derived["city"], derived["region"], derived["location_search"])
        if location_changed or parsed_place != (old_city or "", old_region or "", old_search or ""):
            updates.update({key: value for key, value in derived.items() if key != "country"})
            stats["reparsed"] += 1
        if derived["country"] != (old_country or ""):
            updates["country"] = derived["country"]
            stats["recountried"] += 1

        # The description is hashed sanitized, as cron-ats stores it: the
        # raw text hashed differently, so every Greenhouse/Lever/Ashby row
        # logged a false 'description' edit on its second crawl, and the
        # longer sanitized text moves parse_salary's window. A row that
        # older refresh rewrote holds the raw text and its hash: the same
        # content, healed without an edit.
        new_hash =(compute_raw_hash(job.title, job.location, description, job.salary_text or "")
                    if description else "")
        same_content = bool(new_hash) and (old_hash == new_hash or old_hash == compute_raw_hash(
            job.title, job.location, job.description, job.salary_text or ""))
        content_changed = bool(new_hash) and bool(old_hash) and not same_content

        if job.salary_text or description:
            # The raw text last: escaping lengthens the sanitized text, which
            # moves parse_salary's window off a pay line the raw text shows.
            # Veeva's "Financial Analyst" states "$60,000 - $85,000" at
            # character 3,967 of 4,510, and "&nbsp;" pushes it past 4,000.
            parsed = (parse_salary(job.salary_text or "") or parse_salary(description)
                      or parse_salary(raw_description))
            if parsed:
                salary_min, salary_max, currency, period = parsed
                # A new reading of unchanged content is the parser's, not an edit.
                if old_salary_min and salary_min != old_salary_min and not same_content:
                    changes.append("salary")
                if (salary_min, salary_max, currency, period) != (
                        old_salary_min, old_salary_max, old_currency or "", old_period or ""):
                    updates.update(salary_min=salary_min, salary_max=salary_max,
                                   salary_currency=currency, salary_period=period)
            elif old_salary_min and job.salary_text == "" and content_changed:
                # The source used to state pay and the fresh full content no
                # longer does, the bait-and-switch edit worth flagging. Once:
                # the stored pay goes too. Unchanged content whose pay the
                # parser no longer finds (Veeva's "Starting Salary: $85,000")
                # keeps it.
                changes.append("salary_removed")
                stats["salary_removed"] += 1
                updates.update(salary_min=None, salary_max=None,
                               salary_currency="", salary_period="")

        if description:
            if old_hash and new_hash != old_hash:
                if content_changed and not changes:
                    changes.append("description")
                updates["description"] = cap_description(description, description_max_chars)
                # The same content healed to its sanitized form keeps what was
                # read from it: the LLM section cache (description_sections)
                # and the visa and skills extraction.
                if not same_content:
                    updates["description_sections"] = None
                    updates["visa_sponsorship"] = detect_visa_sponsorship(description)
                    updates["skills"] = extract_skills(job.title, description) or None
            if new_hash != old_hash:
                updates["raw_hash"] = new_hash

        if changes:
            log = list(change_log or [])
            log.append({"at": now.isoformat(), "changed": changes})
            updates["change_log"] = log[-CHANGE_LOG_CAP:]
            updates["edit_count"] = (edit_count or 0) + 1
            stats["edited"] += 1

        if updates.keys() == {"last_seen_at"}:
            seen_only.append(row_id)
        else:
            db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(
                updates, synchronize_session=False,
            )
        stats["refreshed"] += 1

    # Most listed rows only need the confirmation: one UPDATE per chunk
    # instead of one round trip per row, which with every level crawled
    # would not fit cron-ats' 300 s.
    _update_ids(db, seen_only, {"last_seen_at": now})
    db.commit()
    return new_jobs, stats


# ─── Country repair ──────────────────────────────────────────────────────────

REPAIR_COUNTRY_LIMIT = 1000


def repair_country(db: Session, *, limit: int = REPAIR_COUNTRY_LIMIT,
                   board_countries: dict[str, str] | None = None,
                   now: datetime.datetime | None = None) -> dict:
    """Heal visible rows whose ``country`` contradicts their own location.

    The LinkedIn script stored "US" on every Canadian row (", ca" matched
    ", canada": 653 visible rows, 2026-09), cron-ats defaulted a bare
    "Toronto" to "US", and BDO's rows predate its registry country. Ingest now
    derives the country server-side (na_location.job_country) and the crawl
    heals the rows it re-lists, but LinkedIn/Indeed/GitHub rows are never
    re-crawled, so this pass fixes them in place.

    DB-only and bounded: a column-only SELECT of at most ``limit`` candidate
    rows (a cheap SQL prefilter: a US row whose parsed location says Canada
    or whose city is a Canadian one, the reverse, or a one-country board's row
    stored under the other country), the verdict in Python, then one UPDATE
    per chunk per target country. Only positive evidence moves a row: the
    registry's country for the board, or a US/CA verdict on the location; a
    location naming both countries keeps its value. Commits. Returns counts.
    """
    from backend.data import company_registry
    from backend.services.na_location import CA, CA_CITIES, US, US_CITIES, job_country

    stats = {"checked": 0, "repaired": 0, "to_ca": 0, "to_us": 0}
    if board_countries is None:
        board_countries = company_registry.load_board_countries()
    ca_boards = sorted(key for key, country in board_countries.items() if country == CA)
    us_boards = sorted(key for key, country in board_countries.items() if country == US)

    suspect = [
        (ScrapedJob.country == US) & or_(
            ScrapedJob.location_search.like("%|canada|%"),
            ScrapedJob.city.in_(CA_CITIES),
        ),
        (ScrapedJob.country == CA) & or_(
            ScrapedJob.location_search.like("%|united states|%"),
            ScrapedJob.city.in_(US_CITIES),
        ),
    ]
    if ca_boards:
        suspect.append(ScrapedJob.board_key.in_(ca_boards) & (ScrapedJob.country != CA))
    if us_boards:
        suspect.append(ScrapedJob.board_key.in_(us_boards) & (ScrapedJob.country != US))
    rows = (
        db.query(ScrapedJob.id, ScrapedJob.location, ScrapedJob.country, ScrapedJob.board_key)
        .filter(
            ScrapedJob.listing_status.in_((LISTING_ACTIVE, LISTING_STALE)),
            ScrapedJob.duplicate_of.is_(None),
            or_(*suspect),
        )
        .order_by(ScrapedJob.id.asc())
        .limit(limit)
        .all()
    )

    moves: dict[str, list[int]] = {CA: [], US: []}
    for row_id, location, country, board_key in rows:
        stats["checked"] += 1
        current = country or ""
        wanted = job_country(location or "", board_countries.get(board_key or "", ""),
                             current=current, fallback=current)
        if wanted != current and wanted in moves:
            moves[wanted].append(row_id)
    for country, ids in moves.items():
        _update_ids(db, ids, {"country": country})
    stats.update(to_ca=len(moves[CA]), to_us=len(moves[US]),
                 repaired=len(moves[CA]) + len(moves[US]))
    if stats["repaired"]:
        db.commit()
        logger.info("repair_country: %s", stats)
    return stats


# ─── Scheduled sweeps ────────────────────────────────────────────────────────

def sweep_stale(db: Session, now: datetime.datetime | None = None,
                ttl_hours: int = STALE_AFTER_HOURS) -> int:
    """Direct-board rows not re-confirmed within the TTL go ``stale``."""
    now = now or _utcnow()
    cutoff = now - datetime.timedelta(hours=ttl_hours)
    count = (
        db.query(ScrapedJob)
        .filter(
            ScrapedJob.listing_status == LISTING_ACTIVE,
            or_(ScrapedJob.source_platform == "ats", ScrapedJob.board_key != ""),
            ScrapedJob.last_seen_at.isnot(None),
            ScrapedJob.last_seen_at < cutoff,
        )
        .update({"listing_status": LISTING_STALE, "listing_status_changed_at": now},
                synchronize_session=False)
    )
    db.commit()
    return count


def _unreconcilable_board():
    return or_(ScrapedJob.board_key.is_(None),
               ScrapedJob.board_key.in_(_UNRECONCILABLE_BOARD_KEYS))


def _fast_aggregator_row():
    """LinkedIn/Indeed rows, by source or by URL host."""
    return or_(
        ScrapedJob.source_platform.in_(_FAST_AGGREGATOR_SOURCES),
        *[ScrapedJob.url.ilike(pattern) for pattern in _FAST_AGGREGATOR_URL_PATTERNS],
    )


def _linkedin_row():
    """Rows whose URL is a LinkedIn page (what the liveness check asks)."""
    return or_(*[ScrapedJob.url.ilike(pattern) for pattern in _LINKEDIN_URL_PATTERNS])


def _older_than(cutoff: datetime.datetime, *columns):
    """least(columns) < cutoff, NULLs ignored, portable: SQLite's min()
    returns NULL when any argument is NULL, Postgres' LEAST skips them."""
    return or_(*[column < cutoff for column in columns])


def sweep_aggregator_expiry(db: Session, now: datetime.datetime | None = None,
                            max_age_days: int = AGGREGATOR_MAX_AGE_DAYS,
                            fast_max_age_days: int = AGGREGATOR_FAST_MAX_AGE_DAYS) -> int:
    """Age out aggregator rows nothing will ever re-confirm.

    LinkedIn/Indeed postings churn fast and can't be board-reconciled, so they
    expire at ``fast_max_age_days``, keyed on the URL host as well as the
    source (the external scraper's LinkedIn rows say 'ats'); the curated
    GitHub lists (which re-publish still-open roles) keep the longer
    ``max_age_days``. Age runs from the EARLIEST of posted/first-seen/scraped,
    so a year-less list date parsed into the future can't keep a row forever.
    Rows a real board reconciles are never touched.
    """
    now = now or _utcnow()
    dates = (ScrapedJob.posted_date, ScrapedJob.first_seen_at, ScrapedJob.scraped_at)
    visible = ScrapedJob.listing_status.in_((LISTING_ACTIVE, LISTING_STALE))
    expire = {"listing_status": LISTING_EXPIRED, "listing_status_changed_at": now,
              "closed_reason": CLOSED_TTL}

    fast = (
        db.query(ScrapedJob)
        .filter(
            visible,
            _unreconcilable_board(),
            _fast_aggregator_row(),
            _older_than(now - datetime.timedelta(days=fast_max_age_days), *dates),
        )
        .update(expire, synchronize_session=False)
    )
    lists = (
        db.query(ScrapedJob)
        .filter(
            visible,
            _unreconcilable_board(),
            ScrapedJob.source_platform.notin_(("ats",) + _FAST_AGGREGATOR_SOURCES),
            not_(_fast_aggregator_row()),
            _older_than(now - datetime.timedelta(days=max_age_days), *dates),
        )
        .update(expire, synchronize_session=False)
    )
    db.commit()
    return fast + lists


def sweep_terminal_expiry(db: Session, now: datetime.datetime | None = None,
                          stale_days: int = STALE_TERMINAL_DAYS,
                          unreconcilable_days: int = UNRECONCILABLE_MAX_AGE_DAYS) -> dict:
    """End the rows nothing will ever confirm again.

    - a ``stale`` row with no positive evidence (``last_seen_at``) for
      ``stale_days`` → expired. A live one had ~6 verifier runs a day and
      every board crawl in that time to prove it.
    - a direct (non-aggregator) row on a board no crawl reconciles
      (board_key '' / 'unknown') → expired ``unreconcilable_days`` after its
      last positive evidence.

    Both only once a check has come back since that evidence
    (``last_probed_at`` after it): a row the verifier never reached (budget,
    time box, a host skipped or rate-limiting) is not aged out unchecked,
    while a row a check could not judge (a bot wall, an SPA shell) still
    ends on schedule. Run it after the verify sweeps, so this run's checks
    count. Board-confirmed rows are untouched: a crawl keeps bumping their
    ``last_seen_at``, and a board that lists an expired row again revives it.
    Column-only UPDATEs. Commits.
    """
    now = now or _utcnow()
    expire = {"listing_status": LISTING_EXPIRED, "listing_status_changed_at": now,
              "closed_reason": CLOSED_TTL}
    evidence = func.coalesce(ScrapedJob.last_seen_at, ScrapedJob.first_seen_at,
                             ScrapedJob.scraped_at)
    checked_since = (ScrapedJob.last_probed_at.isnot(None), ScrapedJob.last_probed_at > evidence)

    stale = (
        db.query(ScrapedJob)
        .filter(
            ScrapedJob.listing_status == LISTING_STALE,
            evidence < now - datetime.timedelta(days=stale_days),
            *checked_since,
        )
        .update(expire, synchronize_session=False)
    )
    unreconcilable = (
        db.query(ScrapedJob)
        .filter(
            ScrapedJob.listing_status.in_((LISTING_ACTIVE, LISTING_STALE)),
            _unreconcilable_board(),
            ScrapedJob.source_platform.notin_(_AGGREGATOR_SOURCES),
            not_(_fast_aggregator_row()),
            evidence < now - datetime.timedelta(days=unreconcilable_days),
            *checked_since,
        )
        .update(expire, synchronize_session=False)
    )
    db.commit()
    return {"stale_expired": stale, "unreconcilable_expired": unreconcilable}


ORPHAN_RETIRE_LIMIT = 2000


def _restore_off_target(db: Session, now: datetime.datetime, limit: int, owned: tuple,
                        *, newest_first: bool = False,
                        expire_before: datetime.datetime | None = None) -> dict:
    """The kill switch's rollback of a sweep no crawl re-judges: the
    off_target rows matching ``owned`` (that sweep's own criteria) go back
    to active, at most ``limit`` a run. With ``expire_before``, a row whose
    age (the earliest of posted/first-seen/scraped) passed it goes to
    expired instead, as the aggregator expiry would have done had it stayed
    visible. Column-only SELECT of ids, chunked UPDATEs. Commits."""
    order = ScrapedJob.id.desc() if newest_first else ScrapedJob.id.asc()
    ids = [row_id for (row_id,) in (
        db.query(ScrapedJob.id)
        .filter(ScrapedJob.listing_status == LISTING_OFF_TARGET, *owned)
        .order_by(order)
        .limit(limit)
        .all()
    )]
    stats = {"restored": 0, "expired": 0}
    for chunk in _chunks(ids):
        if expire_before is not None:
            stats["expired"] += (
                db.query(ScrapedJob)
                .filter(ScrapedJob.id.in_(chunk),
                        _older_than(expire_before, ScrapedJob.posted_date,
                                    ScrapedJob.first_seen_at, ScrapedJob.scraped_at))
                .update({"listing_status": LISTING_EXPIRED, "listing_status_changed_at": now,
                         "closed_reason": CLOSED_TTL},
                        synchronize_session=False)
            )
        stats["restored"] += (
            db.query(ScrapedJob)
            .filter(ScrapedJob.id.in_(chunk), ScrapedJob.listing_status == LISTING_OFF_TARGET)
            .update({"listing_status": LISTING_ACTIVE, "listing_status_changed_at": now},
                    synchronize_session=False)
        )
    if ids:
        db.commit()
    return stats


def _orphan_row() -> tuple:
    """Crawler rows no board crawl reaches (board_key '' / 'unknown'), never
    a LinkedIn/Indeed page stored as one: what the orphan sweep judges."""
    return (ScrapedJob.source_platform == "ats", _unreconcilable_board(),
            not_(_fast_aggregator_row()))


def _relabel_orphans(db: Session, rows) -> int:
    """Give orphan rows (id, title, ..., experience_level,
    experience_years_required, the last two columns) the level
    taxonomy.classify_experience reads in the title, falling back on the
    stored years as a crawl's refresh does. Unlike a crawled row (the
    backfill keeps a stored student label, DECISIONS 13) these rows never
    passed this crawler's filter, and no crawl will ever relabel them: a
    rogue-era "Senior ..." row labeled new_grad would come back into the
    student feed. One UPDATE per chunk per level, uncommitted. Returns the
    rows relabeled."""
    from backend.ingest import taxonomy

    moves: dict[str, list[int]] = {}
    for row in rows:
        row_id, title, level, years = row[0], row[1], row[-2], row[-1]
        wanted = taxonomy.classify_experience(title or "", years_required=years)
        if wanted != (level or ""):
            moves.setdefault(wanted, []).append(row_id)
    for wanted, ids in moves.items():
        _update_ids(db, ids, {"experience_level": wanted})
    return sum(len(ids) for ids in moves.values())


def retire_unreconcilable_off_target(db: Session, now: datetime.datetime | None = None,
                                     limit: int = ORPHAN_RETIRE_LIMIT, *,
                                     entry_level_only: bool = False) -> dict:
    """Retire visible crawler rows no board crawl will ever judge, when their
    own stored title or location fails the crawler's filters.

    Rows on board_key '' / 'unknown' (BGIS on Oracle, Nokia, Huawei's
    Recruitee: 144 visible, 2026-09, all rogue-era inserts) are never
    reconciled, and every authoritative "alive" from the verify sweeps bumps
    their last_seen_at, so the terminal expiry never ends them either.
    The verdict is ATSScraper.rejection on the stored title and location,
    the same one a board crawl acts on. By default every experience level is
    in scope (scraper v2), so only "location" retires, and only on positive
    foreign evidence, and rows an earlier sweep retired for their level come
    back. ``entry_level_only`` is the rule from before scraper v2, for
    cron-freshness while CRON_ATS_V2_CATALOGUE is off: the scraper filters by
    level too, so a title that is not entry level retires as "level", and a
    row retired for its level stays retired. Without it the sweep would
    restore, hourly, every orphan row production retired for its level, in a
    catalogue the switch exists to keep as it was. The runner's sweeps never
    set it: the runner stores every level whatever the switch says. Only
    "ats" rows, never a LinkedIn/Indeed page stored as one. With every level
    in scope, every row the sweep checks or restores gets the level its title
    says (_relabel_orphans, ``relabeled``); under ``entry_level_only`` no row
    is relabeled, as before scraper v2, so a relabel cannot put a level the
    switch keeps out (an "Associate Professor" read as senior) into the feed.

    No crawl ever brings these rows back (reconcile_board goes by board_key,
    and backfill_board_keys re-derives only a '' or NULL key), so with the
    CRON_ATS_RETIRE_OFF_TARGET kill switch off this sweep rolls itself back
    instead: its off_target rows return to active (``restored``), at most
    ``limit`` a run. Column-only SELECT (at most ``limit`` rows), chunked
    UPDATEs. Commits.
    """
    from backend.services.ats_scraper import ATSJob, ATSScraper

    now = now or _utcnow()
    stats = {"checked": 0, "off_target": 0, "level": 0, "location": 0, "relabeled": 0}
    columns = (ScrapedJob.id, ScrapedJob.title, ScrapedJob.company, ScrapedJob.location,
               ScrapedJob.experience_level, ScrapedJob.experience_years_required)
    if not retire_off_target_enabled():
        stats["disabled"] = True
        held = (
            db.query(*columns)
            .filter(ScrapedJob.listing_status == LISTING_OFF_TARGET, *_orphan_row())
            .order_by(ScrapedJob.id.asc())
            .limit(limit)
            .all()
        )
        if held:
            stats["relabeled"] = 0 if entry_level_only else _relabel_orphans(db, held)
            stats["restored"] = _restore_off_target(
                db, now, limit, (ScrapedJob.id.in_([row[0] for row in held]),))["restored"]
        else:
            stats["restored"] = 0
        if stats["restored"]:
            logger.info("retire_unreconcilable_off_target disabled, rolled back: %s", stats)
        return stats

    rows = (
        db.query(*columns)
        .filter(
            *_orphan_row(),
            ScrapedJob.listing_status.in_((LISTING_ACTIVE, LISTING_STALE)),
            ScrapedJob.duplicate_of.is_(None),
        )
        .order_by(ScrapedJob.id.asc())
        .limit(limit)
        .all()
    )
    scraper = ATSScraper(filter_entry_level=entry_level_only, filter_north_america=True)
    retire: list[int] = []
    for row_id, title, company, location, _level, _years in rows:
        stats["checked"] += 1
        reason = scraper.rejection(ATSJob(title=title or "", company=company or "",
                                          location=location or "", url="", department=""))
        if reason in _RETIRABLE:
            retire.append(row_id)
            stats[reason] += 1
    _update_ids(db, retire, {"listing_status": LISTING_OFF_TARGET,
                             "listing_status_changed_at": now})
    stats["off_target"] = len(retire)
    stats["relabeled"] = 0 if entry_level_only else _relabel_orphans(db, rows)
    if retire or stats["relabeled"]:
        db.commit()

    # No crawl re-judges these rows, so this sweep re-reads the ones it hid
    # on every run: a row the current filters no longer reject (a classifier
    # or location fix since) comes back.
    held = (
        db.query(*columns)
        .filter(*_orphan_row(), ScrapedJob.listing_status == LISTING_OFF_TARGET)
        .order_by(ScrapedJob.id.asc())
        .limit(limit)
        .all()
    )
    back = [
        row_id for row_id, title, company, location, _level, _years in held
        if scraper.rejection(ATSJob(title=title or "", company=company or "",
                                    location=location or "", url="", department=""))
        not in _RETIRABLE
    ]
    relabeled = 0 if entry_level_only else _relabel_orphans(db, held)
    stats["relabeled"] += relabeled
    if back:
        stats["restored"] = _restore_off_target(
            db, now, limit, (ScrapedJob.id.in_(back),))["restored"]
    if relabeled:
        db.commit()
    if retire or back or stats["relabeled"]:
        logger.info("retire_unreconcilable_off_target: %s", stats)
    return stats


AGGREGATOR_SENIOR_RETIRE_LIMIT = 5000


def retire_senior_aggregator_rows(db: Session, now: datetime.datetime | None = None,
                                  limit: int = AGGREGATOR_SENIOR_RETIRE_LIMIT) -> dict:
    """Retire LinkedIn/Indeed rows whose title is plainly senior.

    /jobs/ingest-batch took every title its searches returned until it
    learned ats_scraper.HARD_SENIOR, and 2026-09 prod showed "Senior HR
    Specialist" and "Director of Engineering" in a student feed. Those
    searches are already scoped to entry level, so only the hard markers
    count here, never the crawler's weak-tier or frontline rules. No board
    crawl lists these rows and no liveness check revives an off_target row,
    so the verdict holds (the aggregator expiry leaves off_target alone).
    Rows hidden as another row's twin are judged too: if only their winner
    were retired, release_from_closed_winners would hand them back to the
    feed. With the CRON_ATS_RETIRE_OFF_TARGET kill switch off it rolls
    itself back instead: its off_target rows return to active
    (``restored``), or to expired when the aggregator expiry would have
    ended them meanwhile, the newest ``limit`` a run. Column-only SELECT of
    the newest ``limit`` visible rows (they age out after
    AGGREGATOR_FAST_MAX_AGE_DAYS, so the window stays small), chunked
    UPDATEs. Commits.
    """
    from backend.services.ats_scraper import HARD_SENIOR

    now = now or _utcnow()
    stats = {"checked": 0, "off_target": 0}
    if not retire_off_target_enabled():
        stats["disabled"] = True
        stats.update(_restore_off_target(
            db, now, limit, (ScrapedJob.source_platform.in_(_FAST_AGGREGATOR_SOURCES),),
            newest_first=True,
            expire_before=now - datetime.timedelta(days=AGGREGATOR_FAST_MAX_AGE_DAYS),
        ))
        if stats["restored"] or stats["expired"]:
            logger.info("retire_senior_aggregator_rows disabled, rolled back: %s", stats)
        return stats

    rows = (
        db.query(ScrapedJob.id, ScrapedJob.title)
        .filter(
            ScrapedJob.source_platform.in_(_FAST_AGGREGATOR_SOURCES),
            ScrapedJob.listing_status.in_((LISTING_ACTIVE, LISTING_STALE)),
        )
        .order_by(ScrapedJob.id.desc())
        .limit(limit)
        .all()
    )
    retire = [row_id for row_id, title in rows if HARD_SENIOR.search(title or "")]
    _update_ids(db, retire, {"listing_status": LISTING_OFF_TARGET,
                             "listing_status_changed_at": now})
    stats.update(checked=len(rows), off_target=len(retire))
    if retire:
        db.commit()

    # The verdict is re-read every run, as a board crawl re-reads its
    # listings: a title HARD_SENIOR no longer matches (the veto narrowed, e.g.
    # "Junior Planner / Planner / Senior Planner" now opens at its entry band)
    # comes back, or goes to expired if the aggregator expiry would have ended
    # it meanwhile.
    held = (
        db.query(ScrapedJob.id, ScrapedJob.title)
        .filter(
            ScrapedJob.source_platform.in_(_FAST_AGGREGATOR_SOURCES),
            ScrapedJob.listing_status == LISTING_OFF_TARGET,
        )
        .order_by(ScrapedJob.id.desc())
        .limit(limit)
        .all()
    )
    back = [row_id for row_id, title in held if not HARD_SENIOR.search(title or "")]
    if back:
        stats.update(_restore_off_target(
            db, now, limit, (ScrapedJob.id.in_(back),),
            newest_first=True,
            expire_before=now - datetime.timedelta(days=AGGREGATOR_FAST_MAX_AGE_DAYS),
        ))
    if retire or back:
        logger.info("retire_senior_aggregator_rows: %s", stats)
    return stats


# ─── Ghost-risk scoring ──────────────────────────────────────────────────────

# A source's posted_date before this is a placeholder, not a date.
_POSTED_DATE_FLOOR = datetime.datetime(2000, 1, 1)


def _opened_at(posted_date, first_seen_at):
    """When a posting opened: the source's posted_date when it is plausible
    (after 2000, and before we first saw the row: a later one is a repost
    stamp), else our first sighting. Lever lists GoPuff postings from 2021
    and Palantir's from 2016 that we first saw in 2026: measured from the
    sighting they scored as fresh."""
    if (posted_date is not None and posted_date > _POSTED_DATE_FLOOR
            and (first_seen_at is None or posted_date < first_seen_at)):
        return posted_date
    return first_seen_at


def _ghost_score(days_open: int, evergreen: bool, repost_count: int,
                 company_long_open_ratio: float, company_active: int) -> tuple[int, dict]:
    score = 0
    factors: dict = {}
    if days_open > GHOST_DAYS_OPEN:
        bump = 25 if days_open <= 90 else 40
        score += bump
        factors["days_open"] = days_open
    if evergreen:
        score += 25
        factors["evergreen"] = True
    if repost_count > 0:
        score += 20
        factors["reposts"] = repost_count
    if company_active >= 5 and company_long_open_ratio > 0.5:
        score += 15
        factors["company_long_open_ratio"] = round(company_long_open_ratio, 2)
    return min(score, 100), factors


def score_ghost_risk(db: Session, now: datetime.datetime | None = None,
                     batch_size: int = 500) -> dict:
    """Score/rescore ghost risk for active rows.

    Two passes per run:
      1. never-scored rows (factors NULL), the only pass that reads
         descriptions, to cache the evergreen flag into the factors JSON
      2. previously scored rows old enough that age-driven factors move,
         column-only, evergreen reused from the cached factors

    Days open count from when the posting opened (_opened_at: the source's
    posted_date when plausible, else our first sighting).

    Commits. Returns counts.
    """
    now = now or _utcnow()
    stats = {"scored_new": 0, "rescored": 0}

    # Company-level context, one aggregate query: active count + long-open count.
    long_open_cutoff = now - datetime.timedelta(days=GHOST_DAYS_OPEN)
    company_rows = (
        db.query(
            ScrapedJob.company,
            func.count(ScrapedJob.id),
            # SQLite lacks FILTER; a CASE sum works on both engines.
            func.sum(case((ScrapedJob.first_seen_at < long_open_cutoff, 1), else_=0)),
        )
        .filter(ScrapedJob.listing_status == LISTING_ACTIVE,
                ScrapedJob.duplicate_of.is_(None))
        .group_by(ScrapedJob.company)
        .all()
    )
    company_ctx = {
        (name or ""): (int(active or 0), int(long_open or 0))
        for name, active, long_open in company_rows
    }

    def _company_ratio(company: str) -> tuple[float, int]:
        active, long_open = company_ctx.get(company or "", (0, 0))
        return (long_open / active if active else 0.0), active

    def _repost_counts(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], int]:
        """(company, title_norm) → count of removed twins (repost signal)."""
        if not pairs:
            return {}
        norms = list({norm for _c, norm in pairs if norm})
        counts: dict[tuple[str, str], int] = {}
        for chunk in _chunks(norms):
            rows = (
                db.query(ScrapedJob.company, ScrapedJob.title_norm, func.count(ScrapedJob.id))
                .filter(ScrapedJob.listing_status == LISTING_REMOVED,
                        ScrapedJob.title_norm.in_(chunk))
                .group_by(ScrapedJob.company, ScrapedJob.title_norm)
                .all()
            )
            for company, norm, n in rows:
                counts[(company or "", norm or "")] = int(n or 0)
        return counts

    # Pass 1: never scored. Reads the description once to cache `evergreen`.
    fresh = (
        db.query(ScrapedJob.id, ScrapedJob.company, ScrapedJob.title_norm,
                 ScrapedJob.first_seen_at, ScrapedJob.posted_date, ScrapedJob.description)
        .filter(ScrapedJob.listing_status == LISTING_ACTIVE,
                ScrapedJob.duplicate_of.is_(None),
                ScrapedJob.ghost_risk_factors.is_(None))
        .order_by(ScrapedJob.id.desc())
        .limit(batch_size)
        .all()
    )
    reposts = _repost_counts([(c or "", n or "") for _i, c, n, _f, _p, _d in fresh])
    for row_id, company, title_norm, first_seen_at, posted_date, description in fresh:
        opened = _opened_at(posted_date, first_seen_at)
        days_open = (now - opened).days if opened else 0
        evergreen = looks_evergreen(description or "")
        ratio, active_n = _company_ratio(company or "")
        score, factors = _ghost_score(
            days_open, evergreen,
            reposts.get((company or "", title_norm or ""), 0),
            ratio, active_n,
        )
        factors["evergreen"] = evergreen  # cache even when False
        factors["scored_at"] = now.isoformat()
        db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(
            {"ghost_risk_score": score, "ghost_risk_factors": factors},
            synchronize_session=False,
        )
        stats["scored_new"] += 1

    # Pass 2: aging rows whose age factor may have moved. Column-only. A
    # row first seen lately can be old by its source's plausible date.
    # Least recently scored first: ordered by first sighting, the same
    # oldest batch was rescored every run and the rest kept their first
    # score for good (1,584 aging rows, 500 a run; Palantir's since July).
    aging_cutoff = now - datetime.timedelta(days=GHOST_DAYS_OPEN - 5)
    aging = (
        db.query(ScrapedJob.id, ScrapedJob.company, ScrapedJob.title_norm,
                 ScrapedJob.first_seen_at, ScrapedJob.posted_date,
                 ScrapedJob.ghost_risk_factors)
        .filter(ScrapedJob.listing_status == LISTING_ACTIVE,
                ScrapedJob.duplicate_of.is_(None),
                ScrapedJob.ghost_risk_factors.isnot(None),
                or_(ScrapedJob.first_seen_at < aging_cutoff,
                    (ScrapedJob.posted_date > _POSTED_DATE_FLOOR)
                    & (ScrapedJob.posted_date < aging_cutoff)))
        .order_by(nulls_first(ScrapedJob.ghost_risk_factors["scored_at"].as_string().asc()),
                  ScrapedJob.id.asc())
        .limit(batch_size)
        .all()
    )
    reposts = _repost_counts([(c or "", n or "") for _i, c, n, _f, _p, _g in aging])
    for row_id, company, title_norm, first_seen_at, posted_date, old_factors in aging:
        opened = _opened_at(posted_date, first_seen_at)
        days_open = (now - opened).days if opened else 0
        evergreen = bool((old_factors or {}).get("evergreen"))
        ratio, active_n = _company_ratio(company or "")
        score, factors = _ghost_score(
            days_open, evergreen,
            reposts.get((company or "", title_norm or ""), 0),
            ratio, active_n,
        )
        factors["evergreen"] = evergreen
        factors["scored_at"] = now.isoformat()
        db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(
            {"ghost_risk_score": score, "ghost_risk_factors": factors},
            synchronize_session=False,
        )
        stats["rescored"] += 1

    db.commit()
    return stats


# ─── Legacy adoption ─────────────────────────────────────────────────────────

_BOARD_URL_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"greenhouse\.io/(?:v1/boards/)?([^/?#]+)", re.IGNORECASE), "greenhouse"),
    (re.compile(r"jobs\.lever\.co/([^/?#]+)", re.IGNORECASE), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.IGNORECASE), "ashby"),
    (re.compile(r"smartrecruiters\.com/([^/?#]+)", re.IGNORECASE), "smartrecruiters"),
    (re.compile(r"https?://([^./]+)\.wd\d+\.myworkdayjobs\.com", re.IGNORECASE), "workday"),
]


def board_key_from_url(url: str) -> str:
    """Derive "{platform}:{slug}" from a direct-board URL, "" when unknown."""
    for pattern, platform in _BOARD_URL_PATTERNS:
        m = pattern.search(url or "")
        if m:
            slug = m.group(1)
            if platform == "greenhouse" and slug in ("embed", "job", "jobs"):
                continue  # embed URLs put the slug in a query param; skip
            return f"{platform}:{slug}"
    return ""


def backfill_board_keys(db: Session, limit: int = 500) -> int:
    """Adopt legacy direct rows (board_key='') into reconciliation by deriving
    their board from the URL shape. Unknown shapes get 'unknown' so the scan
    doesn't revisit them forever. Commits."""
    rows = (
        db.query(ScrapedJob.id, ScrapedJob.url)
        .filter(ScrapedJob.source_platform == "ats",
                or_(ScrapedJob.board_key.is_(None), ScrapedJob.board_key == ""))
        .limit(limit)
        .all()
    )
    adopted = 0
    for row_id, url in rows:
        key = board_key_from_url(url or "") or "unknown"
        db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(
            {"board_key": key}, synchronize_session=False,
        )
        if key != "unknown":
            adopted += 1
    if rows:
        db.commit()
    return adopted


# ─── URL liveness probing ────────────────────────────────────────────────────

async def probe_url_liveness(client, url: str) -> str:
    """One posting's verdict as a plain string: 'dead', 'alive' or 'unknown'.
    Platform-aware (Workday CXS, SmartRecruiters/Greenhouse/Lever/Oracle APIs,
    Ashby board membership, LinkedIn's closed banner); see
    services/platform_liveness.py for what each verdict rests on."""
    results = await check_listings(client, [url] if url else [], concurrency=1)
    result = results.get(url)
    return result.verdict if result else "unknown"


async def probe_urls_liveness(client, urls: list[str], *, concurrency: int = 8,
                              budget: int = 80) -> dict[str, str]:
    """Probe up to ``budget`` URLs concurrently. Returns {url: verdict};
    URLs past the budget are simply absent (treated as unverified)."""
    urls = [u for u in urls if u][:budget]
    if not urls:
        return {}
    results = await check_listings(client, urls, concurrency=concurrency)
    return {url: result.verdict for url, result in results.items()}


def mark_listing_removed(db: Session, row_id: int,
                         now: datetime.datetime | None = None,
                         reason: str = CLOSED_404) -> None:
    """Soft-remove one listing (dead apply URL), closed for ``reason``.
    Commit is the caller's."""
    now = now or _utcnow()
    db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(
        {"listing_status": LISTING_REMOVED, "listing_status_changed_at": now,
         "closed_reason": reason},
        synchronize_session=False,
    )


def _update_ids(db: Session, ids: list[int], values: dict) -> None:
    for chunk in _chunks(ids):
        db.query(ScrapedJob).filter(ScrapedJob.id.in_(chunk)).update(
            values, synchronize_session=False,
        )


def _liveness_outcome(listing_status: str, result) -> str:
    """What a check means for a row: 'removed' (dead), 'revived' (a
    non-active row the platform's own API/board vouched for), 'confirmed'
    (an active one it vouched for) or 'unverified' (anything else, including
    a page that merely loaded)."""
    if result.verdict == DEAD:
        return "removed"
    if result.verdict == ALIVE and result.authoritative:
        # Open is not on target: only a listing that passes the crawler's
        # filters brings an off_target row back (reconcile_board).
        if listing_status in (LISTING_ACTIVE, LISTING_OFF_TARGET):
            return "confirmed"
        return "revived"
    return "unverified"


def record_liveness(db: Session, row_id: int, listing_status: str, result,
                    now: datetime.datetime | None = None) -> str:
    """Apply one platform_liveness result to one row, by the same rules as
    the verify sweeps: stamp ``last_probed_at``; dead → removed (closed_reason
    from the verdict's reason, closed_reason_for); only an authoritative
    alive bumps ``last_seen_at`` and revives. Returns the row's
    listing_status afterwards. Commit is the caller's."""
    now = now or _utcnow()
    outcome = _liveness_outcome(listing_status, result)
    values: dict = {"last_probed_at": now}
    if outcome == "removed":
        values.update(listing_status=LISTING_REMOVED, listing_status_changed_at=now,
                      closed_reason=closed_reason_for(result.reason))
    elif outcome in ("confirmed", "revived"):
        values["last_seen_at"] = now
        if outcome == "revived":
            values.update(listing_status=LISTING_ACTIVE, listing_status_changed_at=now,
                          closed_reason=None)
    db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(
        values, synchronize_session=False,
    )
    return values.get("listing_status", listing_status)


def _verify_candidates(now: datetime.datetime, recheck_hours: int):
    """Filters every verify sweep shares: rows the feed can show, not probed
    within the recheck window, and not on a host we never judge."""
    recheck_cutoff = now - datetime.timedelta(hours=recheck_hours)
    return (
        ScrapedJob.duplicate_of.is_(None),
        func.trim(func.coalesce(ScrapedJob.company, "")) != "",
        or_(ScrapedJob.last_probed_at.is_(None),
            ScrapedJob.last_probed_at < recheck_cutoff),
        ScrapedJob.url.notilike("%indeed.com/%"),
    )


def _verify_columns():
    """What a verify sweep reads per row (column-only)."""
    return (ScrapedJob.id, ScrapedJob.url, ScrapedJob.listing_status, ScrapedJob.board_key)


async def _verify_rows(db: Session, client, rows: list, *, now: datetime.datetime,
                       deadline: float | None, label: str,
                       cache: dict | None = None) -> dict:
    """Check ``rows`` ((id, url, listing_status, board_key)) and apply the
    verdicts:

      - dead → ``removed``
      - authoritative alive (the platform's own API/board) → ``last_seen_at``
        bumped, and a stale row comes back to ``active``
      - anything else (bot wall, SPA shell, a page that merely loaded) → only
        ``last_probed_at``, never a revival

    Every checked row gets ``last_probed_at``. A check that got no answer
    (host skipped after failures, the run's budget for the host spent, no
    turn at its gate, a 429/999 rate limit) is deferred like a row the
    deadline cut off: no stamp, so it stays first in line for the next run.
    Chunked, each chunk commits; once ``deadline`` passes no new checks
    start. ``cache`` shares per-run liveness state (host breakers, LinkedIn
    budget, Ashby boards) with other sweeps of the same run. Returns counts.
    """
    stats = {"checked": 0, "removed": 0, "revived": 0, "confirmed": 0,
             "unverified": 0, "deferred": 0}
    reasons: Counter = Counter()
    cache = {} if cache is None else cache
    for index, chunk in enumerate(_chunks(rows, _VERIFY_CHUNK)):
        if deadline is not None and time.monotonic() >= deadline:
            stats["deferred"] += len(rows) - index * _VERIFY_CHUNK
            break
        results = await check_listings(
            client, [row[1] for row in chunk],
            concurrency=_VERIFY_CONCURRENCY, deadline=deadline, cache=cache,
            board_keys={row[1]: row[3] or "" for row in chunk},
        )
        probed, confirmed, revived = [], [], []
        removed: dict[str, list[int]] = {}
        for row_id, url, listing_status, _board_key in chunk:
            result = results.get(url)
            if result is None:
                stats["deferred"] += 1
                continue
            reasons[result.reason] += 1
            if is_deferred(result):
                stats["deferred"] += 1
                continue
            probed.append(row_id)
            outcome = _liveness_outcome(listing_status, result)
            if outcome == "removed":
                removed.setdefault(closed_reason_for(result.reason), []).append(row_id)
            elif outcome in ("confirmed", "revived"):
                confirmed.append(row_id)
                if outcome == "revived":
                    revived.append(row_id)
            else:
                stats["unverified"] += 1

        _update_ids(db, probed, {"last_probed_at": now})
        for reason, ids in removed.items():
            _update_ids(db, ids, {"listing_status": LISTING_REMOVED,
                                  "listing_status_changed_at": now, "closed_reason": reason})
        _update_ids(db, confirmed, {"last_seen_at": now})
        _update_ids(db, revived, {"listing_status": LISTING_ACTIVE,
                                  "listing_status_changed_at": now, "closed_reason": None})
        if probed:
            db.commit()
        stats["checked"] += len(probed)
        stats["removed"] += sum(len(ids) for ids in removed.values())
        stats["confirmed"] += len(confirmed)
        stats["revived"] += len(revived)

    stats["reasons"] = dict(reasons.most_common(12))
    logger.info("verify %s: %s", label, stats)
    return stats


async def verify_recent_aggregator_listings(db: Session, client,
                                            limit: int = RECENT_VERIFY_BUDGET,
                                            now: datetime.datetime | None = None,
                                            *, deadline: float | None = None,
                                            recheck_hours: int = VERIFY_RECHECK_HOURS,
                                            linkedin_quota: int = RECENT_LINKEDIN_QUOTA,
                                            cache: dict | None = None) -> dict:
    """Probe visible aggregator rows no board reconciles: GitHub-list rows
    (the curated lists re-publish already-closed roles, and their links point
    at Ashby/Greenhouse/Workday, which the platform checks read honestly) and
    LinkedIn rows (a big share of aged ones are soft-dead, 200 + "no longer
    accepting applications"). Indeed is excluded: Cloudflare walls the probe.

    Least-recently-probed first (newest first among never-probed), so the
    whole backlog rotates instead of the newest rows eating every run. At
    most ``linkedin_quota`` LinkedIn rows per run, the rest of the budget
    goes to the other rows. Commits. Returns counts.
    """
    now = now or _utcnow()
    effective_date = func.coalesce(ScrapedJob.posted_date, ScrapedJob.scraped_at)
    candidates = (
        db.query(*_verify_columns())
        .filter(
            ScrapedJob.listing_status.in_((LISTING_ACTIVE, LISTING_STALE)),
            ScrapedJob.source_platform.in_(("github", "linkedin")),
            *_verify_candidates(now, recheck_hours),
        )
        .order_by(nulls_first(ScrapedJob.last_probed_at.asc()),
                  effective_date.desc(), ScrapedJob.id.desc())
    )
    linkedin = candidates.filter(_linkedin_row()).limit(max(0, min(limit, linkedin_quota))).all()
    others = candidates.filter(not_(_linkedin_row())).limit(max(0, limit - len(linkedin))).all()
    return await _verify_rows(db, client, linkedin + others, now=now, deadline=deadline,
                              label="recent_aggregator", cache=cache)


# ─── Direct-row URL verification ─────────────────────────────────────────────

async def verify_stale_listings(db: Session, client, limit: int = STALE_VERIFY_BUDGET,
                                now: datetime.datetime | None = None,
                                *, deadline: float | None = None,
                                recheck_hours: int = VERIFY_RECHECK_HOURS,
                                cache: dict | None = None) -> dict:
    """Work through the stale backlog, least-recently-probed first (newest
    first among never-probed), so every stale row gets its turn even at ~6
    runs a day. Dead → removed; revived ONLY when the platform's own API or
    board says the posting is open (a Workday or SmartRecruiters page answers
    200 for closed postings too). Anything inconclusive stamps
    ``last_probed_at`` and waits out the recheck window; STALE_TERMINAL_DAYS
    without positive evidence ends it (sweep_terminal_expiry). Commits."""
    now = now or _utcnow()
    effective_date = func.coalesce(ScrapedJob.posted_date, ScrapedJob.scraped_at)
    rows = (
        db.query(*_verify_columns())
        .filter(
            ScrapedJob.listing_status == LISTING_STALE,
            *_verify_candidates(now, recheck_hours),
        )
        .order_by(nulls_first(ScrapedJob.last_probed_at.asc()),
                  effective_date.desc(), ScrapedJob.id.desc())
        .limit(limit)
        .all()
    )
    return await _verify_rows(db, client, rows, now=now, deadline=deadline, label="stale",
                              cache=cache)


async def verify_unconfirmed_active_listings(db: Session, client,
                                             limit: int = UNCONFIRMED_VERIFY_BUDGET,
                                             now: datetime.datetime | None = None,
                                             *, deadline: float | None = None,
                                             unconfirmed_hours: int = UNCONFIRMED_AFTER_HOURS,
                                             recheck_hours: int = VERIFY_RECHECK_HOURS,
                                             cache: dict | None = None) -> dict:
    """Active direct-board rows no crawl has re-listed for
    ``unconfirmed_hours``: rows past a partial crawl's page cap (big Workday
    and SmartRecruiters boards never complete), boards we don't crawl, and
    rows the old verifier revived on a bare 200. Checking them here kills the
    dead ones before they sit visible for the 72h stale TTL; an authoritative
    alive bumps ``last_seen_at`` so live ones never go stale. Oldest
    confirmation first. Commits."""
    now = now or _utcnow()
    unconfirmed_cutoff = now - datetime.timedelta(hours=unconfirmed_hours)
    rows = (
        db.query(*_verify_columns())
        .filter(
            ScrapedJob.listing_status == LISTING_ACTIVE,
            ScrapedJob.source_platform == "ats",
            or_(ScrapedJob.last_seen_at.is_(None),
                ScrapedJob.last_seen_at < unconfirmed_cutoff),
            *_verify_candidates(now, recheck_hours),
        )
        .order_by(nulls_first(ScrapedJob.last_probed_at.asc()),
                  nulls_first(ScrapedJob.last_seen_at.asc()), ScrapedJob.id.asc())
        .limit(limit)
        .all()
    )
    return await _verify_rows(db, client, rows, now=now, deadline=deadline,
                              label="unconfirmed_active", cache=cache)
