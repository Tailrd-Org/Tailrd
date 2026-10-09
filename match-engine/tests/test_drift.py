"""Drift alarms and the population stability index (EVAL section 6; SPEC section 16).

PSI values are worked out by hand from the formula sum((a - e) * ln(a / e)) over the reference's
quantile bins, with an empty bin floored at 1e-4. Each alarm is tested at its threshold, just
inside it and just beyond it.
"""

from __future__ import annotations

import json
import math
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from matcher.audit.drift import (
    ABSTENTION_CEILING,
    COVERAGE_FLOOR,
    FAIRNESS_FLOOR,
    LATENCY_P95_CEILING_MS,
    PSI_CEILING,
    DriftAlarm,
    drift_alarms,
    psi,
)
from matcher.audit.fairness import FOUR_FIFTHS, four_fifths

NO_DATA = dict(
    coverage=None,
    abstention_rate=None,
    abstention_baseline=None,
    psi_value=None,
    min_fairness_ratio=None,
    p95_latency_ms=None,
)


def alarms(**changes) -> tuple[DriftAlarm, ...]:
    return drift_alarms(**{**NO_DATA, **changes})


def codes(**changes) -> list[str]:
    return [a.code for a in alarms(**changes)]


# --- psi ---------------------------------------------------------------------------------------


def test_psi_two_bin_example_by_hand() -> None:
    # reference [1, 2, 3, 4]: the median edge is 2, so bins are (-inf, 2] and (2, inf), each 1/2.
    # current [1, 2, 2, 4]: three values in the first bin, one in the second -> a = [3/4, 1/4].
    # PSI = (3/4 - 1/2) ln((3/4) / (1/2)) + (1/4 - 1/2) ln((1/4) / (1/2))
    #     = 0.25 ln 1.5 + 0.25 ln 2 = 0.1013662770 + 0.1732867951 = 0.2746530722
    assert psi([1, 2, 3, 4], [1, 2, 2, 4], bins=2) == pytest.approx(0.2746530722, abs=1e-9)


def test_psi_of_identical_samples_is_zero() -> None:
    assert psi([1, 2, 3, 4], [1, 2, 3, 4], bins=2) == 0.0
    sample = [0.1 * i for i in range(50)]
    assert psi(sample, sample) == 0.0


def test_psi_floors_an_empty_bin_at_one_ten_thousandth() -> None:
    # reference [1, 2, 3, 4] -> e = [1/2, 1/2]; current [3, 4, 5, 6] sits entirely above the edge:
    # a = [0 -> 1e-4, 1].  PSI = (1e-4 - 1/2) ln(1e-4 / (1/2)) + (1 - 1/2) ln(1 / (1/2))
    #     = 0.4999 * 8.5171931914 + 0.5 * 0.6931471806 = 4.2577448757 + 0.3465735903 = 4.6043184667
    assert psi([1, 2, 3, 4], [3, 4, 5, 6], bins=2) == pytest.approx(4.6043184667, abs=1e-9)


def test_psi_collapses_bins_the_reference_cannot_tell_apart() -> None:
    # Every reference value is 5, so only one edge (5) survives: 2 bins, e = [1, 0 -> 1e-4].
    # current [5, 5, 6, 6] -> a = [1/2, 1/2].
    # PSI = (1/2 - 1) ln(1/2) + (1/2 - 1e-4) ln((1/2) / 1e-4) = 0.3465735903 + 4.2577448757 = 4.6043184667
    assert psi([5, 5, 5, 5], [5, 5, 6, 6], bins=10) == pytest.approx(4.6043184667, abs=1e-9)


def test_psi_default_is_ten_reference_quantile_bins() -> None:
    # reference 1..100 -> edges 10, 20, ..., 90: ten bins of exactly 10 values, e = 0.1 each.
    # current 51..100: bins 5-9 hold 10 of 50 values (a = 0.2); bins 0-4 are empty (a -> 1e-4).
    # PSI = 5 (0.2 - 0.1) ln 2 + 5 (1e-4 - 0.1) ln(1e-3) = 0.3465735903 + 3.4504237618 = 3.7969973521
    assert psi(list(range(1, 101)), list(range(51, 101))) == pytest.approx(3.7969973521, abs=1e-9)


def test_psi_decile_shift_by_hand() -> None:
    reference = list(range(1, 101))
    # 20 values in the first decile (1..10 twice), 10 in each of bins 1-7 and 8, none in bin 9:
    current = list(range(1, 11)) * 2 + list(range(11, 91))
    # a = [0.2, 0.1 x 8, 0 -> 1e-4] over 100 values; e = 0.1 each.
    # PSI = (0.2 - 0.1) ln 2 + 8 * 0 + (1e-4 - 0.1) ln(1e-3) = 0.0693147181 + 0.6900847524
    assert psi(reference, current) == pytest.approx(0.7593994704, abs=1e-9)


