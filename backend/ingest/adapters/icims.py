"""
iCIMS career portals ("careers-acme.icims.com").

iCIMS has no job feed, so this reads the portal's own public pages, the way a
visitor's browser does:

- the search results, ``/jobs/search?pr=N&in_iframe=1``, 20 to 50 postings a
  page ("Page 1 of 3" says how many pages); each posting is a card with the
  link, the title and a few labelled fields (location, posted date, category,
  position type);
- the posting page, which carries the whole job as a JSON-LD ``JobPosting``
  block (``fetch_job`` and ``check_alive`` read it). A posting that is gone
  answers 410.

The labels differ from one employer to the next ("Job Locations" or "Location",
"Category" or "Department/Unit", "Position Type" or "Job Type"), so fields are
matched by label name and everything unknown is ignored.

This is HTML, so it asks robots.txt first and stops when the portal says no.
A portal whose search redirects to the employer's own site (a Radancy or Jibe
front end) is not an iCIMS list: it raises, and the registry entry belongs on
the adapter for that front end.

Config: ``{"host": "careers-kinaxis.icims.com"}``.
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import os
import re
import time
from urllib.parse import urljoin, urlparse

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob
from backend.services.description_extractor import clean_html
from backend.services.na_location import names_north_american_place
from backend.services.platform_liveness import ALIVE, DEAD, UNKNOWN, LivenessResult, says_dead

logger = logging.getLogger(__name__)

PLATFORM = "icims"
TIER = 2
COUNTRIES = ("US", "CA")
RATE_LIMIT = 1.0
REQUIRED_ENV: tuple[str, ...] = ()
HOST_PATTERNS = (r"^https?://[a-z0-9.-]+\.icims\.com/jobs/\d+",)

# The biggest portal found (Enterprise Holdings) is 105 pages of 30; a board
# past the cap stays incomplete and only confirms what it listed.
MAX_PAGES = max(1, int(os.getenv("ICIMS_MAX_PAGES", "150")))
# The newest-first head of the list, where new postings land. It is always
# fetched; later pages only serve reconciliation, so they stop at the deadline.
HEAD_PAGES = min(MAX_PAGES, 5)
DESCRIPTION_MAX_CHARS = 10_000

_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.icims\.com$")
_JOB_PATH_RE = re.compile(r"^/jobs/(\d+)(?:/|$)")
_CARD_RE = re.compile(r"<li\b[^>]*\biCIMS_JobCardItem\b[^>]*>(.*?)</li>", re.S | re.I)
_ANCHOR_RE = re.compile(r"<a\b([^>]*\biCIMS_Anchor\b[^>]*)>(.*?)</a>", re.S | re.I)
_HEADING_RE = re.compile(r"<h3\b[^>]*>(.*?)</h3>", re.S | re.I)
_FIELD_RE = re.compile(r"""field-label">\s*([^<]*?)\s*</span>\s*<span\b[^>]*>(.*?)</span>""", re.S)
_PAIR_RE = re.compile(r"<dt\b[^>]*>(.*?)</dt>\s*<dd\b[^>]*>(.*?)</dd>", re.S | re.I)
_PAGE_OF_RE = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
_JOB_LINK_RE = re.compile(r"""href=["'][^"']*/jobs/\d+/""", re.I)
_LD_JSON_RE = re.compile(r"""<script\b[^>]*type=["']application/ld\+json["'][^>]*>(.*?)</script>""", re.S | re.I)
_TAGS_RE = re.compile(r"<[^>]+>")
_STAMP_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}\s*[AP]M))?", re.I)
_CODED_PLACE_RE = re.compile(r"^(US|CA)-(?:([A-Z]{2})-)?(.+)$")
_COUNTRY_NAMES = {"US": "United States", "CA": "Canada"}

# Labels (lowercased, colon and spaces trimmed) by what they mean.
_LOCATION = ("location", "locations", "job location", "job locations", "primary location")
_MORE_LOCATIONS = ("additional locations", "additional location", "other locations")
_CITY, _STATE, _COUNTRY = ("city",), ("state/province", "state", "province"), ("country",)
_DEPARTMENT = ("category", "job category", "department", "department/unit", "division",
               "job function", "function", "program")
_COMMITMENT = ("position type", "job type", "type", "full/part time", "full-time/part-time",
               "employment type", "schedule", "job status", "worker type", "employee type",
               "time type", "employment status")
