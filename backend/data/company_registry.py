"""
Company registry loader, single source of truth for ATS boards to scrape.

The canonical registry lives in ``backend/data/ats_companies.json`` and ships
with the backend package (so it is available in serverless deploys). Each entry:

    {
      "company_name": "Shopify",
      "ats_platform": "lever",
      "board_slug": "shopify",
      "company_logo_url": "https://...",
      "enabled": true
    }

To add coverage, edit that JSON file, no code changes required.

Workday entries also carry ``workday_url_template`` (the tenant's CxS base,
"https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"). Their
``board_slug`` is the tenant subdomain, so the board key matches what
listing_freshness.board_key_from_url() derives for rows stored from URLs and
those rows get reconciled. When one tenant hosts several career sites
(BlackBerry and QNX both on "bb"), each site takes its own slug instead and
cron-ats moves the tenant-keyed rows onto the site whose URLs they carry.

Optional fields (scraper v2): ``hq_country`` ("CA" or "US", where the
employer is based and mostly hires), ``country`` (the board posts in that one
country only, which switches off the location filter, so it is set only on
single-country boards such as hospitals, universities and governments),
``sector``, ``careers_url``, and ``config``, an object the platform's v2
adapter reads (load_board_config). Platforms beyond the five built-ins are
crawled by adapters in backend/ingest/adapters.
"""

from __future__ import annotations

import copy
import datetime
import json
import logging
import os
import zlib
from functools import lru_cache

logger = logging.getLogger(__name__)

# Platforms the ATS scraper can actually fetch today. Workday entries are
# supported only when they carry a ``workday_url_template`` (the tenant's CxS
# endpoint base), the tenant host/site can't be derived from a bare slug, so
# template-less workday entries stay listed but unscraped.
SUPPORTED_PLATFORMS = {"greenhouse", "lever", "ashby", "smartrecruiters", "workday"}

# One cron-ats invocation scrapes its whole slice inside a single Vercel
# request capped at 300 s by the workflow. This is roughly how many boards
# fit that budget with headroom.
CRON_ATS_SHARD_TARGET = 150

_REGISTRY_PATH = os.path.join(os.path.dirname(__file__), "ats_companies.json")


