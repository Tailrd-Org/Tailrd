"""
BambooHR adapter (scraper v2, Tier 1).

A tenant's careers page is a single-page app over two public JSON endpoints:

    GET https://<tenant>.bamboohr.com/careers/list          every open posting
    GET https://<tenant>.bamboohr.com/careers/<id>/detail   one posting

The list carries no description and no date, only the title, department,
commitment and place, so ``list_jobs`` returns jobs without them and sets each
job's ``detail_ref`` to its id; ``fetch_job`` fills in the description (and
the commitment) from the detail endpoint, one request per posting, for the
postings the ingest decides are worth it. The posting page is
https://<tenant>.bamboohr.com/careers/<id>, and the detail endpoint answers
404 with ``{"type": "not_found"}`` once a posting is gone.

A tenant without a careers page does not answer 404: /careers/list redirects
to bamboohr.com's home page. That is a broken board, not an empty one, and is
raised as such.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

import httpx

from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob, _text
from backend.services.description_extractor import clean_html
from backend.services.platform_liveness import (
    ALIVE,
    DEAD,
    UNKNOWN,
    LivenessResult,
    _unknown_for_status,
)

logger = logging.getLogger(__name__)

PLATFORM = "bamboohr"
TIER = 1
COUNTRIES = ("CA", "US")
RATE_LIMIT = 1.0
REQUIRED_ENV: tuple[str, ...] = ()
HOST_PATTERNS = (r"^https?://[\w-]+\.bamboohr\.com/careers/\d+",)

_SUBDOMAIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")
_POSTING_URL = re.compile(
    r"^https?://(?P<host>[\w-]+\.bamboohr\.com)/careers/(?P<id>\d+)", re.IGNORECASE,
)
_JSON = {"Accept": "application/json"}
# locationType: how the role is worked.
_WORK_TYPES = {"1": "Remote", "2": "Hybrid"}


async def list_jobs(client, slug, company_name, *, config, deadline=None):
    """Every open posting of the tenant, unfiltered. Raises when the tenant
    has no careers list, so a broken board is never read as an empty one."""
    host = _tenant_host(slug)
    response = await http.request(client, "GET", f"https://{host}/careers/list",
                                  rate_limit=RATE_LIMIT, headers=_JSON, follow_redirects=True)
    response.raise_for_status()
    if "json" not in response.headers.get("content-type", "").lower():
        raise ValueError(f"BambooHR tenant {host}: /careers/list is not the careers list")
    data = response.json()
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, list):
        raise ValueError(f"BambooHR tenant {host}: the careers list has no result")
    jobs = []
    for item in result:
        job = _parse(item, slug, host, company_name)
        if job is not None:
            jobs.append(job)
    reported = (data.get("meta") or {}).get("totalCount")
    total = reported if isinstance(reported, int) else len(result)
    # Complete only when every posting the tenant counts was listed and read:
    # one that is missing from the URL set would have its stored row removed.
    return jobs, len(jobs) == len(result) and len(result) >= total, total


async def fetch_job(client, slug, ref, *, config):
    """The posting's description and commitment from its detail endpoint
    (``ref`` is the id list_jobs left in detail_ref); {} without a ref."""
    if not ref:
        return {}
    response = await http.request(
        client, "GET", f"https://{_tenant_host(slug)}/careers/{quote(str(ref), safe='')}/detail",
        rate_limit=RATE_LIMIT, headers=_JSON, follow_redirects=True,
    )
    response.raise_for_status()
    opening = _opening(response)
    if opening is None:
        raise ValueError(f"BambooHR tenant {slug}: posting {ref} has no detail")
    return {
        "description": clean_html(opening.get("description") or ""),
        "employment_type": _text(opening.get("employmentStatusLabel")),
    }


def _tenant_host(slug: str) -> str:
    if not _SUBDOMAIN.match(slug or ""):
        raise ValueError(f"not a BambooHR tenant: {slug!r}")
    return f"{slug}.bamboohr.com"


def _parse(item, slug: str, host: str, company_name: str) -> ATSJob | None:
    if not isinstance(item, dict):
        return None
    raw_id = item.get("id")
    job_id = str(raw_id).strip() if raw_id is not None else ""
    title = _text(item.get("jobOpeningName"))
    if not job_id or not title:
        return None
    if item.get("isRemote"):
        work_type = "Remote"
    else:
        work_type = _WORK_TYPES.get(_text(item.get("locationType")), "On Site")
    return ATSJob(
        title=title,
        company=company_name,
        location=_place(item, work_type),
        url=f"https://{host}/careers/{job_id}",
        department=_text(item.get("departmentLabel")),
        work_type=work_type,
        external_id=f"{PLATFORM}:{slug}:{job_id}",
        employment_type=_text(item.get("employmentStatusLabel")),
        detail_ref=job_id,
    )


def _place(item: dict, work_type: str) -> str:
    """"City, Region, Country" from the posting's location and its ATS
    location; a remote posting that names no place is just "Remote"."""
    located = item.get("location") if isinstance(item.get("location"), dict) else {}
    ats = item.get("atsLocation") if isinstance(item.get("atsLocation"), dict) else {}
    parts = (
        _text(located.get("city")) or _text(ats.get("city")),
        _text(located.get("state")) or _text(ats.get("state")) or _text(ats.get("province")),
        _text(ats.get("country")),
    )
    place = ", ".join(part for part in parts if part)
    return place or ("Remote" if work_type == "Remote" else "")


def _opening(response: httpx.Response) -> dict | None:
    """result.jobOpening of a detail response, None when it is not one."""
    try:
        data = response.json()
    except ValueError:
        return None
    result = data.get("result") if isinstance(data, dict) else None
    opening = result.get("jobOpening") if isinstance(result, dict) else None
    return opening if isinstance(opening, dict) else None


async def check_alive(client, url: str) -> LivenessResult:
    """Ask the tenant's detail endpoint about one posting. The careers page
    is a single-page app whose closed postings bounce to the list; the detail
    endpoint answers the posting while it is open and a JSON 404 once it is
    gone."""
    match = _POSTING_URL.match(url or "")
    if not match:
        return LivenessResult(UNKNOWN, "unsupported_url")
    api = f"https://{match['host']}/careers/{match['id']}/detail"
    try:
        response = await http.request(client, "GET", api, rate_limit=RATE_LIMIT, headers=_JSON,
                                      follow_redirects=True)
    except httpx.HTTPStatusError as exc:
        return _unknown_for_status(exc.response.status_code)
    if response.status_code == 404:
        try:
            gone = response.json().get("type") == "not_found"
        except (ValueError, AttributeError):
            gone = False
        return LivenessResult(DEAD, "bamboohr_not_found", True) if gone \
            else _unknown_for_status(404)
    if response.status_code != 200:
        return _unknown_for_status(response.status_code)
    opening = _opening(response)
    if opening is None:
        return LivenessResult(UNKNOWN, "bamboohr_api_unreadable")
    status = _text(opening.get("jobOpeningStatus"))
    if status in ("", "Open"):
        return LivenessResult(ALIVE, "bamboohr_api_200", True)
    return LivenessResult(UNKNOWN, f"bamboohr_status_{status.lower()}")
