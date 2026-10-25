"""
Jooble (jooble.org), a job-ad aggregator with a REST API.

Built from Jooble's "REST API Documentation" (help.jooble.org, modified
2026-08-16): ``POST https://jooble.org/api/{api_key}`` with a JSON body
(``keywords`` and ``location`` are required; ``page``, ``ResultOnPage``,
``radius``, ``salary``, ``companysearch`` are optional) answers
``{"totalCount": n, "jobs": [{id, title, location, snippet, salary, source, type,
link, company, updated}]}``. Nobody has a key yet, so the adapter ships DISABLED
(REQUIRED_ENV) and has only ever run against fixtures that follow that page.

Two facts about the key shape how this is used. Each Jooble country site issues
its own key, and a key only works on its own site: the one in JOOBLE_API_KEY
belongs to the site named by ``config["host"]`` (default ``jooble.org``, the US
site; Canada is ``ca.jooble.org``). And the free plan allows 500 requests per key
in total, for the key's whole life, so a board is a search of at most
``max_pages`` pages (default 2) and should be crawled rarely.

Aggregator rows are low-trust gap-fillers: ``url`` is the ad's page on Jooble and
the description is Jooble's snippet. Jooble gives no employer link, so
``apply_url`` stays empty.

The key is part of the request URL. The URL handed to the HTTP helper (which logs
it when it retries) is the bare endpoint; the key is put into the path by an
``httpx.Auth`` as the request is sent, and errors are described without the URL.
"""

from __future__ import annotations

import datetime
import html
import logging
import os
import re
import time
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob
from backend.services.description_extractor import clean_html

logger = logging.getLogger(__name__)

PLATFORM = "jooble"
TIER = 3
COUNTRIES = ("CA", "US")
RATE_LIMIT = 1.0
REQUIRED_ENV = ("JOOBLE_API_KEY",)
HOST_PATTERNS = (r"^https?://(?:[\w-]+\.)?jooble\.org/",)

DEFAULT_HOST = "jooble.org"
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
# The free plan allows 500 requests per key, ever.
DEFAULT_MAX_PAGES = 2
DESCRIPTION_LIMIT = 10000

_EMPHASIS_RE = re.compile(r"</?(?:b|strong|em|i|span)\b[^>]*>", re.I)
# Documented optional request fields a board may set, besides the required ones.
_OPTIONAL_FIELDS = ("radius", "salary", "SearchMode", "companysearch")


class JoobleError(RuntimeError):
    """A failed Jooble request, described without its URL (the URL holds the API key)."""


class _KeyInPath(httpx.Auth):
    """Sends the request to /api/{key}, so the key never sits in a URL that is logged."""

    def __init__(self, api_key: str):
        self._api_key = api_key

    def auth_flow(self, request: httpx.Request):
        request.url = request.url.copy_with(path=f"/api/{self._api_key}")
        yield request


# --- Parsing ------------------------------------------------------------------------------------

def _plain(text) -> str:
    """Snippet text without Jooble's keyword highlighting (<b>) or other markup."""
    if not isinstance(text, str):
        return ""
    return clean_html(_EMPHASIS_RE.sub("", text).replace("&nbsp;", " "))


def _line(text) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", "", text if isinstance(text, str) else "")).split())


