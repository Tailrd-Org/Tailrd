"""
Cross-source job dedup: the same posting scraped from LinkedIn/Indeed AND from
the employer's own board (ats/github sources).

The direct row is strictly better (real description, direct apply link) so
inferior twins are soft-hidden (`duplicate_of` = winner id, never deleted:
saved-job and application records may reference them) and the winner inherits
whatever the twin knew that it doesn't (applicant_count, salary_range, a
description when the winner has none, and a real logo when the winner's is
generated).

Matching is deliberately exact, never fuzzy: normalized employer + normalized
title + city containment. "Software Engineer Intern, Infrastructure" is a
different job from "Software Engineer Intern" and must never merge.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, aliased

from backend.db.models import ScrapedJob
from backend.services.listing_freshness import (
    HIDDEN_LISTING_STATUSES,
    LISTING_ACTIVE,
    LISTING_OFF_TARGET,
    LISTING_REMOVED,
    LISTING_STALE,
)
from backend.services.location_parser import fold
from backend.services.logo_cache import logo_quality

logger = logging.getLogger(__name__)

DIRECT_SOURCES = ("ats", "github")
INFERIOR_SOURCES = ("linkedin", "indeed")
# A v2 adapter of this tier or above (Tier 3: Adzuna, Jooble; Tier 4) is an
# aggregator: its rows are stored as source_platform 'ats', like a board
# crawl's, but they re-list other employers' postings. By URL they are their
# aggregator's (effective_source), an inferior source like LinkedIn and
# Indeed: the employer's own row hides them, and they never hide anything.
AGGREGATOR_TIER = 3

# Lower is better. Direct rows beat aggregator rows; LinkedIn beats Indeed
# (richer metadata) when no direct row exists; an aggregator adapter's row
# comes last (the default, 3).
_SOURCE_TIER = {"ats": 0, "github": 0, "linkedin": 1, "indeed": 2}

# (adapter registry, ((platform, compiled HOST_PATTERNS), ...)) of the
# aggregator adapters, rebuilt when the registry is (tests swap it).
_aggregators: tuple = (None, ())


def _aggregator_patterns() -> tuple:
    """((platform, compiled HOST_PATTERNS), ...) of every v2 adapter of
    AGGREGATOR_TIER or above, enabled or not. Read on use, never at import
    (the adapter registry must not be asked at import time)."""
    global _aggregators
    from backend.ingest import adapters

    registry = adapters._adapters()
    if _aggregators[0] is not registry:
        found = []
        for platform, module in sorted(registry.items()):
            tier = getattr(module, "TIER", None)
            if isinstance(tier, int) and not isinstance(tier, bool) and tier >= AGGREGATOR_TIER:
                patterns = getattr(module, "HOST_PATTERNS", ()) or ()
                if isinstance(patterns, (str, re.Pattern)):
                    patterns = (patterns,)
                compiled = tuple(p for p in (adapters._compile(x) for x in patterns) if p is not None)
                found.append((platform, compiled))
        _aggregators = (registry, tuple(found))
    return _aggregators[1]


def aggregator_platforms() -> tuple[str, ...]:
    """The platforms of the aggregator adapters (AGGREGATOR_TIER)."""
    return tuple(platform for platform, _patterns in _aggregator_patterns())


def is_inferior_source(source: str) -> bool:
    """An effective source whose copies the employer's own row hides:
    LinkedIn, Indeed, or an aggregator adapter."""
    return source in INFERIOR_SOURCES or source in aggregator_platforms()


def _inferior_row_filter():
    """SQL: a row that may be an inferior copy (is_inferior_source decides):
    a LinkedIn/Indeed source or URL, or a row of an aggregator adapter's
    board."""
    return or_(
        ScrapedJob.source_platform.in_(INFERIOR_SOURCES),
        # Rogue-era rows are labeled 'ats' but carry aggregator URLs.
        ScrapedJob.url.ilike("%linkedin.com%"),
        ScrapedJob.url.ilike("%indeed.com%"),
        *(ScrapedJob.board_key.startswith(f"{platform}:", autoescape=True)
          for platform in aggregator_platforms()),
    )


# Only aggregator copies may ever be hidden. Two direct-board rows with the
# same title are distinct requisitions (per-country variants, multiple
# openings), their dedup key is the URL, nothing else.
_MIN_COPY_DESC_LEN = 300  # og:description snippets (~186 chars) must not spread

# Secondary fuzzy matcher for aggregator rows whose normalized title differs
# from the direct row's by punctuation-scale noise ("Software Engineer Intern
# Payments" vs "Software Engineer Intern - Payments Team"). Deliberately NOT
# embeddings: deterministic, free, and conservative, a wrong merge hides a
# real job. Both titles must be substantial and near-identical.
FUZZY_TITLE_THRESHOLD = 0.93
_FUZZY_MIN_TITLE_LEN = 12


def titles_fuzzy_match(a: str, b: str) -> bool:
    """True when two ALREADY-NORMALIZED titles are near-identical."""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b or a == b:
        return a == b and len(a) >= 1
    if len(a) < _FUZZY_MIN_TITLE_LEN or len(b) < _FUZZY_MIN_TITLE_LEN:
        return False
    # One title extending the other with a new qualifier ("… infrastructure")
    # is a DIFFERENT job; require the length gap itself to be small.
    if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.2:
        return False
    return SequenceMatcher(None, a, b).ratio() >= FUZZY_TITLE_THRESHOLD


def effective_source(source: str, url: str) -> str:
    """The rogue-scraper era mislabeled LinkedIn/Indeed rows as source='ats';
    the URL is the truth. An aggregator adapter's row ('ats' too) is its
    aggregator's (the adapter whose HOST_PATTERNS own the URL)."""
    blob = (url or "").lower()
    if "linkedin.com" in blob:
        return "linkedin"
    if "indeed.com" in blob:
        return "indeed"
    if source == "ats" and blob:
        aggregators = _aggregator_patterns()
        if aggregators:
            try:
                host = (urlsplit(url).hostname or "").lower()
            except ValueError:
                host = ""
            for platform, patterns in aggregators:
                # As adapters.adapter_for_url reads them: in the URL or its host.
                if any(p.search(url) or (host and p.search(host)) for p in patterns):
                    return platform
    return source or ""


# Query params that only say who sent the click: utm_* everywhere, and the
# lists' own attribution ('?ref=Simplify', '&ref=vansh'). Everything else an
# ATS URL carries (gh_jid, jobid, token) identifies the posting.
_ATTRIBUTION_PARAMS = frozenset({"ref"})


