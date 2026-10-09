"""What every dimension shares, defined once (SPEC §5, §13, §14.5, §16; DECISIONS D-21, D-23, D-24).

* `KIND_ORDER`: the order equal relation credits keep: EXACT, CHILD, SIBLING, PARENT, then NONE.
* `is_provisional`: whether a node is provisional (§16); a provisional node discounts every
  relation it takes part in (D-24).
* `requirement_idf`: a requirement's IDF; `idf.unseen_default` for an id the corpus never saw or the
  graph does not hold (D-21).
* `abstain` / `scored`: a dimension's `DimensionScore`. The configured weight is also the effective
  weight, which the aggregator redistributes (§14.5); an abstaining dimension carries no weight, no
  contribution and no facts; a scored value is clamped to [0, 1] (§13).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import MappingProxyType

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, DimensionScore, Fact, RelationKind
from matcher.score.numeric import clamp01

__all__ = ["KIND_ORDER", "abstain", "is_provisional", "requirement_idf", "scored"]

KIND_ORDER: Mapping[RelationKind, int] = MappingProxyType({
    RelationKind.EXACT: 0,
    RelationKind.CHILD: 1,
    RelationKind.SIBLING: 2,
    RelationKind.PARENT: 3,
    RelationKind.NONE: 4,
})


def is_provisional(graph: SkillGraph, skill_id: str) -> bool:
    node = graph.node(skill_id)
    return node is not None and node.provisional


def requirement_idf(graph: SkillGraph, skill_id: str, config: EngineConfig) -> float:
    unseen = config.scoring.idf.unseen_default
    if skill_id not in graph:
        return clamp01(unseen)
    return clamp01(graph.idf_of(skill_id, unseen))


def abstain(name: DimensionName, config: EngineConfig, reason: Flag) -> DimensionScore:
    return DimensionScore(
        name=name,
        value=None,
        weight=config.weights.of(name),
        effective_weight=0.0,
        contribution=0.0,
        abstained=True,
        abstain_reason=reason,
    )


def scored(name: DimensionName, config: EngineConfig, value: float, facts: Iterable[Fact] = ()) -> DimensionScore:
    weight = config.weights.of(name)
    value = clamp01(value)
    return DimensionScore(
        name=name,
        value=value,
        weight=weight,
        effective_weight=weight,
        contribution=clamp01(weight * value),
        abstained=False,
        facts=tuple(facts),
    )
