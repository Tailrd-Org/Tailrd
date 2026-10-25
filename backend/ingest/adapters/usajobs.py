"""
USAJOBS (data.usajobs.gov), the US federal government's job board, through its
official Search API.

Built from the published documentation (developer.usajobs.gov: "GET /api/Search",
"Authentication", "Rate limiting") and the response shape it shows. Nobody has an
API key yet, so the adapter ships DISABLED (REQUIRED_ENV) and has only ever run
against fixtures that follow the documentation.

The API wants three headers: ``Host: data.usajobs.gov``, ``Authorization-Key:
<the key>`` and ``User-Agent: <the email address the key was requested with>``.
That User-Agent is the one place a request does not carry TailrdBot's: the
provider identifies the registered developer by it. ingest.core.http forces our
User-Agent onto every request, so the credentials go on through an ``httpx.Auth``,
which runs after the request is built and sets all three; pacing and retries
still come from the HTTP helper.

A board is one query: ``config["params"]`` holds the documented query parameters
(``HiringPath`` "student" or "graduates", ``JobCategoryCode``, ``Organization``,
``LocationName``, ``Keyword`` ...), ``max_pages`` the page cap and ``page_size``
the rows per page (the API allows up to 500). The API answers at most 10,000 rows
per query and, unless asked otherwise, only postings open to the public.
"""

from __future__ import annotations

import datetime
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

PLATFORM = "usajobs"
TIER = 2
COUNTRIES = ("US",)
# The documentation sets no per-second limit, only row limits per query and page.
RATE_LIMIT = 1.0
REQUIRED_ENV = ("USAJOBS_API_KEY", "USAJOBS_USER_AGENT")
HOST_PATTERNS = (r"^https?://(?:www\.)?usajobs\.gov/(?:job|GetJob/ViewDetails)/",)

API_URL = "https://data.usajobs.gov/api/search"
API_HOST = "data.usajobs.gov"
JOB_URL = "https://www.usajobs.gov/job/{control_number}"

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500          # "Maximum of 500 rows per page"
DEFAULT_MAX_PAGES = 5        # 500 postings a board unless the registry asks for more
ROW_CEILING = 10_000         # "Maximum of 10,000 rows per query"
DESCRIPTION_LIMIT = 10000

# The salary parser reads these three periods correctly. It takes biweekly or weekly pay for
# monthly pay and does not read daily pay at all, so any other period (or none) leaves the
# salary out rather than stored wrong.
_PAY_PERIODS = (("PA", "yearly"), ("PH", "hourly"), ("PM", "monthly"))
_PAY_LABELS = {"per year": "yearly", "per hour": "hourly", "per month": "monthly"}


class _Credentials(httpx.Auth):
    """Puts the three headers USAJOBS requires on a request."""

    def __init__(self, api_key: str, registered_email: str):
        self._headers = {"Host": API_HOST, "User-Agent": registered_email, "Authorization-Key": api_key}

    def auth_flow(self, request: httpx.Request):
        for name, value in self._headers.items():
            request.headers[name] = value
        yield request


def _credentials() -> _Credentials:
    api_key = (os.environ.get("USAJOBS_API_KEY") or "").strip()
    registered_email = (os.environ.get("USAJOBS_USER_AGENT") or "").strip()
    if not api_key or not registered_email:
        raise RuntimeError("USAJOBS_API_KEY and USAJOBS_USER_AGENT must both be set")
    return _Credentials(api_key, registered_email)


# --- Parsing ------------------------------------------------------------------------------------

def _iso_datetime(value) -> Optional[datetime.datetime]:
    """"2016-06-05T00:00:00Z", "2024-06-17T00:00:00.0000" or a bare date, as UTC."""
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", str(value or "").strip())
    if not match:
        return None
    year, month, day, hour, minute, second = (int(part or 0) for part in match.groups())
    try:
        return datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _names(entries) -> list[str]:
    """The ``Name`` of each object in a documented list (JobCategory,
    PositionSchedule, PositionOfferingType ...)."""
    if not isinstance(entries, list):
        return []
    return [_text(entry.get("Name")) for entry in entries if isinstance(entry, dict) and _text(entry.get("Name"))]


def _public_url(descriptor: dict, control_number: str) -> str:
    """The posting's public page, without query string or fragment."""
    given = _text(descriptor.get("PositionURI"))
    if given.lower().startswith(("http://", "https://")):
        parts = urlsplit(given)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return JOB_URL.format(control_number=control_number)


def _apply_url(descriptor: dict) -> str:
    links = descriptor.get("ApplyURI")
    if isinstance(links, str):
        links = [links]
    for link in links or []:
        if isinstance(link, str) and link.lower().startswith(("http://", "https://")):
            return link.strip()
    return ""


def _location(descriptor: dict) -> str:
    """The posting's place as USAJOBS displays it. A US posting gets
    ", United States" (so "Multiple Locations" still reads as North American),
    and a posting abroad gets its country."""
    display = _text(descriptor.get("PositionLocationDisplay"))
    places = [place for place in descriptor.get("PositionLocation") or [] if isinstance(place, dict)]
    if not display and places:
        display = _text(places[0].get("LocationName"))
    countries = [_text(place.get("CountryCode")) for place in places if _text(place.get("CountryCode"))]
    if not countries:
        return display
    country = "United States" if "United States" in countries else countries[0]
    if country.lower() in display.lower():
        return display
    return f"{display}, {country}" if display else country