def _is_tracking_param(segment: str) -> bool:
    key = segment.split("=", 1)[0].lower()
    return key.startswith("utm_") or key in _ATTRIBUTION_PARAMS


def canonical_url(url: str) -> str:
    """Strip tracking params so 'jobs/86588?utm_source=vansh' and 'jobs/86588'
    dedupe as one posting. Only utm_* and list attribution (ref=) go: ATS URLs
    carry functional params (gh_jid, jobid, token) that must survive, and the
    kept ones keep their exact spelling ('?t=gh_src=&gh_jid=1' stays byte for
    byte what the board crawl stores; re-encoding it to 't=gh_src%3D' made the
    same posting a second row)."""
    raw = (url or "").strip()
    if not raw or "?" not in raw:
        return raw.rstrip("/") if raw else ""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw
    from urllib.parse import urlunsplit

    kept = [seg for seg in parts.query.split("&") if seg and not _is_tracking_param(seg)]
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, "&".join(kept), "")
    ).rstrip("/")


_LINKEDIN_JOB_ID_RE = re.compile(r"(\d{6,})(?!.*\d{6,})")


def posting_key(url: str) -> str:
    """The posting a URL names, host spelling aside: LinkedIn's numeric job
    id ('ca.linkedin.com/jobs/view/dev-at-ibm-4462216626' and
    'www.linkedin.com/jobs/view/4462216626' are one posting), Indeed's jk,
    otherwise the utm-free URL. Two rows with different keys are different
    postings, even when employer, title and city match (a repost)."""
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    host = (parts.hostname or "").lower()
    query = {k.lower(): v for k, v in parse_qsl(parts.query)}
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        job_id = (query.get("currentjobid") or "").strip()
        if not job_id:
            match = _LINKEDIN_JOB_ID_RE.search(parts.path)
            job_id = match.group(1) if match else ""
        if job_id:
            return f"linkedin:{job_id}"
    if host == "indeed.com" or host.endswith(".indeed.com"):
        job_key = (query.get("jk") or query.get("vjk") or "").strip().lower()
        if job_key:
            return f"indeed:{job_key}"
    return canonical_url(raw).lower()


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_GREENHOUSE_JOB_RE = re.compile(r"/jobs/(\d{5,})")
# Workday posting paths end in '_<requisition>' plus an optional '-1'-style
# repost suffix: '.../Firmware-Engineer-Intern_R123', '..._2618885-1'. An
# apply step ('/apply', '/apply/applyManually') is the same posting.
_WORKDAY_REQ_RE = re.compile(r"_([A-Za-z]{0,6}[-_]?\d[\w.]*?)(-\d{1,2})?$")
# What is left once a '-N' is stripped must still name a requisition: at
# least four digits, and not just a year. '_2024-12' and '_2024-13' (or
# '_R2024-15' and '_R2024-16') are two requisitions, not reposts of '2024'.
# Every suffixed Workday URL in prod on 2026-09-29 keeps 4+ non-year digits
# ('JR5108-1', 'R-5994-1', '2618885-1').
_WORKDAY_YEAR_ONLY_RE = re.compile(r"[A-Za-z]{0,6}[-_]?(?:19|20)\d\d")
_WORKDAY_MIN_REQ_DIGITS = 4


def _workday_requisition(requisition: str, repost: str) -> str:
    """The requisition a Workday path names: the '-N' repost suffix goes only
    when what remains still identifies a requisition on its own."""
    if repost and (sum(ch.isdigit() for ch in requisition) < _WORKDAY_MIN_REQ_DIGITS
                   or _WORKDAY_YEAR_ONLY_RE.fullmatch(requisition)):
        return requisition + repost
    return requisition
_WORKDAY_APPLY_TAIL_RE = re.compile(r"/apply(?:/[^/]*)?$", re.I)
# myworkdaysite.com carries the tenant in the path, not the host:
# 'wd3.myworkdaysite.com/en-US/recruiting/cibc/campus/job/...'.
_WORKDAY_SITE_TENANT_RE = re.compile(r"/recruiting/([^/]+)/", re.I)


def posting_identity(url: str) -> str:
    """The ATS posting a direct URL names, whatever the spelling: the same
    Greenhouse job behind boards./job-boards. and an employer-hosted gh_jid
    page, a Workday requisition behind '/en-US/' locale prefixes, site
    aliases ('campus' vs 'search'), '-1' repost suffixes and '/apply' steps,
    a Lever/Ashby posting UUID. Falls back to posting_key(). Only ever
    compared between rows of the same employer and title, so two postings
    it happens to fold together must also share both to merge."""
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw.lower()
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    query = {k.lower(): v for k, v in parse_qsl(parts.query)}
    gh_jid = (query.get("gh_jid") or "").strip()
    if gh_jid.isdigit():
        return f"greenhouse:{gh_jid}"
    if host.endswith("greenhouse.io"):
        match = _GREENHOUSE_JOB_RE.search(path)
        if match:
            return f"greenhouse:{match.group(1)}"
        token = (query.get("token") or "").strip()
        if token.isdigit():
            return f"greenhouse:{token}"
    if host.endswith((".myworkdayjobs.com", ".myworkdaysite.com")):
        match = _WORKDAY_REQ_RE.search(_WORKDAY_APPLY_TAIL_RE.sub("", path))
        tenant = host.split(".")[0]
        if host.endswith(".myworkdaysite.com"):
            site = _WORKDAY_SITE_TENANT_RE.search(path)
            tenant = site.group(1).lower() if site else ""
        if match and tenant:
            requisition = _workday_requisition(match.group(1), match.group(2) or "")
            return f"workday:{tenant}:{requisition.lower()}"
    if host in ("jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com"):
        match = _UUID_RE.search(path)
        if match:
            platform = "lever" if "lever" in host else "ashby"
            return f"{platform}:{match.group(0).lower()}"
    return posting_key(raw)


_SEASON_WORDS = re.compile(r"\b(summer|fall|autumn|winter|spring)\b")
_YEARS = re.compile(r"\b20\d{2}\b")
_PARENTHETICAL = re.compile(r"\([^)]*\)")
_COMPANY_SUFFIX = re.compile(
    r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company)\b"
)