def test_psi_bins_are_right_closed_so_a_value_on_an_edge_belongs_below_it() -> None:
    # reference [1, 2, 2, 3]: the median edge is 2, e = [3/4, 1/4].
    # current all 2: on the edge -> lower bin, a = [1, 1e-4]:
    #   (1 - 3/4) ln(1 / (3/4)) + (1e-4 - 1/4) ln(1e-4 / (1/4)) = 0.0719205 + 1.9552291 = 2.0271496162
    assert psi([1, 2, 2, 3], [2, 2, 2, 2], bins=2) == pytest.approx(2.0271496162, abs=1e-9)
    # current all just above 2 -> upper bin, a = [1e-4, 1]:
    #   (1e-4 - 3/4) ln(1e-4 / (3/4)) + (1 - 1/4) ln(1 / (1/4)) = 6.6907 + 1.0397 = 7.7308222297
    assert psi([1, 2, 2, 3], [2.0000001] * 4, bins=2) == pytest.approx(7.7308222297, abs=1e-9)


def test_the_reference_defines_the_bins_so_psi_is_not_symmetric() -> None:
    # psi(A, B): edge 2 from A, e = [1/2, 1/2], B gives a = [3/4, 1/4]  -> 0.2746530722 (the example above).
    # psi(B, A): edge 1 from B, e = [3/4, 1/4], A gives a = [1/4, 3/4]
    #   -> (1/4 - 3/4) ln(1/3) + (3/4 - 1/4) ln 3 = ln 3 = 1.0986122887
    a, b = [1, 2, 3, 4], [1, 1, 1, 4]
    assert psi(a, b, bins=2) == pytest.approx(0.2746530722, abs=1e-9)
    assert psi(b, a, bins=2) == pytest.approx(math.log(3), abs=1e-9)


def test_psi_does_not_depend_on_sample_order() -> None:
    reference = [3, 9, 1, 7, 5, 2, 8, 4, 6, 10]
    current = [10, 1, 1, 2, 9, 9, 3, 8, 5, 5, 6]
    assert psi(reference, current, bins=5) == psi(sorted(reference), sorted(current, reverse=True), bins=5)


def test_psi_accepts_any_sequence_of_numbers() -> None:
    assert psi((1, 2, 3, 4), (1.0, 2.0, 2.0, 4.0), bins=2) == pytest.approx(0.2746530722, abs=1e-9)


@pytest.mark.parametrize(
    "reference,current,bins",
    [
        ([], [1.0], 10),
        ([1.0], [], 10),
        ([1.0, 2.0], [1.0], 1),
        ([1.0, 2.0], [1.0], 0),
        ([1.0, float("nan")], [1.0], 2),
        ([1.0, 2.0], [float("inf")], 2),
    ],
)
def test_psi_refuses_inputs_it_cannot_measure(reference, current, bins) -> None:
    with pytest.raises(ValueError):
        psi(reference, current, bins=bins)


@given(
    st.lists(st.floats(min_value=0, max_value=1, allow_nan=False), min_size=1, max_size=60),
    st.lists(st.floats(min_value=0, max_value=1, allow_nan=False), min_size=1, max_size=60),
    st.integers(min_value=2, max_value=12),
)
def test_psi_is_never_negative_and_is_zero_against_itself(reference, current, bins) -> None:
    assert psi(reference, current, bins=bins) >= 0.0
    assert psi(reference, reference, bins=bins) == 0.0


# --- drift_alarms: thresholds ------------------------------------------------------------------


def test_thresholds_are_the_eval_section_6_numbers() -> None:
    assert COVERAGE_FLOOR == 0.92
    assert ABSTENTION_CEILING == 0.20
    assert PSI_CEILING == 0.25
    assert FAIRNESS_FLOOR == 0.80
    assert LATENCY_P95_CEILING_MS == 300.0


def test_the_fairness_floor_is_the_exact_four_fifths_as_a_float() -> None:
    # One definition of four-fifths (fairness.FOUR_FIFTHS). The double nearest 0.8 lies just above 4/5,
    # so a double compares below it exactly when its value is below 4/5; a ratio of exactly 4/5 reaches
    # drift_alarms as 0.8 (float() of the exact fraction) and is healthy.
    assert FAIRNESS_FLOOR == float(FOUR_FIFTHS) == 0.8
    assert Fraction(FAIRNESS_FLOOR) > FOUR_FIFTHS
    just_below = math.nextafter(FAIRNESS_FLOOR, 0.0)
    assert Fraction(just_below) < FOUR_FIFTHS
    assert codes(min_fairness_ratio=just_below) == ["FOUR_FIFTHS_VIOLATION"]
    assert codes(min_fairness_ratio=0.8) == []


