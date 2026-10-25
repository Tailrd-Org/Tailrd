"""
SAP SuccessFactors career sites, through their Recruiting Marketing front end
(the Jobs2Web product: ``jobs.rogers.com``, ``careers.telus.com``,
``jobs.utoronto.ca``).

The SuccessFactors back end itself, ``careerNN.sapsf.com`` and
``careerNN.successfactors.com``, answers every crawler with ``Disallow: /`` in
its robots.txt, so it is not read. Most employers also publish the same
postings on a site of their own domain, run by Recruiting Marketing, whose
robots.txt keeps out only the apply, alert and error pages. Its search page is
plain server-rendered HTML:

    GET https://{host}/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow=N

"Results 26 - 35 of 35" says how many postings there are and which rows this
page holds, and each row is a ``tr.data-row`` with ``td.colTitle`` (the link),
``td.colLocation``, ``td.colDate`` and the employer's own optional columns
(``colDepartment``, ``colFacility``, ``colShifttype``). The posting page holds
the description in ``span.jobdescription``.

Config: ``{"host": "jobs.rogers.com"}``, the employer's own Recruiting Marketing
site. A registry entry with only a ``careerNN.sapsf.com`` address has no such
host yet and is not crawlable.
"""

from __future__ import annotations

import datetime
import html
import logging
import os
import re
import time
from urllib.parse import urlparse

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob
from backend.services.description_extractor import clean_html
from backend.services.platform_liveness import ALIVE, DEAD, UNKNOWN, LivenessResult

logger = logging.getLogger(__name__)

PLATFORM = "successfactors"
TIER = 2
COUNTRIES = ("US", "CA")
RATE_LIMIT = 1.0
REQUIRED_ENV: tuple[str, ...] = ()
# Employers run this on their own domains, so a posting URL is recognised by its
# shape: /job/<city-and-title>/<9 or 10 digit id>/.
HOST_PATTERNS = (r"^https?://[^/]+/job/[^/]+/\d{9,10}/?(?:[?#].*)?$",)

# The biggest site found (EY) lists 9,100 postings at 25 a page; a board past
# the cap stays incomplete and only confirms what it listed.
MAX_PAGES = max(1, int(os.getenv("SUCCESSFACTORS_MAX_PAGES", "120")))
# The newest-first head of the list, where new postings land. It is always
# fetched; later pages only serve reconciliation, so they stop at the deadline.
HEAD_PAGES = min(MAX_PAGES, 5)
DESCRIPTION_MAX_CHARS = 10_000

_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
# The careerNN hosts are the robots-blocked back end, never a front end.
_BACK_END_RE = re.compile(r"(^|\.)(sapsf\.(com|eu|cn)|successfactors\.(com|eu|cn))$")
_ROW_RE = re.compile(r"<tr\b[^>]*class=\"[^\"]*\bdata-row\b[^\"]*\"[^>]*>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<td\b[^>]*class=\"[^\"]*\bcol([A-Za-z0-9]+)\b[^\"]*\"[^>]*>(.*?)</td>", re.S | re.I)
_LINK_RE = re.compile(r"<a\b([^>]*\bjobTitle-link\b[^>]*)>(.*?)</a>", re.S | re.I)
_PHONE_LOCATION_RE = re.compile(r"<span\b[^>]*class=\"jobLocation\"[^>]*>(.*?)</span>", re.S | re.I)
_PHONE_DATE_RE = re.compile(r"<span\b[^>]*class=\"[^\"]*\bjobDate\b[^\"]*\"[^>]*>(.*?)</span>", re.S | re.I)
# "Results <b>26 - 35</b> of <b>35</b>", in the site's language ("Resultats 1 - 20 sur 32").
_COUNT_RE = re.compile(
    r"class=\"paginationLabel\"[^>]*>[^<]*<b>\s*(\d[\d,]*)\s*[-–]\s*(\d[\d,]*)\s*</b>[^<]*<b>\s*(\d[\d,]*)\s*</b>", re.I)
