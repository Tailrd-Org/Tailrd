"""§5.1 Required skills coverage (weight `required_skills`).

`S_required = Σ(idf × wm × m) / Σ(idf × wm)` over the posting's REQUIRED requirement groups
(DECISIONS D-30, D-31), with `m = credit × E × R` after the §15.2 overrides (D-23, D-32..D-34).
Every critical group (D-43: IDF above 0.70, `m` below 0.20) adds a CRITICAL_MISS fact; the
aggregator turns those into the cap on the final score. Abstains with NO_REQUIRED_REQUIREMENTS
when the posting has none (its weight then moves to §5.2, SPEC §14.5) and with
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


def score_required_skills(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig,
    *, extra: Sequence[RequirementMatch] = (),
) -> DimensionScore:
    return score_coverage(
        prepared,
        posting,
        graph,
        config,
        name=DimensionName.REQUIRED_SKILLS,
        necessity=Necessity.REQUIRED,
        extra=extra,
        absent=Flag.NO_REQUIRED_REQUIREMENTS,
    )
