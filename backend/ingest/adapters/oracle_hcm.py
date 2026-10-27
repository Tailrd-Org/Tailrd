"""
Oracle Cloud Recruiting (Oracle Fusion HCM "Candidate Experience" career sites).

Banks, hospitals, school districts, universities and utilities run their career
site on it. The site is a single-page app over one REST resource that needs no
login, ``recruitingCEJobRequisitions``, so the list is plain JSON:

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={site},limit=200,offset=N,sortBy=POSTING_DATES_DESC

``host`` is the tenant's Fusion pod ("jpmc.fa.oraclecloud.com",
"fa-etnv-saasfaprod1.fa.ocs.oraclecloud.com") and ``site`` the career site code
from the posting URL ("CX_1001", "CX", or a custom name such as "jobsearch").
Both come from the registry entry's ``config``. A board is one (host, site).

The list carries the title, location, posted date and workplace type but not the
description (only a truncated ``ShortDescriptionStr``, which is deliberately not
stored: it would pass for the whole text). ``fetch_job`` reads the full text
from ``recruitingCEJobRequisitionDetails``, the same finder
``platform_liveness`` uses to tell whether a posting is still open.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import time
from urllib.parse import quote, unquote

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob
from backend.services.description_extractor import clean_html
from backend.services.na_location import names_north_american_place
from backend.services.platform_liveness import ALIVE, DEAD, UNKNOWN, LivenessResult

logger = logging.getLogger(__name__)

PLATFORM = "oracle_hcm"
TIER = 2
COUNTRIES = ("US", "CA")
RATE_LIMIT = 1.0
REQUIRED_ENV: tuple[str, ...] = ()
# Posting pages live on the tenant's pod host, whatever its pod name.
HOST_PATTERNS = (
    r"^https?://[a-z0-9.-]+\.fa(?:\.[a-z0-9-]+)*\.oraclecloud\.com/hcmUI/CandidateExperience/",
)

# The finder serves at most 200 requisitions a page: a larger limit is cut
# back to 200, so asking for more would only make the page arithmetic lie.
PAGE_SIZE = 200
# 50 pages list 10,000 postings, past the biggest board found (JPMorgan Chase,
# about 7,300). A board that needs more stays incomplete and only confirms what
# it listed.
MAX_PAGES = max(1, int(os.getenv("ORACLE_HCM_MAX_PAGES", "50")))
# The newest-first head of the list, where new postings land. It is always
# fetched; later pages only serve reconciliation, so they stop at the deadline.
HEAD_PAGES = min(MAX_PAGES, 5)
DESCRIPTION_MAX_CHARS = 10_000

_HEADERS = {"Accept": "application/json, text/plain, */*"}
# The API is only ever called on an Oracle cloud host, so a bad registry config
# or a stored posting URL can never point the crawler somewhere else.
_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.oraclecloud\.com$")
_POSTING_RE = re.compile(
    r"^https?://([a-z0-9.-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/(?:[^/]+/)*?sites/([^/?#]+)/"
    r"(?:job|jobs/job|jobs/preview|requisitions/preview)/(\d+)",
    re.IGNORECASE,
)
_WORKPLACE_TYPES = {"ORA_REMOTE": "Remote", "ORA_HYBRID": "Hybrid", "ORA_ON_SITE": "On Site"}


def _clean(value) -> str:
    """A source string, stripped; anything that is not text reads as empty."""
    return value.strip() if isinstance(value, str) else ""


def _tenant(slug: str, config: dict) -> tuple[str, str]:
    """The (host, site) a board's config names. Raises on a config that cannot
    work: a board that can never be read must show up as a failure in
    ``source_health``, not as an empty list."""
    config = config or {}
    host = re.sub(r"^https?://", "", _clean(config.get("host")).lower()).split("/")[0]
    site = _clean(config.get("site"))
    if not host or not site:
        raise ValueError(f"oracle_hcm/{slug} needs config host and site, got {config!r}")
    if not _HOST_RE.match(host):
        raise ValueError(f"oracle_hcm/{slug} host {host!r} is not an oraclecloud.com host")
    return host, site


def _list_url(host: str, site: str, offset: int) -> str:
    # The finder arguments are separated by raw ';' and ',', exactly as the
    # career site itself sends them; escaping them is not worth the risk.
    return (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
            f"?onlyData=true&expand=requisitionList.secondaryLocations"
            f"&finder=findReqs;siteNumber={quote(site, safe='')},limit={PAGE_SIZE},"
            f"offset={offset},sortBy=POSTING_DATES_DESC")


def _detail_url(host: str, site: str, requisition_id: str) -> str:
    return (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?expand=all&onlyData=true"
            f"&finder=ById;Id=%22{quote(requisition_id, safe='')}%22,siteNumber={quote(site, safe='')}")


def _posting_url(host: str, site: str, requisition_id: str) -> str:
    return (f"https://{host}/hcmUI/CandidateExperience/en/sites/"
            f"{quote(site, safe='')}/job/{requisition_id}")


def _posted(value) -> datetime.datetime | None:
    """``PostedDate`` is a bare date ("2026-10-24"); read it as midnight UTC."""
    try:
        return datetime.datetime.strptime(_clean(value)[:10], "%Y-%m-%d").replace(
            tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _work_type(req: dict, location: str, title: str) -> str:
    mapped = _WORKPLACE_TYPES.get(_clean(req.get("WorkplaceTypeCode")).upper())
    if mapped:
        return mapped
    # Tenants that leave the field empty (JPMorgan, DTCC) are read from the
    # text, the same rule the other connectors use.
    combined = f"{location} {title}".lower()
    if "remote" in combined:
        return "Hybrid" if "hybrid" in combined else "Remote"
    return "Hybrid" if "hybrid" in combined else "On Site"


def _employment_type(item: dict) -> str:
    """Every commitment word the posting declares, so an "Intern" in the worker
    type is not lost behind a "Full time" schedule."""
    declared: list[str] = []
    for key in ("JobSchedule", "ContractType", "JobType", "WorkerType"):
        value = _clean(item.get(key))
        if value and value not in declared:
            declared.append(value)
    return ", ".join(declared)


def _other_na_places(primary: str, secondary) -> str:
    """The North American places among a posting's other locations, "; "-joined.
    A posting whose primary location is abroad but which is also open in New
    Jersey is a North American posting (DTCC lists Hyderabad first and Jersey
    City second), not a "location" reject."""
    places: list[str] = []
    for location in secondary or []:
        name = _clean(location.get("Name")) if isinstance(location, dict) else ""
        if name and name != primary and name not in places and names_north_american_place(name):
            places.append(name)
    return "; ".join(places)


def _to_job(req: dict, host: str, site: str, slug: str, company_name: str) -> ATSJob | None:
    requisition_id = _clean(str(req.get("Id") or ""))
    title = _clean(req.get("Title"))
    if not requisition_id or not title:
        return None
    secondary = req.get("secondaryLocations")
    location = _clean(req.get("PrimaryLocation"))
    if not location and isinstance(secondary, list) and secondary and isinstance(secondary[0], dict):
        location = _clean(secondary[0].get("Name"))
    return ATSJob(
        title=title,
        company=company_name,
        location=location,
        url=_posting_url(host, site, requisition_id),
        posted_date=_posted(req.get("PostedDate")),
        # Only some tenants fill the family (JPMorgan's "Software Engineering");
        # the department is the next best name for the team.
        department=_clean(req.get("JobFamily")) or _clean(req.get("Department")),
        work_type=_work_type(req, location, title),
        external_id=f"{PLATFORM}:{slug}:{requisition_id}",
        employment_type=_employment_type(req),
        detail_ref=requisition_id,
        location_hint=_other_na_places(location, secondary),
    )


def _search(payload, host: str, site: str) -> dict:
    """The finder's single result envelope. A wrong site code does not fail: the
    API answers 200 with the tenant-wide total and no ``requisitionList``. That
    must not read as an empty board, which reconciliation would act on."""
    items = payload.get("items") if isinstance(payload, dict) else None
    search = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    if search is None or not isinstance(search.get("requisitionList"), list):
        raise ValueError(f"oracle_hcm {host} site {site!r}: the finder returned no "
                         f"requisitionList (wrong site code, or the API changed)")
    return search


async def _get_json(client: httpx.AsyncClient, url: str):
    response = await http.request(client, "GET", url, rate_limit=RATE_LIMIT, headers=_HEADERS)
    response.raise_for_status()
    return response.json()


def _count(value) -> int | None:
    """A reported count as a non-negative integer, or None when the reply gives
    none that can be trusted: absent, null, a boolean, a word, a negative or a
    fraction. A missing count used to read as 0 beside hundreds of listed
    postings, so a bad count must never pass for a small one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        value = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        value = int(value) if value.strip().isdigit() else None
    return value if isinstance(value, int) and value >= 0 else None