_MORE_LOCATIONS_RE = re.compile(r"\s*\+\s*\d+\s+more\W*$", re.I)
_JOB_PATH_RE = re.compile(r"^/job/[^/]+/(\d+)/?$")
_TAGS_RE = re.compile(r"<[^>]+>")
_SPAN_TAG_RE = re.compile(r"<(/?)span\b[^>]*>", re.I)
# "Toronto, ON, CA" or "Toronto, ON, CA, M1K5L1": the country comes after the region, then maybe a postal code.
_PLACE_RE = re.compile(r"^(.*?,\s*[A-Za-z]{2}),\s*(CA|US)(?:,\s*[A-Za-z0-9 -]{3,10})?$")
_COUNTRY_NAMES = {"CA": "Canada", "US": "United States"}
# A column counts as a commitment only when every word in it is one of these:
# "Full-Time" is, "Faculty" is not.
_COMMITMENT_WORDS = frozenset({
    "permanent", "temporary", "full", "part", "time", "full-time", "part-time", "fulltime", "parttime",
    "casual", "contract", "seasonal", "regular", "fixed", "term", "fixed-term", "student", "intern",
    "internship", "co-op", "coop", "relief", "on", "call", "on-call", "per", "diem", "and", "or"})
_WORD_RE = re.compile(r"[a-z]+(?:-[a-z]+)*")


def _host(slug: str, config: dict) -> str:
    """The Recruiting Marketing host the config names. Raises on one that cannot
    work, so a dead entry shows up as a failure in ``source_health``, not as an
    empty board."""
    host = re.sub(r"^https?://", "", str((config or {}).get("host") or "").strip().lower()).split("/")[0]
    if not host:
        raise ValueError(f"successfactors/{slug} needs config host (the employer's own careers site), got {config!r}")
    if not _HOST_RE.match(host):
        raise ValueError(f"successfactors/{slug} host {host!r} is not a host name")
    if _BACK_END_RE.search(host):
        raise ValueError(f"successfactors/{slug} host {host!r} is the SuccessFactors back end, whose robots.txt "
                         f"bars crawlers; give the employer's own Recruiting Marketing site")
    return host


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAGS_RE.sub(" ", fragment or ""))).strip()


def _only_words(value: str) -> bool:
    words = _WORD_RE.findall(value.lower())
    return bool(words) and all(word in _COMMITMENT_WORDS for word in words)


def _place(text: str) -> str:
    """"Toronto, ON, CA, M1K5L1" as "Toronto, ON, Canada": a bare "CA" after a
    region reads as California to anything that parses it, and the postal code
    names no place a visitor searches for."""
    text = _MORE_LOCATIONS_RE.sub("", text.strip())  # "Brampton, ON, CA +1 more..."
    if text.upper() in _COUNTRY_NAMES:
        return _COUNTRY_NAMES[text.upper()]  # SaskTel's remote postings say only "CA"
    match = _PLACE_RE.match(text)
    if not match:
        return text
    return f"{match.group(1)}, {_COUNTRY_NAMES[match.group(2).upper()]}"


