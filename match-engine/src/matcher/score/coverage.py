"""Skill coverage: requirement matching and the coverage value of §5.1, §5.2 and §5.7.

SPEC §5.1, §5.2, §5.7, §13, §14.4; DECISIONS D-21, D-23, D-24, D-30, D-31, D-43.

* `match_skill` (D-23, D-24): a requirement's match quality `m` is the best credit any résumé
  skill earns for it: EXACT `relations.exact × value`, CHILD the prepared child cover, SIBLING
  `clamp(edge, sibling_min, sibling_max) × value` over related edges stored either way, PARENT
  `relations.parent × value` for each ancestor within `relations.max_depth`; × the provisional
  multiplier when the requirement or the résumé skill is a provisional node (once; the child
  cover already carries it). Equal credits keep the order EXACT, CHILD, SIBLING, PARENT, then the
  smaller résumé skill id. A requirement absent from the graph matches its exact id only. No
  credit at all is `(0.0, NONE, None)`; `via` is the résumé skill that earned the credit (the
  requirement itself for EXACT).
* `match_requirements` (D-31): requirements sharing an `or_group` form one group (otherwise each
  skill id is its own group, so a duplicated requirement counts once). A group scores its best
  member; its IDF is the max member IDF (DECISIONS D-21: `idf.unseen_default` for an id the
  corpus never saw or the graph does not hold); its weight multiplier is the max member's
  (§14.4 overflow); its necessity the strongest member's. A REQUIRED group with IDF above
  `critical_miss.idf_threshold` and `m` below `critical_miss.m_threshold` is critical (D-43; the
  aggregator applies the cap).
* `coverage_value`: `Σ(idf × wm × m) / Σ(idf × wm)`, None when no group carries weight.
* `score_coverage` builds the dimension: §5.1 over REQUIRED, §5.2 over PREFERRED, §5.7 over
  IMPLIED requirements (D-30). It abstains when the posting has none of that necessity or when
  none carries weight; an abstaining dimension carries no facts, so no critical-miss cap.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Optional

from pydantic import Field

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.graph.traversal import ancestors
from matcher.models import (
    DimensionName,
    DimensionScore,
    Fact,
    Frozen,
    JobPosting,
    JobRequirement,
    Necessity,
    RelationKind,
)
from matcher.score.numeric import clamp01, finite
from matcher.score.prepare import PreparedResume

__all__ = [
    "CRITICAL_MISS",
    "REQ_MATCH",
    "REQ_MISS",
    "REQ_PARTIAL",
    "RequirementMatch",
    "coverage_value",
    "match_requirements",
    "match_skill",
    "requirement_facts",
    "score_coverage",
]

# Fact codes (SPEC §5: "the specific facts that produced it").
REQ_MATCH = "REQ_MATCH"  # m >= MATCH_FROM
REQ_PARTIAL = "REQ_PARTIAL"  # 0 < m < MATCH_FROM
REQ_MISS = "REQ_MISS"  # m < NO_CREDIT
CRITICAL_MISS = Flag.CRITICAL_MISS.value

MATCH_FROM = 0.5
NO_CREDIT = 1e-12  # a credit below this is no match at all
NO_WEIGHT = 1e-12  # Σ(idf × wm) at or below this: nothing to divide by (SPEC §13)

_KIND_ORDER = {
    RelationKind.EXACT: 0,
    RelationKind.CHILD: 1,
    RelationKind.SIBLING: 2,
    RelationKind.PARENT: 3,
    RelationKind.NONE: 4,
}
_NECESSITY_ORDER = {Necessity.REQUIRED: 0, Necessity.PREFERRED: 1, Necessity.IMPLIED: 2}
_NO_MATCH: tuple[float, RelationKind, Optional[str]] = (0.0, RelationKind.NONE, None)


class RequirementMatch(Frozen):
    """One requirement group scored against a prepared résumé."""

    group: str = Field(max_length=200)  # the or_group, or the skill id of a lone requirement
    members: tuple[str, ...] = Field(min_length=1)  # sorted, distinct
    best_member: str = Field(min_length=1, max_length=200)
    m: float = Field(ge=0.0, le=1.0)
    relation: RelationKind
    via: Optional[str]
    idf: float = Field(ge=0.0, le=1.0)
    weight_multiplier: float = Field(ge=0.0, le=1.0)
    necessity: Necessity
    critical: bool


def _provisional(graph: SkillGraph, skill_id: str) -> bool:
    node = graph.node(skill_id)
    return node is not None and node.provisional


def match_skill(
    prepared: PreparedResume, requirement_id: str, graph: SkillGraph, config: EngineConfig
) -> tuple[float, RelationKind, Optional[str]]:
    """`(m, relation, via)` for one requirement id (SPEC §5.1 table; DECISIONS D-23, D-24)."""
    relations = config.scoring.relations
    best = prepared.best
    wanted_provisional = _provisional(graph, requirement_id)
    candidates: list[tuple[float, RelationKind, str]] = []  # (credit, relation, via)

    def offer(credit: float, kind: RelationKind, via: str) -> None:
        if wanted_provisional or _provisional(graph, via):
            credit *= relations.provisional_multiplier
        candidates.append((clamp01(credit), kind, via))

    exact = best.get(requirement_id)
    if exact is not None:
        offer(relations.exact * exact.value, RelationKind.EXACT, requirement_id)
    if requirement_id in graph:
        cover = prepared.child_cover.get(requirement_id)
        if cover is not None:  # already × the provisional multiplier where it applies
            candidates.append((clamp01(cover[0]), RelationKind.CHILD, cover[1]))
        for skill_id, value in best.items():
            if skill_id == requirement_id:
                continue
            weight = graph.related_weight(skill_id, requirement_id)
            if weight is not None:
                clamped = min(relations.sibling_max, max(relations.sibling_min, finite(weight)))
                offer(clamped * value.value, RelationKind.SIBLING, skill_id)
        for ancestor in ancestors(graph, requirement_id, relations.max_depth):
            value = best.get(ancestor)
            if value is not None:
                offer(relations.parent * value.value, RelationKind.PARENT, ancestor)
    if not candidates:
        return _NO_MATCH
    # Highest credit; equal credits by relation order, then the smaller via id.
    credit, kind, via = min(candidates, key=lambda c: (-c[0], _KIND_ORDER[c[1]], c[2]))
    if credit < NO_CREDIT:
        return _NO_MATCH
    return credit, kind, via


def _idf(graph: SkillGraph, skill_id: str, config: EngineConfig) -> float:
    unseen = config.scoring.idf.unseen_default
    if skill_id not in graph:
        return clamp01(unseen)
    return clamp01(graph.idf_of(skill_id, unseen))


def match_requirements(
    prepared: PreparedResume,
    requirements: Sequence[JobRequirement],
    graph: SkillGraph,
    config: EngineConfig,
) -> tuple[RequirementMatch, ...]:
    """Every requirement group scored, sorted by group key (SPEC §14.4; DECISIONS D-31, D-43)."""
    groups: dict[str, list[JobRequirement]] = {}
    for requirement in requirements:
        key = requirement.or_group if requirement.or_group is not None else requirement.skill_id
        groups.setdefault(key, []).append(requirement)
    critical_miss = config.scoring.critical_miss
    matches = []
    for key in sorted(groups):
        group = groups[key]
        members = tuple(sorted({requirement.skill_id for requirement in group}))
        found = {member: match_skill(prepared, member, graph, config) for member in members}
        best_member = min(members, key=lambda member: (-found[member][0], _KIND_ORDER[found[member][1]], member))
        m, relation, via = found[best_member]
        idf = max(_idf(graph, member, config) for member in members)
        necessity = min((Necessity(r.necessity) for r in group), key=_NECESSITY_ORDER.__getitem__)
        matches.append(
            RequirementMatch(
                group=key,
                members=members,
                best_member=best_member,
                m=m,
                relation=relation,
                via=via,
                idf=idf,
                weight_multiplier=max(clamp01(requirement.weight_multiplier) for requirement in group),
                necessity=necessity,
                critical=(
                    necessity is Necessity.REQUIRED
                    and idf > critical_miss.idf_threshold
                    and m < critical_miss.m_threshold
                ),
            )
        )
    return tuple(matches)


def coverage_value(matches: Sequence[RequirementMatch]) -> Optional[float]:
    """`Σ(idf × wm × m) / Σ(idf × wm)` (SPEC §5.1 with the §14.4 weight multiplier); None when
    no group carries weight, never a division by zero (SPEC §13)."""
    weights = [finite(match.idf) * finite(match.weight_multiplier) for match in matches]
    numerator = finite(math.fsum(weight * finite(match.m) for weight, match in zip(weights, matches)))
    denominator = finite(math.fsum(weights))
    if denominator <= NO_WEIGHT:
        return None
    return clamp01(numerator / denominator)


def _fact_code(m: float) -> str:
    if m >= MATCH_FROM:
        return REQ_MATCH
    if m < NO_CREDIT:
        return REQ_MISS
    return REQ_PARTIAL


def requirement_facts(matches: Sequence[RequirementMatch]) -> tuple[Fact, ...]:
    """One fact per group in group order, then a CRITICAL_MISS fact per critical group."""
    facts = [
        Fact(
            code=_fact_code(match.m),
            subject=match.best_member,
            value=match.m,
            detail={
                "relation": RelationKind(match.relation).value,
                "via": match.via,
                "idf": match.idf,
                "weight_multiplier": match.weight_multiplier,
                "necessity": Necessity(match.necessity).value,
                "members": ",".join(match.members),
            },
        )
        for match in matches
    ]
    facts.extend(
        Fact(code=CRITICAL_MISS, subject=match.best_member, value=match.m, detail={"idf": match.idf})
        for match in matches
        if match.critical
    )
    return tuple(facts)


def _abstain(name: DimensionName, weight: float, reason: Flag) -> DimensionScore:
    return DimensionScore(
        name=name,
        value=None,
        weight=weight,
        effective_weight=0.0,
        contribution=0.0,
        abstained=True,
        abstain_reason=reason,
    )


def score_coverage(
    prepared: PreparedResume,
    posting: JobPosting,
    graph: SkillGraph,
    config: EngineConfig,
    *,
    name: DimensionName,
    necessity: Necessity,
    absent: Flag,
) -> DimensionScore:
    """The coverage dimension over the posting's requirements of one necessity (DECISIONS D-30).

    `effective_weight` is the configured weight; the aggregator redistributes (SPEC §14.5)."""
    weight = config.weights.of(name)
    requirements = [requirement for requirement in posting.requirements if requirement.necessity == necessity]
    if not requirements:
        return _abstain(name, weight, absent)
    matches = match_requirements(prepared, requirements, graph, config)
    value = coverage_value(matches)
    if value is None:
        return _abstain(name, weight, Flag.NO_REQUIREMENTS_RESOLVED)
    return DimensionScore(
        name=name,
        value=value,
        weight=weight,
        effective_weight=weight,
        contribution=clamp01(weight * value),
        abstained=False,
        facts=requirement_facts(matches),  # only REQUIRED groups can be critical (§5.2: no cap)
    )