def _shortfall_allowed(total: int) -> int:
    """How many fewer postings than ``TotalJobsCount`` a finished crawl may list.

    The count includes rows the finder lists once or never: a posting open in
    two places can sit in the index twice, and the listing shows it once.
    Measured on 29 tenants, 24 listed exactly the count and the rest fell short
    by 1 or 2 (DTCC 446 of 447, Oracle 2,485 of 2,487, JPMorgan Chase 7,249 of
    7,250), always with the count steady from the first page to the last. The
    smallest board that fell short had 447, so a board under 400 must match
    exactly and a large one may miss two or three."""
    if total < 400:
        return 0
    return 2 if total < 5000 else 3


async def list_jobs(client: httpx.AsyncClient, slug: str, company_name: str, *,
                    config: dict, deadline: float | None = None) -> tuple[list[ATSJob], bool, int]:
    """Every posting on one career site, newest first, unfiltered.

    Returns (listings, complete, total_on_board). ``complete`` is True only with
    positive evidence that the whole list was read, all of:
    - every page reported a count, a whole number, the same one on every page
      (a posting removed between two page requests changes it, and shifts the
      list up by one so a live posting is skipped), and it is above zero;
    - the crawl ran to the end (the count reached, or an empty page), not to the
      deadline or the page cap, and no page repeated postings already seen (a
      server that ignores ``offset``);
    - every row was readable;
    - the distinct postings listed are within ``_shortfall_allowed`` of the count
      and not above it.
    A missing, null, zero or non-numeric count, an empty first page beside a
    count, and an empty board all read as partial, never as a takedown."""
    host, site = _tenant(slug, config)
    jobs: list[ATSJob] = []
    seen: set[str] = set()
    counts: list[int | None] = []
    unreadable = 0
    offset = 0
    ended = repeated = False
    for page in range(MAX_PAGES):
        if page >= HEAD_PAGES and deadline is not None and time.monotonic() >= deadline:
            break
        search = _search(await _get_json(client, _list_url(host, site, offset)), host, site)
        requisitions = search["requisitionList"]
        count = _count(search.get("TotalJobsCount"))
        counts.append(count)
        fresh = 0
        for req in requisitions:
            job = _to_job(req, host, site, slug, company_name) if isinstance(req, dict) else None
            if job is None:
                unreadable += 1
            elif job.url not in seen:  # listed twice: identical rows, or the list shifted
                seen.add(job.url)
                jobs.append(job)
                fresh += 1
        offset += len(requisitions)
        if not requisitions:
            ended = True
            break
        if not fresh:
            repeated = True  # the same postings again: the server is not honouring offset
            break
        if count and offset >= count:  # a count of 0 beside rows is a contradiction, not the end
            ended = True
            break
    known = [count for count in counts if count is not None]
    total = max(known, default=0)
    steady = bool(counts) and len(known) == len(counts) and len(set(known)) == 1
    # More listed than counted is as much a contradiction as fewer: the count
    # cannot be taken as the size of the board.
    complete = (ended and not repeated and not unreadable and steady and total > 0
                and 0 <= total - len(seen) <= _shortfall_allowed(total))
    return jobs, complete, total


