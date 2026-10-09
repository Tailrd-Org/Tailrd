"""Aggregation and the critical-miss cap (SPEC §6, §5.1, §13; DECISIONS D-42, D-43, D-46).

raw = Σ(effective weight × value) over the dimensions that scored (weights redistributed as
tests/test_weight_redistribution.py pins), then the critical-miss cap from the required dimension's
CRITICAL_MISS facts: one caps the final raw score at 0.70, two or more at 0.55, as a cap (min), never
a subtraction. Nothing scored → None (the engine's NEEDS_REVIEW: NOTHING_SCORABLE). A NaN or an
infinity reaching the aggregator raises EngineFault(NUMERIC_FAULT); it is never coerced to 0.

Expected values are closed-form arithmetic over the SPEC §5 weights (EVAL §2.2).
"""

from __future__ import annotations

import math
import random

import pytest
from pydantic import ValidationError

from matcher.config import EngineConfig, Weights, replace_weights
from matcher.errors import EngineFault, Flag
from matcher.models import CapApplied, DimensionName, DimensionScore, Fact, canonical_json
from matcher.score.aggregate import Aggregate, aggregate
from tests.builders import SPEC_WEIGHTS, abstained_dimension, dimension, with_scoring

REQUIRED, PREFERRED = DimensionName.REQUIRED_SKILLS, DimensionName.PREFERRED_SKILLS
EXPERIENCE, DOMAIN, TOOLS = DimensionName.EXPERIENCE, DimensionName.DOMAIN, DimensionName.TOOLS
VALUES = {REQUIRED: 0.8, PREFERRED: 0.5, EXPERIENCE: 0.6, DimensionName.SENIORITY: 0.72, DOMAIN: 0.15,
          DimensionName.EDUCATION: 1.0, TOOLS: 0.4}
CAP_ONE, CAP_TWO = 0.70, 0.55  # SPEC §5.1
W = SPEC_WEIGHTS


def _close(a: float, b: float, tol: float = 1e-12) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


def _critical(skill_id: str, m: float = 0.0, idf: float = 0.9) -> Fact:
    """The fact §5.1 emits for a critical group (Task 3.2: subject = the group's best member)."""
    return Fact(code="CRITICAL_MISS", subject=skill_id, value=m, detail={"idf": idf})


def _all(value: float | None = None, *, required_facts=(), facts=None) -> list[DimensionScore]:
    """The seven dimensions scored: VALUES, or every one at `value`."""
    facts = facts or {}
    dims = []
    for name in DimensionName:
        extra = tuple(required_facts) if name is REQUIRED else tuple(facts.get(name, ()))
        dims.append(dimension(name, VALUES[name] if value is None else value, facts=extra))
    return dims


def test_all_seven_scored(config) -> None:
    facts = (Fact(code="REQ_MATCH", subject="skill.python", value=0.8),)
    agg = aggregate(_all(required_facts=facts), config)
    assert isinstance(agg, Aggregate)
    assert _close(agg.raw, 0.35 * 0.8 + 0.10 * 0.5 + 0.15 * 0.6 + 0.10 * 0.72 + 0.10 * 0.15 + 0.10 * 1.0 + 0.10 * 0.4)
    assert _close(agg.raw, 0.647)
    assert list(agg.dimensions) == [name.value for name in DimensionName]  # keyed by name, in SPEC order
    for name in DimensionName:
        dim = agg.dimensions[name.value]
        assert dim.name is name and not dim.abstained and dim.value == VALUES[name]
        assert _close(dim.weight, W[name]) and _close(dim.effective_weight, W[name])
        assert _close(dim.contribution, W[name] * VALUES[name])
    assert agg.dimensions[REQUIRED.value].facts == facts  # facts pass through untouched
    assert agg.caps == ()
    assert _close(agg.scored_weight, 1.0)


def test_the_order_dimensions_arrive_in_never_matters(config) -> None:
    dims = _all(required_facts=(_critical("skill.kubernetes"),))
    reference = canonical_json(aggregate(dims, config))
    rng = random.Random(7)
    for _ in range(10):
        rng.shuffle(dims)
        assert canonical_json(aggregate(dims, config)) == reference


def test_nothing_scored_is_none(config) -> None:
    dims = [abstained_dimension(name, Flag.NO_REQUIREMENTS_RESOLVED) for name in DimensionName]
    assert aggregate(dims, config) is None