def _updated(value) -> Optional[datetime.datetime]:
    """"2023-09-15T12:55:35.3870000" (no zone: UTC) -> an aware datetime."""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", str(value or "").strip())
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part or 0) for part in match.groups())
    try:
        return datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _page_url(link) -> str:
    """The ad's page on Jooble without the click-tracking query string."""
    if not isinstance(link, str) or not link.lower().startswith(("http://", "https://")):
        return ""
    parts = urlsplit(link.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _to_job(ad: dict, slug: str, company_name: str) -> Optional[ATSJob]:
    ad_id = str(ad.get("id") or "").strip()
    title = _line(ad.get("title"))
    url = _page_url(ad.get("link"))
    if not ad_id or not title or not url:
        return None
    location = _line(ad.get("location"))
    salary = _line(ad.get("salary"))
    remote = re.search(r"\b(remote|work from home)\b", f"{title} {location}", re.I)
    return ATSJob(
        title=title,
        company=_line(ad.get("company")) or company_name,
        location=location,
        url=url,
        posted_date=_updated(ad.get("updated")),
        work_type="Remote" if remote else None,
        description=_plain(ad.get("snippet"))[:DESCRIPTION_LIMIT],
        external_id=f"{PLATFORM}:{slug}:{ad_id}",
        employment_type=_line(ad.get("type")),
        salary_text=salary if re.search(r"\d", salary) else "",
        detail_ref=ad_id,
    )


def _bounded(value, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


# --- The contract -------------------------------------------------------------------------------

async def list_jobs(client, slug, company_name, *, config, deadline=None):
    """List one search, at most ``max_pages`` pages of ``page_size`` ads.

    Returns (jobs, complete, total) with ``total`` Jooble's ``totalCount``. The
    listing is complete only when Jooble reported a positive count and every
    matching ad was returned; ads that come without a count are refused like any
    unusable page, so one page can never stand for the whole board. A page that
    fails after the first keeps what was listed as an incomplete listing."""
    config = config if isinstance(config, dict) else {}
    keywords = str(config.get("keywords") or "").strip()
    location = str(config.get("location") or "").strip()
    if not keywords or not location:
        raise ValueError(f"jooble board {slug!r} needs config.keywords and config.location")
    host = str(config.get("host") or DEFAULT_HOST).strip().lower()
    if not re.fullmatch(r"(?:[a-z0-9-]+\.)*jooble\.org", host):
        raise ValueError(f"jooble board {slug!r}: host {host!r} is not a Jooble site")
    api_key = (os.environ.get("JOOBLE_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("JOOBLE_API_KEY must be set")
    auth = _KeyInPath(api_key)
    page_size = _bounded(config.get("page_size"), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)
    body = {"keywords": keywords, "location": location, "ResultOnPage": page_size}
    body.update({name: config[name] for name in _OPTIONAL_FIELDS if config.get(name) not in (None, "")})

    jobs: list[ATSJob] = []
    seen: set[str] = set()
    total = 0
    for page_number in range(1, _bounded(config.get("max_pages"), DEFAULT_MAX_PAGES, 1000) + 1):
        if page_number > 1 and deadline is not None and time.monotonic() >= deadline:
            break
        try:
            response = await http.request(
                client, "POST", f"https://{host}/api/", rate_limit=RATE_LIMIT, auth=auth,
                headers={"Accept": "application/json"}, json={**body, "page": page_number},
            )
            response.raise_for_status()
            data = response.json()
            ads = data["jobs"]
            page_total = int(data.get("totalCount") or 0)
            if ads and page_total <= 0:
                raise ValueError("ads without a total")
        except httpx.HTTPStatusError as exc:
            if page_number == 1:
                raise JoobleError(f"Jooble answered HTTP {exc.response.status_code}") from None
            logger.warning("jooble %s: page %d answered HTTP %d; keeping the %d ads listed",
                           slug, page_number, exc.response.status_code, len(jobs))
            break
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            if page_number == 1:
                raise JoobleError(f"Jooble request failed ({type(exc).__name__})") from None
            logger.warning("jooble %s: page %d unusable (%s); keeping the %d ads listed",
                           slug, page_number, type(exc).__name__, len(jobs))
            break
        total = max(total, page_total)
        for ad in ads:
            job = _to_job(ad, slug, company_name) if isinstance(ad, dict) else None
            if job is None or job.detail_ref in seen:
                continue
            seen.add(job.detail_ref)
            jobs.append(job)
        if not ads or len(seen) >= total:
            break

    return jobs, bool(jobs) and total > 0 and len(seen) >= total, total
