"""
Teamtailor adapter (scraper v2, Tier 1).

Every Teamtailor career site publishes its open jobs as an RSS feed, no key
needed, descriptions included:

    GET https://<host>/jobs.rss

``<host>`` is where the tenant's career site lives, and that varies: the
usual <name>.teamtailor.com, the North American data centre's
<name>.na.teamtailor.com (owlco.na.teamtailor.com answers, owlco.teamtailor.com
is a 404), or the employer's own domain (careers.usg.com serves the same
feed). The registry entry names the tenant in ``board_slug`` and, when the
host is not <slug>.teamtailor.com, gives it in ``config``: {"host": "..."}.

The feed has no state or province (a place is a city and a country), no
commitment type, and the <link> of each item is the posting's public URL on
whatever domain the tenant uses. A job open in several places lists them all
in the one item.
"""

from __future__ import annotations

import datetime
import email.utils
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob, _other_na_locations, _text
from backend.services.description_extractor import clean_html

logger = logging.getLogger(__name__)

PLATFORM = "teamtailor"
TIER = 1
COUNTRIES = ("CA", "US")
RATE_LIMIT = 1.0
REQUIRED_ENV: tuple[str, ...] = ()
HOST_PATTERNS = (r"^https?://(?:[\w-]+\.)+teamtailor\.com/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?jobs/\d+",)

_NS = {"tt": "https://teamtailor.com/locations"}
_HOSTNAME = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_SUBDOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_JOB_ID = re.compile(r"/jobs/(\d+)")
# No feed needs these, and an entity declaration is how a small document is
# made to expand into a huge one.
_DECLARATIONS = re.compile(rb"<!(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_WORK_TYPES = {"fully": "Remote", "hybrid": "Hybrid"}


async def list_jobs(client, slug, company_name, *, config, deadline=None):
    """Every job in the tenant's feed, unfiltered, descriptions included.
    Raises when the feed is missing or is not an RSS document, so a broken
    board is never read as an empty one."""
    host = _feed_host(slug, config)
    response = await http.request(
        client, "GET", f"https://{host}/jobs.rss", rate_limit=RATE_LIMIT,
        headers={"Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.1"},
        follow_redirects=True,
    )
    response.raise_for_status()
    items = _items(response.content, host)
    jobs = []
    for item in items:
        job = _parse(item, slug, company_name)
        if job is not None:
            jobs.append(job)
    # An item that could not be read is missing from the URL set, so the
    # board must not count as complete: its stored row would be marked removed.
    return jobs, len(jobs) == len(items), len(items)


def _feed_host(slug: str, config) -> str:
    """The host serving the tenant's feed: the registry's ``host`` when it
    names one, else <slug>.teamtailor.com. Either becomes part of a URL, so
    anything that is not a plain hostname is refused."""
    named = _text((config or {}).get("host"))
    if named:
        if not _HOSTNAME.match(named):
            raise ValueError(f"not a Teamtailor host: {named!r}")
        return named
    if not _SUBDOMAIN.match(slug or ""):
        raise ValueError(f"not a Teamtailor subdomain: {slug!r}")
    return f"{slug}.teamtailor.com"


def _items(content: bytes, host: str) -> list[ET.Element]:
    if _DECLARATIONS.search(content):
        raise ValueError(f"Teamtailor feed {host}: unexpected XML declarations")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f"Teamtailor feed {host}: not an RSS document ({exc})") from exc
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise ValueError(f"Teamtailor feed {host}: no RSS channel")
    return channel.findall("item")


def _parse(item: ET.Element, slug: str, company_name: str) -> ATSJob | None:
    title = _text(item.findtext("title"))
    url = _posting_url(_text(item.findtext("link")))
    match = _JOB_ID.search(url)
    job_id = match.group(1) if match else _text(item.findtext("guid"))
    if not (title and url and job_id):
        return None
    status = _text(item.findtext("remoteStatus")).lower()
    places = _places(item, status)
    return ATSJob(
        title=title,
        company=company_name,
        location="; ".join(places),
        url=url,
        posted_date=_published(item.findtext("pubDate")),
        department=_text(item.findtext("tt:department", namespaces=_NS)),
        work_type=_WORK_TYPES.get(status, "On Site"),
        description=clean_html(item.findtext("description") or ""),
        external_id=f"{PLATFORM}:{slug}:{job_id}",
        # The North American places among the job's other ones, so a primary
        # place abroad does not hide a role that is also open in Toronto.
        location_hint=_other_na_locations(places[0], places[1:]) if places else "",
    )


def _posting_url(link: str) -> str:
    """The job page without tracking parameters."""
    parts = urlsplit(link)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _places(item: ET.Element, remote_status: str) -> list[str]:
    """The job's places as "City, Country", primary first; a fully remote job
    with none is just "Remote"."""
    places = []
    for place in item.findall("tt:locations/tt:location", _NS):
        city = _text(place.findtext("tt:city", namespaces=_NS)) \
            or _text(place.findtext("tt:name", namespaces=_NS))
        country = _text(place.findtext("tt:country", namespaces=_NS))
        places.append(", ".join(part for part in (city, country) if part))
    places = list(dict.fromkeys(place for place in places if place))
    if not places and remote_status == "fully":
        return ["Remote"]
    return places


def _published(value) -> datetime.datetime | None:
    """RFC 822 ("Tue, 13 Oct 2026 11:06:34 -0700") as an aware UTC datetime."""
    try:
        when = email.utils.parsedate_to_datetime(_text(value))
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(datetime.timezone.utc)