def normalize_title(title: str) -> str:
    """Fold a title for twin matching. Season/year decorations vary across
    boards and are stripped; role-level words (intern, new grad, senior) are
    kept, different levels are different jobs."""
    text = fold(_PARENTHETICAL.sub(" ", title or ""))
    text = _SEASON_WORDS.sub(" ", text)
    text = _YEARS.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_company(company: str) -> str:
    """Fold a company name for grouping ("Acme Widgets Inc." == "Acme Widgets")."""
    text = fold(company or "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = _COMPANY_SUFFIX.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _cities_compatible(
    loser_city: str,
    winner_city: str,
    winner_search: str,
    loser_country: str = "",
    winner_country: str = "",
) -> bool:
    loser_city = fold(loser_city or "")
    winner_city = fold(winner_city or "")
    if not loser_city and not winner_city:
        # Both remote/unlocated: same-country only ("Remote US" and
        # "Remote Canada" are different postings).
        return (loser_country or "") == (winner_country or "")
    if not loser_city or not winner_city:
        return False
    return loser_city == winner_city or f"|{loser_city}|" in (winner_search or "")


# Since scraper v2 a board stores every level, and normalize_title drops a
# parenthetical: "Software Engineer (New Grad)" and a board's mid-level
# "Software Engineer" share a title_norm. A student-feed copy is never the
# twin of a row above entry level.
_STUDENT_FEED_LEVELS = ("internship", "new_grad", "entry")


def same_audience(a: str | None, b: str | None) -> bool:
    """Both rows are student-feed level (or carry no level, as rows from
    before the levels may), or neither is."""
    def student(level):
        return not level or level in _STUDENT_FEED_LEVELS
    return student(a) == student(b)


def _employer_filter(company: str, company_domain: str):
    """SQL predicate: same employer by resolved domain OR case-folded name."""
    conditions = [func.lower(ScrapedJob.company) == (company or "").strip().lower()]
    domain = (company_domain or "").strip()
    if domain:
        conditions.append(ScrapedJob.company_domain == domain)
    return or_(*conditions)


# ─── Which rows may stand in for their twins ────────────────────────────────
#
# A row hides (absorbs) its twins, and answers for them on a deep link, only
# while it is visible. A closed aggregator row speaks for its own posting id
# alone: LinkedIn/Indeed reposts carry new ids, and the 21-day age-out says
# nothing about a newer copy. The one closed row that still speaks for its
# mirrors is a direct (ats/github) row the board or the platform check
# REMOVED: that is a death verdict on the employer's own requisition, and
# LinkedIn keeps "apply on company site" mirrors of closed requisitions open
# (their apply link lands on the employer's 404). ``expired`` is only age or
# missing evidence, never a death verdict, so an expired row speaks for no
# one.

# Open, not closed. ``stale`` is hidden from the feed since scraper v2, but it
# is no closure (usually a board crawl that is behind), so it counts as open here.
_VISIBLE_LISTING_STATUSES = (LISTING_ACTIVE, LISTING_STALE)
# An ``off_target`` direct row speaks for its mirrors too: the employer's own
# listing says the posting is not entry level or not in North America, and a
# LinkedIn copy of the same posting is no more on target than the original,
# so its twins stay hidden rather than coming back in its place. So does a
# ``stale`` direct row: it usually recovers, and a mirror released while it
# was stale would then sit beside it as a duplicate nothing hides again.
_SPEAKS_WHEN_HIDDEN = (LISTING_REMOVED, LISTING_OFF_TARGET, LISTING_STALE)


def stands_in_for_twins(listing_status: str | None, source_platform: str, url: str) -> bool:
    """True when a row may hide its twins or answer for them (see above)."""
    if listing_status not in HIDDEN_LISTING_STATUSES:
        return True
    return (listing_status in _SPEAKS_WHEN_HIDDEN
            and effective_source(source_platform or "", url or "") in DIRECT_SOURCES)


def _stands_in_filter():
    """SQL side of stands_in_for_twins. Loose on one point: a rogue-era 'ats'
    row with an aggregator URL passes here and is rejected in Python."""
    return or_(
        ScrapedJob.listing_status.is_(None),
        ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES),
        and_(ScrapedJob.listing_status.in_(_SPEAKS_WHEN_HIDDEN),
             ScrapedJob.source_platform.in_(DIRECT_SOURCES)),
    )


# Column-only view of a possible absorber: id, source_platform, url,
# description length, city, location_search, country, title_norm, status,
# experience level.
_TWIN_COLUMNS = (
    ScrapedJob.id, ScrapedJob.source_platform, ScrapedJob.url,
    func.length(func.coalesce(ScrapedJob.description, "")),
    ScrapedJob.city, ScrapedJob.location_search, ScrapedJob.country,
    ScrapedJob.title_norm, ScrapedJob.listing_status, ScrapedJob.experience_level,
)


def _find_absorber(db: Session, *, row_id: int, source: str, title_norm: str,
                   company: str, company_domain: str, city: str, country: str,
                   desc_len: int, experience_level: str = "") -> int | None:
    """The id of the row that should hide this aggregator row: a better twin
    (a direct row, or a same-tier aggregator row that has a description when
    this one doesn't, the older one when both do) for the same employer,
    title and city, and of the same audience (same_audience on the row's
    ``experience_level``), among the rows that may stand in for it. Exact
    title first; failing that, a near-identical DIRECT title only
    (fuzzy-merging two aggregator copies risks eating a genuinely different
    posting). Column-only."""
    row_tier = _SOURCE_TIER.get(source, 3)

    def pick(twins) -> int | None:
        for (twin_id, twin_source, twin_url, twin_desc_len, twin_city,
             twin_search, twin_country, _norm, twin_status, twin_level) in twins:
            if not stands_in_for_twins(twin_status, twin_source, twin_url):
                continue
            if not same_audience(experience_level, twin_level):
                continue
            twin_tier = _SOURCE_TIER.get(effective_source(twin_source or "", twin_url or ""), 3)
            better = twin_tier < row_tier or (
                twin_tier == row_tier
                and (twin_desc_len or 0) >= 50
                and (desc_len < 50 or twin_id < row_id)
            )
            if not better:
                continue
            if not _cities_compatible(city, twin_city or "", twin_search or "",
                                      country, twin_country or ""):
                continue
            return twin_id
        return None

    twins = (
        db.query(*_TWIN_COLUMNS)
        .filter(
            ScrapedJob.duplicate_of.is_(None),
            _stands_in_filter(),
            ScrapedJob.title_norm == title_norm,
            ScrapedJob.id != row_id,
            _employer_filter(company, company_domain),
        )
        .limit(20)
        .all()
    )
    best = pick(twins)
    if best is not None:
        return best
    near = (
        db.query(*_TWIN_COLUMNS)
        .filter(
            ScrapedJob.duplicate_of.is_(None),
            _stands_in_filter(),
            ScrapedJob.source_platform.in_(DIRECT_SOURCES),
            ScrapedJob.title_norm != title_norm,
            ScrapedJob.title_norm != "",
            ScrapedJob.id != row_id,
            _employer_filter(company, company_domain),
        )
        .limit(40)
        .all()
    )
    return pick([twin for twin in near if titles_fuzzy_match(title_norm, twin[7] or "")])


