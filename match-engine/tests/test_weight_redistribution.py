"""Weight redistribution: "we don't know" is not "they have none" (SPEC §14.5; DECISIONS D-42).

A dimension that abstains (insufficient data) gives its weight to the dimensions that scored, in
proportion to their configured weights. A dimension that scored 0.0 keeps its weight and pulls the
score down. SPEC §14.5 calls conflating the two "the most damaging silent bug available in this
system": the rule lives once, in the aggregator, and is tested here directly.

Special case (§14.5 "All requirements PREFERRED, none REQUIRED"; D-42): when §5.1 abstains because
the posting has no REQUIRED requirements and §5.2 scored, §5.1's whole weight goes to §5.2.

Expected values are closed-form arithmetic over the SPEC §5 weights (EVAL §2.2).
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from matcher.errors import Flag
from matcher.models import DimensionName
from matcher.score.aggregate import aggregate
from tests.builders import SPEC_WEIGHTS, abstained_dimension, dimension, with_scoring

REQUIRED, PREFERRED = DimensionName.REQUIRED_SKILLS, DimensionName.PREFERRED_SKILLS
EXPERIENCE, SENIORITY = DimensionName.EXPERIENCE, DimensionName.SENIORITY
DOMAIN, EDUCATION, TOOLS = DimensionName.DOMAIN, DimensionName.EDUCATION, DimensionName.TOOLS

# The brief's example: all seven scored → raw = .35×.8 + .10×.5 + .15×.6 + .10×.72 + .10×.15
# + .10×1.0 + .10×.4 = 0.647.
VALUES = {REQUIRED: 0.8, PREFERRED: 0.5, EXPERIENCE: 0.6, SENIORITY: 0.72, DOMAIN: 0.15, EDUCATION: 1.0,
          TOOLS: 0.4}
RAW_ALL = 0.647
W = SPEC_WEIGHTS

# A reason each dimension can abstain with (§5.6 never abstains in practice; the aggregator must
# still treat an abstaining education dimension like any other).
ABSTAIN = {
    REQUIRED: Flag.NO_REQUIREMENTS_RESOLVED,
    PREFERRED: Flag.NO_PREFERRED_REQUIREMENTS,
    EXPERIENCE: Flag.DATES_MISSING,
    SENIORITY: Flag.SENIORITY_UNKNOWN,
    DOMAIN: Flag.INDUSTRY_UNKNOWN,
    EDUCATION: Flag.NO_REQUIREMENTS_RESOLVED,
    TOOLS: Flag.NO_IMPLIED_REQUIREMENTS,
}


def _close(a: float, b: float, tol: float = 1e-12) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


def _dims(values=None, abstain=None):
    """The seven dimensions: `VALUES` unless `values` overrides one, abstaining where `abstain` says."""
    values = {**VALUES, **(values or {})}
    abstain = abstain or {}
    return [abstained_dimension(name, abstain[name]) if name in abstain else dimension(name, values[name])
            for name in DimensionName]


def test_the_brief_example_scores_0_647(config) -> None:
    agg = aggregate(_dims(), config)
    assert _close(agg.raw, 0.35 * 0.8 + 0.10 * 0.5 + 0.15 * 0.6 + 0.10 * 0.72 + 0.10 * 0.15 + 0.10 * 1.0 + 0.10 * 0.4)
    assert _close(agg.raw, RAW_ALL)


def test_domain_abstaining_redistributes_its_weight_proportionally(config) -> None:
    agg = aggregate(_dims(abstain={DOMAIN: Flag.INDUSTRY_UNKNOWN}), config)
    assert _close(agg.raw, (0.647 - 0.015) / 0.9)
    assert math.isclose(agg.raw, 0.702222, abs_tol=5e-7)
    assert _close(agg.scored_weight, 0.9)
    domain = agg.dimensions[DOMAIN.value]
    assert domain.abstained and domain.value is None and domain.abstain_reason is Flag.INDUSTRY_UNKNOWN
    assert domain.effective_weight == 0.0 and domain.contribution == 0.0
    assert _close(domain.weight, 0.10)  # the configured weight is reported unchanged
    for name in DimensionName:
        if name is not DOMAIN:
            dim = agg.dimensions[name.value]
            assert _close(dim.effective_weight, W[name] / 0.9)
            assert _close(dim.contribution, W[name] / 0.9 * VALUES[name])
            assert _close(dim.weight, W[name])


def test_abstaining_is_not_scoring_zero(config) -> None:
    """THE silent-bug test (SPEC §14.5): undated experience must not count as no experience."""
    unknown = aggregate(_dims(abstain={EXPERIENCE: Flag.DATES_MISSING}), config)
    none = aggregate(_dims(values={EXPERIENCE: 0.0}), config)
    # Abstaining: experience's 0.15 is spread over the other 0.85 of the weight.
    assert _close(unknown.raw, (0.647 - 0.09) / 0.85)
    assert math.isclose(unknown.raw, 0.655294, abs_tol=5e-7)
    # Scoring 0.0: experience keeps its 0.15 and contributes nothing.
    assert _close(none.raw, 0.647 - 0.09)
    assert math.isclose(none.raw, 0.557, abs_tol=1e-12)
    assert unknown.raw - none.raw > 0.09  # the bug would cost this candidate about ten points
    assert unknown.dimensions[EXPERIENCE.value].value is None
    assert unknown.dimensions[EXPERIENCE.value].effective_weight == 0.0
    assert none.dimensions[EXPERIENCE.value].value == 0.0
    assert _close(none.dimensions[EXPERIENCE.value].effective_weight, 0.15)
    assert none.dimensions[EXPERIENCE.value].contribution == 0.0
    for name in DimensionName:
        if name is not EXPERIENCE:
            assert _close(unknown.dimensions[name.value].effective_weight, W[name] / 0.85)
            assert _close(none.dimensions[name.value].effective_weight, W[name])
    assert _close(unknown.scored_weight, 0.85) and _close(none.scored_weight, 1.0)


@pytest.mark.parametrize("name", list(DimensionName), ids=lambda name: name.value)
def test_any_one_dimension_abstaining(config, name: DimensionName) -> None:
    # raw = (0.647 − w_d × v_d) / (1 − w_d): the other six keep their proportions.
    agg = aggregate(_dims(abstain={name: ABSTAIN[name]}), config)
    assert _close(agg.raw, (RAW_ALL - W[name] * VALUES[name]) / (1.0 - W[name]))
    assert _close(math.fsum(d.effective_weight for d in agg.dimensions.values()), 1.0)
    assert agg.dimensions[name.value].effective_weight == 0.0


def test_proportional_means_the_ratios_of_the_configured_weights_hold(config) -> None:
    agg = aggregate(_dims(abstain={EXPERIENCE: Flag.DATES_MISSING, DOMAIN: Flag.INDUSTRY_UNKNOWN}), config)
    required = agg.dimensions[REQUIRED.value].effective_weight
    tools = agg.dimensions[TOOLS.value].effective_weight
    assert _close(required / tools, 0.35 / 0.10, tol=1e-9)
    assert _close(required, 0.35 / 0.75) and _close(tools, 0.10 / 0.75)
    expected = (0.647 - 0.09 - 0.015) / 0.75
    assert _close(agg.raw, expected)


# ─── The §14.5 special case: no REQUIRED requirements ──────────────────────────────────────


def test_no_required_requirements_moves_the_whole_required_weight_to_preferred(config) -> None:
    agg = aggregate(_dims(abstain={REQUIRED: Flag.NO_REQUIRED_REQUIREMENTS}), config)
    preferred = agg.dimensions[PREFERRED.value]
    assert _close(preferred.effective_weight, 0.45)  # 0.10 + 0.35
    assert _close(preferred.weight, 0.10)  # as configured
    assert _close(preferred.contribution, 0.45 * 0.5)
    for name in (EXPERIENCE, SENIORITY, DOMAIN, EDUCATION, TOOLS):  # untouched: nothing else moved
        assert _close(agg.dimensions[name.value].effective_weight, W[name])
    expected = 0.45 * 0.5 + 0.15 * 0.6 + 0.10 * 0.72 + 0.10 * 0.15 + 0.10 * 1.0 + 0.10 * 0.4
    assert _close(agg.raw, expected) and _close(agg.raw, 0.542)
    assert agg.dimensions[REQUIRED.value].effective_weight == 0.0
    assert _close(agg.scored_weight, 1.0)  # the required weight scored, through §5.2


def test_the_special_case_combines_with_ordinary_redistribution(config) -> None:
    # Preferred carries 0.45 before renormalising over the 0.9 that scored: 0.45 / 0.9 = 0.5.
    agg = aggregate(_dims(abstain={REQUIRED: Flag.NO_REQUIRED_REQUIREMENTS, DOMAIN: Flag.INDUSTRY_UNKNOWN}), config)
    assert _close(agg.dimensions[PREFERRED.value].effective_weight, 0.5)
    assert _close(agg.dimensions[EXPERIENCE.value].effective_weight, 0.15 / 0.9)
    expected = (0.45 * 0.5 + 0.15 * 0.6 + 0.10 * 0.72 + 0.10 * 1.0 + 0.10 * 0.4) / 0.9
    assert _close(agg.raw, expected) and math.isclose(agg.raw, 0.585556, abs_tol=5e-7)
    assert _close(agg.scored_weight, 0.9)


def test_required_abstaining_for_another_reason_redistributes_proportionally(config) -> None:
    # REQUIRED requirements exist but none carries weight: that is "we don't know", not "none asked".
    agg = aggregate(_dims(abstain={REQUIRED: Flag.NO_REQUIREMENTS_RESOLVED}), config)
    assert _close(agg.dimensions[PREFERRED.value].effective_weight, 0.10 / 0.65)
    assert _close(agg.raw, (0.647 - 0.28) / 0.65) and math.isclose(agg.raw, 0.564615, abs_tol=5e-7)
    assert _close(agg.scored_weight, 0.65)


def test_no_required_and_no_preferred_falls_back_to_the_proportional_rule(config) -> None:
    agg = aggregate(_dims(abstain={REQUIRED: Flag.NO_REQUIRED_REQUIREMENTS,
                                   PREFERRED: Flag.NO_PREFERRED_REQUIREMENTS}), config)
    assert _close(agg.raw, (0.647 - 0.28 - 0.05) / 0.55) and math.isclose(agg.raw, 0.576364, abs_tol=5e-7)
    assert _close(agg.dimensions[TOOLS.value].effective_weight, 0.10 / 0.55)
    assert agg.dimensions[PREFERRED.value].effective_weight == 0.0


def test_the_special_case_follows_the_config_switch(config) -> None:
    off = with_scoring(config, "redistribution", required_to_preferred_when_no_required=False)
    agg = aggregate(_dims(abstain={REQUIRED: Flag.NO_REQUIRED_REQUIREMENTS}), off)
    assert _close(agg.dimensions[PREFERRED.value].effective_weight, 0.10 / 0.65)
    assert _close(agg.raw, (0.647 - 0.28) / 0.65)


# ─── The rule, stated once more as a property ───────────────────────────────────────────


_REQUIRED_REASONS = st.sampled_from([Flag.NO_REQUIRED_REQUIREMENTS, Flag.NO_REQUIREMENTS_RESOLVED])
_VALUE = st.one_of(st.none(), st.floats(min_value=0.0, max_value=1.0, allow_nan=False))


@given(values=st.lists(_VALUE, min_size=7, max_size=7), required_reason=_REQUIRED_REASONS, special=st.booleans())
def test_effective_weights_are_the_configured_ones_renormalised_over_what_scored(
    config, values, required_reason, special
) -> None:
    cfg = config if special else with_scoring(config, "redistribution", required_to_preferred_when_no_required=False)
    names = list(DimensionName)
    given_values = dict(zip(names, values))
    dims = [
        abstained_dimension(name, required_reason if name is REQUIRED else ABSTAIN[name])
        if given_values[name] is None else dimension(name, given_values[name])
        for name in names
    ]
    agg = aggregate(dims, cfg)
    # Oracle, straight from SPEC §14.5 / D-42.
    base = dict(W)
    if (special and given_values[REQUIRED] is None and required_reason is Flag.NO_REQUIRED_REQUIREMENTS
            and given_values[PREFERRED] is not None):
        base[PREFERRED] = base[PREFERRED] + base[REQUIRED]
    scoring = [name for name in names if given_values[name] is not None]
    if not scoring:
        assert agg is None
        return
    total = math.fsum(base[name] for name in scoring)
    expected_raw = math.fsum(base[name] / total * given_values[name] for name in scoring)
    assert _close(agg.raw, min(1.0, expected_raw), tol=1e-12)
    assert 0.0 <= agg.raw <= 1.0
    assert _close(agg.scored_weight, min(1.0, total), tol=1e-12)
    assert _close(math.fsum(d.effective_weight for d in agg.dimensions.values()), 1.0, tol=1e-12)
    for name in names:
        dim = agg.dimensions[name.value]
        if given_values[name] is None:
            assert dim.abstained and dim.value is None
            assert dim.effective_weight == 0.0 and dim.contribution == 0.0
        else:
            assert _close(dim.effective_weight, base[name] / total, tol=1e-12)
            assert _close(dim.contribution, base[name] / total * given_values[name], tol=1e-12)
            assert dim.value == given_values[name]