def test_one_scored_dimension_carries_the_whole_weight(config) -> None:
    dims = [abstained_dimension(name, Flag.NO_REQUIREMENTS_RESOLVED) for name in DimensionName if name is not TOOLS]
    agg = aggregate([*dims, dimension(TOOLS, 0.4)], config)
    assert agg.dimensions[TOOLS.value].effective_weight == 1.0
    assert _close(agg.raw, 0.4) and _close(agg.scored_weight, 0.10)


def test_zero_overlap_is_a_valid_zero_not_nothing(config) -> None:
    # SPEC §14.5: zero skill overlap → raw = 0.0, a valid (Limited) result.
    agg = aggregate(_all(0.0), config)
    assert agg is not None and agg.raw == 0.0
    assert all(dim.contribution == 0.0 and not dim.abstained for dim in agg.dimensions.values())


def test_a_perfect_match_never_exceeds_one(config) -> None:
    agg = aggregate(_all(1.0), config)
    assert agg.raw <= 1.0 and _close(agg.raw, 1.0)
    assert all(dim.effective_weight <= 1.0 and dim.contribution <= 1.0 for dim in agg.dimensions.values())


# ─── Critical-miss cap (SPEC §5.1; D-43) ─────────────────────────────────────────────────


def test_one_critical_miss_caps_at_0_70(config) -> None:
    agg = aggregate(_all(0.95, required_facts=(_critical("skill.kubernetes"),)), config)
    assert _close(agg.raw, CAP_ONE)  # 0.95 → 0.70
    assert agg.caps == (CapApplied(reason=Flag.CRITICAL_MISS, cap=CAP_ONE, requirement_ids=("skill.kubernetes",)),)
    # The cap bounds the final score only: the dimensions keep their own numbers.
    assert _close(math.fsum(dim.contribution for dim in agg.dimensions.values()), 0.95)


def test_two_critical_misses_cap_at_0_55(config) -> None:
    facts = (_critical("skill.kubernetes"), _critical("skill.terraform", m=0.1))
    agg = aggregate(_all(0.95, required_facts=facts), config)
    assert _close(agg.raw, CAP_TWO)  # 0.95 → 0.55
    assert agg.caps == (CapApplied(reason=Flag.CRITICAL_MISS, cap=CAP_TWO,
                                   requirement_ids=("skill.kubernetes", "skill.terraform")),)


def test_more_than_two_critical_misses_still_cap_at_0_55(config) -> None:
    facts = tuple(_critical(f"skill.s{i}") for i in range(3))
    agg = aggregate(_all(0.95, required_facts=facts), config)
    assert _close(agg.raw, CAP_TWO)
    (cap,) = agg.caps
    assert cap.requirement_ids == ("skill.s0", "skill.s1", "skill.s2")


@pytest.mark.parametrize("misses,cap", [(1, CAP_ONE), (2, CAP_TWO)])
def test_the_cap_never_raises_a_lower_raw(config, misses: int, cap: float) -> None:
    facts = tuple(_critical(f"skill.s{i}") for i in range(misses))
    agg = aggregate(_all(0.40, required_facts=facts), config)
    assert _close(agg.raw, 0.40)  # min(0.40, cap)
    # The cap is still in force (it bounds what the rest of the résumé could reach) and recorded.
    assert agg.caps == (CapApplied(reason=Flag.CRITICAL_MISS, cap=cap,
                                   requirement_ids=tuple(f"skill.s{i}" for i in range(misses))),)


def test_only_the_required_dimensions_critical_misses_cap(config) -> None:
    # §5.2: "Same machinery, no critical-miss cap." A stray CRITICAL_MISS fact elsewhere is ignored.
    stray = {PREFERRED: (_critical("skill.vue"),), TOOLS: (_critical("skill.git"),)}
    agg = aggregate(_all(0.95, facts=stray), config)
    assert _close(agg.raw, 0.95) and agg.caps == ()


def test_an_abstaining_required_dimension_never_caps(config) -> None:
    dims = [dim for dim in _all(0.95) if dim.name is not REQUIRED]
    required = DimensionScore(name=REQUIRED, value=None, weight=0.35, effective_weight=0.0, contribution=0.0,
                              abstained=True, abstain_reason=Flag.NO_REQUIREMENTS_RESOLVED,
                              facts=(_critical("skill.kubernetes"),))
    agg = aggregate([required, *dims], config)
    assert _close(agg.raw, 0.95) and agg.caps == ()


