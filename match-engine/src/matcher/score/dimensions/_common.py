"""How the §5.3–§5.6 dimensions build their `DimensionScore`, the same way the coverage dimensions do
(`coverage.score_coverage`): the configured weight is also the effective weight, which the
aggregator redistributes (SPEC §14.5); an abstaining dimension carries no weight, no contribution
and no facts; a scored value is clamped to [0, 1] (SPEC §13)."""

from __future__ import annotations

from collections.abc import Iterable

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.models import DimensionName, DimensionScore, Fact
from matcher.score.coverage import _abstain
from matcher.score.numeric import clamp01

__all__ = ["abstain", "scored"]


def abstain(name: DimensionName, config: EngineConfig, reason: Flag) -> DimensionScore:
    return _abstain(name, config.weights.of(name), reason)


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
