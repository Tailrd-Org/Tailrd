"""§5.4 Seniority alignment (weight `seniority`).

SPEC §5.3, §5.4, §14.5; DECISIONS D-38, D-39.

* `candidate_levels` (D-38): the levels the candidate holds now. When any role is current, only the
  current roles count; otherwise only the roles with the latest end. The levels those roles carry
  are the candidate's; when none of them carries one there is no level, and the dimension abstains
  rather than fall back on an older role's level, which would be a known-wrong score exactly where
  the title mapper missed the current title (§14.5). A role that neither ends nor is current cannot
  be placed in time and never counts.
* Every candidate level is scored against the posting and the best score is kept (equal scores: the
  level closest to the posting's, then the same track, then the smaller code). So a current
  internship is never out-ranked by a concurrent "lead" title, and a manager who also holds an IC
  role meets a management posting as a manager.
* One level against the posting's: on the same track `gap = job.level − level`; across tracks the
  level is first placed on the posting's ladder (M n ≈ IC n + `cross_track_offset`), then the same
  gap, and `cross_track_penalty` applies. A gap below 0 (above the posting's level) raises an
  OVERQUALIFIED fact and counts as 0 unless `penalize_overqualification` (D-39, a deliberate
  deviation from §5.4's |gap|: §5.3 forbids that deduction as an age proxy); the cross-track
  penalty still applies. Value `max(0, 1 − step × gap − penalty)`.
* `candidate_level`: the highest of the candidate's levels once the ladders are aligned by the
  `cross_track_offset` the caller must pass (comparing raw ladder indices would call a Junior and a
  Lead the same level, D-38); a tie goes to the management level. For display; scoring tries every
  level as above.

Unknown on either side → abstain SENIORITY_UNKNOWN. Facts: SENIORITY_GAP for the best-scoring level
(subject = the posting's level; value = the signed gap, positive below the posting's level; detail =
that level and whether the tracks differ), then OVERQUALIFIED (subject = that level; value = how
many levels above) when it applies.
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional

from matcher.config import EngineConfig, SeniorityConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, DimensionScore, Fact, JobPosting, Resume, SeniorityLevel, Track
from matcher.score._shared import abstain, scored
from matcher.score.numeric import clamp01, tie_key
from matcher.score.prepare import PreparedResume, ensure_bound

__all__ = ["OVERQUALIFIED", "SENIORITY_GAP", "candidate_level", "candidate_levels", "score_seniority"]

SENIORITY_GAP = "SENIORITY_GAP"
OVERQUALIFIED = Flag.OVERQUALIFIED.value


def candidate_levels(resume: Resume) -> tuple[SeniorityLevel, ...]:
    """The levels the candidate holds now, by code (DECISIONS D-38); empty when unknown."""
    current = [role for role in resume.roles if role.is_current]
    if current:
        recent = current
    else:
        ended = [role for role in resume.roles if role.end is not None]
        if not ended:
            return ()
        latest = max(role.end.value for role in ended)
        recent = [role for role in ended if role.end.value == latest]
    levels = {role.seniority for role in recent if role.seniority is not None}
    return tuple(sorted(levels, key=lambda level: level.code))


def _height(level: SeniorityLevel, cross_track_offset: int) -> tuple[int, bool]:
    """Position on the IC ladder (M n ≈ IC n + offset), then management first on a tie."""
    management = Track(level.track) is Track.MGMT
    return level.level + (cross_track_offset if management else 0), management


def candidate_level(resume: Resume, *, cross_track_offset: int) -> Optional[SeniorityLevel]:
    """The highest of `candidate_levels` with the ladders aligned (DECISIONS D-38); None when unknown."""
    levels = candidate_levels(resume)
    if not levels:
        return None
    return max(levels, key=lambda level: _height(level, cross_track_offset))


class _Fit(NamedTuple):
    level: SeniorityLevel
    value: float
    gap: int  # the posting's level minus the candidate's, on the posting's ladder
    cross_track: bool


def _fit(level: SeniorityLevel, job: SeniorityLevel, rules: SeniorityConfig) -> _Fit:
    job_track = Track(job.track)
    cross_track = Track(level.track) is not job_track
    if not cross_track:
        placed = level.level
    elif job_track is Track.IC:
        placed = level.level + rules.cross_track_offset  # M n ≈ IC n + offset
    else:
        placed = level.level - rules.cross_track_offset  # IC n ≈ M n − offset
    gap = job.level - placed
    if gap >= 0:
        counted = gap
    else:
        counted = -gap if rules.penalize_overqualification else 0
    penalty = rules.cross_track_penalty if cross_track else 0.0
    return _Fit(level, clamp01(math.fsum((1.0, -rules.step * counted, -penalty))), gap, cross_track)


def score_seniority(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig
) -> DimensionScore:
    ensure_bound(prepared, graph, config)
    name = DimensionName.SENIORITY
    job = posting.seniority
    levels = candidate_levels(prepared.resume)
    if job is None or not levels:
        return abstain(name, config, Flag.SENIORITY_UNKNOWN)
    rules = config.scoring.seniority
    best = min(
        (_fit(level, job, rules) for level in levels),
        key=lambda fit: (-tie_key(fit.value), abs(fit.gap), fit.cross_track, fit.level.code),
    )
    facts = [Fact(code=SENIORITY_GAP, subject=job.code, value=float(best.gap),
                  detail={"candidate": best.level.code, "cross_track": best.cross_track})]
    if best.gap < 0:
        facts.append(Fact(code=OVERQUALIFIED, subject=best.level.code, value=float(-best.gap),
                          detail={"job": job.code}))
    return scored(name, config, best.value, facts)
