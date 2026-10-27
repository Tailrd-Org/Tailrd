"""
Job Bank Canada (jobbank.gc.ca): the federal job board, which carries federal,
provincial and municipal government jobs next to employers of every size and
postings that partner boards (Jobillico and others) feed into it.

What was checked on 2026-10-25, and what that decided:

- Official open data first. open.canada.ca publishes "Job Postings Advertised
  on Canada's National Job Bank Website", but only as monthly CSV files (the
  previous month's postings, tab separated, UTF-16) that carry neither an
  employer name, a posting URL nor a description. They cannot say what is open
  today, so they are no source for a live feed. The site's own Atom feed
  (/jobsearch/feed/jobSearchRSSfeed) stops at the newest 100 postings and has
  no paging, so it cannot list a whole board either.
- The public search result pages can: GET /jobsearch/jobsearch?sort=D&page=N
  plus the site's own filters (fprov, fet, fsrc, fjsf, fsct ...) lists 25
  postings a page, newest first, in plain server-rendered HTML. Paging by the
  ``page`` parameter needs no session. A page past the end is empty.
- robots.txt allows every path but asks for ``Crawl-delay: 5``, so RATE_LIMIT
  is 5 seconds and everything below goes through ingest.core.http at that pace.
- The posting pages describe the job with schema.org JobPosting markup as RDFa
  (``property="title"``, ``property="description"`` ...), not as JSON-LD. A
  posting somebody removed answers HTTP 410.

A "board" is one search: the registry slug names it and ``config`` holds the
filters and the page cap, e.g. {"params": {"fet": "3"}, "max_pages": 12} for
the federal government. A search with more results than ``max_pages`` pages is
returned as an incomplete listing (the newest postings first), so it never
counts as proof that anything was taken down.

A search listing carries the title, employer, place, date and pay. The
description and the terms of employment live on the posting page, which
``fetch_job`` reads for the new postings the pipeline picks.
"""

from __future__ import annotations

import datetime
import html
import logging
import re
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob

logger = logging.getLogger(__name__)

PLATFORM = "jobbank_ca"
TIER = 2
COUNTRIES = ("CA",)
# robots.txt: "Crawl-delay: 5".
RATE_LIMIT = 5.0
REQUIRED_ENV: tuple[str, ...] = ()
HOST_PATTERNS = (r"^https?://(?:www\.)?jobbank\.gc\.ca/jobsearch/jobposting/",)

BASE_URL = "https://www.jobbank.gc.ca"
SEARCH_URL = f"{BASE_URL}/jobsearch/jobsearch"
POSTING_URL = f"{BASE_URL}/jobsearch/jobposting/{{posting_id}}"

# The site shows 25 postings a page and offers no other size.
PAGE_SIZE = 25
# A few hundred postings a board unless the registry asks for more: Job Bank
# lists about 70,000, and every page costs five seconds.
DEFAULT_MAX_PAGES = 12
# Whatever the config says, one board stays under this many pages.
MAX_PAGES_CEILING = 200
DESCRIPTION_LIMIT = 10000

_ARTICLE_RE = re.compile(r'<article id="article-(\d+)"(.*?)</article>', re.S)
_POSTING_ID_RE = re.compile(r"/jobsearch/jobposting/(\d+)")
_COUNT_RE = re.compile(r'id="results-count">\s*([\d,.\s  ]*)\s*<')
_TAG_RE = re.compile(r"<[^>]+>")
_HIDDEN_LABEL_RE = re.compile(r'<span class="wb-inv">.*?</span>', re.S)
_PROVINCE_RE = re.compile(r"\s*\(([A-Z]{2})\)\s*$")
_DATE_RE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})")
_PAY_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)(?:\s*(?:to|-)\s*\$?\s*([\d,]+(?:\.\d+)?))?\s*(hourly|weekly|bi-weekly|biweekly|monthly|annually|yearly|daily)?",
    re.I,
)
_PAY_PERIOD = {"annually": "yearly"}
# The salary parser reads these three periods correctly. It takes "$2,123 to $2,348 biweekly" for a
# monthly figure (and weekly or daily pay the same way), so those are left out rather than stored wrong.
_PAY_PERIODS_READ = {"hourly", "yearly", "monthly"}
_MONTHS = {name: number for number, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"), start=1)}
_SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "via"}
_WORK_TYPES = {"on site": "On Site", "hybrid": "Hybrid", "remote": "Remote"}


