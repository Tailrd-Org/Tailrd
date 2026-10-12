"""The skill gaps of a scored pair (SPEC §8 `gaps`; Task 4.1).

`find_gaps` reads the requirement facts the coverage dimensions recorded (`REQ_MATCH`,
`REQ_PARTIAL`, `REQ_MISS`, one per requirement group; score/coverage.py), never re-scores:

* A gap is a REQUIRED or PREFERRED group with `m < 0.5` (the line between REQ_MATCH and the rest).
  IMPLIED groups (§5.7, secondary keywords) are not listed as gaps: missing a keyword the posting
  only mentions is normal, and the counterfactual search still considers them.
* `impact = −(effective weight of its dimension × idf×wm / Σ(idf×wm) × (1 − m))`: the share of the
  raw score the group leaves on the table, Σ over that dimension's groups. A dimension's value is
  `Σ idf×wm×m / Σ idf×wm`, so the impacts of a dimension's gaps sum to what its gaps cost it.
* `covered_by`: the résumé skills (affirmed or aspirational, value above zero) related to a member
  of the group through the graph: a child (the résumé skill is a kind of it), a related skill, or a
  parent within `relations.max_depth`, never a member itself. Ordered child, related, parent, then
  the stronger mention, then the id.
* A critical miss (D-43) is the group's own gap; whether its cap bound is `MatchResult.caps`, which
  `breakdown` reads before it ever says the score was limited.

Ordered by impact (most negative first), then REQUIRED before PREFERRED, then the id. Pure.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Optional

from pydantic import Field

from matcher.config import EngineConfig
from matcher.graph.model import SkillGraph
from matcher.graph.traversal import ancestors, related_neighbors
from matcher.models import DimensionName, Fact, Frozen, JobPosting, MatchResult, Necessity, RelationKind
from matcher.score.coverage import MATCH_FROM, NO_WEIGHT, REQ_MATCH, REQ_MISS, REQ_PARTIAL
from matcher.score.prepare import PreparedResume

__all__ = ["Gap", "MAX_COVERED_BY", "find_gaps", "group_facts"]

MAX_COVERED_BY = 5
REQUIREMENT_CODES = frozenset({REQ_MATCH, REQ_PARTIAL, REQ_MISS})
_COVERAGE = {DimensionName.REQUIRED_SKILLS: Necessity.REQUIRED, DimensionName.PREFERRED_SKILLS: Necessity.PREFERRED}
_NECESSITY_ORDER = {Necessity.REQUIRED: 0, Necessity.PREFERRED: 1, Necessity.IMPLIED: 2}
_RELATION_ORDER = {RelationKind.CHILD: 0, RelationKind.SIBLING: 1, RelationKind.PARENT: 2}


class Gap(Frozen):
    """One requirement group the résumé covers less than halfway."""

    requirement_id: str = Field(min_length=1, max_length=200)  # the group's best member
    necessity: Necessity
    m: float = Field(ge=0.0, le=1.0)
    impact: float = Field(le=0.0)  # raw scale, never positive
    covered_by: tuple[str, ...] = ()
    members: tuple[str, ...] = ()  # every skill of the group (an OR-group has several)
    via: Optional[str] = None  # the résumé skill that earned the partial credit, if any
    relation: RelationKind = RelationKind.NONE


def group_facts(facts: Sequence[Fact]) -> list[Fact]:
    """The requirement-group facts of a coverage dimension, in their recorded order."""
    return [fact for fact in facts if fact.code in REQUIREMENT_CODES]


def _members(fact: Fact) -> tuple[str, ...]:
    raw = fact.detail.get("members")
    members = tuple(part for part in str(raw).split(",") if part) if isinstance(raw, str) else ()
    if fact.subject and fact.subject not in members:
        members = (fact.subject, *members)
    return members or ((fact.subject,) if fact.subject else ())


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    return number if math.isfinite(number) else default


def _relation(fact: Fact) -> RelationKind:
    try:
        return RelationKind(str(fact.detail.get("relation", RelationKind.NONE.value)))
    except ValueError:
        return RelationKind.NONE


def covering_skills(
    prepared: Optional[PreparedResume], members: Sequence[str], graph: SkillGraph, config: EngineConfig
) -> tuple[str, ...]:
    """The résumé skills related to any of `members` (child, related, parent), strongest first."""
    if prepared is None:
        return ()
    wanted = set(members)
    depth = config.scoring.relations.max_depth
    found: dict[str, tuple[int, float]] = {}

    def offer(skill_id: str, kind: RelationKind) -> None:
        held = prepared.best.get(skill_id)
        if held is None or held.value <= 0.0 or skill_id in wanted:
            return
        rank = (_RELATION_ORDER[kind], -held.value)
        if skill_id not in found or rank < found[skill_id]:
            found[skill_id] = rank

    for skill_id in prepared.best:
        if wanted & set(ancestors(graph, skill_id, depth)):
            offer(skill_id, RelationKind.CHILD)
    for member in sorted(wanted):
        for neighbour in related_neighbors(graph, member):
            offer(neighbour, RelationKind.SIBLING)
        for ancestor in ancestors(graph, member, depth):
            offer(ancestor, RelationKind.PARENT)
    ordered = sorted(found, key=lambda skill_id: (*found[skill_id], skill_id))
    return tuple(ordered[:MAX_COVERED_BY])


def find_gaps(
    result: MatchResult,
    posting: Optional[JobPosting],
    prepared: Optional[PreparedResume],
    graph: SkillGraph,
    config: EngineConfig,
) -> tuple[Gap, ...]:
    """Every REQUIRED and PREFERRED group with m < 0.5, with its impact (see the module docstring).
    `posting` is accepted for the interface and not read: the facts carry every group."""
    del posting
    gaps: list[Gap] = []
    for name, necessity in _COVERAGE.items():
        dimension = result.dimensions.get(name.value)
        if dimension is None or dimension.abstained:
            continue
        groups = group_facts(dimension.facts)
        weights = [_number(fact.detail.get("idf")) * _number(fact.detail.get("weight_multiplier"), 1.0)
                   for fact in groups]
        total = math.fsum(weights)
        for fact, weight in zip(groups, weights):
            m = min(1.0, max(0.0, _number(fact.value)))
            if m >= MATCH_FROM or not fact.subject:
                continue
            share = weight / total if total > NO_WEIGHT else 0.0
            impact = -(dimension.effective_weight * share * (1.0 - m))
            members = _members(fact)
            via = fact.detail.get("via")
            gaps.append(
                Gap(
                    requirement_id=fact.subject,
                    necessity=necessity,
                    m=m,
                    impact=min(0.0, impact),
                    covered_by=covering_skills(prepared, members, graph, config),
                    members=members,
                    via=via if isinstance(via, str) and via else None,
                    relation=_relation(fact),
                )
            )
    gaps.sort(key=lambda gap: (round(gap.impact, 12), _NECESSITY_ORDER[gap.necessity], gap.requirement_id))
    return tuple(gaps)
