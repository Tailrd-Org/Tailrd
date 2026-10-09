"""The dimension scorers (SPEC §5). Each takes a prepared résumé, the posting, the graph and the
config, and returns a `DimensionScore` with its configured weight as the effective weight; the
aggregator redistributes the weight of abstaining dimensions (SPEC §14.5)."""

from __future__ import annotations

from matcher.score.dimensions.domain import score_domain
from matcher.score.dimensions.education import score_education
from matcher.score.dimensions.experience import score_experience
from matcher.score.dimensions.preferred_skills import score_preferred_skills
from matcher.score.dimensions.required_skills import score_required_skills
from matcher.score.dimensions.seniority import score_seniority
from matcher.score.dimensions.tools import score_tools

__all__ = [
    "score_domain",
    "score_education",
    "score_experience",
    "score_preferred_skills",
    "score_required_skills",
    "score_seniority",
    "score_tools",
]
