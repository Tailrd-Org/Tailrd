"""§5.7 Tools & secondary keywords (weight `tools`).

"Same machinery as §5.1 at lower stakes. Absorbs the long tail." The coverage over the posting's
IMPLIED requirement groups: skills it mentions outside its required/preferred statements, plus
the overflow beyond the top 25 by IDF at `posting.overflow_weight_multiplier` (SPEC §14.4,
DECISIONS D-30). Node kind never routes a requirement here. Abstains with
NO_IMPLIED_REQUIREMENTS when the posting has none and with NO_REQUIREMENTS_RESOLVED when none
carries weight.
"""

from __future__ import annotations

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, DimensionScore, JobPosting, Necessity
from matcher.score.coverage import score_coverage
from matcher.score.prepare import PreparedResume


def score_tools(prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig) -> DimensionScore:
    return score_coverage(
        prepared,
        posting,
        graph,
        config,
        name=DimensionName.TOOLS,
        necessity=Necessity.IMPLIED,
        absent=Flag.NO_IMPLIED_REQUIREMENTS,
    )
