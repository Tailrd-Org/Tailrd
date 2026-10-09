"""§5.4 Seniority alignment (weight `seniority`).

SPEC §5.3, §5.4; DECISIONS D-38, D-39.

* `candidate_level` (D-38): among the roles that carry a seniority, the highest level of the current
  roles; without a current one, the level of the most recent role (the latest end; on the same end,
  the highest). A level whose role neither ends nor is current cannot be placed in time and is not
  used. Levels on the two ladders compare after aligning M n with IC n + `cross_track_offset`
  (`score_seniority` passes the configured offset; the default 0 compares ladder indices); a tie
  goes to the management level, which is at or above IC n under any offset.
* Same track: `gap = job.level − candidate.level`. Cross track: the candidate is first placed on the
  posting's ladder (M n ≈ IC n + `cross_track_offset`), then the same gap, and
  `cross_track_penalty` applies.
* A gap below 0 (above the posting's level) raises an OVERQUALIFIED fact and counts as 0 unless
  `penalize_overqualification` (D-39, a deliberate deviation from §5.4's |gap|: §5.3 forbids that
  deduction as an age proxy); the cross-track penalty still applies.
* Value `max(0, 1 − step × gap − penalty)`. Unknown on either side → abstain SENIORITY_UNKNOWN.

Facts: SENIORITY_GAP (subject = the posting's level; value = the signed gap, positive below the
posting's level; detail = the candidate's level and whether the tracks differ), then OVERQUALIFIED
(subject = the candidate's level; value = how many levels above) when it applies.
"""

from __future__ import annotations

import math
from typing import Optional

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, DimensionScore, Fact, JobPosting, Resume, SeniorityLevel, Track
from matcher.score.dimensions._common import abstain, scored
from matcher.score.numeric import clamp01
from matcher.score.prepare import PreparedResume, ensure_bound

__all__ = ["OVERQUALIFIED", "SENIORITY_GAP", "candidate_level", "score_seniority"]

SENIORITY_GAP = "SENIORITY_GAP"
OVERQUALIFIED = Flag.OVERQUALIFIED.value


def _height(level: SeniorityLevel, cross_track_offset: int) -> tuple[int, bool]:
    """Position on the IC ladder (M n ≈ IC n + offset), then management first on a tie."""
    management = Track(level.track) is Track.MGMT
    return level.level + (cross_track_offset if management else 0), management


def candidate_level(resume: Resume, *, cross_track_offset: int = 0) -> Optional[SeniorityLevel]:
    """The candidate's level (DECISIONS D-38); None when no role carries one that can be placed."""
    leveled = [role for role in resume.roles if role.seniority is not None]
    pool = [role.seniority for role in leveled if role.is_current]
    if not pool:
        ended = [role for role in leveled if role.end is not None]
        if not ended:
            return None
        latest = max(role.end.value for role in ended)
        pool = [role.seniority for role in ended if role.end.value == latest]
    return max(pool, key=lambda level: _height(level, cross_track_offset))


def score_seniority(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig
) -> DimensionScore:
    ensure_bound(prepared, graph, config)
    name = DimensionName.SENIORITY
    rules = config.scoring.seniority
    job = posting.seniority
    candidate = candidate_level(prepared.resume, cross_track_offset=rules.cross_track_offset)
    if job is None or candidate is None:
        return abstain(name, config, Flag.SENIORITY_UNKNOWN)
    job_track = Track(job.track)
    cross_track = Track(candidate.track) is not job_track
    if not cross_track:
        placed = candidate.level
    elif job_track is Track.IC:
        placed = candidate.level + rules.cross_track_offset  # M n ≈ IC n + offset
    else:
        placed = candidate.level - rules.cross_track_offset  # IC n ≈ M n − offset
    gap = job.level - placed
    facts = [Fact(code=SENIORITY_GAP, subject=job.code, value=float(gap),
                  detail={"candidate": candidate.code, "cross_track": cross_track})]
    counted = gap
    if gap < 0:
        facts.append(Fact(code=OVERQUALIFIED, subject=candidate.code, value=float(-gap), detail={"job": job.code}))
        counted = -gap if rules.penalize_overqualification else 0
    penalty = rules.cross_track_penalty if cross_track else 0.0
    value = clamp01(math.fsum((1.0, -rules.step * counted, -penalty)))
    return scored(name, config, value, facts)
