"""
Recruitee adapter (scraper v2, Tier 1).

Every Recruitee careers site publishes its offers as JSON, descriptions
included, in one response without paging:

    GET https://<subdomain>.recruitee.com/api/offers/

An offer's page is https://<subdomain>.recruitee.com/o/<slug> and one offer is
https://<subdomain>.recruitee.com/api/offers/<slug>. The slug follows the
title, so an edited title gets a new slug while the numeric id stays; the id
is what identifies the posting here, the page URL is what is shown.

The response is large (about 30 KB an offer, mostly the application form's
questions and translated copies of the text), which is the cost of having
the descriptions in the list.
"""

from __future__ import annotations

import datetime
import logging
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

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

PLATFORM = "recruitee"
TIER = 1
COUNTRIES = ("CA", "US")
RATE_LIMIT = 1.0
REQUIRED_ENV: tuple[str, ...] = ()
HOST_PATTERNS = (r"^https?://[\w-]+\.recruitee\.com/o/[^/?#]+",)

_SUBDOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_OFFER_URL = re.compile(
    r"^https?://(?P<host>[\w-]+\.recruitee\.com)/o/(?P<slug>[^/?#]+)", re.IGNORECASE,
)
_JSON = {"Accept": "application/json"}


async def list_jobs(client, slug, company_name, *, config, deadline=None):
    """Every published offer of the site, unfiltered, descriptions included.
    Raises when the site is missing or does not answer with an offers list,
    so a broken board is never read as an empty one."""
    if not _SUBDOMAIN.match(slug or ""):
        raise ValueError(f"not a Recruitee subdomain: {slug!r}")
    host = f"{slug}.recruitee.com"
    response = await http.request(client, "GET", f"https://{host}/api/offers/",
                                  rate_limit=RATE_LIMIT, headers=_JSON, follow_redirects=True)
    response.raise_for_status()
    data = response.json()
    offers = data.get("offers") if isinstance(data, dict) else None
    if not isinstance(offers, list):
        raise ValueError(f"Recruitee site {host}: the response has no offers list")
    jobs, unreadable = [], 0
    for offer in offers:
        if isinstance(offer, dict) and _text(offer.get("status")) not in ("", "published"):
            continue  # a draft or archived offer is not a public posting
        job = _parse(offer, slug, host, company_name)
        if job is None:
            unreadable += 1
        else:
            jobs.append(job)
    # An offer that could not be read is missing from the URL set, so the
    # board must not count as complete: its stored row would be marked removed.
    return jobs, unreadable == 0, len(jobs) + unreadable


def _parse(offer, slug: str, host: str, company_name: str) -> ATSJob | None:
    if not isinstance(offer, dict):
        return None
    raw_id = offer.get("id")
    offer_id = str(raw_id).strip() if raw_id is not None else ""
    title = _text(offer.get("title"))
    if not offer_id or not title:
        return None
    places = _places(offer)
    location = "; ".join(places)
    return ATSJob(
        title=title,
        company=company_name,
        location=location,
        url=_offer_url(offer, host),
        posted_date=_when(offer.get("published_at") or offer.get("created_at")),
        department=_text(offer.get("department")),
        work_type=_work_type(offer, location, title),
        # The two halves of the posting the careers page shows.
        description=clean_html(f"{offer.get('description') or ''}\n{offer.get('requirements') or ''}"),
        external_id=f"{PLATFORM}:{slug}:{offer_id}",
        employment_type=_employment_type(offer.get("employment_type_code")),
        salary_text=_salary_text(offer.get("salary")),
        # The North American places among the offer's other ones, so a
        # primary place abroad does not hide a role that is also open in Toronto.
        location_hint=_other_na_locations(places[0], places[1:]) if places else "",
    )


def _offer_url(offer: dict, host: str) -> str:
    """The offer page without tracking parameters."""
    url = _text(offer.get("careers_url"))
    if url:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return f"https://{host}/o/{_text(offer.get('slug'))}"