# --- Parsing ------------------------------------------------------------------------------------

def _plain(fragment: str) -> str:
    """The text of an HTML fragment: tags dropped, entities decoded, runs of
    whitespace collapsed."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", fragment or "")).split())


def _field(block: str, css_class: str, tag: str = "li") -> str:
    match = re.search(rf'<{tag} class="{css_class}">(.*?)</{tag}>', block, re.S)
    return _plain(_HIDDEN_LABEL_RE.sub(" ", match.group(1))) if match else ""


def _title_case(title: str) -> str:
    """Job Bank files every title in lower case ("sales consultant - retail"),
    which looks broken in a feed. A title that already has capitals was cased by
    its employer and stays as it is."""
    if not title or title != title.lower():
        return title
    words = title.split(" ")
    return " ".join(
        word if (index and word in _SMALL_WORDS)
        else re.sub(r"(^|[-/(])([a-zà-ÿ])", lambda m: m.group(1) + m.group(2).upper(), word)
        for index, word in enumerate(words)
    )


def _location(text: str) -> str:
    """"Saint-Georges (QC)" -> "Saint-Georges, QC", the form the rest of the feed uses."""
    return _PROVINCE_RE.sub(lambda m: f", {m.group(1)}", text).strip()


def _posted(text: str) -> Optional[datetime.datetime]:
    """"October 25, 2026" -> that day at midnight UTC; None when unreadable."""
    match = _DATE_RE.search(text or "")
    month = _MONTHS.get(match.group(1).lower()) if match else None
    if not match or not month:
        return None
    try:
        return datetime.datetime(int(match.group(3)), month, int(match.group(2)),
                                 tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _amount(text: str) -> str:
    value = float(text.replace(",", ""))
    return str(int(value)) if value == int(value) else f"{value:g}"


def _salary_text(text: str) -> str:
    """The listing's pay in a shape the salary parser reads with the right
    currency: "27.61-30.44 CAD hourly" for a range, "CAD 21 hourly" for one rate
    (the listing says "$27.61 to $30.44 hourly" and never names the currency).
    Pay that is not an amount per period, such as a commission, stays as the
    site wrote it; weekly, biweekly and daily pay is left out (see
    _PAY_PERIODS_READ)."""
    text = re.sub(r"^Salary\s*", "", text or "").strip()
    match = _PAY_RE.search(text)
    if not match or not match.group(3):
        return text
    low, high = _amount(match.group(1)), _amount(match.group(2) or match.group(1))
    period = match.group(3).lower()
    period = _PAY_PERIOD.get(period, period)
    if period not in _PAY_PERIODS_READ:
        return ""
    return f"CAD {low} {period}" if low == high else f"{low}-{high} CAD {period}"


class _Listing:
    """One search-result item, as the page states it."""

    __slots__ = ("posting_id", "title", "employer", "location", "posted", "salary", "work_type")

    def __init__(self, posting_id: str, block: str):
        self.posting_id = posting_id
        self.title = _field(block, "noctitle", "span")
        self.employer = _field(block, "business")
        self.location = _location(_field(block, "location"))
        self.posted = _posted(_field(block, "date"))
        self.salary = _salary_text(_field(block, "salary"))
        flag = _field(block, "telework", "span")
        self.work_type = _WORK_TYPES.get(flag.lower())


def parse_search_page(page: str) -> tuple[int, list[_Listing]]:
    """The reported result count and the items of one search page. Raises
    ValueError for a page that is not a Job Bank search result (a bot wall, an
    error page, a changed layout): an empty answer must never be mistaken for a
    search that genuinely found nothing."""
    count = _COUNT_RE.search(page)
    if not count:
        raise ValueError("not a Job Bank search page: no result count")
    digits = re.sub(r"\D", "", count.group(1))
    if not digits:
        # A search that found nothing prints 0. A blank count is a broken page, and
        # without a count the listing could not be checked for completeness.
        raise ValueError("not a Job Bank search page: empty result count")
    items = []
    for match in _ARTICLE_RE.finditer(page):
        link = _POSTING_ID_RE.search(match.group(2))
        items.append(_Listing(link.group(1) if link else match.group(1), match.group(2)))
    if items and not any(item.title for item in items):
        raise ValueError("not a Job Bank search page: no item has a title")
    return int(digits), items


def _canonical_url(posting_id: str) -> str:
    return POSTING_URL.format(posting_id=posting_id)


def _to_job(item: _Listing, slug: str, company_name: str) -> ATSJob:
    return ATSJob(
        title=_title_case(item.title),
        company=item.employer or company_name,
        location=item.location,
        url=_canonical_url(item.posting_id),
        posted_date=item.posted,
        work_type=item.work_type,
        external_id=f"{PLATFORM}:{slug}:{item.posting_id}",
        salary_text=item.salary,
        detail_ref=item.posting_id,
    )


def _max_pages(config: dict) -> int:
    try:
        wanted = int(config.get("max_pages") or DEFAULT_MAX_PAGES)
    except (TypeError, ValueError):
        wanted = DEFAULT_MAX_PAGES
    return max(1, min(wanted, MAX_PAGES_CEILING))


async def _allowed(client: httpx.AsyncClient, url: str) -> bool:
    return await http.robots_allowed(client, url)


# --- The contract -------------------------------------------------------------------------------

async def list_jobs(client, slug, company_name, *, config, deadline=None):
    """List one search, newest postings first, at most ``max_pages`` pages.

    Returns (jobs, complete, total): ``total`` is the result count the site
    reports. The listing is complete only when the pages ran out before the cap
    or the deadline and the postings seen cover that (positive) count; a search
    that found nothing is never called complete, because a site hiccup that serves
    an empty page would otherwise read as every posting being taken down. A page that
    fails after the first keeps what was listed so far, as an incomplete listing."""
    config = config if isinstance(config, dict) else {}
    params = {key: value for key, value in (config.get("params") or {}).items()
              if key not in ("page", "sort")}
    if not await _allowed(client, SEARCH_URL):
        raise RuntimeError("robots.txt does not allow the Job Bank search pages")

    jobs: list[ATSJob] = []
    seen: set[str] = set()
    total = 0
    first_total = 0
    pages_ran_out = False
    for page_number in range(1, _max_pages(config) + 1):
        if page_number > 1 and deadline is not None and time.monotonic() >= deadline:
            break
        try:
            response = await http.request(
                client, "GET", SEARCH_URL, rate_limit=RATE_LIMIT,
                params={**params, "sort": "D", "page": page_number},
            )
            response.raise_for_status()
            page_total, items = parse_search_page(response.text)
        except (httpx.HTTPError, ValueError):
            if page_number == 1:
                raise
            logger.warning("jobbank_ca %s: page %d failed; keeping the %d postings listed",
                           slug, page_number, len(jobs), exc_info=True)
            break
        if page_number == 1:
            first_total = page_total
        total = max(total, page_total)
        added = 0
        for item in items:
            if item.posting_id in seen:
                continue  # the list shifted under us between pages
            if not item.title:
                continue  # unusable; left out of ``seen`` so the listing stays short of the count
            seen.add(item.posting_id)
            jobs.append(_to_job(item, slug, company_name))
            added += 1
        if len(items) < PAGE_SIZE or not added:
            pages_ran_out = True
            break

    complete = pages_ran_out and bool(jobs) and first_total > 0 and len(seen) >= first_total
    return jobs, complete, max(total, len(seen))


async def fetch_job(client, slug, ref, *, config) -> dict:
    """The description and the terms of employment of one posting (``ref`` is
    the id in its URL), plus ``apply_url``: the employer's own page when the
    posting links out to it (federal and partner-board postings do; those
    posted directly on Job Bank are applied for there, and come back with an
    empty one). Partner postings (Jobillico and others) carry no description on
    Job Bank, and some federal ones no terms of employment; those fields come
    back empty. {} when the posting is gone."""
    url = _canonical_url(ref)
    if not await _allowed(client, url):
        return {}
    response = await http.request(client, "GET", url, rate_limit=RATE_LIMIT)
    if response.status_code in (404, 410):
        return {}
    response.raise_for_status()
    page = response.text
    return {
        "description": _property_text(page, "description")[:DESCRIPTION_LIMIT],
        "employment_type": _employment_type(page),
        "apply_url": _external_link(page),
    }


async def check_alive(client, url):
    """Whether a posting is still open, from its own page: HTTP 410 (or 404)
    is a removed posting, a page past its "Advertised until" date is closed,
    and a readable posting page within its dates is open."""
    from backend.services.platform_liveness import ALIVE, DEAD, UNKNOWN, LivenessResult

    match = _POSTING_ID_RE.search(urlparse(url).path)
    if not match:
        return LivenessResult(UNKNOWN, "jobbank_unrecognised_url")
    page_url = _canonical_url(match.group(1))
    if not await _allowed(client, page_url):
        return LivenessResult(UNKNOWN, "jobbank_robots_disallowed")
    try:
        response = await http.request(client, "GET", page_url, rate_limit=RATE_LIMIT)
    except httpx.HTTPError:
        return LivenessResult(UNKNOWN, "jobbank_unreachable")
    if response.status_code in (404, 410):
        return LivenessResult(DEAD, f"jobbank_http_{response.status_code}", True)
    if response.status_code != 200:
        return LivenessResult(UNKNOWN, f"http_{response.status_code}")
    page = response.text
    if 'property="title"' not in page or 'property="datePosted"' not in page:
        return LivenessResult(UNKNOWN, "jobbank_unreadable")
    closes = _valid_through(page)
    if closes is not None and closes < _today():
        return LivenessResult(DEAD, "jobbank_expired", True)
    return LivenessResult(ALIVE, "jobbank_posting_200", True)


# --- Posting pages ------------------------------------------------------------------------------

def _property_inner(page: str, name: str) -> str:
    """The inner HTML of the first <span> carrying the RDFa property ``name``,
    up to its own closing tag (spans nest inside it), or "" without one."""
    start = re.search(rf'<span\b[^>]*\bproperty="{name}"[^>]*>', page)
    if not start:
        return ""
    depth = 1
    for tag in re.finditer(r"<(/?)span\b[^>]*>", page[start.end():]):
        depth += -1 if tag.group(1) else 1
        if depth == 0:
            return page[start.end():start.end() + tag.start()]
    return ""


def _property_text(page: str, name: str) -> str:
    return _plain(_property_inner(page, name))


def _employment_type(page: str) -> str:
    """"Permanent employment, Full time": the lines of the terms-of-employment
    entry, joined."""
    parts = [_plain(piece) for piece in _TAG_RE.split(_property_inner(page, "employmentType"))]
    return ", ".join(part for part in parts if part)


def _external_link(page: str) -> str:
    """The "View the full job posting on ..." button's target, or ""."""
    button = re.search(r'<a\b[^>]*\bid="externalJobLink"[^>]*>', page)
    href = re.search(r'\bhref="([^"]+)"', button.group(0)) if button else None
    link = html.unescape(href.group(1)).strip() if href else ""
    return link if link.lower().startswith(("http://", "https://")) else ""


def _valid_through(page: str) -> Optional[datetime.date]:
    match = re.search(r'property="validThrough"[^>]*>\s*(\d{4})-(\d{2})-(\d{2})', page)
    if not match:
        return None
    try:
        return datetime.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()