def _posted(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(value.strip(), "%b %d, %Y").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _page_url(host: str, start: int) -> str:
    return f"https://{host}/search/?q=&sortColumn=referencedate&sortDirection=desc&startrow={start}"


def _to_job(row: str, host: str, slug: str, company_name: str) -> ATSJob | None:
    link = _LINK_RE.search(row)
    href = re.search(r"href=\"([^\"]+)\"", link.group(1)) if link else None
    if not href:
        return None
    path = urlparse(html.unescape(href.group(1))).path
    match = _JOB_PATH_RE.match(path)
    path = path.rstrip("/") + "/"  # some rows link without the closing slash the site itself uses
    title = _text(link.group(2))
    if not match or not title:
        return None
    cells = {name.lower(): _text(value) for name, value in _CELL_RE.findall(row)}
    if not cells.get("location"):
        # A site without a location column puts it in the title cell, for phones.
        phone = _PHONE_LOCATION_RE.search(row)
        cells["location"] = _text(phone.group(1)) if phone else ""
    if not cells.get("date"):
        phone_date = _PHONE_DATE_RE.search(row)
        cells["date"] = _text(phone_date.group(1)) if phone_date else ""
    location = _place(cells["location"])
    shift = cells.get("shifttype", "")
    combined = f"{location} {title}".lower()
    if "remote" in combined:
        work_type = "Hybrid" if "hybrid" in combined else "Remote"
    else:
        work_type = "Hybrid" if "hybrid" in combined else "On Site"
    return ATSJob(
        title=title,
        company=company_name,
        location=location,
        url=f"https://{host}{path}",
        posted_date=_posted(cells["date"]),
        department=cells.get("department", ""),
        work_type=work_type,
        external_id=f"{PLATFORM}:{slug}:{match.group(1)}",
        employment_type=shift if _only_words(shift) else "",
        detail_ref=path,
    )


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await http.request(client, "GET", url, rate_limit=RATE_LIMIT,
                              headers={"Accept": "text/html,application/xhtml+xml"})


async def list_jobs(client: httpx.AsyncClient, slug: str, company_name: str, *,
                    config: dict, deadline: float | None = None) -> tuple[list[ATSJob], bool, int]:
    """Every posting on one Recruiting Marketing site, newest first, unfiltered.

    Returns (listings, complete, total_the_site_reports). ``complete`` needs the
    crawl to have reached the last row (not the deadline or the page cap), the
    reported count to have stayed the same, every row to have been readable, and
    as many distinct postings listed as the site counts: a posting removed
    between two page requests shifts the list up by one and skips a live
    posting, and that must read as partial, never as a takedown."""
    host = _host(slug, config)
    if not await http.robots_allowed(client, _page_url(host, 0)):
        raise PermissionError(f"successfactors/{slug}: {host} robots.txt does not allow TailrdBot to read its job search")
    jobs: list[ATSJob] = []
    seen: set[str] = set()
    totals: set[int] = set()
    unreadable = 0
    uncounted = False
    start = 0
    ended = False
    for page in range(MAX_PAGES):
        if page >= HEAD_PAGES and deadline is not None and time.monotonic() >= deadline:
            break
        response = await _get(client, _page_url(host, start))
        response.raise_for_status()
        if (response.url.host or "").lower() != host:
            raise RuntimeError(f"successfactors/{slug}: {host} sends its job search to {response.url.host}")
        body = response.text
        rows = _ROW_RE.findall(body)
        if not rows:
            if page == 0 and not re.search(r"no open positions|aucun poste", _text(body), re.I) and not _COUNT_RE.search(body):
                raise RuntimeError(f"successfactors/{slug}: {host} did not answer with a Recruiting Marketing job search page")
            ended = True  # a site with no postings, or the page after the last one
            break
        for row in rows:
            job = _to_job(row, host, slug, company_name)
            if job is None:
                unreadable += 1
            elif job.url not in seen:
                seen.add(job.url)
                jobs.append(job)
        start += len(rows)
        counted = _COUNT_RE.search(body)
        if counted:
            totals.add(int(counted.group(3).replace(",", "")))
            if start >= max(totals):
                ended = True
                break
        else:
            # A custom template with no count: the next-page links say whether there is more.
            uncounted = True
            if not any(int(number) >= start for number in re.findall(r"startrow=(\d+)", body)):
                ended = True
                break
    total = max(totals, default=len(jobs))
    complete = (ended and len(totals) <= 1 and not unreadable and not uncounted
                and len(seen) >= total)
    return jobs, complete, total


def _description_html(page: str) -> str:
    """The inside of ``span.jobdescription``, which holds spans of its own."""
    opening = re.search(r"<span\b[^>]*class=\"[^\"]*\bjobdescription\b[^\"]*\"[^>]*>", page or "", re.I)
    if not opening:
        return ""
    depth = 1
    for tag in _SPAN_TAG_RE.finditer(page, opening.end()):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return page[opening.end():tag.start()]
    return ""


async def fetch_job(client: httpx.AsyncClient, slug: str, ref: str, *, config: dict) -> dict:
    """One posting's description, from its page. ``ref`` is the posting's path
    (``ATSJob.detail_ref``). Empty when the posting is gone or has no text."""
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
    if (response.url.host or "").lower() != host or response.url.path.startswith("/errorpage"):
        return {}
    description = clean_html(_description_html(response.text))[:DESCRIPTION_MAX_CHARS]
    return {"description": description, "employment_type": ""} if description else {}


async def check_alive(client: httpx.AsyncClient, url: str) -> LivenessResult:
    """Is this posting still open? An open one is a page with its description; a
    posting that is gone sends the visitor on to the site's error page. That
    page is also where a server fault lands, so it is a soft answer, not an
    authoritative one."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if not parsed.scheme.startswith("http") or not _HOST_RE.match(host) or _BACK_END_RE.search(host) \
            or not _JOB_PATH_RE.match(parsed.path):
        return LivenessResult(UNKNOWN, "successfactors_url_unreadable")
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
        return LivenessResult(UNKNOWN, "successfactors_redirected")  # whatever another site answers is not this posting's
    if response.status_code in (404, 410):
        return LivenessResult(DEAD, f"successfactors_{response.status_code}", True)
    if response.status_code != 200:
        return LivenessResult(UNKNOWN, f"http_{response.status_code}")
    if response.url.path.startswith("/errorpage"):
        return LivenessResult(DEAD, "successfactors_errorpage")
    if _description_html(response.text):
        return LivenessResult(ALIVE, "successfactors_job_page", True)
    return LivenessResult(UNKNOWN, "successfactors_no_job_data")
