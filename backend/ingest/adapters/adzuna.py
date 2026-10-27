"""
Adzuna (api.adzuna.com), a job-ad aggregator with an official search API, for
Canada and the United States.

Built from the provider's OpenAPI description (developer.adzuna.com, the
"Search ads" endpoint: https://developer.adzuna.com/swagger/spec/test2.json).
Nobody has an app id and key yet, so the adapter ships DISABLED (REQUIRED_ENV) and
has only ever run against fixtures that follow that description.

Aggregator rows are low-trust gap-fillers: ``url`` is the ad's page on Adzuna and
``apply_url`` Adzuna's ``redirect_url``, the link Adzuna's terms say users must be
sent through. The description is the snippet Adzuna returns (500 characters at
most). A salary Adzuna predicted itself (``salary_is_predicted`` "1") is not a
posted salary and is left out.

A board is one search: ``config["country"]`` is "ca" or "us", ``config["params"]``
holds documented query parameters (what, what_or, where, category, max_days_old,
salary_min ...), ``max_pages`` the page cap and ``page_size`` the ads per page.
Without a cap a search matches millions of ads, so the default is a few pages of
the newest, and a listing that stops short is returned incomplete.

The credentials travel in the query string, as the API requires. Nothing here
puts a request URL into an error message or a log line, because the URL carries
the app key.
"""

from __future__ import annotations

import datetime
import html
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urlsplit

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob
from backend.services.description_extractor import clean_html

logger = logging.getLogger(__name__)

PLATFORM = "adzuna"
TIER = 3
COUNTRIES = ("CA", "US")
# Adzuna's free plan is understood to allow about 25 calls a minute (not
# published on the pages read on 2026-10-25); one call every 2.5 seconds stays under that.
RATE_LIMIT = 2.5
REQUIRED_ENV = ("ADZUNA_APP_ID", "ADZUNA_APP_KEY")
HOST_PATTERNS = (r"^https?://(?:www\.)?adzuna\.(?:ca|com)/",)

API_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
DETAIL_HOSTS = {"ca": "www.adzuna.ca", "us": "www.adzuna.com"}
CURRENCIES = {"ca": "CAD", "us": "USD"}

DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 3
DESCRIPTION_LIMIT = 10000

# The query parameters of the search endpoint, from the OpenAPI description. A board
# naming anything else is a typo, and an ignored filter would list the whole
# database, so it is refused.
QUERY_PARAMS = frozenset({
    "what", "what_and", "what_phrase", "what_or", "what_exclude", "title_only", "where", "distance",
    "location0", "location1", "location2", "location3", "location4", "location5", "location6", "location7",
    "max_days_old", "category", "sort_dir", "sort_by", "salary_min", "salary_max", "salary_include_unknown",
    "full_time", "part_time", "contract", "permanent", "company",
})

_EMPHASIS_RE = re.compile(r"</?(?:strong|b|em|i|span)\b[^>]*>", re.I)
_CONTRACT_TIME = {"full_time": "Full-time", "part_time": "Part-time"}
_CONTRACT_TYPE = {"permanent": "Permanent", "contract": "Contract"}


class AdzunaError(RuntimeError):
    """A failed Adzuna request, described without its URL (the URL holds the app key)."""


# --- Parsing ------------------------------------------------------------------------------------

def _plain(text) -> str:
    """Ad text without Adzuna's keyword highlighting (<strong>) or other markup."""
    if not isinstance(text, str):
        return ""
    return clean_html(_EMPHASIS_RE.sub("", text))


def _title(text) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", text if isinstance(text, str) else "")).split())