def _places(offer: dict) -> list[str]:
    """The offer's places as "City, Region, Country", primary first. A remote
    offer carries the places it hires in; its own ``location`` text is only
    "Remote job"."""
    entries = [place for place in offer.get("locations") or [] if isinstance(place, dict)]
    places = [_place(_text(place.get("city")) or _text(place.get("name")), _text(place.get("state")),
                      _text(place.get("country"))) for place in entries]
    if not any(places):
        places = [_place(_text(offer.get("city")), _text(offer.get("state_name")),
                         _text(offer.get("country")))]
    places = list(dict.fromkeys(place for place in places if place))
    if not places and offer.get("remote"):
        return ["Remote"]
    return places


def _place(city: str, region: str, country: str) -> str:
    return ", ".join(part for part in (city, region, country) if part)


def _work_type(offer: dict, location: str, title: str) -> str:
    if offer.get("remote"):
        return "Remote"
    if offer.get("hybrid") or "hybrid" in f"{location} {title}".lower():
        return "Hybrid"
    return "On Site"


def _employment_type(code) -> str:
    """Recruitee's code ("fulltime_permanent", "parttime_temporary",
    "internship") as the words the rest of ingest reads."""
    code = _text(code).lower()
    if "intern" in code:
        return "Internship"
    if "part" in code:
        return "Part-time"
    if any(word in code for word in ("freelance", "contract", "temporary", "fixed")):
        return "Contract"
    if "full" in code:
        return "Full-time"
    return code.replace("_", " ").title()


def _salary_text(salary) -> str:
    """"20-22 CAD per hour" or "CAD 20 per hour", the shapes parse_salary reads."""
    if not isinstance(salary, dict):
        return ""
    low, high = (_amount(salary.get("min")), _amount(salary.get("max")))
    currency = _text(salary.get("currency")).upper()
    if low and high and low != high:
        text = f"{low}-{high} {currency}".strip()
    elif low or high:
        text = f"{currency} {low or high}".strip()
    else:
        return ""
    period = _text(salary.get("period")).lower()
    return f"{text} per {period}" if period else text


def _amount(value) -> str:
    text = _text(str(value)) if value is not None else ""
    return "" if text in ("", "0") else text


def _when(value) -> datetime.datetime | None:
    """"2026-10-16 19:29:54 UTC" as an aware UTC datetime."""
    try:
        when = datetime.datetime.fromisoformat(_text(value).removesuffix(" UTC").strip())
    except ValueError:
        return None
    if when.tzinfo is None:
        return when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc)


async def check_alive(client, url: str) -> LivenessResult:
    """Ask the site's offers API about one offer. A 404 there is death only
    when the offer page is gone too: an edited title gets a new slug and the
    old one redirects to it, so a slug that bounces is left undecided."""
    match = _OFFER_URL.match(url or "")
    if not match:
        return LivenessResult(UNKNOWN, "unsupported_url")
    api = f"https://{match['host']}/api/offers/{quote(unquote(match['slug']), safe='')}"
    try:
        response = await http.request(client, "GET", api, rate_limit=RATE_LIMIT, headers=_JSON,
                                      follow_redirects=True)
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                data = None
            offer = data.get("offer") if isinstance(data, dict) else None
            if isinstance(offer, dict) and offer.get("id") \
                    and _text(offer.get("status")) in ("", "published"):
                return LivenessResult(ALIVE, "recruitee_api_200", True)
            return LivenessResult(UNKNOWN, "recruitee_api_unreadable")
        if response.status_code not in (404, 410):
            return _unknown_for_status(response.status_code)
        # The one HTML page this adapter reads, so robots.txt gets a say.
        if not await http.robots_allowed(client, url):
            return LivenessResult(UNKNOWN, "robots_disallowed")
        page = await http.request(client, "GET", url, rate_limit=RATE_LIMIT,
                                  headers={"Accept": "text/html"}, follow_redirects=True)
    except httpx.HTTPStatusError as exc:
        return _unknown_for_status(exc.response.status_code)
    if page.status_code in (404, 410):
        return LivenessResult(DEAD, "recruitee_offer_404", True)
    if page.history:
        return LivenessResult(UNKNOWN, "recruitee_redirected")
    return _unknown_for_status(page.status_code)
