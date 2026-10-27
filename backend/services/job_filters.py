"""Shared helpers for dashboard job list filtering."""

from __future__ import annotations

import datetime
from typing import Iterable, Optional

from backend.ingest.taxonomy import LEGACY_CATEGORY_TO_FAMILIES

# UI filter values -> stored experience_level spellings in scraped_jobs.
#
# The six UI options are Jobright's (frontend EXPERIENCE_OPTIONS): Intern/New
# Grad, Entry Level, Mid Level, Senior Level, Lead/Staff, Director/Executive.
# Two of them cover two stored levels each.
#
# The legacy keys are the values the two-option UI saved (and onboarding wrote
# to user_settings.experience_levels) before every level was in scope. They are
# kept so a stale client, an open tab, a bookmarked /jobs?experience_level=...
# URL or an existing user's saved choice still resolves. Legacy "new_grad" was
# the "New Grad / Entry Level" option, so it keeps covering entry level, which
# the new taxonomy splits out of new_grad. "entry" now means Entry Level only:
# under the old taxonomy it meant internship plus new grad, and that meaning is
# deliberately dropped (an old "entry" filter now shows the entry-level tier).
EXPERIENCE_FILTER_MAP: dict[str, list[str]] = {
    "intern_new_grad": ["internship", "new_grad"],
    "entry": ["entry"],
    "mid": ["mid"],
    "senior": ["senior"],
    "lead": ["lead"],
    "director": ["director", "executive"],
    "internship": ["internship"],
    "new_grad": ["new_grad", "entry"],
    "executive": ["executive"],
}

# What a person who never chose levels gets, in the feed and in match alerts:
# Intern/New Grad and Entry Level, the student catalogue Tailrd has always shown.
DEFAULT_EXPERIENCE_FILTER: tuple[str, ...] = ("intern_new_grad", "entry")

# The six options the Jobs page and onboarding offer, in the order they list them
# (the frontend's EXPERIENCE_OPTIONS; test_taxonomy_frontend_agreement.py pins the
# two lists to each other). Each one is a key of EXPERIENCE_FILTER_MAP.
EXPERIENCE_OPTION_KEYS: tuple[str, ...] = (
    "intern_new_grad", "entry", "mid", "senior", "lead", "director",
)


def experience_options_with_jobs(stored_levels: Iterable[Optional[str]]) -> list[str]:
    """The options, in listing order, that have jobs behind them.

    ``stored_levels`` are the scraped_jobs.experience_level values that have at
    least one live row. An option qualifies when any of the stored levels it
    expands to is among them: while production stores only the entry-level
    tiers (CRON_ATS_V2_CATALOGUE off) that is Intern/New Grad, plus Entry Level
    once a weak-tier row (junior, associate) exists, and an option with no rows
    would be a filter that empties the feed."""
    present = {(level or "").strip().lower() for level in stored_levels}
    return [
        key for key in EXPERIENCE_OPTION_KEYS
        if present.intersection(EXPERIENCE_FILTER_MAP[key])
    ]


def expand_experience_filter_values(levels: list[str]) -> list[str]:
    """Expand UI experience filters to DB values (OR within the filter).

    An unknown key expands to itself, so it matches only rows stored under
    that exact spelling (in practice none) instead of widening the filter."""
    out: set[str] = set()
    for level in levels:
        key = (level or "").strip().lower()
        if not key:
            continue
        mapped = EXPERIENCE_FILTER_MAP.get(key)
        if mapped:
            out.update(mapped)
        else:
            out.add(key)
    return sorted(out)


def user_experience_levels(saved: Optional[str | Iterable[str]]) -> list[str]:
    """The stored experience levels a user's saved choice covers.

    ``saved`` is user_settings.experience_levels (comma-joined, as onboarding
    writes it) or a list of the same values. Only keys of EXPERIENCE_FILTER_MAP
    count; nothing saved, or nothing recognized, means DEFAULT_EXPERIENCE_FILTER,
    so an odd setting narrows to the student catalogue rather than silencing a
    user or widening them to every level."""
    if isinstance(saved, str):
        values = saved.split(",")
    else:
        values = list(saved or ())
    known = [key for key in (str(v or "").strip().lower() for v in values)
             if key in EXPERIENCE_FILTER_MAP]
    return expand_experience_filter_values(known or list(DEFAULT_EXPERIENCE_FILTER))


def parse_career_families(raw: Optional[str]) -> list[str]:
    """Career family slugs from a comma-separated filter param, deduped, in order.

    Slugs, never labels: five family labels contain commas. An unknown slug is
    kept and simply matches no row."""
    out: list[str] = []
    for value in (raw or "").split(","):
        slug = value.strip().lower()
        if slug and slug not in out:
            out.append(slug)
    return out


def legacy_categories_for_families(families: Iterable[str]) -> list[str]:
    """The old job functions (role_category values) that map onto any of these
    families through taxonomy.LEGACY_CATEGORY_TO_FAMILIES.

    A row nothing has given a career family yet (every row from before scraper
    v2, until it is re-tagged) is matched through its role_category this way,
    so picking a family never empties the feed of the rows it would have
    matched under the old filter."""
    wanted = set(families)
    return sorted(category for category, mapped in LEGACY_CATEGORY_TO_FAMILIES.items()
                  if wanted.intersection(mapped))


def date_posted_cutoff(value: str) -> datetime.datetime | None:
    """Return UTC cutoff for a date_posted filter token."""
    token = (value or "").strip().lower()
    if not token:
        return None
    now = datetime.datetime.utcnow()
    if token == "24h":
        return now - datetime.timedelta(hours=24)
    if token == "3d":
        return now - datetime.timedelta(days=3)
    if token == "week":
        return now - datetime.timedelta(days=7)
    if token == "month":
        return now - datetime.timedelta(days=30)
    return None