def has_direct_twin(
    db: Session,
    *,
    company: str,
    company_domain: str,
    title: str,
    city: str,
    country: str = "",
    experience_level: str = "",
) -> bool:
    """True when a direct (ats/github) row for the same employer, title, and
    city, and of the same audience as ``experience_level`` (same_audience:
    the default, no level, is the student feed's, which the LinkedIn and
    Indeed searches are), already exists, the ingest guard for
    LinkedIn/Indeed sources. An expired direct row doesn't count (no death
    verdict, the new copy may be live); a removed one does
    (stands_in_for_twins)."""
    title_norm = normalize_title(title)
    if not title_norm:
        return False
    city = fold(city or "")

    query = (
        db.query(
            ScrapedJob.id, ScrapedJob.city, ScrapedJob.location_search,
            ScrapedJob.country, ScrapedJob.url, ScrapedJob.source_platform,
            ScrapedJob.listing_status, ScrapedJob.experience_level,
        )
        .filter(
            ScrapedJob.duplicate_of.is_(None),
            ScrapedJob.source_platform.in_(DIRECT_SOURCES),
            _stands_in_filter(),
            ScrapedJob.title_norm == title_norm,
            _employer_filter(company, company_domain),
        )
        .limit(20)
    )
    for (_id, winner_city, winner_search, winner_country, url, source, status,
         winner_level) in query.all():
        if effective_source(source, url) not in DIRECT_SOURCES:
            continue  # rogue-era mislabel: an aggregator URL is not a direct twin
        if not stands_in_for_twins(status, source, url):
            continue
        if not same_audience(experience_level, winner_level):
            continue
        if _cities_compatible(city, winner_city or "", winner_search or "",
                              country or "", winner_country or ""):
            return True
    return False


def mark_inferior_twins(db: Session, winner: ScrapedJob) -> int:
    """Hide pre-existing LinkedIn/Indeed (or aggregator adapter) twins of a
    freshly inserted direct row (same employer, title and city, and the same
    audience: same_audience), pulling their enrichment fields onto the
    winner. A row that is no direct row itself (an aggregator adapter's)
    hides nothing. Returns the number of rows hidden. Commits."""
    title_norm = winner.title_norm or normalize_title(winner.title)
    if not title_norm or winner.id is None:
        return 0
    if effective_source(winner.source_platform or "", winner.url or "") not in DIRECT_SOURCES:
        return 0

    candidates = (
        db.query(ScrapedJob)
        .filter(
            ScrapedJob.duplicate_of.is_(None),
            _inferior_row_filter(),
            ScrapedJob.title_norm == title_norm,
            ScrapedJob.id != winner.id,
            _employer_filter(winner.company, winner.company_domain or ""),
        )
        .limit(50)
        .all()
    )

    marked = 0
    for twin in candidates:
        if not is_inferior_source(effective_source(twin.source_platform, twin.url)):
            continue
        if not same_audience(twin.experience_level, winner.experience_level):
            continue
        if not _cities_compatible(twin.city or "", winner.city or "",
                                  winner.location_search or "",
                                  twin.country or "", winner.country or ""):
            continue
        twin.duplicate_of = winner.id
        if winner.applicant_count is None and twin.applicant_count is not None:
            winner.applicant_count = twin.applicant_count
        if not (winner.salary_range or "") and (twin.salary_range or ""):
            winner.salary_range = twin.salary_range
        if (len(winner.description or "") < 50
                and len(twin.description or "") >= _MIN_COPY_DESC_LEN):
            winner.description = twin.description
            winner.description_sections = None
        _inherit_logo(winner, twin)
        marked += 1
    if marked:
        db.commit()
    return marked


def _inherit_logo(winner: ScrapedJob, twin: ScrapedJob) -> None:
    """A LinkedIn twin often carries the employer's real logo while the direct
    winner has only a generated favicon; hiding the twin must not hide the
    logo too."""
    if logo_quality(winner.company_logo) == 0 and logo_quality(twin.company_logo) > 0:
        winner.company_logo = twin.company_logo


def release_from_closed_winners(db: Session, *, clear_probe: bool = True) -> list[tuple]:
    """Give back the LinkedIn/Indeed rows hidden behind a winner that has
    since closed and can no longer stand in for them (stands_in_for_twins):
    an older copy that aged out or whose own posting died, while this copy,
    a repost with a posting id of its own, may well be live.

    Each such row (active or stale itself, a different posting_key from its
    winner) either moves under a better twin that is still visible, or
    comes back to the feed with ``last_probed_at`` cleared, so the
    least-recently-probed-first verifier checks it before anything else and
    the dead share leaves again quickly. Rows sharing the winner's posting
    id stay hidden: that posting's closure is theirs too. ``clear_probe``
    False skips the probe stamp (a database without the column yet).

    Order-proof: rows go best first, in _find_absorber's own ``better``
    order, so a copy released early is the better twin its later same-job
    rows find; and one posting (posting_key) comes back once, its other
    copies going under the one released, even when neither copy is
    ``better`` (no description on either).

    Returns (row id, old winner id, new winner id or None) per row moved.
    Column-only reads; one UPDATE per row by id. Commits."""
    winner = aliased(ScrapedJob)
    rows = (
        db.query(
            ScrapedJob.id, ScrapedJob.url, ScrapedJob.source_platform,
            ScrapedJob.company, ScrapedJob.company_domain, ScrapedJob.title,
            ScrapedJob.title_norm, ScrapedJob.city, ScrapedJob.country,
            func.length(func.coalesce(ScrapedJob.description, "")),
            winner.id, winner.url, winner.source_platform, winner.listing_status,
            ScrapedJob.experience_level,
        )
        .join(winner, winner.id == ScrapedJob.duplicate_of)
        .filter(
            ScrapedJob.listing_status.in_(_VISIBLE_LISTING_STATUSES),
            winner.listing_status.in_(HIDDEN_LISTING_STATUSES),
        )
        .all()
    )
    # Mirrors ``better`` in _find_absorber: lower tier, then a description,
    # then the older id. Not the longer description: the older of two
    # described copies is the better one, so it must come back first.
    rows.sort(key=lambda r: (
        _SOURCE_TIER.get(effective_source(r[2] or "", r[1] or ""), 3),
        0 if (r[9] or 0) >= 50 else 1,
        r[0],
    ))

    moved: list[tuple] = []
    released: dict[str, int] = {}  # posting_key -> the copy this pass released
    for (row_id, url, source, company, domain, title, title_norm, city, country,
         desc_len, winner_id, winner_url, winner_source, winner_status, level) in rows:
        row_source = effective_source(source or "", url or "")
        if not is_inferior_source(row_source):
            continue  # direct rows are only ever hidden as URL twins
        if stands_in_for_twins(winner_status, winner_source, winner_url):
            continue
        key = posting_key(url)
        if key == posting_key(winner_url):
            continue
        norm = title_norm or normalize_title(title or "")
        new_home = released.get(key) if key else None
        if new_home is None and norm and norm != "\x01":
            new_home = _find_absorber(
                db, row_id=row_id, source=row_source, title_norm=norm,
                company=company or "", company_domain=domain or "",
                city=city or "", country=country or "", desc_len=desc_len or 0,
                experience_level=level or "",
            )
        values: dict = {"duplicate_of": new_home}
        if new_home is None and clear_probe:
            values["last_probed_at"] = None
        db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(values)
        moved.append((row_id, winner_id, new_home))
        if new_home is None and key:
            released[key] = row_id
    if moved:
        db.commit()
    return moved