@lru_cache(maxsize=1)
def _load_raw() -> list[dict]:
    """Load and cache the raw registry entries from disk."""
    try:
        with open(_REGISTRY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        companies = data.get("companies", [])
        if not isinstance(companies, list):
            logger.error("ats_companies.json: 'companies' is not a list")
            return []
        return companies
    except FileNotFoundError:
        logger.error("Registry file not found at %s", _REGISTRY_PATH)
        return []
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Failed to load registry: %s", e)
        return []


def load_companies(
    include_disabled: bool = False,
    supported_only: bool = True,
) -> list[tuple[str, str, str]]:
    """Return registry entries as (platform, slug, company_name) tuples.

    Args:
        include_disabled: include entries with ``enabled: false``.
        supported_only: only return platforms the scraper can fetch: the
            built-ins, plus any platform whose v2 adapter is enabled (one
            that lacks its API keys is not).

    Deduplicates on (platform, slug). Skips malformed rows.
    """
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    crawlable: dict[str, bool] = {}

    for entry in _load_raw():
        try:
            platform = (entry.get("ats_platform") or "").strip().lower()
            slug = (entry.get("board_slug") or "").strip()
            name = (entry.get("company_name") or "").strip()
            enabled = entry.get("enabled", True)
        except AttributeError:
            continue

        if not platform or not slug or not name:
            continue
        if not include_disabled and not enabled:
            continue
        if supported_only and platform not in SUPPORTED_PLATFORMS:
            if platform not in crawlable:
                crawlable[platform] = _adapter_enabled(platform)
            if not crawlable[platform]:
                continue
        if supported_only and platform == "workday" and not (
            entry.get("workday_url_template") or ""
        ).strip():
            continue

        key = (platform, slug)
        if key in seen:
            continue
        seen.add(key)
        out.append((platform, slug, name))

    return out


def _adapter_enabled(platform: str) -> bool:
    # Imported here, not at the top, so loading this module never loads the
    # ingest package.
    from backend.ingest.adapters import adapter_enabled

    return adapter_enabled(platform)


def _shard_count(companies: list[tuple[str, str, str]]) -> int:
    """``CRON_ATS_SHARDS`` when set, otherwise sized so a run scrapes
    ~CRON_ATS_SHARD_TARGET companies."""
    shard_count = 0
    raw = os.getenv("CRON_ATS_SHARDS", "").strip()
    if raw:
        try:
            shard_count = int(raw)
        except ValueError:
            logger.warning("CRON_ATS_SHARDS=%r is not an integer; ignoring", raw)
    if shard_count < 1:
        shard_count = max(1, -(-len(companies) // CRON_ATS_SHARD_TARGET))
    return shard_count


def _shard_subset(
    companies: list[tuple[str, str, str]], shard_index: int, shard_count: int,
) -> list[tuple[str, str, str]]:
    """Assignment hashes the board slug with crc32, Python's ``hash()`` is
    salted per process, and the assignment must be stable across serverless
    invocations so every board lands in exactly one shard."""
    if shard_count == 1:
        return list(companies)
    return [
        entry
        for entry in companies
        if zlib.crc32(entry[1].lower().encode("utf-8")) % shard_count == shard_index
    ]


def shard_for_hour(
    companies: list[tuple[str, str, str]],
    hour: int,
) -> tuple[int, int, list[tuple[str, str, str]]]:
    """Return (shard_index, shard_count, subset) for this hour's cron run.

    Hour-based rotation assumes the cron fires every hour. It doesn't (the
    GitHub schedule fires ~6x a day at irregular gaps), so cron-ats uses
    ``pick_shard`` instead; this stays as the fixed-rotation primitive.
    """
    shard_count = _shard_count(companies)
    shard_index = hour % shard_count
    return shard_index, shard_count, _shard_subset(companies, shard_index, shard_count)


def pick_shard(
    companies: list[tuple[str, str, str]],
    last_crawled: dict[str, datetime.datetime],
) -> tuple[int, int, list[tuple[str, str, str]]]:
    """Return (shard_index, shard_count, subset) for the shard crawled least
    recently, so irregular cron firing can't starve a shard the way
    ``hour % shard_count`` does when the same hours keep getting skipped.

    ``last_crawled`` maps board_key ("platform:slug") → last successful crawl
    (source_health.last_success_at). A shard's last crawl is the NEWEST stamp
    among its boards: a permanently failing board keeps an ancient stamp and
    must not make its shard look overdue forever. A shard none of whose boards
    was ever crawled goes first; an empty shard goes last; ties go to the
    lowest index so the choice is deterministic.
    """
    shard_count = _shard_count(companies)
    if shard_count == 1:
        return 0, 1, list(companies)

    def _naive(stamp: datetime.datetime) -> datetime.datetime:
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return stamp

    subsets = [_shard_subset(companies, i, shard_count) for i in range(shard_count)]

    def _order(index: int):
        stamps = [
            _naive(last_crawled[key])
            for key in (f"{platform}:{slug}" for platform, slug, _ in subsets[index])
            if last_crawled.get(key) is not None
        ]
        last_run = max(stamps) if stamps else datetime.datetime.min
        return (not subsets[index], last_run, index)

    shard_index = min(range(shard_count), key=_order)
    return shard_index, shard_count, subsets[shard_index]


def load_workday_bases() -> dict[str, str]:
    """Return {board_slug: CxS endpoint base} for workday entries that carry a
    ``workday_url_template``, the connector can't fetch a tenant without it."""
    out: dict[str, str] = {}
    for entry in _load_raw():
        if (entry.get("ats_platform") or "").strip().lower() != "workday":
            continue
        slug = (entry.get("board_slug") or "").strip()
        template = (entry.get("workday_url_template") or "").strip()
        if slug and template:
            out[slug] = template.rstrip("/")
    return out


def load_board_config(platform: str, slug: str) -> dict:
    """The ``config`` object of the registry entry for (platform, slug), the
    first one when the registry lists the board twice; {} when the entry has
    none or the board is not listed. A copy: the adapter may change it
    without touching the cached registry."""
    platform = (platform or "").strip().lower()
    slug = (slug or "").strip()
    for entry in _load_raw():
        try:
            listed = ((entry.get("ats_platform") or "").strip().lower() == platform
                      and (entry.get("board_slug") or "").strip() == slug)
        except AttributeError:  # a malformed row, skipped as load_companies does
            continue
        if listed:
            config = entry.get("config")
            return copy.deepcopy(config) if isinstance(config, dict) else {}
    return {}


def load_board_countries() -> dict[str, str]:
    """Return {"platform:slug": "US" | "CA"} for boards that post in one
    country only (an optional ``country`` field). Such a board may list a
    bare, ambiguous city: BDO Canada's "London" is London, Ontario."""
    out: dict[str, str] = {}
    for entry in _load_raw():
        platform = (entry.get("ats_platform") or "").strip().lower()
        slug = (entry.get("board_slug") or "").strip()
        country = (entry.get("country") or "").strip().upper()
        if platform and slug and country in ("US", "CA"):
            out[f"{platform}:{slug}"] = country
    return out


def load_logo_map() -> dict[str, str]:
    """Return {company_name_lower: company_logo_url} for enrichment."""
    return {
        (e.get("company_name") or "").strip().lower(): (e.get("company_logo_url") or "")
        for e in _load_raw()
        if e.get("company_name")
    }
