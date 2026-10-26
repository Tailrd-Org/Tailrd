"""
iCIMS Career Sites on the Jibe platform (careers.costco.com, jobs.statefarm.com,
careers.amd.com, careers.akima.com, dish.jibeapply.com ...).

These are the employer's own career sites, served from its domain, and each
publishes the whole board as public JSON:

    GET https://{host}/api/jobs?limit=100&page=N&sortBy=posted_date&descending=true

``limit`` tops out at 100 (a larger one answers without a ``jobs`` list), pages
count from 1, and ``totalCount`` is the board's size. Without ``sortBy`` the
order is by relevance, which would shuffle between page requests; sorted by
posting date, newest first, a page boundary only moves when a posting is added
or removed. Every posting arrives with its full description, so there is no
second request per job and no ``fetch_job``.

Every Jibe site checked asks for ``Crawl-delay: 5`` in its robots.txt, so
``RATE_LIMIT`` is five seconds, a site that asks for more is given up to a
minute, and one that asks for more than that is skipped with an error. A
page of 100 postings is 1 to 3 MB of JSON, so a very large board (Costco lists
20,000 postings) takes minutes and, past the crawl deadline, stops after its
newest pages and reports itself incomplete.

There is no per-posting API (``/api/jobs/{id}`` is a 404), and the career sites
live on arbitrary domains, so this adapter claims no posting URLs and has no
``check_alive``: a posting page answers 404 once the posting is gone, which the
generic page check already reads.

Config: ``{"host": "careers.akima.com"}``.
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import time
from urllib.parse import quote

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob
from backend.services.description_extractor import clean_html

logger = logging.getLogger(__name__)

PLATFORM = "jibe"
TIER = 2
COUNTRIES = ("US", "CA")
RATE_LIMIT = 5.0
REQUIRED_ENV: tuple[str, ...] = ()
HOST_PATTERNS: tuple[str, ...] = ()

PAGE_SIZE = 100  # the API's maximum
# At five seconds a page, 250 pages are twenty minutes: enough for Costco's 207,
# and the crawl deadline ends a slower run long before that.
MAX_PAGES = max(1, int(os.getenv("JIBE_MAX_PAGES", "250")))
# The newest-first head of the list, where new postings land. It is always
# fetched; later pages only serve reconciliation, so they stop at the deadline.
HEAD_PAGES = min(MAX_PAGES, 3)
DESCRIPTION_MAX_CHARS = 10_000
# The longest Crawl-delay honoured: at 100 postings a page, a minute between pages
# is already an hour for a thousand postings. A site that asks for more is skipped.
MAX_CRAWL_DELAY = 60.0

_HOST_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
_COMMITMENT_WORDS = {"FULL_TIME": "Full-time", "PART_TIME": "Part-time", "CONTRACTOR": "Contract",
                     "TEMPORARY": "Temporary", "INTERN": "Intern", "PER_DIEM": "Per diem",
                     "VOLUNTEER": "Volunteer"}


def _host(slug: str, config: dict) -> str:
    """The career site host the config names. Raises on one that cannot work, so
    a dead entry shows up as a failure in ``source_health``, not as an empty board."""
    host = re.sub(r"^https?://", "", str((config or {}).get("host") or "").strip().lower()).split("/")[0]
    if not host:
        raise ValueError(f"jibe/{slug} needs config host (the employer's career site), got {config!r}")
    if not _HOST_RE.match(host):
        raise ValueError(f"jibe/{slug} host {host!r} is not a host name")
    return host


def _page_url(host: str, page: int) -> str:
    return (f"https://{host}/api/jobs?limit={PAGE_SIZE}&page={page}"
            f"&sortBy=posted_date&descending=true")


def _clean(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _place(entry: dict) -> str:
    """"Santa Clara, California, United States" from a location's own fields."""
    parts: list[str] = []
    for key in ("city", "state", "country"):
        part = _clean(entry.get(key))
        if part and part not in parts:
            parts.append(part)
    return ", ".join(parts)


def _places(data: dict) -> str:
    """Every place the posting is open in, " | "-joined: the primary location,
    then the additional ones."""
    places: list[str] = []
    entries = [data] + [e for e in (data.get("additional_locations") or []) if isinstance(e, dict)]
    for entry in entries:
        place = _place(entry)
        if place and place not in places:
            places.append(place)
    return " | ".join(places)