def absorb_new_aggregator_rows(db: Session, limit: int = 300) -> int:
    """Incremental dedup for rows the one-time sweep never saw: the newest
    unmarked LinkedIn/Indeed (or aggregator adapter) rows get absorbed by
    any better existing twin that may stand in for them (_find_absorber).
    Rows hidden behind a winner that has since closed are given back first
    (release_from_closed_winners). Called from the hourly backfill cron.
    Commits. Returns rows absorbed."""
    moved = release_from_closed_winners(db)
    if moved:
        logger.info(
            "dedup: %d rows left closed winners (%d back in the feed, %d under a live twin)",
            len(moved), sum(1 for m in moved if m[2] is None),
            sum(1 for m in moved if m[2] is not None),
        )

    candidates = (
        db.query(ScrapedJob)
        .filter(ScrapedJob.duplicate_of.is_(None), _inferior_row_filter())
        .order_by(ScrapedJob.id.desc())
        .limit(limit)
        .all()
    )

    marked = 0
    for row in candidates:
        row_source = effective_source(row.source_platform, row.url)
        if not is_inferior_source(row_source):
            continue
        title_norm = row.title_norm or normalize_title(row.title)
        if not title_norm or title_norm == "\x01":
            continue
        best_id = _find_absorber(
            db, row_id=row.id, source=row_source, title_norm=title_norm,
            company=row.company, company_domain=row.company_domain or "",
            city=row.city or "", country=row.country or "",
            desc_len=len(row.description or ""), experience_level=row.experience_level or "",
        )
        best = db.get(ScrapedJob, best_id) if best_id is not None else None
        if best is None:
            continue
        row.duplicate_of = best.id
        if best.applicant_count is None and row.applicant_count is not None:
            best.applicant_count = row.applicant_count
        if not (best.salary_range or "") and (row.salary_range or ""):
            best.salary_range = row.salary_range
        if (len(best.description or "") < 50
                and len(row.description or "") >= _MIN_COPY_DESC_LEN):
            best.description = row.description
            best.description_sections = None
        _inherit_logo(best, row)
        marked += 1

    # A row absorbed early in the pass may itself absorb later (A→B, B→C):
    # flatten so every duplicate points at a visible survivor.
    if marked:
        for row in candidates:
            hops = 0
            while row.duplicate_of is not None and hops < 5:
                target = db.get(ScrapedJob, row.duplicate_of)
                if target is None or target.duplicate_of is None:
                    break
                row.duplicate_of = target.duplicate_of
                hops += 1
        db.commit()
    return marked


@dataclass
class _Row:
    id: int
    source: str  # effective source (URL-derived)
    company: str
    domain: str
    title_norm: str
    city: str
    country: str
    location_search: str
    desc_len: int
    applicant_count: int | None
    salary_range: str
    stands_in: bool  # may hide its twins (stands_in_for_twins)