def test_the_cap_applies_after_redistribution(config) -> None:
    dims = _all(0.95, required_facts=(_critical("skill.kubernetes"),))
    dims = [abstained_dimension(DOMAIN, Flag.INDUSTRY_UNKNOWN) if dim.name is DOMAIN else dim for dim in dims]
    agg = aggregate(dims, config)
    assert _close(agg.raw, CAP_ONE)
    assert _close(agg.dimensions[REQUIRED.value].effective_weight, 0.35 / 0.9)


def test_cap_values_come_from_the_config(config) -> None:
    tuned = with_scoring(config, "critical_miss", cap_one=0.6, cap_two_or_more=0.4)
    one = aggregate(_all(0.95, required_facts=(_critical("skill.a"),)), tuned)
    two = aggregate(_all(0.95, required_facts=(_critical("skill.a"), _critical("skill.b"))), tuned)
    assert _close(one.raw, 0.6) and one.caps[0].cap == 0.6
    assert _close(two.raw, 0.4) and two.caps[0].cap == 0.4


# ─── Weights, inputs, numeric safety ─────────────────────────────────────────────────────


def test_weights_come_from_the_config(config) -> None:
    # Re-aggregating the same dimension values under other weights (sensitivity runs, tuning)
    # uses those weights, as configured and as effective.
    moved = replace_weights(config, {**{name.value: W[name] for name in DimensionName},
                                     "required_skills": 0.30, "preferred_skills": 0.15})
    agg = aggregate(_all(), moved)
    assert _close(agg.dimensions[REQUIRED.value].weight, 0.30)
    assert _close(agg.dimensions[REQUIRED.value].effective_weight, 0.30)
    assert _close(agg.dimensions[PREFERRED.value].effective_weight, 0.15)
    assert _close(agg.raw, 0.647 - 0.05 * 0.8 + 0.05 * 0.5)


def test_each_dimension_exactly_once(config) -> None:
    dims = _all()
    for broken in (dims + [dimension(TOOLS, 0.9)], dims[:-1], [], dims[:-1] + ["tools"]):
        with pytest.raises(EngineFault) as err:
            aggregate(broken, config)
        assert err.value.flag is Flag.INTERNAL_ERROR


def _unvalidated(name: DimensionName, value: object) -> DimensionScore:
    """A dimension that skipped validation, as a defect upstream could hand over."""
    return DimensionScore.model_construct(name=name, value=value, weight=W[name], effective_weight=W[name],
                                          contribution=0.0, abstained=False, abstain_reason=None, facts=())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None])
def test_a_non_finite_value_is_a_numeric_fault(config, bad) -> None:
    dims = [_unvalidated(EXPERIENCE, bad) if dim.name is EXPERIENCE else dim for dim in _all()]
    with pytest.raises(EngineFault) as err:
        aggregate(dims, config)
    assert err.value.flag is Flag.NUMERIC_FAULT


def _with_weights(config: EngineConfig, weights: dict) -> EngineConfig:
    """A config whose weights skipped validation on purpose (a defect the aggregator must catch)."""
    fields = {name: getattr(config, name) for name in EngineConfig.model_fields}
    fields["weights"] = Weights.model_construct(dimensions=weights)
    return EngineConfig.model_construct(**fields)


def test_a_non_finite_configured_weight_is_a_numeric_fault(config) -> None:
    broken = _with_weights(config, {**config.weights.dimensions, TOOLS: float("nan")})
    with pytest.raises(EngineFault) as err:
        aggregate(_all(), broken)
    assert err.value.flag is Flag.NUMERIC_FAULT


def test_no_weight_to_share_is_a_numeric_fault_not_a_division(config) -> None:
    broken = _with_weights(config, {name: 0.0 for name in DimensionName})
    with pytest.raises(EngineFault) as err:
        aggregate(_all(), broken)
    assert err.value.flag is Flag.NUMERIC_FAULT


def test_the_aggregate_cannot_be_changed_afterwards(config) -> None:
    agg = aggregate(_all(), config)
    with pytest.raises(TypeError):
        agg.dimensions["tools"] = agg.dimensions["domain"]
    with pytest.raises(ValidationError):
        agg.raw = 1.0  # frozen model


def test_deterministic(config) -> None:
    dims = _all(required_facts=(_critical("skill.kubernetes"),))
    assert len({canonical_json(aggregate(dims, config)) for _ in range(50)}) == 1
