"""
Oracle Taleo career sections (the "Taleo Enterprise" ones, ``*.taleo.net``).

A career section's page runs on a JSON search that needs no login, only the
timezone headers its own scripts send:

    POST https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}

``portal`` is the career section's id, written in the section's own search page
(``/careersection/{section}/jobsearch.ftl``), which is also where this reads it
from when the config does not name it. The reply pages the whole board, 25
requisitions a page, newest first. Each requisition carries its job id and a
``column`` list whose meaning each section chooses for itself: the first column
is the title, the ones named in ``locationsColumns`` are places, and the rest
may be a schedule, a work arrangement or a date. Only the title and the places
are certain, so other columns are read for commitment and workplace words and
nothing else; no posted date is taken from them, because one section shows the
posting date there and another the closing date.

The description is on the posting's own page, ``jobdetail.ftl?job={id}``, which
ships it percent-encoded inside a data island that
``description_extractor._extract_taleo_html`` already knows how to open.

There is no ``check_alive`` and no claimed URL, on purpose. A posting that is gone still
answers 200 with the career section's search page, and the section's maintenance page
("Career Section Unavailable") looks much the same from outside; the generic page check
in ``platform_liveness`` already reads the maintenance page as "unknown", and a wrong
"dead" from here would close live postings. A complete board listing is what retires a
posting that is gone.

Not covered, and raising when pointed at: Taleo Business Edition
(``*.tbe.taleo.net``, an HTML list behind a session) and the older career
section layout, which has no JSON search (its page has no portal id).

Config: ``{"host": "bayshore.taleo.net", "section": "bs_ex"}``, and optionally
``"portal"`` to skip reading it from the page.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob
from backend.services.description_extractor import _extract_taleo_html

logger = logging.getLogger(__name__)

PLATFORM = "taleo"
TIER = 2
COUNTRIES = ("US", "CA")
RATE_LIMIT = 1.0
REQUIRED_ENV: tuple[str, ...] = ()
HOST_PATTERNS: tuple[str, ...] = ()

# What the search replies with; the reply says its real page size.
PAGE_SIZE = 25
# 80 pages list 2,000 postings. A board past that stays incomplete and only
# confirms what it listed.
MAX_PAGES = max(1, int(os.getenv("TALEO_MAX_PAGES", "80")))
# The newest-first head of the list, where new postings land. It is always
# fetched; later pages only serve reconciliation, so they stop at the deadline.
HEAD_PAGES = min(MAX_PAGES, 3)

# Some career sections (albertahs.taleo.net) label a reply as compressed and
# send something zlib cannot read, so every request asks for the plain text.
_PLAIN = {"Accept-Encoding": "identity"}
_SEARCH_HEADERS = {**_PLAIN, "Accept": "application/json, text/javascript, */*; q=0.01",
                   "X-Requested-With": "XMLHttpRequest", "tz": "GMT-05:00", "tzname": "America/New_York"}
# One taleo.net label before the domain: "x.tbe.taleo.net" (Business Edition) is a different product.
_HOST_RE = re.compile(r"^[a-z0-9-]+\.taleo\.net$")
_SECTION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_PORTAL_RE = re.compile(r"portal=(\d+)")
_DATE_RE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{2,4}|[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}.*|\d{4}-\d{2}-\d{2}.*)$")
# A column counts as a commitment or a workplace only when every word in it is
# one of these: "Permanent Full-Time" is a commitment, "Long Term Care" is not.
_COMMITMENT_WORDS = frozenset({
    "permanent", "temporary", "full", "part", "time", "full-time", "part-time", "fulltime", "parttime",
    "casual", "contract", "seasonal", "regular", "fixed", "term", "fixed-term", "student", "intern",
    "internship", "co-op", "coop", "relief", "on", "call", "on-call", "per", "diem", "and", "or"})
_WORKPLACE_WORDS = frozenset({"remote", "hybrid", "in-office", "in", "office", "on-site", "onsite", "on", "site"})
_WORD_RE = re.compile(r"[a-z]+(?:-[a-z]+)*")


def _board(slug: str, config: dict) -> tuple[str, str, str]:
    """The (host, section, portal) a board's config names; portal is "" until
    read from the page. Raises on a config that cannot work, so a dead entry
    shows up as a failure in ``source_health`` and not as an empty board."""
    config = config or {}
    host = re.sub(r"^https?://", "", str(config.get("host") or "").strip().lower()).split("/")[0]
    section = str(config.get("section") or "").strip()
    portal = str(config.get("portal") or "").strip()
    if not host or not section:
        raise ValueError(f"taleo/{slug} needs config host and section, got {config!r}")
    if not _HOST_RE.match(host):
        raise ValueError(f"taleo/{slug} host {host!r} is not a <name>.taleo.net host "
                         f"(Taleo Business Edition, *.tbe.taleo.net, is not supported)")
    if not _SECTION_RE.match(section):
        raise ValueError(f"taleo/{slug} section {section!r} is not a career section name")
    if portal and not portal.isdigit():
        raise ValueError(f"taleo/{slug} portal {portal!r} is not a number")
    return host, section, portal


def _page_url(host: str, section: str) -> str:
    return f"https://{host}/careersection/{section}/jobsearch.ftl?lang=en"


def _detail_url(host: str, section: str, job_id: str) -> str:
    return f"https://{host}/careersection/{section}/jobdetail.ftl?job={job_id}&lang=en"


def _query(page_number: int) -> dict:
    """The body the career section's own search sends for "all jobs, newest first"."""
    def selections(*ids: str) -> list[dict]:
        return [{"id": name, "selectedValues": []} for name in ids]

    return {
        "multilineEnabled": False,
        "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
        "fieldData": {"fields": {"KEYWORD": "", "LOCATION": ""}, "valid": True},
        "filterSelectionParam": {"searchFilterSelections": selections(
            "POSTING_DATE", "LOCATION", "JOB_FIELD", "JOB_TYPE", "JOB_SCHEDULE", "JOB_LEVEL")},
        "advancedSearchFiltersSelectionParam": {"searchFilterSelections": selections(
            "ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER", "URGENT_JOB", "EMPLOYEE_STATUS",
            "STUDY_LEVEL", "WILL_TRAVEL", "JOB_SHIFT")},
        "pageNo": page_number,
    }