def dedup_sweep(db: Session) -> dict:
    """Collapse cross-source twins across the whole catalogue (one-time /
    maintenance pass). A closed row never becomes the winner that hides a
    live copy (stands_in_for_twins). Column-only reads; descriptions are only
    fetched for the rare copy onto a description-less winner. Commits.
    Idempotent."""
    raw = (
        db.query(
            ScrapedJob.id,
            ScrapedJob.source_platform,
            ScrapedJob.url,
            ScrapedJob.company,
            ScrapedJob.company_domain,
            ScrapedJob.title,
            ScrapedJob.title_norm,
            ScrapedJob.city,
            ScrapedJob.country,
            ScrapedJob.location_search,
            func.length(func.coalesce(ScrapedJob.description, "")),
            ScrapedJob.applicant_count,
            ScrapedJob.salary_range,
            ScrapedJob.listing_status,
        )
        .filter(ScrapedJob.duplicate_of.is_(None))
        .all()
    )

    stats = {"groups": 0, "marked": 0, "winners_enriched": 0,
             "descriptions_copied": 0, "url_twins_marked": 0}

    # Phase A: identical canonical URL = the same posting, whatever the tier.
    # (GitHub lists append utm_* params, so the URL-unique constraint lets the
    # same job in twice.) Keep the best copy, a live one first; hide the rest.
    by_canonical: dict[str, list] = {}
    for row in raw:
        canon = canonical_url(row[2] or "")
        if canon:
            by_canonical.setdefault(canon, []).append(row)
    url_hidden: set[int] = set()
    for canon, rows in by_canonical.items():
        if len(rows) < 2:
            continue
        rows = sorted(
            rows,
            key=lambda r: (
                1 if r[13] in HIDDEN_LISTING_STATUSES else 0,
                _SOURCE_TIER.get(effective_source(r[1] or "", r[2] or ""), 3),
                -(r[10] or 0),  # desc_len
                r[0],
            ),
        )
        keeper = rows[0]
        for extra in rows[1:]:
            db.query(ScrapedJob).filter(ScrapedJob.id == extra[0]).update(
                {"duplicate_of": keeper[0]}
            )
            url_hidden.add(extra[0])
            stats["url_twins_marked"] += 1

    # Phase B: aggregator copies of direct postings (employer+title+location).
    groups: dict[tuple[str, str], list[_Row]] = {}
    for (rid, source, url, company, domain, title, title_norm, city, country,
         location_search, desc_len, applicant_count, salary_range, status) in raw:
        if rid in url_hidden:
            continue
        norm = title_norm or normalize_title(title or "")
        employer = normalize_company(company or "")
        if not norm or norm == "\x01" or not employer:
            continue  # unnormalizable titles must never form a group
        groups.setdefault((employer, norm), []).append(_Row(
            id=rid, source=effective_source(source or "", url or ""),
            company=company or "",
            domain=(domain or "").strip(), title_norm=norm,
            city=fold(city or ""), country=country or "",
            location_search=location_search or "",
            desc_len=desc_len or 0, applicant_count=applicant_count,
            salary_range=salary_range or "",
            stands_in=stands_in_for_twins(status, source or "", url or ""),
        ))

    for rows in groups.values():
        if len(rows) < 2:
            continue
        stats["groups"] += 1
        rows.sort(key=lambda r: (_SOURCE_TIER.get(r.source, 3), -r.desc_len, r.id))

        winners: list[_Row] = []
        for row in rows:
            home = None
            # Only aggregator copies may be absorbed; a direct row is always
            # its own posting (distinct requisitions share titles).
            if is_inferior_source(row.source):
                for winner in winners:
                    domains_ok = not row.domain or not winner.domain or row.domain == winner.domain
                    if domains_ok and _cities_compatible(
                        row.city, winner.city, winner.location_search,
                        row.country, winner.country,
                    ):
                        home = winner
                        break
            if home is None:
                if row.stands_in:
                    winners.append(row)
                continue

            updates: dict = {"duplicate_of": home.id}
            db.query(ScrapedJob).filter(ScrapedJob.id == row.id).update(updates)
            stats["marked"] += 1

            winner_updates: dict = {}
            if row.applicant_count is not None:
                current = db.query(ScrapedJob.applicant_count).filter(ScrapedJob.id == home.id).scalar()
                if current is None:
                    winner_updates["applicant_count"] = row.applicant_count
            if row.salary_range:
                current = db.query(ScrapedJob.salary_range).filter(ScrapedJob.id == home.id).scalar()
                if not (current or ""):
                    winner_updates["salary_range"] = row.salary_range
            if home.desc_len < 50 and row.desc_len >= _MIN_COPY_DESC_LEN:
                description = db.query(ScrapedJob.description).filter(ScrapedJob.id == row.id).scalar()
                if description:
                    winner_updates["description"] = description
                    winner_updates["description_sections"] = None
                    home.desc_len = row.desc_len
                    stats["descriptions_copied"] += 1
            if winner_updates:
                db.query(ScrapedJob).filter(ScrapedJob.id == home.id).update(winner_updates)
                stats["winners_enriched"] += 1

    db.commit()
    return stats


# ─── GitHub-list copies of board-crawled postings ───────────────────────────
#
# A GitHub list and the board crawl often link one posting under two
# spellings ('/en-US/marvellcareers/...' vs '/MarvellCareers/...', a repost
# '-1' suffix, 'jobs/8199729' vs 'jobs/8199729?gh_jid=8199729'). The URL is
# UNIQUE, not the posting, so both became visible rows. The board row wins:
# its crawl reconciles it and it carries the description. Only a board row
# that may stand in for a list copy counts (stands_in_for_list_copy): a
# visible one, or one its board removed (that death verdict is the list
# copy's too). An expired one says nothing about the posting, and neither
# does an off_target one: the crawler's level and location filters are not
# the list's, and a human-curated new-grad row the classifier reads as mid
# level is still listed as new grad. The list copy is shown then. (An
# off_target row still stands in for its LinkedIn/Indeed twins:
# stands_in_for_twins.) Among board rows carrying one posting, a visible
# one wins over a removed sibling ('_R123' removed, its '_R123-1' repost
# live).
#
# A precise identity (a Workday tenant + requisition, a Greenhouse job id, a
# Lever/Ashby UUID) is the posting whatever either side titles it or calls
# the employer: the list's 'Software Engineering AMTS - College Grad' is the
# board's 'Software Engineering AMTS (College Grad)', 'BDO Canada' is 'BDO'
# (tenant 'bdo'). Any other URL also needs the same employer and title.
# A match on the identity alone takes a visible board row only, so a
# retitled copy goes only where the feed still shows its posting: a removed
# row's death verdict holds for a copy titled like it, while a retitled one
# may be a later repost ('-1') of the closed requisition, and ingest probes
# its own link.

_BOARD_IDENTITY_PREFIXES = ("workday:", "greenhouse:", "lever:", "ashby:")


def _identity_token(identity: str) -> str:
    """What every URL spelling of a precise identity carries, case-folded:
    '_' + the Workday requisition, the Greenhouse job id, the Lever/Ashby
    UUID. Only a SQL prefilter: posting_identity() decides."""
    token = identity.rsplit(":", 1)[-1]
    return "_" + token if identity.startswith("workday:") else token


def _url_carries(token: str):
    """SQL: the row's URL contains ``token`` (lower-case), LIKE wildcards
    escaped."""
    return func.lower(ScrapedJob.url).contains(token, autoescape=True)


def stands_in_for_list_copy(listing_status: str | None) -> bool:
    """True when a board row may hide a GitHub-list copy of its posting, or
    keep one from being stored (see above)."""
    return (listing_status is None or listing_status not in HIDDEN_LISTING_STATUSES
            or listing_status == LISTING_REMOVED)


def _stands_in_for_list_filter():
    """SQL side of stands_in_for_list_copy."""
    return or_(ScrapedJob.listing_status.is_(None),
               ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES),
               ScrapedJob.listing_status == LISTING_REMOVED)


def _visible_first(listing_status: str | None) -> int:
    """Sort key among board rows for one posting: visible before removed."""
    return 0 if listing_status is None or listing_status not in HIDDEN_LISTING_STATUSES else 1


def _visible_board_rows_carrying(db: Session, identities: list[str]) -> dict[str, int]:
    """identity -> id of the oldest visible board row whose posting is one
    of ``identities`` (precise ones). Column-only, 400 ids a query."""
    wanted = set(identities)
    carriers: dict[str, int] = {}
    for i in range(0, len(identities), 400):
        for row_id, url in (
            db.query(ScrapedJob.id, ScrapedJob.url)
            .filter(ScrapedJob.source_platform == "ats",
                    ScrapedJob.duplicate_of.is_(None),
                    or_(ScrapedJob.listing_status.is_(None),
                        ScrapedJob.listing_status.in_(_VISIBLE_LISTING_STATUSES)),
                    or_(*(_url_carries(_identity_token(identity))
                          for identity in identities[i:i + 400])))
        ):
            identity = posting_identity(url or "")
            if identity in wanted and effective_source("ats", url or "") == "ats":
                carriers[identity] = min(row_id, carriers.get(identity, row_id))
    return carriers


