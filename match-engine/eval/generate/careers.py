"""Careers: how long, how senior, and the timeline of roles of a synthetic candidate.

Dates are month precision throughout (the first day of a month, or a month index
`year * 12 + month - 1`), roles are listed oldest first while they are built, and nothing here
reads the clock: every date is relative to the `as_of` the caller passes.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from matcher.models import SeniorityLevel, Track

IC0 = SeniorityLevel(track=Track.IC, level=0)  # intern / co-op / student (DECISIONS D-38)

# Typical total years at a seniority, for candidates not built against a posting.
BASE_YEARS = {
    "IC1": 1.0,
    "IC2": 3.0,
    "IC3": 5.5,
    "IC4": 8.5,
    "IC5": 12.0,
    "M1": 6.0,
    "M2": 8.0,
    "M3": 11.0,
    "M4": 14.0,
}

# --------------------------------------------------------------------------------------------
# months
# --------------------------------------------------------------------------------------------


def month_index(d: date) -> int:
    return d.year * 12 + d.month - 1


def from_index(i: int) -> date:
    return date(i // 12, i % 12 + 1, 1)


def role_months(start: date, end: date | None, as_of: date) -> range:
    """The months a role covers: [start, end), a role without an end runs to `as_of`."""
    return range(month_index(start), month_index(end or as_of))


def union_months(spans: Sequence[range]) -> int:
    """Calendar months covered by at least one span: concurrent roles are never double-counted."""
    covered: set[int] = set()
    for months in spans:
        covered.update(months)
    return len(covered)


# --------------------------------------------------------------------------------------------
# seniority and years
# --------------------------------------------------------------------------------------------


def step(sen: SeniorityLevel, delta: int) -> SeniorityLevel:
    """`delta` rungs up (+) or down (-); the first management rung sits above IC3."""
    if delta == 0:
        return sen
    if sen.track is Track.IC:
        return SeniorityLevel(track=Track.IC, level=min(5, max(0, sen.level + delta)))
    level = sen.level + delta
    if level < 1:
        return SeniorityLevel(track=Track.IC, level=3)
    return SeniorityLevel(track=Track.MGMT, level=min(4, level))


def final_seniority(rng: random.Random, level: int, posting_sen: SeniorityLevel) -> SeniorityLevel:
    """Where a candidate of fit `level` stands against the posting: at it (3), at most one rung
    below (2), one or two below (1)."""
    if level == 3:
        return step(posting_sen, 1 if rng.random() < 0.15 else 0)
    if level == 2:
        return step(posting_sen, -1 if rng.random() < 0.5 else 0)
    return step(posting_sen, rng.choice((-1, -1, -2)))


def career_path(final: SeniorityLevel, n: int, rng: random.Random) -> list[SeniorityLevel]:
    """The seniority of each of `n` roles, oldest first, ending at `final`. A manager was a senior
    individual contributor first (IC3 -> M1), and an early-career person often started as an intern."""

    def ic(k: int) -> SeniorityLevel:
        return SeniorityLevel(track=Track.IC, level=k)

    if final.track is Track.IC:
        chain = [ic(k) for k in range(1, final.level + 1)] if final.level >= 1 else [ic(0)]
        if 1 <= final.level <= 3 and rng.random() < 0.35:
            chain.insert(0, ic(0))
    else:
        chain = [ic(1), ic(2), ic(3)] + [
            SeniorityLevel(track=Track.MGMT, level=k) for k in range(1, final.level + 1)
        ]
    return chain[-n:] if n <= len(chain) else [chain[0]] * (n - len(chain)) + chain


def target_years(rng: random.Random, level: int, posting_years: float, final: SeniorityLevel) -> float:
    """Total years of experience: meets the posting (3), a little short (2), clearly short (1);
    a level-0 candidate is typical for the seniority it holds."""
    if level == 3:
        years = posting_years * rng.uniform(1.0, 1.5) + rng.uniform(0.4, 1.6)
    elif level == 2:
        years = posting_years * rng.uniform(0.75, 1.0) + rng.uniform(0.0, 0.8)
    elif level == 1:
        years = posting_years * rng.uniform(0.35, 0.65) + rng.uniform(0.0, 0.5)
    else:
        years = BASE_YEARS.get(final.code, 3.0) * rng.uniform(0.8, 1.3)
    return max(years, 0.5)


# --------------------------------------------------------------------------------------------
# the timeline
# --------------------------------------------------------------------------------------------


@dataclass
class Job:
    seniority: SeniorityLevel
    start: int  # month index
    end: int  # month index, exclusive; equals the as-of index for a current role
    current: bool = False


def timeline(
    rng: random.Random, level: int, final: SeniorityLevel, years: float, min_months: int, as_of: date
) -> list[Job]:
    """Roles oldest first. Gaps and short overlaps between roles are part of real careers; a role
    is current (no end date) three times in four."""
    as_idx = month_index(as_of)
    internships_only = final.track is Track.IC and final.level == 0
    if internships_only:
        n = 2 if (level == 3 and rng.random() < 0.7) or (level == 2 and rng.random() < 0.5) else 1
        path = [IC0] * n
        durations = [rng.randint(3, 5) for _ in range(n)]
        gaps = [rng.randint(4, 9) for _ in range(n - 1)]
        tail = rng.randint(1, 9)
    else:
        total = max(6, round(years * 12))
        n = max(1, min(5, round(years / 2.4)))
        if years >= 2.5:
            n = max(n, 2)
        path = career_path(final, n, rng)
        interns = [k for k, s in enumerate(path) if s == IC0]
        durations = [0] * n
        for k in interns:
            durations[k] = rng.randint(3, 6)
        others = [k for k in range(n) if k not in interns]
        left = max(6 * len(others), total - sum(durations))
        weights = [rng.uniform(0.6, 1.4) for _ in others]
        for k, w in zip(others, weights):
            durations[k] = max(6, round(left * w / sum(weights)))
        gaps = []
        for k in range(1, n):
            roll = rng.random()
            touches_internship = path[k] == IC0 or path[k - 1] == IC0
            if touches_internship:
                gaps.append(rng.randint(0, 6))
            elif roll < 0.77:
                gaps.append(0)
            elif roll < 0.92:
                gaps.append(rng.randint(1, 4))
            else:
                gaps.append(-rng.randint(1, 2))  # a short overlap: part-time work or a handover
        tail = 0 if rng.random() < 0.75 else rng.randint(1, 5)
    start = as_idx - (sum(durations) + sum(gaps) + tail)
    jobs: list[Job] = []
    for k in range(n):
        jobs.append(Job(path[k], start, start + durations[k]))
        start += durations[k] + (gaps[k] if k < n - 1 else 0)
    jobs[-1].current = tail == 0
    # a level-3 candidate must really meet the posting's years: stretch the oldest role if gaps ate them
    covered = union_months([range(j.start, j.end) for j in jobs])
    if level == 3 and covered < min_months:
        jobs[0].start -= min_months - covered
    return jobs


def pick_industry(rng: random.Random, codes: tuple[str, ...], target: str | None, mode: str) -> str:
    """An industry (NAICS) of the profession: the posting's own ("exact"), a neighbour sharing its
    3- or 2-digit prefix ("near"), or any ("random")."""
    if mode != "random" and target:
        if mode == "exact" and target in codes:
            return target
        near = [c for c in codes if c[:3] == target[:3]] or [c for c in codes if c[:2] == target[:2]]
        if near:
            return rng.choice(near)
    return rng.choice(codes)