def _created(value) -> Optional[datetime.datetime]:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", str(value or "").strip())
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part or 0) for part in match.groups())
    try:
        return datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _amount(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number <= 0:
        return ""
    return str(int(number)) if number == int(number) else f"{number:g}"


def _salary_text(ad: dict, country: str) -> str:
    """The posted yearly pay as "55000-65000 CAD yearly" (Adzuna states pay per
    year in local currency); "" when the ad has none or Adzuna only predicted it."""
    if str(ad.get("salary_is_predicted") or "0").strip() == "1":
        return ""
    low, high = _amount(ad.get("salary_min")), _amount(ad.get("salary_max"))
    if not low and not high:
        return ""
    low, high = low or high, high or low
    currency = CURRENCIES.get(country, "")
    return f"{currency} {low} yearly" if low == high else f"{low}-{high} {currency} yearly"


def _location(ad: dict) -> str:
    place = ad.get("location") if isinstance(ad.get("location"), dict) else {}
    name = " ".join(str(place.get("display_name") or "").split())
    if name:
        return name
    area = [str(part) for part in place.get("area") or [] if part]
    return ", ".join(reversed(area[-2:]))


def _page_url(ad_id: str, redirect_url: str, country: str) -> str:
    """The ad's page on Adzuna, without the tracking Adzuna's redirect link carries
    (that link includes our app id). The market's host comes from the redirect
    link when there is one."""
    host = urlsplit(redirect_url).netloc.lower() if redirect_url.lower().startswith(("http://", "https://")) else ""
    if not host.endswith(tuple(DETAIL_HOSTS.values())):
        host = DETAIL_HOSTS[country]
    return f"https://{host}/details/{ad_id}"


def _to_job(ad: dict, slug: str, company_name: str, country: str) -> Optional[ATSJob]:
    ad_id = str(ad.get("id") or "").strip()
    title = _title(ad.get("title"))
    if not ad_id or not title:
        return None
    redirect_url = str(ad.get("redirect_url") or "").strip()
    company = ad.get("company") if isinstance(ad.get("company"), dict) else {}
    category = ad.get("category") if isinstance(ad.get("category"), dict) else {}
    location = _location(ad)
    employment = ", ".join(part for part in (
        _CONTRACT_TIME.get(str(ad.get("contract_time") or "")),
        _CONTRACT_TYPE.get(str(ad.get("contract_type") or "")),
    ) if part)
    remote = re.search(r"\b(remote|work from home)\b", f"{title} {location}", re.I)
    return ATSJob(
        title=title,
        company=_title(company.get("display_name")) or company_name,
        location=location,
        url=_page_url(ad_id, redirect_url, country),
        posted_date=_created(ad.get("created")),
        department=_title(category.get("label")) or None,
        work_type="Remote" if remote else None,
        description=_plain(ad.get("description"))[:DESCRIPTION_LIMIT],
        external_id=f"{PLATFORM}:{slug}:{ad_id}",
        employment_type=employment,
        salary_text=_salary_text(ad, country),
        detail_ref=ad_id,
        apply_url=redirect_url if redirect_url.lower().startswith(("http://", "https://")) else "",
    )


def _bounded(value, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


def _failure(exc: httpx.HTTPStatusError) -> AdzunaError:
    """The error for a rejected request, with Adzuna's own explanation when it
    gave one ({"exception": ..., "display": ...}) and never the request URL."""
    detail = ""
    try:
        body = exc.response.json()
        detail = str(body.get("display") or body.get("exception") or "") if isinstance(body, dict) else ""
    except ValueError:
        pass
    status = exc.response.status_code
    return AdzunaError(f"Adzuna answered HTTP {status}" + (f": {detail}" if detail else ""))


# --- The contract -------------------------------------------------------------------------------

async def list_jobs(client, slug, company_name, *, config, deadline=None):
    """List one search, newest ads first, at most ``max_pages`` pages.

    Returns (jobs, complete, total) with ``total`` Adzuna's ``count`` of matching
    ads. The listing is complete only when Adzuna reported a positive count and
    every matching ad was returned; ads that come without a count are refused like
    any unusable page, so one page can never stand for the whole board. A page that
    fails after the first keeps what was listed as an incomplete listing."""
    config = config if isinstance(config, dict) else {}
    country = str(config.get("country") or "").strip().lower()
    if country not in DETAIL_HOSTS:
        raise ValueError(f"adzuna board {slug!r} needs config.country 'ca' or 'us'")
    params = dict(config.get("params") or {})
    unknown = sorted(set(params) - QUERY_PARAMS)
    if unknown:
        raise ValueError(f"adzuna board {slug!r}: unknown search parameter(s) {', '.join(unknown)}")
    app_id = (os.environ.get("ADZUNA_APP_ID") or "").strip()
    app_key = (os.environ.get("ADZUNA_APP_KEY") or "").strip()
    if not app_id or not app_key:
        raise RuntimeError("ADZUNA_APP_ID and ADZUNA_APP_KEY must both be set")
    params.setdefault("sort_by", "date")
    page_size = _bounded(config.get("page_size"), DEFAULT_PAGE_SIZE, 50)

    jobs: list[ATSJob] = []
    seen: set[str] = set()
    total = 0
    for page_number in range(1, _bounded(config.get("max_pages"), DEFAULT_MAX_PAGES, 1000) + 1):
        if page_number > 1 and deadline is not None and time.monotonic() >= deadline:
            break
        try:
            response = await http.request(
                client, "GET", API_URL.format(country=country, page=page_number), rate_limit=RATE_LIMIT,
                headers={"Accept": "application/json"},
                params={"app_id": app_id, "app_key": app_key, "results_per_page": page_size,
                        "content-type": "application/json", **params},
            )
            response.raise_for_status()
            body = response.json()
            ads = body["results"]
            page_total = int(body.get("count") or 0)
            if ads and page_total <= 0:
                raise ValueError("ads without a total")
        except httpx.HTTPStatusError as exc:
            if page_number == 1:
                raise _failure(exc) from None
            logger.warning("adzuna %s: page %d answered HTTP %d; keeping the %d ads listed",
                           slug, page_number, exc.response.status_code, len(jobs))
            break
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            if page_number == 1:
                raise AdzunaError(f"Adzuna request failed ({type(exc).__name__})") from None
            logger.warning("adzuna %s: page %d unusable (%s); keeping the %d ads listed",
                           slug, page_number, type(exc).__name__, len(jobs))
            break
        total = max(total, page_total)
        for ad in ads:
            job = _to_job(ad, slug, company_name, country) if isinstance(ad, dict) else None
            if job is None or job.detail_ref in seen:
                continue
            seen.add(job.detail_ref)
            jobs.append(job)
        if not ads or len(seen) >= total:
            break

    return jobs, bool(jobs) and total > 0 and len(seen) >= total, total