_POSTED = ("posted date", "date posted", "posting date", "posted", "post date")
# schema.org's employmentType words, as the posting page states them.
_COMMITMENT_WORDS = {"FULL_TIME": "Full-time", "PART_TIME": "Part-time", "CONTRACTOR": "Contract",
                     "TEMPORARY": "Temporary", "INTERN": "Intern", "PER_DIEM": "Per diem",
                     "VOLUNTEER": "Volunteer"}


def _host(slug: str, config: dict) -> str:
    """The portal host the config names. Raises on one that cannot work, so a
    dead entry shows up as a failure in ``source_health``, not as an empty board."""
    host = re.sub(r"^https?://", "", str((config or {}).get("host") or "").strip().lower()).split("/")[0]
    if not host:
        raise ValueError(f"icims/{slug} needs config host, got {config!r}")
    if not _HOST_RE.match(host):
        raise ValueError(f"icims/{slug} host {host!r} is not an icims.com host")
    return host


def _text(fragment: str) -> str:
    """The visible text of an HTML fragment, entities decoded, spaces folded."""
    return re.sub(r"\s+", " ", html.unescape(_TAGS_RE.sub(" ", fragment or ""))).strip()


def _label(fragment: str) -> str:
    return re.sub(r"\s+", " ", _text(fragment).rstrip(": ").lower()).strip()


def _place(text: str) -> str:
    """iCIMS writes places as Country-State-City ("CA-ON-Ottawa", "US-Remote").
    The country comes first, so a leading "CA" is Canada, never California;
    spelled out, the place reads the same everywhere downstream. Places in other
    countries ("GB-ENG-London") are left as written."""
    match = _CODED_PLACE_RE.match(text.strip())
    if not match:
        return text.strip()
    country, state, city = match.groups()
    return ", ".join(part for part in (city.strip(), state, _COUNTRY_NAMES[country]) if part)


def _places(value: str) -> str:
    """A location field can hold several places, separated by " | "."""
    parts = [_place(part) for part in value.split("|") if part.strip()]
    return " | ".join(dict.fromkeys(parts))


def _other_na_places(primary: str, value: str) -> str:
    """The North American places among a posting's additional locations, "; "-joined."""
    places: list[str] = []
    for place in _places(value).split(" | "):
        if place and place not in primary and place not in places and names_north_american_place(place):
            places.append(place)
    return "; ".join(places)


def _stamp(value: str) -> datetime.datetime | None:
    """"10/23/2026 11:54 AM" (or just the date) as UTC. The portal does not say
    which zone it means; read as UTC the time is never in the future."""
    match = _STAMP_RE.search(value or "")
    if not match:
        return None
    date, clock = match.groups()
    try:
        parsed = datetime.datetime.strptime(f"{date} {clock.upper().replace(' ', '')}" if clock else date,
                                            "%m/%d/%Y %I:%M%p" if clock else "%m/%d/%Y")
    except ValueError:
        return None
    return parsed.replace(tzinfo=datetime.timezone.utc)


def _fields(card: str) -> dict[str, str]:
    """The card's labelled values, by label. Where a label repeats the first wins."""
    found: dict[str, str] = {}
    for label, value in _FIELD_RE.findall(card):
        found.setdefault(_label(label), _text(value))
    for label, value in _PAIR_RE.findall(card):
        found.setdefault(_label(label), _text(value))
        # The posted date shows "2 days ago"; the exact stamp is in the title attribute.
        stamp = re.search(r'title="([^"]*)"', value)
        if stamp and _label(label) in _POSTED:
            found[_label(label)] = html.unescape(stamp.group(1))
    return found


def _first(fields: dict[str, str], labels) -> str:
    return next((fields[label] for label in labels if fields.get(label)), "")


def _commitment(fields: dict[str, str]) -> str:
    """Every commitment word the card gives, so "Internship" or "Student" is not
    lost behind "Full Time"."""
    words = [fields[label] for label in fields if label in _COMMITMENT and fields[label]]
    return ", ".join(dict.fromkeys(words))


def _work_type(fields: dict[str, str], location: str, title: str) -> str:
    if fields.get("remote", "").lower() in ("yes", "true"):
        return "Remote"
    combined = f"{location} {title}".lower()
    if "remote" in combined:
        return "Hybrid" if "hybrid" in combined else "Remote"
    return "Hybrid" if "hybrid" in combined else "On Site"


