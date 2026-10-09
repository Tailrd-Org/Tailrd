"""Confidence: how much the engine knows about one pair, and what it shows because of it.

SPEC §17.1, §17.2, §13, §14.5; DECISIONS D-42, D-44.

    confidence = exp(Σ wᵢ ln max(cᵢ, component_floor) / Σ wᵢ)

the weighted geometric mean of the components defined for the pair (§17.1: "one catastrophically
bad input should drag the result down rather than being averaged away by five good ones"). A
component that is undefined for the pair is left out and the weights renormalised over the rest
(D-44), so "zero overlap" stays a valid Limited score (§14.5). Weights, the floor (1e-6) and every
threshold are `scoring.confidence` in config. The components, in this order (`COMPONENTS`):

* parse_quality: the résumé's parse tier (A 1.0, B 0.8, C 0.55).
* taxonomy_coverage: `(requirement_token_count − len(unresolved tokens)) / requirement_token_count`,
  clamped to [0, 1]; undefined when the posting counted no requirement tokens.
* evidence_density: the share of matched requirement groups (any credit: `via` is set) whose
  matching résumé skill (`via`: the exact skill, or the child, sibling or parent that earned the
  credit) has its best mention beyond the skills list (evidence other than SKILLS_BLOB); undefined
  when nothing matched. A negated-only skill is worth 0 and matches nothing.
* temporal_completeness: dated roles (a start, and an end or current) over roles; undefined
  without roles.
* requirement_count: `requirement_count_factor` of the number of distinct requirement ids.
* dimension_coverage: `Aggregate.scored_weight`, the configured weight of the dimensions that
  scored, where the §5.1 weight moved to §5.2 for an all-PREFERRED posting counts as scored (D-42).

Components are reported as measured (clamped to [0, 1]); the floor applies inside the logarithm
only. `reasons` name every component below `weak_component` (0.70), in component order. When the
value misses `scored_threshold` and no component is below `weak_component`, the weakest defined
component (every one tied with it) is named instead: a result that suppresses its percentage always
says why, specifically (§17.2, §13).

`outcome_for`: at or above `scored_threshold` (0.80) SCORED, at or above `low_confidence_threshold`
(0.60) SCORED_LOW_CONFIDENCE, else NEEDS_REVIEW (§17.2). Thresholds are compared at
`numeric.TIE_DECIMALS` places, so a geometric mean that lands on a threshold up to float noise is
on it, not one ulp below.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Optional

from matcher.config import EngineConfig
from matcher.errors import EngineFault, Flag
from matcher.models import Confidence, Evidence, JobPosting, Outcome, ParseTier, Resume
from matcher.score.aggregate import Aggregate
from matcher.score.coverage import RequirementMatch
from matcher.score.numeric import clamp01, finite, tie_key
from matcher.score.prepare import PreparedResume

__all__ = [
    "COMPONENTS",
    "LOW_FLAGS",
    "compute_confidence",
    "confidence_components",
    "confidence_from_components",
    "outcome_for",
    "requirement_count_factor",
]

COMPONENTS: tuple[str, ...] = (
    "parse_quality",
    "taxonomy_coverage",
    "evidence_density",
    "temporal_completeness",
    "requirement_count",
    "dimension_coverage",
)

# The reason each component gives when it is weak (SPEC §17.2: "the specific reason").
LOW_FLAGS: Mapping[str, Flag] = MappingProxyType({
    "parse_quality": Flag.LOW_PARSE_QUALITY,
    "taxonomy_coverage": Flag.LOW_TAXONOMY_COVERAGE,
    "evidence_density": Flag.LOW_EVIDENCE_DENSITY,
    "temporal_completeness": Flag.LOW_TEMPORAL_COMPLETENESS,
    "requirement_count": Flag.FEW_REQUIREMENTS,
    "dimension_coverage": Flag.LOW_DIMENSION_COVERAGE,
})

NO_WEIGHT = 1e-12  # the defined components' weight at or below this: nothing to average (SPEC §13)


def requirement_count_factor(n: int, config: EngineConfig) -> float:
    """The saturating `requirement_count_curve` at `n` (§17.1: 1 → 0.40, 5 → 0.85, 10+ → 1.0):
    linear between its points, clamped to the end points outside them. A point's own `n` takes that
    point's value exactly (the segment starting there is used), never `y0 + (y1 − y0) × 1`."""
    x = finite(n)
    points = config.scoring.confidence.requirement_count_curve
    first_x, first_y = points[0]
    last_x, last_y = points[-1]
    if x <= first_x:
        return clamp01(first_y)
    if x >= last_x:
        return clamp01(last_y)
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x0 <= x < x1:  # x1 > x0: the config refuses a curve that is not strictly increasing
            y = y0 + (y1 - y0) * ((x - x0) / (x1 - x0))
            return clamp01(min(max(y, min(y0, y1)), max(y0, y1)))
    raise EngineFault(Flag.INTERNAL_ERROR, f"requirement count {n!r} fell outside the curve")