def _only_words(value: str, vocabulary: frozenset) -> bool:
    words = _WORD_RE.findall(value.lower())
    return bool(words) and all(word in vocabulary for word in words)


def _places(row: dict, columns: list) -> str:
    """The places in the columns the row marks as locations: each is a JSON list
    of strings ('["ON-Mississauga"]'), joined with " | " like iCIMS and Workday."""
    places: list[str] = []
    for index in row.get("locationsColumns") or []:
        if not isinstance(index, int) or not 0 <= index < len(columns):
            continue
        raw = str(columns[index] or "").strip()
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        for place in parsed if isinstance(parsed, list) else [parsed]:
            place = str(place or "").strip()
            if place and place not in places:
                places.append(place)
    return " | ".join(places)


def _to_job(row: dict, host: str, section: str, slug: str, company_name: str) -> ATSJob | None:
    columns = row.get("column")
    job_id = str(row.get("jobId") or "").strip()
    if not job_id or not isinstance(columns, list):
        return None
    linked = row.get("linkedColumn")
    title = str(columns[linked] if isinstance(linked, int) and 0 <= linked < len(columns) else "").strip()
    if not title:
        return None
    location = _places(row, columns)
    marked = {index for index in (row.get("locationsColumns") or []) if isinstance(index, int)}
    commitments: list[str] = []
    workplaces: list[str] = []
    for index, value in enumerate(columns):
        value = str(value or "").strip()
        if index == linked or index in marked or not value or _DATE_RE.match(value):
            continue
        if _only_words(value, _COMMITMENT_WORDS):
            if value not in commitments:
                commitments.append(value)
        elif _only_words(value, _WORKPLACE_WORDS) and value not in workplaces:
            workplaces.append(value)
    workplace = " ".join(workplaces).lower()
    combined = f"{location} {title} {workplace}".lower()
    if "remote" in combined:
        work_type = "Hybrid" if "hybrid" in combined else "Remote"
    else:
        work_type = "Hybrid" if "hybrid" in combined else "On Site"
    return ATSJob(
        title=title,
        company=company_name,
        location=location,
        url=_detail_url(host, section, job_id),
        department="",  # no column is known to be the department
        work_type=work_type,
        external_id=f"{PLATFORM}:{slug}:{job_id}",
        employment_type=", ".join(commitments),
        detail_ref=job_id,
    )


async def _discover_portal(client: httpx.AsyncClient, slug: str, host: str, section: str) -> str:
    response = await http.request(client, "GET", _page_url(host, section), rate_limit=RATE_LIMIT, headers=_PLAIN)
    response.raise_for_status()
    match = _PORTAL_RE.search(response.text)
    if not match:
        raise RuntimeError(f"taleo/{slug}: the {section!r} career section on {host} has no JSON job search "
                           f"(an older layout, or not a career section page)")
    return match.group(1)


async def _search(client: httpx.AsyncClient, slug: str, host: str, portal: str, page_number: int) -> dict:
    response = await http.request(
        client, "POST", f"https://{host}/careersection/rest/jobboard/searchjobs?lang=en&portal={portal}",
        rate_limit=RATE_LIMIT, headers=_SEARCH_HEADERS, json=_query(page_number))
    response.raise_for_status()
    data = response.json()
    if (not isinstance(data, dict) or not isinstance(data.get("requisitionList"), list)
            or not isinstance(data.get("pagingData"), dict)):
        raise ValueError(f"taleo/{slug}: the job search on {host} answered without a requisition list")
    if data.get("careerSectionUnAvailable"):
        raise RuntimeError(f"taleo/{slug}: the career section on {host} reports itself unavailable")
    return data