def _to_job(card: str, base: str, slug: str, company_name: str) -> ATSJob | None:
    anchor = _ANCHOR_RE.search(card)
    href = re.search(r'href="([^"]+)"', anchor.group(1)) if anchor else None
    if not href:
        return None
    url = urljoin(base, html.unescape(href.group(1)))
    parsed = urlparse(url)
    match = _JOB_PATH_RE.match(parsed.path)
    if not match:
        return None
    url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"  # no ?in_iframe=1 or other query
    heading = _HEADING_RE.search(anchor.group(2))
    title = _text(heading.group(1)) if heading else ""
    if not title:
        # The link's own title reads "35595 - Senior Payroll Specialist".
        named = re.search(r'title="([^"]*)"', anchor.group(1))
        title = re.sub(r"^\s*\d+\s*-\s*", "", html.unescape(named.group(1))) if named else ""
    if not title:
        return None
    fields = _fields(card)
    location = _places(_first(fields, _LOCATION))
    if not location:
        location = ", ".join(part for part in (_first(fields, _CITY), _first(fields, _STATE),
                                               _first(fields, _COUNTRY)) if part)
    more = _first(fields, _MORE_LOCATIONS)
    return ATSJob(
        title=title,
        company=company_name,
        location=location,
        url=url,
        posted_date=_stamp(_first(fields, _POSTED)),
        department=_first(fields, _DEPARTMENT),
        work_type=_work_type(fields, location, title),
        external_id=f"{PLATFORM}:{slug}:{match.group(1)}",
        employment_type=_commitment(fields),
        detail_ref=parsed.path,
        location_hint=_other_na_places(location, more) if more else "",
    )


def _page_url(host: str, page: int) -> str:
    return f"https://{host}/jobs/search?pr={page}&in_iframe=1"


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await http.request(client, "GET", url, rate_limit=RATE_LIMIT,
                              headers={"Accept": "text/html,application/xhtml+xml"})


async def list_jobs(client: httpx.AsyncClient, slug: str, company_name: str, *,
                    config: dict, deadline: float | None = None) -> tuple[list[ATSJob], bool, int]:
    """Every posting on one iCIMS portal, newest first, unfiltered.

    Returns (listings, complete, total). The portal gives no count, so ``total``
    is the postings listed and the evidence that the whole list was read is the
    pager the portal prints on every page, "Page X of Y". ``complete`` is True
    only when all of these hold:
    - every page carried that pager, X was the page asked for (a portal or CDN
      that ignores ``pr`` serves page 1 again), and Y was the same on every page;
    - the walk reached page Y, not the deadline or the page cap, and no page
      repeated postings already seen;
    - every card was readable, and at least one posting was listed.
    A page without the pager, a page that is not the page asked for, a card-less
    page before the last, and an empty first page (which nothing tells from a
    search that hiccuped) all read as partial, never as a takedown."""
    host = _host(slug, config)
    if not await http.robots_allowed(client, _page_url(host, 0)):
        raise PermissionError(f"icims/{slug}: {host} robots.txt does not allow TailrdBot to read its job search")
    jobs: list[ATSJob] = []
    seen: set[str] = set()
    page_counts: set[int] = set()
    unreadable = 0
    ended = broken = unmarked = False
    for page in range(MAX_PAGES):
        if page >= HEAD_PAGES and deadline is not None and time.monotonic() >= deadline:
            break
        response = await _get(client, _page_url(host, page))
        response.raise_for_status()
        if (response.url.host or "").lower() != host:
            raise RuntimeError(f"icims/{slug}: {host} sends its job search to {response.url.host}, "
                               f"an employer front end this adapter does not read")
        body = response.text
        cards = _CARD_RE.findall(body)
        if not cards:
            if page == 0 and _JOB_LINK_RE.search(body):
                raise RuntimeError(f"icims/{slug}: {host} lists postings in a layout this adapter does not read")
            if page == 0 and 'id="searchForm"' not in body:
                raise RuntimeError(f"icims/{slug}: {host} did not answer with an iCIMS job search page")
            # Past the last page the pager promised, the walk is over. Anywhere else a
            # card-less page is an empty first page or a failed read.
            ended = page > 0 and bool(page_counts) and page >= max(page_counts)
            break
        fresh = 0
        for card in cards:
            job = _to_job(card, f"https://{host}", slug, company_name)
            if job is None:
                unreadable += 1
            elif job.url not in seen:  # a posting added mid-crawl shifts one onto the next page
                seen.add(job.url)
                jobs.append(job)
                fresh += 1
        pager = _PAGE_OF_RE.search(body)
        if pager is None:
            unmarked = True  # nothing says which page this is or how many there are
        else:
            page_counts.add(int(pager.group(2)))
            if int(pager.group(1)) != page + 1:
                broken = True  # the portal served another page than the one asked for
                break
        if not fresh:
            broken = True  # the same postings again: the portal is not honouring pr
            break
        if pager is not None and page + 1 >= int(pager.group(2)):
            ended = True
            break
    complete = (ended and not broken and not unmarked and len(page_counts) == 1 and not unreadable
                and bool(jobs))
    return jobs, complete, len(jobs)