def test_coverage_floor_matches_the_configured_taxonomy_alert(config) -> None:
    assert COVERAGE_FLOOR == config.scoring.taxonomy.coverage_alert_threshold


def test_no_data_means_no_alarm() -> None:
    assert drift_alarms(**NO_DATA) == ()


def test_everything_healthy_means_no_alarm() -> None:
    assert (
        alarms(
            coverage=0.97,
            abstention_rate=0.05,
            abstention_baseline=0.05,
            psi_value=0.04,
            min_fairness_ratio=0.93,
            p95_latency_ms=120.0,
        )
        == ()
    )


def test_coverage_alarm_fires_below_92_percent_only() -> None:
    assert codes(coverage=0.92) == []
    assert codes(coverage=0.9201) == []
    assert codes(coverage=1.0) == []
    assert codes(coverage=0.9199) == ["COVERAGE_LOW"]
    assert codes(coverage=0.0) == ["COVERAGE_LOW"]
    alarm = alarms(coverage=0.9)[0]
    assert (alarm.value, alarm.threshold) == (0.9, 0.92)


def test_absolute_abstention_alarm_fires_above_20_percent_only() -> None:
    assert codes(abstention_rate=0.20) == []
    assert codes(abstention_rate=0.1999) == []
    assert codes(abstention_rate=0.2001) == ["ABSTENTION_HIGH"]
    assert codes(abstention_rate=1.0) == ["ABSTENTION_HIGH"]
    alarm = alarms(abstention_rate=0.25)[0]
    assert (alarm.value, alarm.threshold) == (0.25, 0.20)


def test_abstention_alarm_fires_above_twice_the_baseline_only() -> None:
    # baseline 5%: the line is 10%.
    assert codes(abstention_rate=0.10, abstention_baseline=0.05) == []
    assert codes(abstention_rate=0.0999, abstention_baseline=0.05) == []
    assert codes(abstention_rate=0.1001, abstention_baseline=0.05) == ["ABSTENTION_RISING"]
    alarm = alarms(abstention_rate=0.12, abstention_baseline=0.05)[0]
    assert alarm.code == "ABSTENTION_RISING"
    assert (alarm.value, alarm.threshold) == (0.12, 0.10)


def test_a_zero_baseline_means_any_abstention_is_more_than_twice_it() -> None:
    assert codes(abstention_rate=0.0, abstention_baseline=0.0) == []
    assert codes(abstention_rate=0.01, abstention_baseline=0.0) == ["ABSTENTION_RISING"]


def test_both_abstention_rules_can_fire_together() -> None:
    assert codes(abstention_rate=0.30, abstention_baseline=0.05) == ["ABSTENTION_HIGH", "ABSTENTION_RISING"]
    # A high baseline silences only the relative rule.
    assert codes(abstention_rate=0.30, abstention_baseline=0.20) == ["ABSTENTION_HIGH"]


def test_the_baseline_alone_or_the_rate_alone_never_triggers_the_relative_rule() -> None:
    assert codes(abstention_baseline=0.05) == []
    assert codes(abstention_rate=0.15) == []


def test_psi_alarm_fires_above_0_25_only() -> None:
    assert codes(psi_value=0.25) == []
    assert codes(psi_value=0.2499) == []
    assert codes(psi_value=0.2501) == ["SCORE_DRIFT"]
    assert codes(psi_value=4.6) == ["SCORE_DRIFT"]
    alarm = alarms(psi_value=0.3)[0]
    assert (alarm.value, alarm.threshold) == (0.3, 0.25)


def test_fairness_alarm_fires_below_0_80_only() -> None:
    assert codes(min_fairness_ratio=0.80) == []
    assert codes(min_fairness_ratio=0.8001) == []
    assert codes(min_fairness_ratio=1.0) == []
    assert codes(min_fairness_ratio=0.7999) == ["FOUR_FIFTHS_VIOLATION"]
    assert codes(min_fairness_ratio=0.79) == ["FOUR_FIFTHS_VIOLATION"]
    assert codes(min_fairness_ratio=0.0) == ["FOUR_FIFTHS_VIOLATION"]
    alarm = alarms(min_fairness_ratio=0.79)[0]
    assert (alarm.value, alarm.threshold) == (0.79, 0.80)