def _bound(resume: Resume, prepared: PreparedResume, config: EngineConfig) -> None:
    """The prepared résumé is this résumé, prepared under this config (as `prepare.ensure_bound`
    checks the graph and config wherever a prepared résumé is read)."""
    if prepared.resume is not resume and prepared.resume != resume:
        raise EngineFault(Flag.INTERNAL_ERROR, "confidence asked about a résumé with another's preparation")
    if prepared.config_hash != config.content_hash:
        raise EngineFault(
            Flag.INTERNAL_ERROR,
            f"résumé prepared for config {prepared.config_hash!r}, used with config {config.content_hash!r}",
        )


def _evidence_density(prepared: PreparedResume, matches: Sequence[RequirementMatch]) -> Optional[float]:
    matched = [match.via for match in matches if match.via is not None]
    if not matched:
        return None
    shown = 0
    for via in matched:
        best = prepared.best.get(via)
        if best is None:
            raise EngineFault(Flag.INTERNAL_ERROR, f"a match credits {via!r}, which the résumé does not hold")
        if Evidence(best.evidence) is not Evidence.SKILLS_BLOB:
            shown += 1
    return clamp01(shown / len(matched))


def confidence_components(
    resume: Resume,
    posting: JobPosting,
    prepared: PreparedResume,
    aggregated: Aggregate,
    matches: Sequence[RequirementMatch],
    config: EngineConfig,
) -> dict[str, Optional[float]]:
    """The six §17.1 components of one pair, in `COMPONENTS` order; None = undefined (D-44)."""
    _bound(resume, prepared, config)
    rules = config.scoring.confidence
    count = posting.requirement_token_count
    taxonomy = None
    if count > 0:
        taxonomy = clamp01((count - len(posting.unresolved_requirement_tokens)) / count)
    roles = resume.roles
    temporal = clamp01(sum(1 for role in roles if role.dated) / len(roles)) if roles else None
    distinct = len({requirement.skill_id for requirement in posting.requirements})
    return {
        "parse_quality": clamp01(rules.parse_quality[ParseTier(resume.parse_tier).value]),
        "taxonomy_coverage": taxonomy,
        "evidence_density": _evidence_density(prepared, matches),
        "temporal_completeness": temporal,
        "requirement_count": requirement_count_factor(distinct, config),
        "dimension_coverage": clamp01(aggregated.scored_weight),
    }


def _below(value: float, limit: float) -> bool:
    """`value < limit`, decided at tie precision (no float noise on a threshold, SPEC §13)."""
    return tie_key(value) < tie_key(limit)


def confidence_from_components(components: Mapping[str, Optional[float]], config: EngineConfig) -> Confidence:
    """The weighted geometric mean of the defined components and the reasons (§17.1, §17.2, D-44)."""
    if set(components) != set(COMPONENTS):
        raise EngineFault(Flag.INTERNAL_ERROR, f"confidence components must be exactly {list(COMPONENTS)}")
    rules = config.scoring.confidence
    measured = {name: None if components[name] is None else clamp01(components[name]) for name in COMPONENTS}
    defined = [(name, value) for name, value in measured.items() if value is not None]
    weights = [finite(rules.weights[name]) for name, _ in defined]
    total = finite(math.fsum(weights))
    if total <= NO_WEIGHT:
        raise EngineFault(Flag.NUMERIC_FAULT, "no confidence component is defined")
    floor = finite(rules.component_floor)
    terms = (weight * math.log(max(component, floor)) for weight, (_, component) in zip(weights, defined))
    logs = finite(math.fsum(terms))
    value = clamp01(math.exp(logs / total))
    reasons = [LOW_FLAGS[name] for name, component in defined if _below(component, rules.weak_component)]
    if not reasons and _below(value, rules.scored_threshold):
        weakest = min(component for _, component in defined)
        reasons = [LOW_FLAGS[name] for name, component in defined
                   if math.isclose(component, weakest, rel_tol=0.0, abs_tol=1e-12)]
    return Confidence(value=value, components=measured, reasons=tuple(reasons))


def compute_confidence(
    resume: Resume,
    posting: JobPosting,
    prepared: PreparedResume,
    aggregated: Aggregate,
    matches: Sequence[RequirementMatch],
    config: EngineConfig,
) -> Confidence:
    """SPEC §17.1 confidence of one scored pair. `aggregated` is the pair's `Aggregate` (its
    `scored_weight` is the dimension coverage); `matches` are its requirement groups
    (`coverage.match_requirements` over all of the posting's requirements)."""
    components = confidence_components(resume, posting, prepared, aggregated, matches, config)
    return confidence_from_components(components, config)


def outcome_for(confidence: Confidence, config: EngineConfig) -> Outcome:
    """SCORED at or above `scored_threshold`, SCORED_LOW_CONFIDENCE at or above
    `low_confidence_threshold`, else NEEDS_REVIEW (SPEC §17.2)."""
    rules = config.scoring.confidence
    value = clamp01(confidence.value)
    if not _below(value, rules.scored_threshold):
        return Outcome.SCORED
    if not _below(value, rules.low_confidence_threshold):
        return Outcome.SCORED_LOW_CONFIDENCE
    return Outcome.NEEDS_REVIEW