def board_row_for_posting(db: Session, *, company: str, company_domain: str,
                          title: str, url: str) -> int | None:
    """Id of a board-crawled (ats) row for the posting ``url`` names that
    may stand in for it, or None: one of the same employer and title (a
    visible one before a removed sibling), else, for a precise identity,
    a visible one of any title (see above). The ingest guard for
    GitHub-list rows. Column-only."""
    title_norm = normalize_title(title)
    identity = posting_identity(url)
    if not identity:
        return None
    best: tuple[int, int] | None = None  # (_visible_first, id)
    if title_norm:
        rows = (
            db.query(ScrapedJob.id, ScrapedJob.url, ScrapedJob.listing_status)
            .filter(ScrapedJob.source_platform == "ats",
                    ScrapedJob.duplicate_of.is_(None),
                    _stands_in_for_list_filter(),
                    ScrapedJob.title_norm == title_norm,
                    _employer_filter(company, company_domain))
            .order_by(ScrapedJob.id)
            .limit(50)
            .all()
        )
        # A rogue-era 'ats' row with a LinkedIn/Indeed URL is no board row.
        best = min(((_visible_first(status), row_id) for row_id, row_url, status in rows
                    if effective_source("ats", row_url or "") == "ats"
                    and posting_identity(row_url or "") == identity), default=None)
    if identity.startswith(_BOARD_IDENTITY_PREFIXES) and (best is None or best[0]):
        retitled = _visible_board_rows_carrying(db, [identity]).get(identity)
        if retitled is not None:
            return retitled
    return best[1] if best else None


def hide_list_copies_of_board_rows(db: Session) -> int:
    """Soft-hide visible GitHub-list rows whose posting a board row carries
    under another URL spelling (duplicate_of = the board row), like the
    ingest guard (board_row_for_posting): one of the same employer (folded
    name) and title, a visible one before a removed sibling, else, for a
    precise identity, a visible one of any title. A copy hidden behind a
    board row that is removed later stays hidden
    (release_list_copies_of_lapsed_board_rows).
    Idempotent; run by every cron-poll. Column-only reads: the visible list
    rows (a few thousand at most), the board rows sharing their titles, then
    the visible board rows whose URLs carry the precise ids left over.
    Commits. Returns the number hidden."""
    listed = (
        db.query(ScrapedJob.id, ScrapedJob.url, ScrapedJob.company, ScrapedJob.title_norm)
        .filter(ScrapedJob.source_platform == "github",
                ScrapedJob.duplicate_of.is_(None),
                or_(ScrapedJob.listing_status.is_(None),
                    ScrapedJob.listing_status.in_(_VISIBLE_LISTING_STATUSES)),
                ScrapedJob.title_norm != "")
        .all()
    )
    if not listed:
        return 0
    titles = sorted({row.title_norm for row in listed if row.title_norm})
    board: dict[tuple[str, str, str], tuple[int, int]] = {}  # key -> (_visible_first, id)
    for i in range(0, len(titles), 400):
        candidates = (
            db.query(ScrapedJob.id, ScrapedJob.url, ScrapedJob.company, ScrapedJob.title_norm,
                     ScrapedJob.listing_status)
            .filter(ScrapedJob.source_platform == "ats",
                    ScrapedJob.duplicate_of.is_(None),
                    _stands_in_for_list_filter(),
                    ScrapedJob.title_norm.in_(titles[i:i + 400]))
            .all()
        )
        # A chunk holds whole titles, so every key's candidates are in it.
        for row_id, url, company, title_norm, status in sorted(
                candidates, key=lambda row: (_visible_first(row[4]), row[0])):
            if effective_source("ats", url or "") != "ats":
                continue
            board.setdefault(
                (normalize_company(company or ""), title_norm, posting_identity(url or "")),
                (_visible_first(status), row_id))
    matches: list[tuple[int, str, tuple[int, int] | None]] = []
    retitled: set[str] = set()  # precise identities with no visible same-titled board row
    for row_id, url, company, title_norm in listed:
        identity = posting_identity(url or "")
        match = board.get((normalize_company(company or ""), title_norm, identity))
        if identity.startswith(_BOARD_IDENTITY_PREFIXES) and (match is None or match[0]):
            retitled.add(identity)
        matches.append((row_id, identity, match))
    carriers = _visible_board_rows_carrying(db, sorted(retitled)) if retitled else {}
    hidden = 0
    for row_id, identity, match in matches:
        winner = match[1] if match else None
        if match is None or match[0]:
            winner = carriers.get(identity, winner)
        if winner and winner != row_id:
            db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(
                {"duplicate_of": winner}, synchronize_session=False)
            hidden += 1
    if hidden:
        db.commit()
        logger.info("hid %d GitHub-list copies of board-crawled postings", hidden)
    return hidden


def release_list_copies_of_lapsed_board_rows(db: Session) -> int:
    """Give back the visible GitHub-list rows hidden behind a board row that
    no longer stands in for them (stands_in_for_list_copy): one that aged
    out, or one the crawl retired as off_target. 1,443 of prod's ats rows
    are 'expired', which says nothing about the posting, while the list
    still offers it; release_from_closed_winners never frees a direct row,
    so without this the copy stayed hidden for good. A board row its board
    removed keeps its copies hidden (the posting's death verdict), and a
    released copy retires with its list as usual. Run by cron-poll before
    hide_list_copies_of_board_rows, which hides it again under any live
    board twin (a retitled one too, for a precise identity). Column-only.
    Commits. Returns the number released."""
    winner = aliased(ScrapedJob)
    rows = (
        db.query(ScrapedJob.id, winner.listing_status)
        .join(winner, winner.id == ScrapedJob.duplicate_of)
        .filter(ScrapedJob.source_platform == "github",
                or_(ScrapedJob.listing_status.is_(None),
                    ScrapedJob.listing_status.in_(_VISIBLE_LISTING_STATUSES)),
                winner.source_platform == "ats")
        .all()
    )
    released = [row_id for row_id, status in rows if not stands_in_for_list_copy(status)]
    for i in range(0, len(released), 400):
        db.query(ScrapedJob).filter(ScrapedJob.id.in_(released[i:i + 400])).update(
            {"duplicate_of": None}, synchronize_session=False)
    if released:
        db.commit()
        logger.info("released %d GitHub-list copies of lapsed board rows", len(released))
    return len(released)