def _amount(value) -> str:
    try:
        number = float(str(value).replace(",", ""))
    except ValueError:
        return ""
    return str(int(number)) if number == int(number) else f"{number:g}"


def _salary_text(descriptor: dict) -> str:
    """The first pay range per year, hour or month as "92108-119746 USD yearly",
    the shape the salary parser reads; "" without such a range."""
    for pay in descriptor.get("PositionRemuneration") or []:
        if not isinstance(pay, dict):
            continue
        low, high = _amount(pay.get("MinimumRange")), _amount(pay.get("MaximumRange"))
        if not low or not high or float(high) <= 0:
            continue
        code = _text(pay.get("RateIntervalCode")).upper()
        period = dict(_PAY_PERIODS).get(code) or _PAY_LABELS.get(_text(pay.get("Description")).lower())
        if not period:
            continue  # biweekly, weekly, daily, no pay: the salary parser would misread it
        amounts = f"USD {low}" if low == high else f"{low}-{high} USD"
        return f"{amounts} {period}"
    return ""


def _description(descriptor: dict) -> str:
    """The job summary, the major duties and the qualification summary, as text."""
    details = (descriptor.get("UserArea") or {}).get("Details") or {}
    parts = []
    for value in (details.get("JobSummary"), details.get("MajorDuties"), descriptor.get("QualificationSummary")):
        if isinstance(value, list):
            value = "\n".join(str(entry) for entry in value if entry)
        text = clean_html(value) if isinstance(value, str) else ""
        if text and text not in parts:
            parts.append(text)
    return "\n\n".join(parts)[:DESCRIPTION_LIMIT]


def _to_job(item: dict, slug: str, company_name: str) -> Optional[ATSJob]:
    descriptor = item.get("MatchedObjectDescriptor")
    control_number = str(item.get("MatchedObjectId") or "").strip()
    if not isinstance(descriptor, dict) or not control_number:
        return None
    title = _text(descriptor.get("PositionTitle"))
    if not title:
        return None
    categories = _names(descriptor.get("JobCategory"))
    location = _location(descriptor)
    employment = ", ".join(dict.fromkeys(
        _names(descriptor.get("PositionSchedule"))[:1] + _names(descriptor.get("PositionOfferingType"))[:1]))
    remote = re.search(r"\b(remote|telework)\b", f"{title} {location}", re.I)
    return ATSJob(
        title=title,
        company=_text(descriptor.get("OrganizationName")) or _text(descriptor.get("DepartmentName")) or company_name,
        location=location,
        url=_public_url(descriptor, control_number),
        posted_date=_iso_datetime(descriptor.get("PublicationStartDate") or descriptor.get("PositionStartDate")),
        department=categories[0] if categories else (_text(descriptor.get("DepartmentName")) or None),
        work_type="Remote" if remote else None,
        description=_description(descriptor),
        external_id=f"{PLATFORM}:{slug}:{control_number}",
        employment_type=employment,
        salary_text=_salary_text(descriptor),
        detail_ref=control_number,
        apply_url=_apply_url(descriptor),
    )


def _bounded(value, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, ceiling))


# --- The contract -------------------------------------------------------------------------------

async def list_jobs(client, slug, company_name, *, config, deadline=None):
    """List one query, newest first, at most ``max_pages`` pages of ``page_size``.

    Returns (jobs, complete, total) with ``total`` the API's SearchResultCountAll.
    The listing is complete only when the API reported a positive count, every row
    the query matched was returned and the query fits under the 10,000-row ceiling;
    rows that come without a count are refused like any unusable page, so one page
    can never stand for the whole board. A page that fails after the first keeps
    what was listed as an incomplete listing."""
    config = config if isinstance(config, dict) else {}
    auth = _credentials()
    query = {key: value for key, value in (config.get("params") or {}).items()
             if key not in ("Page", "ResultsPerPage")}
    query.setdefault("SortField", "opendate")
    query.setdefault("SortDirection", "desc")
    if config.get("fields"):
        query["Fields"] = config["fields"]
    page_size = _bounded(config.get("page_size"), DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE)

    jobs: list[ATSJob] = []
    seen: set[str] = set()
    total = 0
    for page_number in range(1, _bounded(config.get("max_pages"), DEFAULT_MAX_PAGES, 10_000) + 1):
        if page_number > 1 and deadline is not None and time.monotonic() >= deadline:
            break
        try:
            response = await http.request(
                client, "GET", API_URL, rate_limit=RATE_LIMIT, auth=auth,
                headers={"Accept": "application/json"},
                params={**query, "ResultsPerPage": page_size, "Page": page_number},
            )
            response.raise_for_status()
            result = response.json()["SearchResult"]
            items = result["SearchResultItems"]
            page_total = int(result["SearchResultCountAll"])
            if items and page_total <= 0:
                raise ValueError("rows without a total")
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            if page_number == 1:
                raise
            logger.warning("usajobs %s: page %d failed; keeping the %d postings listed",
                           slug, page_number, len(jobs), exc_info=True)
            break
        total = max(total, page_total)
        for item in items:
            job = _to_job(item, slug, company_name) if isinstance(item, dict) else None
            if job is None or job.detail_ref in seen:
                continue  # the list shifted between pages, or an item without an id
            seen.add(job.detail_ref)
            jobs.append(job)
        if not items or len(seen) >= total:
            break

    return jobs, bool(jobs) and 0 < total <= ROW_CEILING and len(seen) >= total, total