def _job_posting(page: str) -> dict | None:
    """The page's JSON-LD ``JobPosting`` block."""
    for block in _LD_JSON_RE.findall(page or ""):
        try:
            data = json.loads(block, strict=False)  # descriptions carry raw newlines
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            kind = item.get("@type") if isinstance(item, dict) else None
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return item
    return None


def _commitment_from(posting: dict) -> str:
    kinds = posting.get("employmentType")
    kinds = kinds if isinstance(kinds, list) else [kinds]
    return ", ".join(dict.fromkeys(_COMMITMENT_WORDS[str(kind).upper()] for kind in kinds
                                   if str(kind).upper() in _COMMITMENT_WORDS))


async def fetch_job(client: httpx.AsyncClient, slug: str, ref: str, *, config: dict) -> dict:
    """One posting's description and commitment, from its page's JSON-LD.
    ``ref`` is the posting's path (``ATSJob.detail_ref``). Empty when the posting
    is gone, the page has no job data, or robots.txt says not to read it."""
    host = _host(slug, config)
    path = str(ref or "").strip()
    if not _JOB_PATH_RE.match(path):
        return {}
    url = f"https://{host}{path}"
    if not await http.robots_allowed(client, url):
        return {}
    response = await _get(client, url)
    if response.status_code in (404, 410):
        return {}
    response.raise_for_status()
    posting = _job_posting(response.text)
    if posting is None:
        return {}
    description = clean_html(posting.get("description") or "")[:DESCRIPTION_MAX_CHARS]
    return {"description": description, "employment_type": _commitment_from(posting)}


async def check_alive(client: httpx.AsyncClient, url: str) -> LivenessResult:
    """Is this posting still open? A closed or removed posting answers 410 (the
    portal's own word for it); an open one carries its JSON-LD ``JobPosting``."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not _HOST_RE.match(host) or not _JOB_PATH_RE.match(parsed.path):
        return LivenessResult(UNKNOWN, "icims_url_unreadable")
    page = f"https://{host}{parsed.path}"
    try:
        if not await http.robots_allowed(client, page):
            return LivenessResult(UNKNOWN, "robots_disallowed")
        response = await _get(client, page)
    except httpx.TimeoutException:
        return LivenessResult(UNKNOWN, "timeout")
    except httpx.HTTPStatusError as exc:
        return LivenessResult(UNKNOWN, f"http_{exc.response.status_code}")
    except httpx.HTTPError:
        return LivenessResult(UNKNOWN, "network_error")
    if (response.url.host or "").lower() != host:
        return LivenessResult(UNKNOWN, "icims_redirected")  # the employer's own front end took over; its answer is not ours to read
    if response.status_code in (404, 410):
        return LivenessResult(DEAD, f"icims_{response.status_code}", True)
    if response.status_code != 200:
        return LivenessResult(UNKNOWN, f"http_{response.status_code}")
    # A portal that answers 200 with a "no longer available" notice instead of 410 is read
    # the way the generic page check reads it, and before the JSON-LD: a closed page can
    # still carry the block, and alive here would revive a row that is closed.
    if says_dead(response.text):
        return LivenessResult(DEAD, "icims_body_closed")
    if _job_posting(response.text) is not None:
        return LivenessResult(ALIVE, "icims_job_posting", True)
    return LivenessResult(UNKNOWN, "icims_no_posting_data")
