"""§5.2 Preferred skills coverage (weight `preferred_skills`).

"Same machinery, no critical-miss cap. Missing these is normal." The §5.1 coverage over the
posting's PREFERRED requirement groups (DECISIONS D-30); a PREFERRED group is never critical.
Abstains with NO_PREFERRED_REQUIREMENTS when the posting has none and with
NO_REQUIREMENTS_RESOLVED when none carries weight.
"""

from __future__ import annotations

from collections.abc import Sequence

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, DimensionScore, JobPosting, Necessity
from matcher.score.coverage import RequirementMatch, score_coverage
from matcher.score.prepare import PreparedResume


def score_preferred_skills(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig,
    *, extra: Sequence[RequirementMatch] = (),
) -> DimensionScore:
    return score_coverage(
        prepared,
        posting,
        graph,
        config,
        name=DimensionName.PREFERRED_SKILLS,
        necessity=Necessity.PREFERRED,
        extra=extra,
        absent=Flag.NO_PREFERRED_REQUIREMENTS,
    )
