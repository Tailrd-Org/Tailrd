"""
AggregatorService, orchestrates scraping, classification, and storage of jobs
from jobright-ai GitHub repositories.

Pipeline: seed sources → check commit SHA → fetch README → parse markdown →
classify (country, work_type, role_category) → deduplicate by URL → store.
"""

import os
import re
import logging
import datetime
from typing import Optional
from urllib.parse import quote

import httpx
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend.db.models import GitHubSource, ScrapedJob
from backend.services.markdown_parser import (
    MarkdownParser,
    ParsedJob,
    clean_company_name,
    is_job_url,
    is_list_vendor_url,
)
from backend.services.country_filter import CountryFilter
from backend.services.work_type_classifier import WorkTypeClassifier
from backend.services.logo_cache import brand, load_branding, logo_quality
from backend.services.logo_resolver import domain_from_url, resolve_logo
from backend.services.location_parser import location_fields

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

# GitHub statuses meaning the repo itself is gone. Every other failure (5xx,
# 403/429 rate limits, timeouts) clears on a later poll, so the source stays
# in rotation instead of being parked in 'error' forever by one bad minute.
PERMANENT_HTTP_STATUSES = frozenset({404, 410, 451})

# How long a source parked in 'error' by a transient failure waits before
# cron-poll tries it again.
ERROR_RETRY_COOLDOWN = datetime.timedelta(hours=12)

# A re-parse that would retire more than this share of a source's visible rows
# as "gone from the README" is a format change, not a mass closure.
_VANISHED_GUARD_RATIO = 0.5
_VANISHED_GUARD_MIN = 10

# Revision of the parse + ingest rules, stored beside the commit SHA
# ('<sha>@r<N>'). A README is only re-parsed when its commit changes, so a
# parser fix never reached a list that had not committed since; bumping this
# re-parses every source once, on its next poll.
PARSE_REVISION = 1

# Poll cadence. cron-poll runs every few hours and polls the sources that are
# due, most overdue first: a list that committed this week is due every run,
# one that has been quiet for a week once a day. A list with nothing we can
# ingest (every row links to a list vendor's redirector, or the README has
# no job table) is parked: re-checked weekly in case it changes, with the
# slots the lists that feed the catalogue leave free.
ACTIVE_POLL_MINUTES = 60
DORMANT_POLL_MINUTES = 24 * 60
AUTO_POLL_MINUTES = (ACTIVE_POLL_MINUTES, DORMANT_POLL_MINUTES)  # any other value is an admin's
DORMANT_AFTER = datetime.timedelta(days=7)
PARKED_RECHECK = datetime.timedelta(days=7)
STATUS_PARKED = "parked"


# Internship wording in a title: whole words only, so 'Internal Tools',
# 'International Assignment' and 'OS Internals' stay new-grad roles.
# 'Stagiaire' is the Quebec postings' word ('Développeur Logiciels -
# Stagiaire - Backend' in speedyapply's INTERN_INTL.md).
_INTERNSHIP_TITLE_RE = re.compile(r"\bintern(?:ship)?s?\b|\bco-?ops?\b|\bstagiaires?\b",
                                  re.IGNORECASE)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.utcnow()


class RepoMovedError(Exception):
    """The repo was renamed to one another source already tracks."""


def is_retryable_error(message: str) -> bool:
    """True when a source parked in 'error' failed for a reason a later poll
    can clear. Includes the 301s recorded before redirects were followed:
    those repos were renamed, and the rename is now adopted on poll. An
    unexpected exception ('Error: ...', a non-JSON body, a parser crash) is
    retried too: an active source that hits one is polled again next run, so
    a retried one must not be parked for good by it."""
    match = re.match(r"HTTP (\d{3})\b", message or "")
    if match:
        return int(match.group(1)) not in PERMANENT_HTTP_STATUSES
    return (message or "").startswith(("Timeout", "Network", "Error:"))


def _commit_time(commit: dict) -> Optional[datetime.datetime]:
    """Naive-UTC committer time of a GitHub commits-API entry, or None."""
    raw = (((commit or {}).get("commit") or {}).get("committer") or {}).get("date")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return parsed


DEFAULT_FILE = "README.md"


def source_url(repo_url: str, file_path: str = DEFAULT_FILE) -> str:
    """A source's unique URL: the repo for its README, the file otherwise
    (speedyapply keeps its new-grad list in NEW_GRAD_USA.md and its
    international lists in *_INTL.md, beside the README another source
    reads)."""
    repo_url = repo_url.rstrip("/")
    if not file_path or file_path == DEFAULT_FILE:
        return repo_url
    return f"{repo_url}/blob/HEAD/{file_path}"


_REPO_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/#?]+)")


def _split_stamp(stored: Optional[str]) -> tuple[str, int]:
    """(commit sha, parse revision) of a stored last_commit_sha. A bare SHA
    (stored before revisions existed) is revision 0."""
    sha, sep, rev = (stored or "").partition("@r")
    return sha, int(rev) if sep and rev.isdigit() else 0


def _stamp(sha: str) -> str:
    return f"{sha}@r{PARSE_REVISION}"


def _listing_key(company: str, title: str) -> tuple[str, str]:
    """Company + title folded for matching a closed list row to a stored one
    (stored GitHub companies may still carry the old '**Name**' emphasis, and
    stored titles the legend marks the parser now strips: 'Engineer 🛂')."""
    return (clean_company_name(company).lower(),
            " ".join(clean_company_name(title).lower().split()))