def test_the_fairness_alarm_agrees_with_the_exact_verdict() -> None:
    def group(name: str, n_people: int, n_selected: int) -> list[tuple[str, str, bool]]:
        return [(f"{name}-{i:05d}", name, i < n_selected) for i in range(n_people)]

    exactly_four_fifths = four_fifths(group("A", 100, 5) + group("B", 100, 4))
    assert exactly_four_fifths.passed is True
    assert codes(min_fairness_ratio=exactly_four_fifths.min_ratio) == []
    just_below = four_fifths(group("A", 100, 100) + group("B", 100, 79))
    assert just_below.passed is False
    assert codes(min_fairness_ratio=just_below.min_ratio) == ["FOUR_FIFTHS_VIOLATION"]


def test_latency_alarm_fires_above_300_ms_only() -> None:
    assert codes(p95_latency_ms=300.0) == []
    assert codes(p95_latency_ms=299.9) == []
    assert codes(p95_latency_ms=300.1) == ["LATENCY_HIGH"]
    assert codes(p95_latency_ms=1200) == ["LATENCY_HIGH"]
    alarm = alarms(p95_latency_ms=450.0)[0]
    assert (alarm.value, alarm.threshold) == (450.0, 300.0)


def test_every_alarm_can_fire_at_once_in_a_fixed_order() -> None:
    fired = alarms(
        coverage=0.9,
        abstention_rate=0.3,
        abstention_baseline=0.1,
        psi_value=0.5,
        min_fairness_ratio=0.5,
        p95_latency_ms=500.0,
    )
    assert [a.code for a in fired] == [
        "COVERAGE_LOW",
        "ABSTENTION_HIGH",
        "ABSTENTION_RISING",
        "SCORE_DRIFT",
        "FOUR_FIFTHS_VIOLATION",
        "LATENCY_HIGH",
    ]
    assert fired == alarms(
        p95_latency_ms=500.0,
        min_fairness_ratio=0.5,
        psi_value=0.5,
        abstention_baseline=0.1,
        abstention_rate=0.3,
        coverage=0.9,
    )


# --- drift_alarms: alarm contents --------------------------------------------------------------


def test_messages_are_plain_and_name_the_observed_value_and_the_limit() -> None:
    texts = {
        a.code: a.message
        for a in alarms(
            coverage=0.9,
            abstention_rate=0.25,
            abstention_baseline=0.05,
            psi_value=0.3,
            min_fairness_ratio=0.75,
            p95_latency_ms=350.0,
        )
    }
    assert "90.00%" in texts["COVERAGE_LOW"] and "92" in texts["COVERAGE_LOW"]
    assert "25.00%" in texts["ABSTENTION_HIGH"] and "20" in texts["ABSTENTION_HIGH"]
    assert "25.00%" in texts["ABSTENTION_RISING"] and "5.00%" in texts["ABSTENTION_RISING"]
    assert "0.3000" in texts["SCORE_DRIFT"] and "0.25" in texts["SCORE_DRIFT"]
    assert "0.7500" in texts["FOUR_FIFTHS_VIOLATION"] and "0.80" in texts["FOUR_FIFTHS_VIOLATION"]
    assert "350" in texts["LATENCY_HIGH"] and "300" in texts["LATENCY_HIGH"]
    for message in texts.values():
        assert message and message == message.strip() and "\n" not in message


def test_the_fairness_message_says_it_is_terminal() -> None:
    message = alarms(min_fairness_ratio=0.7)[0].message
    assert "do not tune around it" in message


def test_alarms_are_frozen_json_safe_models() -> None:
    alarm = alarms(psi_value=0.3)[0]
    assert isinstance(alarm, DriftAlarm)
    assert json.loads(alarm.model_dump_json()) == {
        "code": "SCORE_DRIFT",
        "value": 0.3,
        "threshold": 0.25,
        "message": alarm.message,
    }
    with pytest.raises(ValidationError):
        alarm.value = 0.0  # type: ignore[misc]


# --- drift_alarms: bad inputs ------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,bad",
    [
        ("coverage", float("nan")),
        ("coverage", 92.0),  # a percentage where a fraction is expected: would silently never alarm
        ("coverage", -0.1),
        ("abstention_rate", 20.0),
        ("abstention_rate", float("nan")),
        ("abstention_baseline", -0.5),
        ("abstention_baseline", 5.0),
        ("psi_value", float("nan")),
        ("psi_value", -0.01),
        ("psi_value", float("inf")),
        ("min_fairness_ratio", float("nan")),
        ("min_fairness_ratio", 80.0),
        ("min_fairness_ratio", -0.2),
        ("p95_latency_ms", float("nan")),
        ("p95_latency_ms", -1.0),
        ("p95_latency_ms", float("inf")),
    ],
)
def test_a_metric_that_cannot_be_right_is_an_error_not_a_silent_pass(field: str, bad: float) -> None:
    with pytest.raises(ValueError):
        alarms(**{field: bad})