# ─── One posting on two GitHub lists, or twice on one ───────────────────────
#
# speedyapply's files list one Workday requisition under several site
# aliases ('jobsathpe' and 'wfmathpe', 'displacedemployees' and 'jj'), and
# repeat postings negarprh already lists under another path case
# ('/en-US/mfcjh_jobs/' vs '/en-US/MFCJH_Jobs/'), often retitled ('Software
# Engineer Co-op, Backend' vs 'Software Engineer - Backend - Co-op') and
# under another employer name ('RELX' vs 'LexisNexis Risk Solutions',
# 'Manulife' vs 'Manulife Financial'). A precise identity (Workday tenant +
# requisition, Greenhouse job id, Lever/Ashby UUID, LinkedIn/Indeed job id)
# names one posting whatever the list calls it: the tenant, or the
# platform's global id, is the employer. Any other URL is one posting only
# with the same folded employer and title (a careers page may stand for
# several roles). One row per posting stays in the feed, the rest are
# soft-hidden (duplicate_of) and come back when it leaves.

_PRECISE_IDENTITY_PREFIXES = _BOARD_IDENTITY_PREFIXES + ("linkedin:", "indeed:")


def list_row_key(company: str, title_norm: str, url: str) -> tuple | None:
    """What makes two GitHub-list rows one posting (see above), or None when
    the row can't be matched."""
    identity = posting_identity(url or "")
    if identity.startswith(_PRECISE_IDENTITY_PREFIXES):
        return (identity,)
    employer = normalize_company(company or "")
    if not identity or not employer or not title_norm or title_norm == "\x01":
        return None
    return (employer, title_norm, identity)


def list_posting_keys(url: str) -> set[str]:
    """The ingest guard's keys for a list URL: its posting identity and its
    case-folded canonical URL."""
    return {key for key in (posting_identity(url or ""), canonical_url(url or "").lower()) if key}


def hide_repeated_list_rows(db: Session) -> int:
    """Soft-hide visible GitHub-list rows that repeat a posting another row
    already carries (list_row_key). The posting stays with the board row a
    repeat of it is already hidden behind (hide_list_copies_of_board_rows
    puts a retitled repeat behind a board row only while that row is
    visible), else with its oldest visible list row. Idempotent; run by
    every cron-poll after the board pass. Column-only: the visible list rows
    (a few thousand at most) and the platforms of the rows they are hidden
    behind, one UPDATE per kept row. Commits. Returns the number hidden."""
    rows = (
        db.query(ScrapedJob.id, ScrapedJob.url, ScrapedJob.company, ScrapedJob.title_norm,
                 ScrapedJob.duplicate_of)
        .filter(ScrapedJob.source_platform == "github",
                or_(ScrapedJob.listing_status.is_(None),
                    ScrapedJob.listing_status.in_(_VISIBLE_LISTING_STATUSES)))
        .order_by(ScrapedJob.id)
        .all()
    )
    groups: dict[tuple, list[tuple[int, int | None]]] = {}
    for row_id, url, company, title_norm, duplicate_of in rows:
        key = list_row_key(company or "", title_norm or "", url or "")
        if key is not None:
            groups.setdefault(key, []).append((row_id, duplicate_of))
    groups = {key: members for key, members in groups.items() if len(members) > 1}
    targets = sorted({dup for members in groups.values() for _id, dup in members if dup is not None})
    board_rows: set[int] = set()
    for i in range(0, len(targets), 400):
        board_rows.update(
            row_id for (row_id,) in db.query(ScrapedJob.id).filter(
                ScrapedJob.id.in_(targets[i:i + 400]), ScrapedJob.source_platform == "ats"))

    to_hide: dict[int, list[int]] = {}
    for members in groups.values():
        shown = [row_id for row_id, dup in members if dup is None]
        behind_board = [dup for _id, dup in members if dup in board_rows]
        keeper = behind_board[0] if behind_board else (shown[0] if shown else None)
        repeats = [row_id for row_id in shown if row_id != keeper]
        if keeper is not None and repeats:
            to_hide.setdefault(keeper, []).extend(repeats)
    hidden = 0
    for keeper, ids in to_hide.items():
        db.query(ScrapedJob).filter(ScrapedJob.id.in_(ids)).update(
            {"duplicate_of": keeper}, synchronize_session=False)
        hidden += len(ids)
    if hidden:
        db.commit()
        logger.info("hid %d GitHub-list rows repeating a posting another row carries", hidden)
    return hidden


def release_repeated_list_rows(db: Session) -> int:
    """Give back the visible GitHub-list rows hidden behind another list row
    (hide_repeated_list_rows) that has left the feed: removed, expired or
    retired on its own list, while this row's list still offers the
    posting. A row whose keeper is itself hidden now (behind a board row,
    or behind an older repeat) moves on to that row instead, so no copy
    points at a hidden row. Run by cron-poll before the hide passes, which
    hide a released row again under anything still carrying the posting.
    Column-only. Commits. Returns the number released."""
    keeper = aliased(ScrapedJob)
    rows = (
        db.query(ScrapedJob.id, keeper.listing_status, keeper.duplicate_of)
        .join(keeper, keeper.id == ScrapedJob.duplicate_of)
        .filter(ScrapedJob.source_platform == "github",
                or_(ScrapedJob.listing_status.is_(None),
                    ScrapedJob.listing_status.in_(_VISIBLE_LISTING_STATUSES)),
                keeper.source_platform == "github")
        .all()
    )
    released: list[int] = []
    moved = 0
    for row_id, status, onward in rows:
        if (status is not None and status in HIDDEN_LISTING_STATUSES) or onward == row_id:
            released.append(row_id)
        elif onward is not None:
            db.query(ScrapedJob).filter(ScrapedJob.id == row_id).update(
                {"duplicate_of": onward}, synchronize_session=False)
            moved += 1
    for i in range(0, len(released), 400):
        db.query(ScrapedJob).filter(ScrapedJob.id.in_(released[i:i + 400])).update(
            {"duplicate_of": None}, synchronize_session=False)
    if released or moved:
        db.commit()
        logger.info("released %d GitHub-list repeats of lapsed list rows, moved %d onward",
                    len(released), moved)
    return len(released)
