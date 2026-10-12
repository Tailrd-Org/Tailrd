"""Scope features from quantities in bullets (ADVANCED §2.3, quoted in the Task 6.3 brief).

"QUANTITY PATTERNS -> normalized scope features: '40k req/s', '2M users', '$12M budget', 'team of
14', 'petabyte' -> scale_tier in {personal, team, org, internet} (log-bucketed); people_managed:
int; budget_managed: normalized currency". Users or requests below 1e3 are personal, below 1e5
team, below 1e7 org, from 1e7 (or petabyte scale) internet. Extraction only: parse diagnostics
report it and scoring never reads it.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from matcher.parse.scope import ScopeFeatures, extract_scope


@pytest.mark.parametrize(
    "bullet, tier",
    [
        ("Built an internal tool used by 40 analysts", None),  # people, not users or requests
        ("Built a Discord bot used by 300 users", "personal"),
        ("Scaled the checkout API to 40k req/s", "team"),
        ("Grew the app to 2M monthly active users", "org"),
        ("Served 15 million customers across North America", "internet"),
        ("Designed a pipeline processing 3B events per day", "internet"),
        ("Migrated a petabyte-scale data lake to S3", "internet"),
        ("Handled 1,500 requests per second at peak", "team"),
        ("Supported 100,000+ subscribers", "org"),
        ("Reached millions of readers each month", "org"),
        ("Tuned queries for a 2 TB database", None),
        ("Released version 2.0 in 2024", None),
    ],
)
def test_scale_tier(bullet: str, tier) -> None:
    assert extract_scope([bullet]).scale_tier == tier


@pytest.mark.parametrize(
    "bullet, people",
    [
        ("Led a team of 14 engineers across three time zones", 14),
        ("Managed a team of 5", 5),
        ("Supervised 12 volunteers at weekend food drives", 12),
        ("Managing 3 direct reports", 3),
        ("Géré une équipe de 6 analystes", 6),
        ("Worked in a team of 6 developers", None),  # a member, not a manager
        ("Mentored 4 interns", None),
        ("Team of 8, two-week sprints", None),
    ],
)
def test_people_managed(bullet: str, people) -> None:
    assert extract_scope([bullet]).people_managed == people


@pytest.mark.parametrize(
    "bullet, usd",
    [
        ("Owned a $12M budget for cloud spend", 12_000_000.0),
        ("Managed a budget of $250K", 250_000.0),
        ("Oversaw a US$1.5 million marketing budget", 1_500_000.0),
        ("Managed a CAD $2M budget", 2_000_000.0 * 0.73),
        ("Ran a €400k budget", 400_000.0 * 1.08),
        ("Generated $2M in revenue", None),  # revenue is not a budget managed
        ("Saved $40K a year", None),
    ],
)
def test_budget(bullet: str, usd) -> None:
    found = extract_scope([bullet]).budget_usd
    if usd is None:
        assert found is None
    else:
        assert found is not None and math.isclose(found, usd, rel_tol=1e-9)


def test_the_largest_figures_win_and_the_evidence_is_kept_in_order() -> None:
    scope = extract_scope([
        "Built a Discord bot used by 300 users",
        "Led a team of 4 to ship a mobile app with 2M users",
        "Owned a $50K budget for the hackathon",
        "Led a team of 9 volunteers",
    ])
    assert scope == ScopeFeatures(scale_tier="org", people_managed=9, budget_usd=50_000.0,
                                  evidence=("300 users", "team of 4", "2M users", "$50K budget", "team of 9"))


def test_no_scope() -> None:
    assert extract_scope([]) == ScopeFeatures(scale_tier=None, people_managed=None, budget_usd=None, evidence=())
    assert extract_scope(["Wrote unit tests in Jest", ""]).evidence == ()


@given(st.lists(st.text(max_size=80), max_size=6))
def test_extract_scope_is_total(bullets: list[str]) -> None:
    scope = extract_scope(bullets)
    assert extract_scope(bullets) == scope
    assert scope.scale_tier in (None, "personal", "team", "org", "internet")
    assert scope.people_managed is None or scope.people_managed >= 1
    assert scope.budget_usd is None or (math.isfinite(scope.budget_usd) and scope.budget_usd > 0)