async def fetch_job(client: httpx.AsyncClient, slug: str, ref: str, *, config: dict) -> dict:
    """One posting's description and commitment, from the details finder.
    ``ref`` is the requisition id (``ATSJob.detail_ref``). Empty when the
    posting is gone or has no text."""
    host, site = _tenant(slug, config)
    requisition_id = _clean(str(ref or ""))
    if not requisition_id:
        return {}
    response = await http.request(client, "GET", _detail_url(host, site, requisition_id),
                                  rate_limit=RATE_LIMIT, headers=_HEADERS)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    payload = response.json()
    items = payload.get("items") if isinstance(payload, dict) else None
    item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None
    if item is None:
        return {}
    # The three job-specific sections, in the order the career site shows them.
    # The employer-wide "about us" blurbs are left out: they are the same on
    # every posting and would crowd the real text out of the length cap.
    sections = [clean_html(item.get(key) or "") for key in
                ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
    description = "\n\n".join(section for section in sections if section)[:DESCRIPTION_MAX_CHARS]
    return {"description": description, "employment_type": _employment_type(item)}


async def check_alive(client: httpx.AsyncClient, url: str) -> LivenessResult:
    """Is this posting still open? The details finder returns the requisition
    while it is open and an empty item list once it is gone; the page itself
    is a single-page app that looks the same either way."""
    match = _POSTING_RE.match(url or "")
    if not match:
        return LivenessResult(UNKNOWN, "oracle_url_unreadable")
    host, site, requisition_id = match.group(1).lower(), unquote(match.group(2)), match.group(3)
    try:
        response = await http.request(client, "GET", _detail_url(host, site, requisition_id),
                                      rate_limit=RATE_LIMIT, headers=_HEADERS)
    except httpx.TimeoutException:
        return LivenessResult(UNKNOWN, "timeout")
    except httpx.HTTPStatusError as exc:
        return LivenessResult(UNKNOWN, f"http_{exc.response.status_code}")
    except httpx.HTTPError:
        return LivenessResult(UNKNOWN, "network_error")
    if response.status_code != 200:
        return LivenessResult(UNKNOWN, f"http_{response.status_code}")
    try:
        items = response.json().get("items")
    except (ValueError, AttributeError):
        items = None
    if not isinstance(items, list):
        return LivenessResult(UNKNOWN, "oracle_api_unreadable")
    if items:
        return LivenessResult(ALIVE, "oracle_api_item", True)
    return LivenessResult(DEAD, "oracle_items_0", True)
