"""
Workable adapter (scraper v2, Tier 1).

Workable's public widget API lists a whole account in one request, and with
``details=true`` it carries every posting's description as well:

    GET https://apply.workable.com/api/v1/widget/accounts/<account>?details=true

So a board costs one request however many postings it has (a 1,400 posting
account is a 4 MB response, no paging). A posting lives at
https://apply.workable.com/<account>/j/<shortcode>/ (the widget's own link
drops the account and redirects), and one posting is answered by
https://apply.workable.com/api/v2/accounts/<account>/jobs/<shortcode>, which
is 404 once the posting is gone. An account with no postings answers 200 with
an empty list; an account that does not exist answers 404.

Every Workable account shares the one host, so a registry full of Workable
boards is a run of requests against it: the spacing is wider than the 1 s
minimum.
"""

from __future__ import annotations

import datetime
import logging
import re
from urllib.parse import quote

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob, _other_na_locations, _text
from backend.services.description_extractor import clean_html
from backend.services.platform_liveness import (
    ALIVE,
    DEAD,
    UNKNOWN,
    LivenessResult,
    _unknown_for_status,
)

logger = logging.getLogger(__name__)

PLATFORM = "workable"
TIER = 1
COUNTRIES = ("CA", "US")
RATE_LIMIT = 1.5
REQUIRED_ENV: tuple[str, ...] = ()
HOST_PATTERNS = (r"^https?://apply\.workable\.com/[^/?#]+/j/[A-Za-z0-9]+",)

_API = "https://apply.workable.com/api"
_POSTING_URL = re.compile(
    r"^https?://apply\.workable\.com/(?P<account>[^/?#]+)/j/(?P<code>[A-Za-z0-9]+)", re.IGNORECASE,
)
_JSON = {"Accept": "application/json"}


async def list_jobs(client, slug, company_name, *, config, deadline=None):
    """Every posting of the account, unfiltered, descriptions included.
    Raises when the account is missing or the payload is not a widget
    response, so a broken board is never read as an empty one."""
    response = await http.request(
        client, "GET", f"{_API}/v1/widget/accounts/{quote(slug, safe='')}",
        rate_limit=RATE_LIMIT, params={"details": "true"}, headers=_JSON, follow_redirects=True,
    )
    response.raise_for_status()
    data = response.json()
    raw = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError(f"Workable account {slug}: the widget payload has no jobs list")
    jobs = []
    for item in raw:
        job = _parse(item, slug, company_name)
        if job is not None:
            jobs.append(job)
    # A posting that could not be read is missing from the URL set, so the
    # board must not count as complete: its stored row would be marked removed.
    return jobs, len(jobs) == len(raw), len(raw)


def _parse(item, slug: str, company_name: str) -> ATSJob | None:
    if not isinstance(item, dict):
        return None
    shortcode = _text(item.get("shortcode"))
    title = _text(item.get("title"))
    if not shortcode or not title:
        return None
    places = _places(item)
    location = "; ".join(places)
    department = item.get("department")
    if isinstance(department, list):
        department = department[0] if department else ""
    return ATSJob(
        title=title,
        company=company_name,
        location=location,
        url=f"https://apply.workable.com/{quote(slug, safe='')}/j/{shortcode}/",
        posted_date=_published(item.get("published_on") or item.get("created_at")),
        department=_text(department),
        work_type=_work_type(item, location, title),
        description=clean_html(item.get("description") or ""),
        external_id=f"{PLATFORM}:{slug}:{shortcode}",
        employment_type=_text(item.get("employment_type")),
        # The North American places among the posting's other ones, so a
        # primary place abroad does not hide a role that is also open in Toronto.
        location_hint=_other_na_locations(places[0], places[1:]) if places else "",
    )


def _places(item: dict) -> list[str]:
    """The posting's places as "City, Region, Country", primary first. A
    place the employer hides on the public page still counts when it is the
    only one: it is where the role is."""
    entries = [place for place in item.get("locations") or [] if isinstance(place, dict)]
    shown = [place for place in entries if not place.get("hidden")]
    places = [_place(place) for place in shown or entries]
    if not any(places):
        places = [_place({"city": item.get("city"), "region": item.get("state"),
                          "country": item.get("country")})]
    return list(dict.fromkeys(place for place in places if place))


def _place(entry: dict) -> str:
    parts = (_text(entry.get("city")), _text(entry.get("region")), _text(entry.get("country")))
    return ", ".join(part for part in parts if part)


def _work_type(item: dict, location: str, title: str) -> str:
    """The widget only says whether the role is remote (``telecommuting``);
    "hybrid" appears in titles and places, as it does on the other boards."""
    text = f"{location} {title}".lower()
    if "hybrid" in text:
        return "Hybrid"
    if item.get("telecommuting") or "remote" in text:
        return "Remote"
    return "On Site"


def _published(value) -> datetime.datetime | None:
    """"2026-10-23" (the widget gives a date, no time) as that day in UTC."""
    try:
        when = datetime.datetime.fromisoformat(_text(value))
    except ValueError:
        return None
    if when.tzinfo is None:
        return when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc)


async def check_alive(client, url: str) -> LivenessResult:
    """Ask Workable's own API about one posting. The posting page is an SPA
    shell for live and gone shortcodes alike; the job endpoint answers 200
    with the posting while it is published and 404 once it is gone."""
    match = _POSTING_URL.match(url or "")
    if not match:
        return LivenessResult(UNKNOWN, "unsupported_url")
    api = f"{_API}/v2/accounts/{quote(match['account'], safe='')}/jobs/{match['code']}"
    try:
        response = await http.request(client, "GET", api, rate_limit=RATE_LIMIT, headers=_JSON,
                                      follow_redirects=True)
    except httpx.HTTPStatusError as exc:
        return _unknown_for_status(exc.response.status_code)
    # A timeout or a dropped connection says nothing about the posting.
    except httpx.TimeoutException:
        return LivenessResult(UNKNOWN, "timeout")
    except httpx.HTTPError:
        return LivenessResult(UNKNOWN, "network_error")
    if response.status_code == 404:
        return LivenessResult(DEAD, "workable_job_404", True)
    if response.status_code != 200:
        return _unknown_for_status(response.status_code)
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict) or not data.get("shortcode"):
        return LivenessResult(UNKNOWN, "workable_api_unreadable")
    state = _text(data.get("state"))
    if state in ("", "published"):
        return LivenessResult(ALIVE, "workable_api_200", True)
    return LivenessResult(UNKNOWN, f"workable_state_{state}")
