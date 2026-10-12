"""The dimension scorers (SPEC §5). Each takes a prepared résumé, the posting, the graph and the
config, and returns a `DimensionScore` with its configured weight as the effective weight; the
aggregator redistributes the weight of abstaining dimensions (SPEC §14.5).

`SCORERS` holds the seven in SPEC order, keyed by dimension; `score_dimensions` runs them all. The
engine and the score intervals (SPEC §17.3) both score through this one list."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from matcher.config import EngineConfig
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, DimensionScore, JobPosting
from matcher.score.dimensions.domain import score_domain
from matcher.score.dimensions.education import score_education
from matcher.score.dimensions.experience import score_experience
from matcher.score.dimensions.preferred_skills import score_preferred_skills
from matcher.score.dimensions.required_skills import score_required_skills
from matcher.score.dimensions.seniority import score_seniority
from matcher.score.dimensions.tools import score_tools
from matcher.score.prepare import PreparedResume

__all__ = [
    "SCORERS",
    "Scorer",
    "score_dimensions",
    "score_domain",
    "score_education",
    "score_experience",
    "score_preferred_skills",
    "score_required_skills",
    "score_seniority",
    "score_tools",
]

Scorer = Callable[[PreparedResume, JobPosting, SkillGraph, EngineConfig], DimensionScore]

SCORERS: Mapping[DimensionName, Scorer] = MappingProxyType({
    DimensionName.REQUIRED_SKILLS: score_required_skills,
    DimensionName.PREFERRED_SKILLS: score_preferred_skills,
    DimensionName.EXPERIENCE: score_experience,
    DimensionName.SENIORITY: score_seniority,
    DimensionName.DOMAIN: score_domain,
    DimensionName.EDUCATION: score_education,
    DimensionName.TOOLS: score_tools,
})


def score_dimensions(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig
) -> tuple[DimensionScore, ...]:
    """The seven dimension scores of one pair, in SPEC order (DimensionName order)."""
    return tuple(scorer(prepared, posting, graph, config) for scorer in SCORERS.values())
