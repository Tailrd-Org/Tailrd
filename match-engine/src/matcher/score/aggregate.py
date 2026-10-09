"""Aggregation: the weighted sum, redistribution and the critical-miss cap.

SPEC §6, §14.5, §5.1, §13; DECISIONS D-42, D-43, D-46.

`raw = Σ(effective_weight × value)` over the dimensions that scored, then the critical-miss cap,
then a clamp to [0, 1]:

* Redistribution (§14.5, D-42), implemented here once. A dimension that abstains ("we don't know")
  gives its weight to the dimensions that scored, in proportion to their configured weights:
  `effective_weight = w / Σ w over the dimensions that scored`. A dimension that scored 0.0 ("they
  have none") keeps its weight. Special case: when §5.1 abstains because the posting lists no
  REQUIRED requirements (NO_REQUIRED_REQUIREMENTS) and §5.2 scored, §5.1's whole weight goes to §5.2
  first (`redistribution.required_to_preferred_when_no_required`); every other abstention, §5.1's
  for any other reason included, redistributes proportionally.
* The weights are the configured ones (`config.weights`). Dimensions report values; the aggregator
  owns the weighting, so the same dimension values can be re-aggregated under other weights
  (sensitivity runs, tuning). `weight` keeps the configured weight, `effective_weight` the
  redistributed one; an abstaining dimension has none and contributes nothing.
* Critical-miss cap (§5.1, D-43): the required dimension's CRITICAL_MISS facts (one per critical
  REQUIRED group, Task 3.2) cap the final raw score: one at `critical_miss.cap_one` (0.70), two or
  more at `cap_two_or_more` (0.55). A cap, never a subtraction: it never raises a lower score and
  never drives one below 0. It is recorded as a `CapApplied` whenever a critical miss puts it in
  force, also when the score is already below it, since it still bounds what the rest of the résumé
  can reach. An abstaining required dimension never caps, and §5.2 / §5.7 never do.
* Nothing scored → None (the engine's NEEDS_REVIEW: NOTHING_SCORABLE, D-42).

Inputs are checked: exactly one `DimensionScore` per configured dimension, anything else is an
engine defect (`EngineFault(INTERNAL_ERROR)`); a NaN or an infinity, or no weight to share, raises
`EngineFault(NUMERIC_FAULT)`, never a silent 0 (§13, D-46). Sums use `math.fsum`; effective weights,
contributions and the result are clamped, so float noise never takes one past 1.

Reads dimension scores and the config only, never a résumé field (SPEC §9). A dimension's key is
read through the model's field mapping: `score/` never reads a `.name` attribute (the SPEC §9
forbidden-features scan), and a dimension's name is not a person's.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Optional

from pydantic import Field, field_validator

from matcher.config import EngineConfig
from matcher.errors import EngineFault, Flag
from matcher.models import CapApplied, DimensionName, DimensionScore, Frozen
from matcher.score.numeric import clamp01, finite
from matcher.score.prepare import ReadOnlyDict

__all__ = ["Aggregate", "aggregate"]

NO_WEIGHT = 1e-12  # the scored dimensions' weight at or below this: nothing to share (SPEC §13)
_CRITICAL_MISS = Flag.CRITICAL_MISS.value  # the fact code §5.1 emits per critical group
_REQUIRED = DimensionName.REQUIRED_SKILLS
_PREFERRED = DimensionName.PREFERRED_SKILLS


class Aggregate(Frozen):
    """The weighted result of one pair, before calibration."""

    raw: float = Field(ge=0.0, le=1.0)  # after the critical-miss cap and the clamp
    dimensions: dict[str, DimensionScore]  # every dimension by name, in SPEC order; read-only
    caps: tuple[CapApplied, ...] = ()
    # The configured weight the scoring dimensions carry, §14.5's transfer included (1.0 when nothing
    # abstained): the renormalisation denominator, i.e. the share of the weight that actually scored.
    scored_weight: float = Field(gt=0.0, le=1.0)

    @field_validator("dimensions", mode="after")
    @classmethod
    def _read_only(cls, value: dict) -> ReadOnlyDict:
        return ReadOnlyDict(value)


def aggregate(dimensions: Sequence[DimensionScore], config: EngineConfig) -> Optional[Aggregate]:
    """The weighted raw score of the seven dimensions; None when none of them scored."""
    by_name = _by_name(dimensions)
    scoring = [name for name in DimensionName if not by_name[name].abstained]
    if not scoring:
        return None
    base = _base_weights(by_name, config)
    total = finite(math.fsum(base[name] for name in scoring))
    if total <= NO_WEIGHT:
        raise EngineFault(Flag.NUMERIC_FAULT, "the dimensions that scored carry no weight")
    weighted: dict[str, DimensionScore] = {}
    for name in DimensionName:
        score = by_name[name]
        weight = clamp01(config.weights.of(name))
        if score.abstained:
            weighted[name.value] = DimensionScore(
                name=name, value=None, weight=weight, effective_weight=0.0, contribution=0.0, abstained=True,
                abstain_reason=score.abstain_reason, facts=score.facts,
            )
            continue
        value = clamp01(score.value)
        effective = clamp01(base[name] / total)
        weighted[name.value] = DimensionScore(
            name=name, value=value, weight=weight, effective_weight=effective,
            contribution=clamp01(effective * value), abstained=False, facts=score.facts,
        )
    raw = clamp01(math.fsum(weighted[name.value].contribution for name in scoring))
    caps = _critical_miss_cap(weighted[_REQUIRED.value], config)
    for cap in caps:
        raw = min(raw, cap.cap)  # a cap, never a subtraction (SPEC §5.1)
    return Aggregate(raw=clamp01(raw), dimensions=weighted, caps=caps, scored_weight=clamp01(total))


def _by_name(dimensions: Sequence[DimensionScore]) -> dict[DimensionName, DimensionScore]:
    """Exactly one score per configured dimension, keyed by name."""
    found: dict[DimensionName, DimensionScore] = {}
    for score in dimensions:
        if not isinstance(score, DimensionScore):
            raise EngineFault(Flag.INTERNAL_ERROR, f"not a DimensionScore: {type(score).__name__}")
        try:
            key = DimensionName(dict(score)["name"])
        except (KeyError, ValueError) as exc:
            raise EngineFault(Flag.INTERNAL_ERROR, "a dimension score without a known dimension") from exc
        if key in found:
            raise EngineFault(Flag.INTERNAL_ERROR, f"dimension {key.value} given twice")
        found[key] = score
    missing = [name.value for name in DimensionName if name not in found]
    if missing:
        raise EngineFault(Flag.INTERNAL_ERROR, f"dimensions missing: {missing}")
    return found


def _base_weights(by_name: Mapping[DimensionName, DimensionScore], config: EngineConfig) -> dict[DimensionName, float]:
    """The configured weights, with §14.5's transfer of the required weight to §5.2 when the posting
    lists no REQUIRED requirement and §5.2 scored (D-42)."""
    base = {name: finite(config.weights.of(name)) for name in DimensionName}
    required, preferred = by_name[_REQUIRED], by_name[_PREFERRED]
    if (
        config.scoring.redistribution.required_to_preferred_when_no_required
        and required.abstained
        and required.abstain_reason == Flag.NO_REQUIRED_REQUIREMENTS
        and not preferred.abstained
    ):
        base[_PREFERRED] = finite(math.fsum((base[_PREFERRED], base[_REQUIRED])))
        base[_REQUIRED] = 0.0
    return base


def _critical_miss_cap(required: DimensionScore, config: EngineConfig) -> tuple[CapApplied, ...]:
    """The cap in force: none, or one `CapApplied` naming the critical requirements (SPEC §5.1, D-43)."""
    if required.abstained:
        return ()  # an abstaining dimension carries no facts and caps nothing
    misses = [fact for fact in required.facts if fact.code == _CRITICAL_MISS]
    if not misses:
        return ()
    rules = config.scoring.critical_miss
    cap = clamp01(rules.cap_one if len(misses) == 1 else rules.cap_two_or_more)
    ids = tuple(fact.subject for fact in misses if fact.subject)
    return (CapApplied(reason=Flag.CRITICAL_MISS, cap=cap, requirement_ids=ids),)