def _posted(value) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(_clean(value), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


def _department(data: dict) -> str:
    named = _clean(data.get("department"))
    if named:
        return named
    categories = data.get("categories")
    first = categories[0] if isinstance(categories, list) and categories else None
    return _clean(first.get("name")) if isinstance(first, dict) else ""


def _work_type(data: dict, location: str, title: str) -> str:
    """Remote or hybrid when the posting says so in its location, title or tags;
    the tags are the employer's own and mostly say something else."""
    tags: list[str] = []
    for key, value in data.items():
        if key.startswith("tags") and isinstance(value, list):
            tags += [tag.lower() for tag in value if isinstance(tag, str)]
    combined = " ".join([location.lower(), title.lower()] + tags)
    if "remote" in combined:
        return "Hybrid" if "hybrid" in combined else "Remote"
    return "Hybrid" if "hybrid" in combined else "On Site"


def _to_job(item, host: str, slug: str, company_name: str) -> ATSJob | None:
    data = item.get("data") if isinstance(item, dict) else None
    if not isinstance(data, dict):
        return None
    posting_id = _clean(str(data.get("slug") or data.get("req_id") or ""))
    title = _clean(data.get("title"))
    if not posting_id or not title:
        return None
    location = _places(data)
    return ATSJob(
        title=title,
        company=company_name,
        location=location,
        url=f"https://{host}/jobs/{quote(posting_id, safe='')}",
        posted_date=_posted(data.get("posted_date")),
        department=_department(data),
        work_type=_work_type(data, location, title),
        description=clean_html(data.get("description") or "")[:DESCRIPTION_MAX_CHARS],
        external_id=f"{PLATFORM}:{slug}:{posting_id}",
        employment_type=_COMMITMENT_WORDS.get(_clean(data.get("employment_type")).upper(), ""),
        apply_url=_clean(data.get("apply_url")),
    )


def _crawl_delay(slug: str, host: str) -> float:
    """The seconds between requests: ours, or the site's Crawl-delay when longer.
    ``http.robots_allowed`` has just parsed the site's robots.txt and kept it;
    if that ever stops being so, the five seconds still stand. A delay no crawl
    can wait out raises, so ``source_health`` records why the board is skipped."""
    rules = getattr(http, "_robots", {}).get(f"https://{host}")
    try:
        delay = float(rules.crawl_delay(http.USER_AGENT) or 0) if rules is not None else 0.0
    except (TypeError, ValueError):
        delay = 0.0
    if delay > MAX_CRAWL_DELAY:
        raise RuntimeError(f"jibe/{slug}: {host} asks for a crawl delay of {delay:g} s, more than the "
                           f"{MAX_CRAWL_DELAY:g} s a crawl can wait between requests")
    return max(RATE_LIMIT, delay)


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


async def list_jobs(client: httpx.AsyncClient, slug: str, company_name: str, *,
                    config: dict, deadline: float | None = None) -> tuple[list[ATSJob], bool, int]:
    """Every posting on one Jibe career site, newest first, unfiltered, with its
    description.

    Returns (listings, complete, totalCount). ``complete`` is True only with
    positive evidence that the whole list was read, all of:
    - every page reported ``totalCount`` as a whole number, the same one on every
      page (a posting removed between two page requests changes it, and shifts
      the list up by one so a live posting is skipped), and it is above zero;
    - the crawl ran to the end (the count reached, or an empty page), not to the
      deadline or the page cap, and no page repeated postings already seen (a
      server that ignores ``page``);
    - every posting was readable;
    - exactly as many distinct postings were listed as the site counts.
    A missing, null, zero or non-numeric count, and an empty site, read as
    partial, never as a takedown."""
    host = _host(slug, config)
    if not await http.robots_allowed(client, _page_url(host, 1)):
        raise PermissionError(f"jibe/{slug}: {host} robots.txt does not allow TailrdBot to read its jobs")
    pause = _crawl_delay(slug, host)
    jobs: list[ATSJob] = []
    seen: set[str] = set()
    counts: list[int | None] = []
    unreadable = 0
    fetched = 0
    ended = repeated = False
    for page in range(1, MAX_PAGES + 1):
        if page > HEAD_PAGES and deadline is not None and time.monotonic() >= deadline:
            break
        response = await http.request(client, "GET", _page_url(host, page), rate_limit=pause,
                                      headers={"Accept": "application/json"})
        response.raise_for_status()
        data = response.json()
        postings = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(postings, list):
            raise ValueError(f"jibe/{slug}: {host} answered the jobs API without a jobs list")
        count = _count(data.get("totalCount"))
        counts.append(count)
        fresh = 0
        for item in postings:
            job = _to_job(item, host, slug, company_name)
            if job is None:
                unreadable += 1
            elif job.url not in seen:  # a posting added mid-crawl shifts one onto the next page
                seen.add(job.url)
                jobs.append(job)
                fresh += 1
        fetched += len(postings)
        if not postings:
            ended = True
            break
        if not fresh:
            repeated = True  # the same postings again: the server is not honouring page
            break
        if count and fetched >= count:  # a count of 0 beside postings is a contradiction, not the end
            ended = True
            break
    known = [count for count in counts if count is not None]
    total = max(known, default=0)
    steady = bool(counts) and len(known) == len(counts) and len(set(known)) == 1
    complete = (ended and not repeated and not unreadable and steady and total > 0
                and len(seen) == total)
    return jobs, complete, total