class AggregatorService:
    """Orchestrates scraping, classification, and storage of jobs from GitHub sources."""

    REPOS: list[dict] = [
        # === Community repos with DIRECT company apply links ===
        # These lists rename every season (2026 -> 2027). "renamed_from" keeps
        # the former URLs: a source still on one is the same list (it adopts
        # the new name on its next poll), so seeding never creates a second
        # source for it. Add the new name here when a list renames.
        {
            "url": "https://github.com/vanshb03/Summer2027-Internships",
            "renamed_from": ["https://github.com/Ouckah/Summer2025-Internships"],
            "category": "Software Engineering",
            "level": "internship",
        },
        {
            "url": "https://github.com/vanshb03/New-Grad-2027",
            "category": "Software Engineering",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/speedyapply/2027-SWE-College-Jobs",
            "renamed_from": ["https://github.com/speedyapply/2026-SWE-College-Jobs"],
            "category": "Software Engineering",
            "level": "new_grad",
        },
        # speedyapply keeps each list in its own file; the README is the USA
        # internships. The international files are read for Canada only.
        {
            "url": "https://github.com/speedyapply/2027-SWE-College-Jobs",
            "renamed_from": ["https://github.com/speedyapply/2026-SWE-College-Jobs"],
            "file_path": "NEW_GRAD_USA.md",
            "category": "Software Engineering",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/speedyapply/2027-SWE-College-Jobs",
            "renamed_from": ["https://github.com/speedyapply/2026-SWE-College-Jobs"],
            "file_path": "INTERN_INTL.md",
            "countries": ["CA"],
            "category": "Software Engineering",
            "level": "internship",
        },
        {
            "url": "https://github.com/speedyapply/2027-SWE-College-Jobs",
            "renamed_from": ["https://github.com/speedyapply/2026-SWE-College-Jobs"],
            "file_path": "NEW_GRAD_INTL.md",
            "countries": ["CA"],
            "category": "Software Engineering",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/speedyapply/2027-AI-College-Jobs",
            "renamed_from": ["https://github.com/speedyapply/2026-AI-College-Jobs"],
            "category": "",
            "level": "internship",
        },
        {
            "url": "https://github.com/speedyapply/2027-AI-College-Jobs",
            "renamed_from": ["https://github.com/speedyapply/2026-AI-College-Jobs"],
            "file_path": "NEW_GRAD_USA.md",
            "category": "",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/speedyapply/2027-AI-College-Jobs",
            "renamed_from": ["https://github.com/speedyapply/2026-AI-College-Jobs"],
            "file_path": "INTERN_INTL.md",
            "countries": ["CA"],
            "category": "",
            "level": "internship",
        },
        {
            "url": "https://github.com/speedyapply/2027-AI-College-Jobs",
            "renamed_from": ["https://github.com/speedyapply/2026-AI-College-Jobs"],
            "file_path": "NEW_GRAD_INTL.md",
            "countries": ["CA"],
            "category": "",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/negarprh/Canadian-Tech-Internships-2027",
            "renamed_from": ["https://github.com/negarprh/Canadian-Tech-Internships-2026"],
            "category": "Software Engineering",
            "level": "internship",
        },
        {
            "url": "https://github.com/zapplyjobs/underclassmen-internships",
            "renamed_from": ["https://github.com/zapplyjobs/New-Grad-Jobs-2026"],
            "category": "",
            "level": "internship",
        },
        {
            "url": "https://github.com/zapplyjobs/New-Grad-Software-Engineering-Jobs-2027",
            "renamed_from": ["https://github.com/zapplyjobs/New-Grad-Software-Engineering-Jobs-2026"],
            "category": "Software Engineering",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/zapplyjobs/New-Grad-Data-Science-Jobs-2027",
            "renamed_from": ["https://github.com/zapplyjobs/New-Grad-Data-Science-Jobs-2026"],
            "category": "Data Analysis",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/zapplyjobs/Internships-2027",
            "renamed_from": ["https://github.com/zapplyjobs/Internships-2026"],
            "category": "",
            "level": "internship",
        },
        # === Jobright-AI Internship repos (2026) ===
        # No Accounting or Engineering internship, or Accounting new-grad, list:
        # those URLs were typos (the repos are 2026-Account-Internship,
        # 2026-Engineer-Internship and 2026-Account-New-Grad), 404ed from the
        # day they were seeded and sat in status=error. Not re-added under the
        # right names: like every jobright-ai list they link only to
        # jobright.ai, which ingest skips (is_list_vendor_url), so they would
        # store nothing.
        {
            "url": "https://github.com/jobright-ai/2026-Software-Engineer-Internship",
            "category": "Software Engineering",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Data-Analysis-Internship",
            "category": "Data Analysis",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Product-Management-Internship",
            "category": "Product Management",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Business-Analyst-Internship",
            "category": "Business Analyst",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Consultant-Internship",
            "category": "Consultant",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Marketing-Internship",
            "category": "Marketing",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Support-Internship",
            "category": "Customer Service and Support",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Education-Internship",
            "category": "Education and Training",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Public-Sector-Internship",
            "category": "Public Sector and Government",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-HR-Internship",
            "category": "Human Resources",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Legal-Internship",
            "category": "Legal and Compliance",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Art-Internship",
            "category": "Arts and Entertainment",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Sales-Internship",
            "category": "Sales",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Design-Internship",
            "category": "Creatives and Design",
            "level": "internship",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Management-Internship",
            "category": "Management and Executive",
            "level": "internship",
        },
        # === Jobright-AI New Grad repos (2026) ===
        {
            "url": "https://github.com/jobright-ai/2026-Software-Engineer-New-Grad",
            "category": "Software Engineering",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Data-Analysis-New-Grad",
            "category": "Data Analysis",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Product-Management-New-Grad",
            "category": "Product Management",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Business-Analyst-New-Grad",
            "category": "Business Analyst",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Consultant-New-Grad",
            "category": "Consultant",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Marketing-New-Grad",
            "category": "Marketing",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Support-New-Grad",
            "category": "Customer Service and Support",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Education-New-Grad",
            "category": "Education and Training",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Public-Sector-New-Grad",
            "category": "Public Sector and Government",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-HR-New-Grad",
            "category": "Human Resources",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Legal-New-Grad",
            "category": "Legal and Compliance",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Art-New-Grad",
            "category": "Arts and Entertainment",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Sales-New-Grad",
            "category": "Sales",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Design-New-Grad",
            "category": "Creatives and Design",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Management-New-Grad",
            "category": "Management and Executive",
            "level": "new_grad",
        },
        {
            "url": "https://github.com/jobright-ai/2026-Engineering-New-Grad",
            "category": "Engineering and Development",
            "level": "new_grad",
        },
        # === Jobright-AI H1B Sponsorship repo ===
        {
            "url": "https://github.com/jobright-ai/Daily-H1B-Jobs-In-Tech",
            "category": "",
            "level": "new_grad",
        },
    ]

    REPO_CATEGORY_MAP: dict[str, str] = {
        "Summer2027-Internships": "Software Engineering",
        "New-Grad-2027": "Software Engineering",
        "2027-SWE-College-Jobs": "Software Engineering",
        "Canadian-Tech-Internships-2027": "Software Engineering",
        "underclassmen-internships": "",
        "New-Grad-Software-Engineering-Jobs-2027": "Software Engineering",
        "New-Grad-Data-Science-Jobs-2027": "Data Analysis",
        "Internships-2027": "",
        # Jobright-AI repos
        "2026-Software-Engineer-Internship": "Software Engineering",
        "2026-Data-Analysis-Internship": "Data Analysis",
        "2026-Product-Management-Internship": "Product Management",
        "2026-Business-Analyst-Internship": "Business Analyst",
        "2026-Consultant-Internship": "Consultant",
        "2026-Marketing-Internship": "Marketing",
        "2026-Support-Internship": "Customer Service and Support",
        "2026-Education-Internship": "Education and Training",
        "2026-Public-Sector-Internship": "Public Sector and Government",
        "2026-HR-Internship": "Human Resources",
        "2026-Legal-Internship": "Legal and Compliance",
        "2026-Art-Internship": "Arts and Entertainment",
        "2026-Sales-Internship": "Sales",
        "2026-Design-Internship": "Creatives and Design",
        "2026-Management-Internship": "Management and Executive",
        "2026-Software-Engineer-New-Grad": "Software Engineering",
        "2026-Data-Analysis-New-Grad": "Data Analysis",
        "2026-Product-Management-New-Grad": "Product Management",
        "2026-Business-Analyst-New-Grad": "Business Analyst",
        "2026-Consultant-New-Grad": "Consultant",
        "2026-Marketing-New-Grad": "Marketing",
        "2026-Support-New-Grad": "Customer Service and Support",
        "2026-Education-New-Grad": "Education and Training",
        "2026-Public-Sector-New-Grad": "Public Sector and Government",
        "2026-HR-New-Grad": "Human Resources",
        "2026-Legal-New-Grad": "Legal and Compliance",
        "2026-Art-New-Grad": "Arts and Entertainment",
        "2026-Sales-New-Grad": "Sales",
        "2026-Design-New-Grad": "Creatives and Design",
        "2026-Management-New-Grad": "Management and Executive",
        "2026-Engineering-New-Grad": "Engineering and Development",
        "Daily-H1B-Jobs-In-Tech": "",
    }

    def __init__(self, db: Session, deadline: Optional[float] = None):
        self.db = db
        # time.monotonic() after which no description fetch starts (cron-poll
        # shares one request with Vercel's 300 s limit). Descriptions are
        # optional here: cron-backfill fetches whatever this leaves.
        self.deadline = deadline
        # Posting keys of the visible GitHub-list rows (_listed_postings),
        # read once per parse of the list in _listing (id, listed URLs).
        self._listed_keys: Optional[set[str]] = None
        self._listing: tuple[Optional[int], set[str]] = (None, set())
        self.parser = MarkdownParser()
        self.country_filter = CountryFilter()
        self.work_type_classifier = WorkTypeClassifier()

    async def seed_sources(self) -> dict[str, int]:
        """Create GitHubSource records for all configured repos. Idempotent.

        A repo counts as seeded when a source tracks its URL or any of its
        ``renamed_from`` URLs (case-insensitive, as GitHub treats them): a
        second source for a renamed list would park the one that owns its
        rows, or be parked itself, after one wasted poll.

        Returns {"created": N, "existing": M}
        """
        created = 0
        existing = 0

        for repo_config in self.REPOS:
            file_path = repo_config.get("file_path", DEFAULT_FILE)
            repo_url = source_url(repo_config["url"], file_path)
            known_urls = [source_url(url, file_path)
                          for url in (repo_config["url"], *repo_config.get("renamed_from", ()))]

            # Check if source already exists
            source = (
                self.db.query(GitHubSource.id)
                .filter(func.lower(GitHubSource.repo_url).in_([u.lower() for u in known_urls]))
                .first()
            )

            if source:
                existing += 1
                continue

            repo_owner, repo_name = _REPO_URL_RE.match(repo_config["url"]).groups()

            new_source = GitHubSource(
                repo_url=repo_url,
                repo_owner=repo_owner,
                repo_name=repo_name,
                file_path=file_path,
                poll_interval_minutes=60,
                role_category=repo_config["category"],
                experience_level=repo_config["level"],
                status="active",
            )
            self.db.add(new_source)
            created += 1

        if created > 0:
            self.db.commit()

        return {"created": created, "existing": existing}

    async def poll_source(self, source: GitHubSource) -> int:
        """Poll a single source: check SHA → fetch → parse → classify → store.

        Returns count of new jobs added. Always updates last_polled_at so cron
        rotation advances even when the README commit has not changed.
        """
        try:
            changed, new_sha, committed_at = await self._check_commit_sha(source)
            new_count = 0

            if changed:
                content = await self._fetch_readme(source)
                is_mega_repo = "Internship" in source.repo_name
                # Year-less dates ('Sep 26') and ages ('3d') are read as of the
                # commit that published them, not as of this poll.
                listed = self.parser.parse(
                    content, is_mega_repo=is_mega_repo, include_closed=True,
                    now=min(committed_at, _utcnow()) if committed_at else None,
                )
                parsed_jobs = [job for job in listed if not job.closed]
                closed_jobs = [job for job in listed if job.closed]
                listed_urls = self._listed_urls(parsed_jobs)

                # Postings past the aggregator max age never become rows (the
                # expiry sweep would hide them on its next run), and the rest
                # go in oldest first: the lists run newest first, and ids must
                # follow recency for the id-ordered enrichment and alert windows.
                insertable = self._oldest_first(
                    [job for job in parsed_jobs if not self._past_max_age(job)]
                )

                # The lists re-publish postings employers already closed,
                # verify genuinely-new URLs before they become catalogue rows
                # a user can click into a 404. Only rows this source can
                # store (an *_INTL.md file's foreign rows took the whole
                # budget), freshest first, so the probe budget goes on the
                # rows users will see first.
                dead_urls = await self._probe_new_urls(
                    [job for job in insertable[::-1] if self._source_country(job, source)])

                # Read afresh: rows left the feed since the last parse.
                self._listed_keys, self._listing = None, (source.id, listed_urls)
                for job in insertable:
                    stored = self._classify_and_store(job, source, dead_urls=dead_urls)
                    if stored:
                        new_count += 1

                # Judged before retirement: a list that had visible rows
                # before this parse is never parked by it.
                parked_reason = self._parked_reason(source, parsed_jobs)
                self._retire_delisted_rows(source, listed_urls, closed_jobs)
                source.last_commit_sha = _stamp(new_sha)
                if parked_reason:
                    source.status = STATUS_PARKED
                    source.error_message = parked_reason
                else:
                    source.status = "active"
                    source.error_message = ""
                    await self._enrich_missing_descriptions(source.id, limit=8)
            elif source.status != STATUS_PARKED:
                # Same README as last time: nothing to reclassify.
                source.status = "active"
                source.error_message = ""

            # The cadence follows the commits (hourly, daily once quiet)
            # unless an admin set another interval (PUT /github-sources/{id}),
            # which sticks.
            if (source.poll_interval_minutes or ACTIVE_POLL_MINUTES) in AUTO_POLL_MINUTES:
                now = _utcnow()
                quiet = committed_at is not None and now - committed_at > DORMANT_AFTER
                source.poll_interval_minutes = DORMANT_POLL_MINUTES if quiet else ACTIVE_POLL_MINUTES
            source.last_polled_at = datetime.datetime.utcnow()
            self.db.commit()
            return new_count

        except RepoMovedError as e:
            logger.warning("GitHub source %s: %s", source.repo_url, str(e))
            source.status = "error"
            source.error_message = str(e)[:500]
            source.last_polled_at = datetime.datetime.utcnow()
            self.db.commit()
            return 0

        except httpx.HTTPStatusError as e:
            logger.error(
                "GitHub API error polling %s: %s", source.repo_url, str(e)
            )
            # Only a repo that is gone for good leaves the rotation; a 5xx or
            # rate limit is retried on the next poll like any other source.
            if e.response.status_code in PERMANENT_HTTP_STATUSES:
                source.status = "error"
            source.error_message = f"HTTP {e.response.status_code}: {str(e)[:400]}"
            source.last_polled_at = datetime.datetime.utcnow()
            self.db.commit()
            return 0

        except httpx.TransportError as e:
            kind = "Timeout" if isinstance(e, httpx.TimeoutException) else "Network"
            logger.error(
                "%s polling %s: %s", kind, source.repo_url, str(e)
            )
            source.error_message = f"{kind}: {str(e)[:400]}"
            source.last_polled_at = datetime.datetime.utcnow()
            self.db.commit()
            return 0

        except Exception as e:
            logger.warning(
                "Error polling %s: %s", source.repo_url, str(e)
            )
            # Still advance the rotation, or one broken README would hold the
            # head of every cron-poll batch.
            try:
                self.db.rollback()
                source.error_message = f"Error: {str(e)[:400]}"
                source.last_polled_at = datetime.datetime.utcnow()
                self.db.commit()
            except Exception:
                self.db.rollback()
            return 0

    async def poll_all_sources(self) -> dict[str, int]:
        """Poll all active sources. Returns summary of results."""
        sources = (
            self.db.query(GitHubSource)
            .filter(GitHubSource.status == "active")
            .all()
        )

        results: dict[str, int] = {}
        for source in sources:
            count = await self.poll_source(source)
            results[source.repo_name] = count

        return results

    def _parked_reason(self, source: GitHubSource, open_jobs: list[ParsedJob]) -> str:
        """Why a list whose README was just parsed in full can't feed the
        catalogue, or '' when it can. A list is parked when no open row links
        to an employer (jobright.ai / zapply.jobs redirectors, a bullet-list
        README) and it has no visible rows left: a list that still owns
        visible rows is never parked by one odd parse."""
        if any(is_job_url(job.url) and not is_list_vendor_url(job.url) for job in open_jobs):
            return ""
        from backend.services.listing_freshness import HIDDEN_LISTING_STATUSES

        visible = (
            self.db.query(ScrapedJob.id)
            .filter(ScrapedJob.github_source_id == source.id,
                    or_(ScrapedJob.listing_status.is_(None),
                        ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES)))
            .first()
        )
        if visible:
            logger.warning("GitHub source %s: no employer links in %d rows, "
                           "keeping it active (it still has visible rows)",
                           source.repo_url, len(open_jobs))
            return ""
        if not open_jobs:
            return "Parked: no job table in the README (re-checked weekly)"
        return (f"Parked: none of {len(open_jobs)} open rows links to an employer, "
                f"only to list vendors (re-checked weekly)")

    @staticmethod
    def _due_at(source: GitHubSource) -> Optional[datetime.datetime]:
        """When ``source`` is next due for a poll, or None when never (a
        permanent error, polled or not: the typo'd jobright URLs 404ed before
        their first poll ever completed). A never-polled source, and a README
        parsed under an older PARSE_REVISION, are due at once."""
        if source.status == "error":
            if not is_retryable_error(source.error_message):
                return None
            if source.last_polled_at is None:
                return datetime.datetime.min
            return source.last_polled_at + ERROR_RETRY_COOLDOWN
        if source.last_polled_at is None:
            return datetime.datetime.min
        sha, revision = _split_stamp(source.last_commit_sha)
        if sha and revision != PARSE_REVISION:
            return datetime.datetime.min
        if source.status == STATUS_PARKED:
            return source.last_polled_at + PARKED_RECHECK
        minutes = source.poll_interval_minutes or ACTIVE_POLL_MINUTES
        return source.last_polled_at + datetime.timedelta(minutes=minutes)

    def sources_due(self, limit: int = 5,
                    now: Optional[datetime.datetime] = None) -> list[GitHubSource]:
        """The next sources cron-poll should poll, most overdue first: active
        lists by their poll interval (hourly while they commit, daily once
        quiet), parked ones weekly, and 'error' ones whose failure was
        transient once they have cooled down (a single 504 or a repo rename
        used to park a source for good). Anything not yet parsed under the
        current PARSE_REVISION goes first.

        Parked lists only get the slots left over: they come due together
        (the first pass after a deploy parks ~36 at once) and a week late
        costs nothing, while a skipped run delays the lists that feed the
        catalogue. So does a list re-parsed only for a newer PARSE_REVISION
        while it has nothing in the feed: after the r1 bump the ~33 vendor
        lists (still 'active', 0 rows) were all due at once, and once the
        productive lists were re-parsed and due hourly again, the vendor
        lists sorted ahead of them for ~4 runs (12-15 h) until all were
        parked. A never-polled list (a new file) still goes first.
        """
        now = now or datetime.datetime.utcnow()
        # A few dozen rows at most: filter the due rule in Python.
        candidates = (
            self.db.query(GitHubSource)
            .filter(GitHubSource.status.in_(("active", "error", STATUS_PARKED)))
            .all()
        )
        due = [(self._due_at(source), source) for source in candidates]
        due = [(due_at, source) for due_at, source in due if due_at is not None and due_at <= now]
        # Ties (every source is due at once after a PARSE_REVISION bump) go
        # to the lists that feed the catalogue: most visible rows first.
        from backend.services.listing_freshness import HIDDEN_LISTING_STATUSES
        visible = dict(
            self.db.query(ScrapedJob.github_source_id, func.count(ScrapedJob.id))
            .filter(ScrapedJob.github_source_id.in_([source.id for _due, source in due] or [-1]),
                    ScrapedJob.duplicate_of.is_(None),
                    or_(ScrapedJob.listing_status.is_(None),
                        ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES)))
            .group_by(ScrapedJob.github_source_id)
            .all()
        ) if due else {}

        def reparse_only(due_at: datetime.datetime, source: GitHubSource) -> bool:
            """Due at once only for PARSE_REVISION, with nothing in the feed."""
            return (due_at == datetime.datetime.min and source.last_polled_at is not None
                    and not visible.get(source.id))

        due.sort(key=lambda entry: (entry[1].status == STATUS_PARKED, reparse_only(*entry), entry[0],
                                    -visible.get(entry[1].id, 0),
                                    entry[1].last_polled_at or datetime.datetime.min))
        return [source for _due, source in due[:limit]]

    def _github_client(self, timeout: float) -> httpx.AsyncClient:
        """GitHub API client. Follows redirects: a renamed repo answers 301."""
        return httpx.AsyncClient(follow_redirects=True, timeout=timeout)

    async def _check_commit_sha(
        self, source: GitHubSource
    ) -> tuple[bool, str, Optional[datetime.datetime]]:
        """Check if commit SHA has changed using GitHub API.

        Returns (changed, new_sha, committed_at). If the SHA is the same as
        stored, returns (False, current_sha, ...). ``committed_at`` is the head
        commit's committer time (naive UTC), or None when the API omits it.
        """
        url = (
            f"{GITHUB_API_BASE}/repos/{source.repo_owner}/"
            f"{source.repo_name}/commits?per_page=1"
        )
        if (source.file_path or DEFAULT_FILE) != DEFAULT_FILE:
            # Only commits that touch this source's own file.
            url += f"&path={quote(source.file_path)}"

        headers = self._get_github_headers()

        async with self._github_client(timeout=30) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            commits = response.json()
            if response.history:
                # Renamed repo: GitHub redirected to /repositories/{id}. Adopt
                # the new name so the README fetch and later polls go direct.
                await self._adopt_repo_rename(client, source, headers)

        stored_sha, stored_revision = _split_stamp(source.last_commit_sha)
        if not commits:
            return False, stored_sha, None

        new_sha = commits[0]["sha"]
        changed = new_sha != stored_sha or stored_revision != PARSE_REVISION
        return changed, new_sha, _commit_time(commits[0])

    async def _adopt_repo_rename(self, client: httpx.AsyncClient,
                                 source: GitHubSource, headers: dict) -> None:
        """Point ``source`` at its repo's current owner/name (the API's
        ``full_name``). Raises RepoMovedError when another source already
        tracks the new name, so the two never poll the same README.

        In that case this source's rows move to the tracking source first:
        only the source that keeps polling the README can retire them. A
        tracking source parked in 'error' is put back in rotation, since the
        API just answered for its name (a 404 from before the repo took the
        name no longer holds)."""
        response = await client.get(
            f"{GITHUB_API_BASE}/repos/{source.repo_owner}/{source.repo_name}",
            headers=headers,
        )
        response.raise_for_status()
        full_name = str((response.json() or {}).get("full_name") or "")
        owner, _, name = full_name.partition("/")
        if not owner or not name:
            return
        if (owner.lower(), name.lower()) == (source.repo_owner.lower(), source.repo_name.lower()):
            return

        repo_url = source_url(f"https://github.com/{owner}/{name}", source.file_path or DEFAULT_FILE)
        tracked = (
            self.db.query(GitHubSource.id, GitHubSource.status)
            .filter(func.lower(GitHubSource.repo_url) == repo_url.lower(),
                    GitHubSource.id != source.id)
            .first()
        )
        if tracked:
            tracked_id, tracked_status = tracked
            moved = (
                self.db.query(ScrapedJob)
                .filter(ScrapedJob.github_source_id == source.id)
                .update({"github_source_id": tracked_id}, synchronize_session=False)
            )
            if tracked_status == "error":
                self.db.query(GitHubSource).filter(GitHubSource.id == tracked_id).update(
                    {"status": "active", "error_message": ""}, synchronize_session=False,
                )
            if moved:
                logger.info("GitHub source %s: moved %d rows to source %s (%s)",
                            source.repo_url, moved, tracked_id, full_name)
            raise RepoMovedError(f"Renamed to {full_name}, already tracked by source {tracked_id}")

        logger.info("GitHub source %s renamed to %s", source.repo_url, full_name)
        source.repo_owner = owner
        source.repo_name = name
        source.repo_url = repo_url

    async def _fetch_readme(self, source: GitHubSource) -> str:
        """Fetch raw README content from GitHub API."""
        url = (
            f"{GITHUB_API_BASE}/repos/{source.repo_owner}/"
            f"{source.repo_name}/contents/{source.file_path}"
        )

        headers = self._get_github_headers()
        headers["Accept"] = "application/vnd.github.v3.raw"

        async with self._github_client(timeout=60) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            return response.text

    def _get_github_headers(self) -> dict[str, str]:
        """Build GitHub API request headers, including auth token if available."""
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
        }
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _allowed_countries(self, source: GitHubSource) -> frozenset:
        """The seed entry's "countries" for this source, empty when unrestricted.

        Matched by URL, or for a non-README file by owner + file: speedyapply
        renames its repos every season (2026 -> 2027), a source adopts the new
        name on its next poll, and its INTERN_INTL.md must not start admitting
        'Remote - Poland' rows (read as US) until REPOS catches up."""
        url = (source.repo_url or "").lower()
        file_path = source.file_path or DEFAULT_FILE
        for config in self.REPOS:
            if not config.get("countries"):
                continue
            config_file = config.get("file_path", DEFAULT_FILE)
            urls = (config["url"], *config.get("renamed_from", ()))
            if url in {source_url(u, config_file).lower() for u in urls}:
                return frozenset(config["countries"])
            if (config_file != DEFAULT_FILE and config_file.lower() == file_path.lower()
                    and _REPO_URL_RE.match(config["url"]).group(1).lower()
                    == (source.repo_owner or "").lower()):
                return frozenset(config["countries"])
        return frozenset()

    def _source_country(self, job: ParsedJob, source: GitHubSource) -> Optional[str]:
        """The country a row of ``source`` is stored under, or None when the
        source doesn't take it: not US/CA, or outside the seed entry's
        allowlist. A list of mostly non-North-American roles (speedyapply's
        INTERN_INTL.md) is only read for the country it is seeded for: its
        'Remote - Poland' rows classify as US."""
        country = self.country_filter.classify(job.location)
        allowed = self._allowed_countries(source)
        if country is None or (allowed and country not in allowed):
            return None
        return country

    def _get_experience_level(self, source: GitHubSource, title: str = "") -> str:
        """Returns 'internship' or 'new_grad' based on source repo name, or on
        the title for mixed lists (speedyapply's README is all internships)."""
        if "internship" in (source.repo_name or "").lower():
            return "internship"
        if _INTERNSHIP_TITLE_RE.search(title or ""):
            return "internship"
        return "new_grad"

    @staticmethod
    def _past_max_age(job: ParsedJob, now: Optional[datetime.datetime] = None) -> bool:
        """True when the list dates the posting past the aggregator max age:
        the expiry sweep would hide a row for it on its next run."""
        from backend.services.listing_freshness import AGGREGATOR_MAX_AGE_DAYS

        if job.posted_date is None:
            return False
        cutoff = (now or _utcnow()) - datetime.timedelta(days=AGGREGATOR_MAX_AGE_DAYS)
        return job.posted_date < cutoff

    @staticmethod
    def _oldest_first(jobs: list[ParsedJob]) -> list[ParsedJob]:
        """Insert order: undated rows first, then by date ascending. Ties keep
        reversed README order, since the lists add new rows at the top."""
        return sorted(
            reversed(jobs),
            key=lambda job: (job.posted_date is not None,
                             job.posted_date or datetime.datetime.min),
        )

    async def _probe_new_urls(self, parsed_jobs: list[ParsedJob]) -> set[str]:
        """Liveness-check the parsed URLs that aren't in the catalogue yet.

        Returns the set of canonical URLs that answered an honest 404/410.
        Bounded (probe budget + timeouts); URLs past the budget pass through
        unverified and the hourly verify sweep catches them later.
        """
        from backend.services.cross_source_dedup import canonical_url
        from backend.services.description_extractor import BROWSER_HEADERS
        from backend.services.listing_freshness import probe_urls_liveness

        candidates: list[str] = []
        seen: set[str] = set()
        for job in parsed_jobs:
            url = canonical_url(job.url or "")
            if url and url not in seen and not is_list_vendor_url(url):
                seen.add(url)
                candidates.append(url)
        if not candidates:
            return set()

        known: set[str] = set()
        for i in range(0, len(candidates), 400):
            chunk = candidates[i:i + 400]
            known.update(
                row[0] for row in
                self.db.query(ScrapedJob.url).filter(ScrapedJob.url.in_(chunk)).all()
            )
        fresh = [u for u in candidates if u not in known]
        if not fresh:
            return set()

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10,
                                         headers=BROWSER_HEADERS) as client:
                verdicts = await probe_urls_liveness(client, fresh, budget=80)
        except Exception:
            return set()
        return {url for url, verdict in verdicts.items() if verdict == "dead"}

    @staticmethod
    def _listed_urls(jobs: list[ParsedJob]) -> set[str]:
        """Every form a listed URL may be stored under: raw (rows stored before
        canonical_url existed kept '?utm_source=vansh') and canonical."""
        from backend.services.cross_source_dedup import canonical_url

        urls: set[str] = set()
        for job in jobs:
            if job.url:
                urls.add(job.url)
                urls.add(canonical_url(job.url))
        return urls

    def _listed_postings(self) -> set[str]:
        """Posting keys (cross_source_dedup.list_posting_keys) of every
        GitHub-list row whose posting is in the feed (a hidden repeat's is,
        under the row it repeats): read once per parse, then kept current as
        rows are stored. Column-only.

        The list being parsed (``self._listing``: its id and listed URLs)
        doesn't count its own rows it no longer lists: the parse retires
        them, so a list that swaps one site alias of a posting for another
        keeps the posting in the feed."""
        if self._listed_keys is None:
            from backend.services.cross_source_dedup import canonical_url, list_posting_keys
            from backend.services.listing_freshness import HIDDEN_LISTING_STATUSES

            source_id, listed_urls = self._listing
            self._listed_keys = set()
            for url, github_source_id in (
                self.db.query(ScrapedJob.url, ScrapedJob.github_source_id)
                .filter(ScrapedJob.source_platform == "github",
                        or_(ScrapedJob.listing_status.is_(None),
                            ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES)))
            ):
                url = url or ""
                if (source_id is not None and github_source_id == source_id
                        and url not in listed_urls and canonical_url(url) not in listed_urls):
                    continue
                self._listed_keys |= list_posting_keys(url)
        return self._listed_keys

    def _retire_delisted_rows(self, source: GitHubSource, listed_urls: set[str],
                              closed_jobs: list[ParsedJob]) -> dict[str, int]:
        """Soft-remove this source's visible rows the list stopped offering.

        - closed: the list now marks the posting closed (🔒 / strikethrough).
          Those rows drop the link, so they match on company + title.
        - vanished: the URL is gone from the README we just re-parsed in full.

        A URL the list still shows open is never touched, and neither is a row
        a first-party board reconciles (a board_key other than ''/'unknown'):
        that board's crawl decides whether it is live, or the two would flip
        it between removed and active. A re-parse that would retire most of
        the source's rows as vanished is a README format change, not a mass
        closure, so only the closed matches are applied then.
        Column-only reads and updates; the caller commits.
        """
        from backend.services.cross_source_dedup import canonical_url
        from backend.services.listing_freshness import (
            HIDDEN_LISTING_STATUSES,
            LISTING_REMOVED,
            _unreconcilable_board,
        )

        rows = (
            self.db.query(ScrapedJob.id, ScrapedJob.url, ScrapedJob.company, ScrapedJob.title)
            .filter(
                ScrapedJob.github_source_id == source.id,
                _unreconcilable_board(),
                or_(ScrapedJob.listing_status.is_(None),
                    ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES)),
            )
            .all()
        )
        closed_keys = {_listing_key(job.company, job.title) for job in closed_jobs}
        closed_urls = self._listed_urls([job for job in closed_jobs if job.url]) - listed_urls

        closed_ids: list[int] = []
        vanished_ids: list[int] = []
        for row_id, url, company, title in rows:
            url = url or ""
            if url in listed_urls or canonical_url(url) in listed_urls:
                continue
            if (url in closed_urls or canonical_url(url) in closed_urls
                    or _listing_key(company or "", title or "") in closed_keys):
                closed_ids.append(row_id)
            else:
                vanished_ids.append(row_id)

        if vanished_ids and (
            not listed_urls
            or len(vanished_ids) > max(_VANISHED_GUARD_MIN, len(rows) * _VANISHED_GUARD_RATIO)
        ):
            logger.warning(
                "GitHub source %s: %d of %d rows missing from the README, "
                "not retiring them (parse likely broken)",
                source.repo_url, len(vanished_ids), len(rows),
            )
            vanished_ids = []

        now = datetime.datetime.utcnow()
        retired = closed_ids + vanished_ids
        for i in range(0, len(retired), 400):
            chunk = retired[i:i + 400]
            self.db.query(ScrapedJob).filter(ScrapedJob.id.in_(chunk)).update(
                {"listing_status": LISTING_REMOVED, "listing_status_changed_at": now},
                synchronize_session=False,
            )
        if closed_ids or vanished_ids:
            logger.info("GitHub source %s: retired %d closed, %d vanished rows",
                        source.repo_url, len(closed_ids), len(vanished_ids))
        return {"closed": len(closed_ids), "vanished": len(vanished_ids)}

    def _classify_and_store(self, job: ParsedJob, source: GitHubSource,
                            dead_urls: frozenset | set = frozenset()) -> bool:
        """Classify a parsed job and store it if it passes filters.

        Returns True if the job was stored, False if skipped (duplicate or
        excluded). URLs in ``dead_urls`` are stored as already-removed: the
        catalogue never shows them, and remembering the URL stops the next
        poll from re-discovering and re-probing the same dead posting.
        """
        # Reject list-vendor redirect URLs (jobright.ai, zapply.jobs): we only
        # want direct company links
        if is_list_vendor_url(job.url):
            return False
        # Malformed links ('https:/.workable.com/...') are dead on arrival.
        if not is_job_url(job.url):
            return False
        # A plainly senior title ("Software Engineer I -II -III", "Level 4",
        # "Senior ...") is no student job whatever list carries it. Only the
        # crawler's hard markers (ats_scraper.HARD_SENIOR, shared with the
        # LinkedIn/Indeed ingest): the lists are curated for interns and new
        # grads, so its weak tier, soft words ("Manager", "Architect") and
        # frontline rules don't apply to them. A title that also names the
        # student role itself (ats_scraper.STUDENT_ROLE_TITLE) is that role:
        # "Chief of Staff Intern", "Software Engineer Intern - Sr. Design".
        # Naming the program is not enough: "Head of Internships", "Senior
        # Manager, Early Careers" and "Research Fellow - Summer 2027" (a
        # date, no student role) stay out.
        from backend.services.ats_scraper import HARD_SENIOR, STUDENT_ROLE_TITLE
        if (HARD_SENIOR.search(job.title or "")
                and not STUDENT_ROLE_TITLE.search(job.title or "")):
            return False

        # Same posting, different utm_* decorations must collide on the URL
        # unique constraint instead of slipping in twice.
        from backend.services.cross_source_dedup import canonical_url
        job.url = canonical_url(job.url)

        # A list date is a publish date, never a future one: a future date tops
        # the date-sorted feed and never ages past the expiry cutoff.
        now = _utcnow()
        if job.posted_date and job.posted_date > now:
            job.posted_date = now
        # A posting the list dates past the aggregator max age is not a new
        # job: stored active, the expiry sweep would hide it on its next run.
        if self._past_max_age(job, now):
            return False

        country = self._source_country(job, source)
        if country is None:
            return False

        # Work type: prefer the jobright "Work Model" column when present,
        # otherwise infer it from the location string.
        work_type = job.work_model or self.work_type_classifier.classify(job.location)

        # Determine role category, normalised to the canonical taxonomy:
        #   1. section header from the parser (mega-repos), else
        #   2. classify from the job title, falling back to the source repo's
        #      configured category only when the title says nothing. Broad
        #      lists (negarprh, Summer2027) are configured 'Software
        #      Engineering' but carry data, hardware and PM roles too.
        from backend.services.role_classifier import classify, normalize_category

        if job.section_category:
            role_category = normalize_category(job.section_category) or job.section_category
        else:
            role_category = classify(job.title, source.role_category or "")

        # Determine experience level
        experience_level = self._get_experience_level(source, job.title)

        # Scraper v2 tags: the career family from the title (a list row has
        # no department, and its description arrives later), with the
        # version of the rules that set it. Only the family: the level stays
        # the list's own call above, since these are student lists.
        from backend.ingest import taxonomy

        career_family, career_family_secondary = taxonomy.classify_family(job.title)

        # Resolve an accurate company logo + domain. The parser already resolves
        # these from the company website URL; without one its domain is a name
        # guess, and an employer-hosted apply link (carvana.com/careers/...)
        # is better evidence.
        company_logo = job.company_logo or ""
        company_domain = job.company_domain or ""
        if not company_domain or not domain_from_url(job.company_url):
            resolved_logo, company_domain = resolve_logo(
                job.company, job.company_url, apply_url=job.url
            )
            if logo_quality(company_logo) == 0:
                company_logo = resolved_logo

        # Deduplication: check if URL already exists. Query the column, not the
        # entity: loading the row pulls its description over the wire for a boolean.
        existing = (
            self.db.query(ScrapedJob.url)
            .filter(ScrapedJob.url == job.url)
            .first()
        )
        if existing:
            return False
        # The board crawl may already carry this posting under another URL
        # spelling (Workday locale/site aliases, a '-1' repost suffix).
        from backend.services.cross_source_dedup import board_row_for_posting
        if board_row_for_posting(self.db, company=job.company, company_domain=company_domain,
                                 title=job.title, url=job.url):
            return False
        # Another list, or this one under another site alias, already has
        # it in the feed: the same Workday requisition, Greenhouse job or
        # Lever/Ashby UUID, or the same URL in another letter case.
        from backend.services.cross_source_dedup import list_posting_keys
        posting_keys = list_posting_keys(job.url)
        if posting_keys & self._listed_postings():
            return False

        # Store the job
        from backend.services.cross_source_dedup import mark_inferior_twins, normalize_title
        from backend.services.listing_freshness import LISTING_ACTIVE, LISTING_REMOVED

        now = datetime.datetime.utcnow()
        is_dead = job.url in dead_urls
        # A self-hosted logo (and verified domain) from the logo store wins.
        company_logo, company_domain = brand(
            load_branding(self.db, [job.company]), job.company, company_logo, company_domain
        )

        scraped_job = ScrapedJob(
            title=job.title,
            company=job.company,
            location=job.location,
            url=job.url,
            description="",
            source_platform="github",
            github_source_id=source.id,
            posted_date=job.posted_date,
            easy_apply=0,
            work_type=work_type,
            role_category=role_category,
            country=country,
            experience_level=experience_level,
            career_family=career_family,
            career_family_secondary=career_family_secondary,
            enrichment_version=taxonomy.ENRICHMENT_VERSION,
            company_logo=company_logo,
            company_domain=company_domain,
            company_url=job.company_url or "",
            title_norm=normalize_title(job.title),
            listing_status=LISTING_REMOVED if is_dead else LISTING_ACTIVE,
            listing_status_changed_at=now if is_dead else None,
            first_seen_at=now,
            last_seen_at=now,
            source_trust="medium",
            visa_sponsorship="no" if job.no_sponsorship else "unknown",
            **location_fields(job.location),
        )
        self.db.add(scraped_job)
        try:
            self.db.commit()
        except Exception:
            # Duplicate URL or other constraint violation, rollback and skip
            self.db.rollback()
            return False

        if is_dead:
            return False  # remembered, hidden, not a new catalogue job
        self._listed_postings().update(posting_keys)

        # This direct row supersedes LinkedIn/Indeed copies that arrived first.
        try:
            mark_inferior_twins(self.db, scraped_job)
        except Exception:
            self.db.rollback()
        return True

    async def _enrich_missing_descriptions(
        self, source_id: int | None = None, limit: int = 10
    ) -> int:
        """Fetch descriptions for jobs that only have metadata + apply URL.
        No fetch starts past ``self.deadline``: each one can take its whole
        15 s timeout, and a batch of 40 sequential fetches alone could outlast
        the cron request."""
        import time

        def out_of_time() -> bool:
            return self.deadline is not None and time.monotonic() >= self.deadline

        if out_of_time():
            return 0

        from sqlalchemy import or_
        from backend.services.description_extractor import (
            BROWSER_HEADERS,
            extract_description_from_url,
            sanitize_description,
        )

        from backend.services.listing_freshness import HIDDEN_LISTING_STATUSES

        query = self.db.query(ScrapedJob).filter(
            or_(ScrapedJob.description == "", ScrapedJob.description.is_(None)),
            or_(ScrapedJob.listing_status.is_(None),
                ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES)),
        )
        if source_id is not None:
            query = query.filter(ScrapedJob.github_source_id == source_id)
        jobs = query.order_by(ScrapedJob.id.desc()).limit(limit).all()

        enriched = 0
        async with httpx.AsyncClient(follow_redirects=True, timeout=15, headers=BROWSER_HEADERS) as client:
            for job in jobs:
                if out_of_time():
                    break
                if not job.url:
                    continue
                try:
                    description = await extract_description_from_url(client, job.url)
                    if description:
                        job.description = sanitize_description(description)
                        enriched += 1
                except Exception as exc:
                    logger.debug("Description enrich failed for job %s: %s", job.id, exc)

        if enriched:
            self.db.commit()
        return enriched