def _count(value) -> int | None:
    """A reported number as a non-negative integer, or None when the reply gives
    none that can be trusted: absent, null, a boolean, a word, a negative or a
    fraction. A missing count used to read as 0 and end the walk after one page
    as complete, so a bad count must never pass for a small one."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, float):
        value = int(value) if value.is_integer() else None
    elif isinstance(value, str):
        value = int(value) if value.strip().isdigit() else None
    return value if isinstance(value, int) and value >= 0 else None


async def list_jobs(client: httpx.AsyncClient, slug: str, company_name: str, *,
                    config: dict, deadline: float | None = None) -> tuple[list[ATSJob], bool, int]:
    """Every public posting on one career section, newest first, unfiltered.

    Returns (listings, complete, total_the_source_reported). The reported count
    runs well above what is listed (City of Edmonton reports 56 and lists 44,
    Bayshore 574 and 445): it includes requisitions a visitor is not shown, and
    each page is a fixed window over all of them, so a page can come back short.
    The count is therefore an upper bound, not a size to match, and the evidence
    that the whole list was read is the walk itself. ``complete`` is True only
    when all of these hold:
    - every reply gave a count, a whole number above zero, the same on every page
      (a posting removed between two page requests changes it, and shifts the
      list up by one so a live posting is skipped);
    - every page the count implies was read, each reply says it is the page asked
      for (``currentPageNo``) and none repeated postings already seen, so the
      server honoured ``pageNo``; no empty page was followed by rows (the
      requisitions a visitor cannot see sit together at the end, so the walk may
      end on the last page the count implies or on a second empty page in a row,
      never on one empty page with rows behind it); and the crawl was not cut by
      the deadline or the page cap;
    - every row was readable;
    - at least three fifths of the count are listed (measured: 77.5% to 100% on
      the verified career sections), so a walk that read almost nothing cannot
      pass on page arithmetic alone.
    A missing, null or zero count, an empty board and an empty first page beside
    a count all read as partial, never as a takedown."""
    host, section, portal = _board(slug, config)
    if not await http.robots_allowed(client, _page_url(host, section)):
        raise PermissionError(f"taleo/{slug}: {host} robots.txt does not allow TailrdBot to read its career section")
    portal = portal or await _discover_portal(client, slug, host, section)
    jobs: list[ATSJob] = []
    seen: set[str] = set()
    counts: list[int | None] = []
    unreadable = 0
    ended = broken = False
    after_empty = False  # the previous page held no rows
    for number in range(1, MAX_PAGES + 1):
        if number > HEAD_PAGES and deadline is not None and time.monotonic() >= deadline:
            break
        data = await _search(client, slug, host, portal, number)
        paging = data["pagingData"]
        count = _count(paging.get("totalCount"))
        counts.append(count)
        current = _count(paging.get("currentPageNo"))
        if current is not None and current != number:
            broken = True  # the server answered a different page than the one asked for
            break
        rows = data["requisitionList"]
        fresh = 0
        for row in rows:
            job = _to_job(row, host, section, slug, company_name) if isinstance(row, dict) else None
            if job is None:
                unreadable += 1
            elif job.url not in seen:
                seen.add(job.url)
                jobs.append(job)
                fresh += 1
        # A reply may claim any page size; believe no more than the 25 a page is,
        # unless this page really held more.
        size = min(_count(paging.get("pageSize")) or PAGE_SIZE, max(PAGE_SIZE, len(rows)))
        pages = -(-count // size) if count else None  # how many pages the count implies
        if pages is None:
            break  # no usable count: nothing says how far to read, and it cannot be complete
        if not rows:
            if number >= pages or after_empty:
                ended = True  # the last page the count implies, or a second empty page in a row
                break
            if number == 1:
                broken = True  # nothing at all beside a count
                break
            # The requisitions a visitor cannot see sit together at the end of the
            # list (Bayshore: pages 20 to 23 of 23 are empty), so one empty page
            # is a tail only if the next is empty too; rows after it are a hole.
            after_empty = True
            continue
        if after_empty or not fresh:
            broken = True  # rows after an empty page, or the same postings again (pageNo ignored)
            break
        if number >= pages:
            ended = True
            break
    known = [count for count in counts if count is not None]
    total = max(known, default=0)
    steady = bool(counts) and len(known) == len(counts) and len(set(known)) == 1
    complete = (ended and not broken and not unreadable and steady and total > 0
                and len(seen) * 5 >= total * 3)
    return jobs, complete, total


def _has_job_data(page: str) -> bool:
    """A posting's page carries its requisition in the data island; a page for a
    posting that is gone is the bare career section."""
    return "requisitionno" in (page or "").lower()


async def fetch_job(client: httpx.AsyncClient, slug: str, ref: str, *, config: dict) -> dict:
    """One posting's description, from its page's data island. ``ref`` is the job
    id (``ATSJob.detail_ref``). Empty when the posting is gone or has no prose."""
    host, section, _ = _board(slug, config)
    job_id = str(ref or "").strip()
    if not job_id.isdigit():
        return {}
    url = _detail_url(host, section, job_id)
    if not await http.robots_allowed(client, url):
        return {}
    response = await http.request(client, "GET", url, rate_limit=RATE_LIMIT, headers=_PLAIN)
    if response.status_code in (404, 410):
        return {}
    response.raise_for_status()
    description = _extract_taleo_html(response.text) if _has_job_data(response.text) else ""
    return {"description": description, "employment_type": ""} if description else {}
