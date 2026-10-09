"""The four-fifths check on opaque-id records (SPEC section 9; EVAL section 3: ratio >= 0.80, n >= 100).

A fairness failure is terminal in this project (CLAUDE.md), so the edges matter: the threshold is
inclusive, the verdict is exact rational arithmetic, a small group can never decide it, and the
group size that gates eligibility counts people (opaque ids), not scored pairs. Expected values are
worked out by hand in the comments.
"""

from __future__ import annotations

import inspect
import json
import random
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from matcher.audit.fairness import FOUR_FIFTHS, ExcludedGroup, FairnessReport, GroupSelection, four_fifths


def group(name: str, n_people: int, n_selected: int) -> list[tuple[str, str, bool]]:
    """One record per person; the first `n_selected` people are selected."""
    return [(f"{name}-{i:05d}", name, i < n_selected) for i in range(n_people)]


def by_name(report: FairnessReport) -> dict[str, GroupSelection]:
    return {g.group: g for g in report.groups}


# --- the 0.80 boundary -------------------------------------------------------------------------


def test_exactly_four_fifths_passes_where_float_division_says_otherwise() -> None:
    # A: 5/100, B: 4/100. The exact ratio is (1/25) / (1/20) = 4/5. In binary floating point
    # (4/100) / (5/100) is 0.7999999999999999, which a naive `>= 0.80` would call a violation.
    assert (4 / 100) / (5 / 100) < 0.80
    report = four_fifths(group("A", 100, 5) + group("B", 100, 4))
    assert report.passed is True
    assert report.min_ratio == 0.8
    assert report.min_ratio_exact == "4/5"
    assert by_name(report)["B"].ratio_exact == "4/5"
    assert by_name(report)["B"].meets is True


def test_just_below_four_fifths_fails() -> None:
    # A: 100/100 = 1, B: 79/100: ratio 79/100 = 0.79.
    report = four_fifths(group("A", 100, 100) + group("B", 100, 79))
    assert report.passed is False
    assert report.min_ratio == pytest.approx(0.79)
    assert report.min_ratio_exact == "79/100"
    assert by_name(report)["A"].meets is True
    assert by_name(report)["B"].meets is False


def test_exact_boundary_with_unequal_group_sizes() -> None:
    # A: 50/125 = 2/5.  B: 32/100 = 8/25.  8/25 divided by 2/5 is 4/5 exactly: passes.
    # B with 31 selected: 31/100 / (2/5) = 31/40 = 0.775: fails.
    assert four_fifths(group("A", 125, 50) + group("B", 100, 32)).passed is True
    failing = four_fifths(group("A", 125, 50) + group("B", 100, 31))
    assert failing.passed is False
    assert failing.min_ratio_exact == "31/40"


def test_the_threshold_is_four_fifths_and_not_a_parameter() -> None:
    assert FOUR_FIFTHS == Fraction(4, 5)
    assert list(inspect.signature(four_fifths).parameters) == ["records", "min_group_n"]
    assert four_fifths([]).threshold == "4/5"


# --- who is assessed ---------------------------------------------------------------------------


def test_a_group_below_the_minimum_is_excluded_and_cannot_decide() -> None:
    # C has 99 people and a selection rate of 0; counted, it would fail an otherwise fair result.
    records = group("A", 100, 50) + group("B", 100, 45) + group("C", 99, 0)
    report = four_fifths(records)
    assert report.passed is True
    assert report.min_ratio == pytest.approx(0.9)  # B: 0.45 / 0.50
    assert [g.group for g in report.groups] == ["A", "B"]
    assert report.excluded == (ExcludedGroup(group="C", n_people=99, n_records=99),)


def test_the_minimum_group_size_is_inclusive() -> None:
    # 100 people is enough: C joins the verdict, its rate of 0 gives ratio 0/1, and the check fails.
    report = four_fifths(group("A", 100, 50) + group("B", 100, 45) + group("C", 100, 0))
    assert report.passed is False
    assert report.excluded == ()
    assert report.min_ratio == 0.0
    assert report.min_ratio_exact == "0/1"
    assert by_name(report)["C"].meets is False


def test_min_group_n_is_configurable() -> None:
    records = group("A", 100, 50) + group("C", 99, 0)
    default = four_fifths(records)  # C is excluded and A is alone: nothing to compare, no verdict
    assert (default.assessed, default.passed) == (False, None)
    assert [e.group for e in default.excluded] == ["C"]
    lowered = four_fifths(records, min_group_n=99)  # C joins the comparison and its rate of 0 fails it
    assert (lowered.assessed, lowered.passed, lowered.min_group_n) == (True, False, 99)


def test_eligibility_counts_people_not_scored_pairs() -> None:
    # One person scored against 500 postings is not a 500-person group.
    lone = [("lone-person", "C", False)] * 500
    report = four_fifths(group("A", 100, 50) + group("B", 100, 45) + lone)
    assert report.passed is True
    assert report.excluded == (ExcludedGroup(group="C", n_people=1, n_records=500),)


def test_rates_are_over_records_so_every_scored_pair_counts() -> None:
    # A: 100 people, 2 records each (200 records). People 0-29 are selected twice (60), people 30-59
    # once (30), people 60-99 never: 90 of 200 -> 9/20.   B: 100 people, 54 selected -> 27/50.
    # Ratio A/B = (9/20) / (27/50) = 5/6.  (Per person, "ever selected" would be 60/100 and flip
    # which group is the reference.)
    a = []
    for i in range(100):
        picks = (True, True) if i < 30 else ((True, False) if i < 60 else (False, False))
        a.extend((f"A-{i:05d}", "A", p) for p in picks)
    report = four_fifths(a + group("B", 100, 54))
    stats = by_name(report)
    assert (stats["A"].n_people, stats["A"].n_records, stats["A"].selected) == (100, 200, 90)
    assert stats["A"].rate_exact == "9/20"
    assert stats["B"].rate_exact == "27/50"
    assert stats["A"].ratio_exact == "5/6"
    assert report.reference_group == "B"
    assert report.min_ratio_exact == "5/6"
    assert report.passed is True


def test_three_groups_are_compared_to_the_best_groups_rate() -> None:
    # Rates 0.50, 0.45, 0.30 -> ratios 1, 9/10, 3/5. The lowest (C) decides.
    report = four_fifths(group("A", 100, 50) + group("B", 100, 45) + group("C", 100, 30))
    assert {g.group: g.ratio_exact for g in report.groups} == {"A": "1/1", "B": "9/10", "C": "3/5"}
    assert report.reference_group == "A"
    assert report.min_ratio == pytest.approx(0.6)
    assert report.min_ratio_exact == "3/5"
    assert report.passed is False
    assert [g.meets for g in report.groups] == [True, True, False]


def test_groups_come_back_in_group_id_order_and_ties_pick_the_smallest_reference() -> None:
    report = four_fifths(group("B", 100, 40) + group("A", 100, 40) + group("C", 100, 10))
    assert [g.group for g in report.groups] == ["A", "B", "C"]
    assert report.reference_group == "A"


def test_nobody_selected_means_parity_not_a_division_by_zero() -> None:
    report = four_fifths(group("A", 100, 0) + group("B", 100, 0))
    assert report.passed is True
    assert report.min_ratio == 1.0
    assert {g.ratio_exact for g in report.groups} == {"1/1"}
    assert report.reference_group is None  # no group has a best rate to be measured against


def test_no_group_large_enough_means_no_verdict_and_never_a_pass() -> None:
    report = four_fifths(group("A", 40, 20) + group("B", 30, 5))
    assert report.assessed is False
    assert report.passed is None  # not True: a monitor must not be able to read "not assessed" as a pass
    assert report.min_ratio is None and report.min_ratio_exact is None
    assert report.reference_group is None
    assert report.groups == ()
    assert [e.group for e in report.excluded] == ["A", "B"]
    empty = four_fifths([])
    assert (empty.assessed, empty.passed, empty.groups, empty.excluded) == (False, None, (), ())


def test_one_eligible_group_has_nothing_to_be_compared_with_so_there_is_no_verdict() -> None:
    report = four_fifths(group("A", 120, 30))
    assert report.assessed is False
    assert report.passed is None and report.min_ratio is None and report.min_ratio_exact is None
    assert report.reference_group is None
    (only,) = report.groups
    assert (only.group, only.n_people, only.selected, only.rate_exact) == ("A", 120, 30, "1/4")
    # a lone group has no ratio and does not "meet" anything
    assert (only.ratio, only.ratio_exact, only.meets) == (None, None, None)


def test_two_eligible_groups_are_assessed_and_get_a_verdict() -> None:
    passing = four_fifths(group("A", 100, 30) + group("B", 100, 30))
    assert (passing.assessed, passing.passed) == (True, True)
    failing = four_fifths(group("A", 100, 30) + group("B", 100, 10))
    assert (failing.assessed, failing.passed) == (True, False)


def test_assessed_is_part_of_the_report_a_consumer_receives() -> None:
    dumped = four_fifths(group("A", 100, 30) + group("B", 100, 30)).model_dump(mode="json")
    assert dumped["assessed"] is True and dumped["passed"] is True
    unassessed = four_fifths(group("A", 100, 30)).model_dump(mode="json")
    assert unassessed["assessed"] is False and unassessed["passed"] is None


@given(st.lists(st.integers(min_value=1, max_value=130), max_size=4), st.data())
def test_passed_is_a_bool_exactly_when_two_groups_were_assessed(sizes, data) -> None:
    records: list[tuple[str, str, bool]] = []
    for index, n in enumerate(sizes):
        records.extend(group(f"G{index}", n, data.draw(st.integers(min_value=0, max_value=n))))
    report = four_fifths(records)
    assert report.assessed is (sum(1 for n in sizes if n >= 100) >= 2)
    if report.assessed:
        assert isinstance(report.passed, bool) and report.min_ratio is not None
    else:
        assert report.passed is None and report.min_ratio is None and report.min_ratio_exact is None


# --- input handling ----------------------------------------------------------------------------


def test_record_order_and_container_type_do_not_change_the_report() -> None:
    records = group("A", 120, 50) + group("B", 110, 30) + group("C", 40, 3)
    expected = four_fifths(records).model_dump(mode="json")
    shuffled = records[:]
    random.Random(7).shuffle(shuffled)
    assert four_fifths(shuffled).model_dump(mode="json") == expected
    assert four_fifths(iter(shuffled)).model_dump(mode="json") == expected  # a one-shot iterator
    assert four_fifths(tuple(shuffled)).model_dump(mode="json") == expected


def test_one_person_cannot_belong_to_two_groups() -> None:
    with pytest.raises(ValueError, match="two groups"):
        four_fifths([("same-id", "A", True), ("same-id", "B", False)])


def test_a_person_may_appear_in_many_records_of_one_group() -> None:
    report = four_fifths([("p1", "A", True), ("p1", "A", False), ("p2", "A", False)], min_group_n=1)
    only = by_name(report)["A"]
    assert (only.n_people, only.n_records, only.selected) == (2, 3, 1)


@pytest.mark.parametrize(
    "record",
    [
        ("only-two", "A"),
        ("a", "b", True, "extra"),
        ("", "A", True),
        ("id", "", True),
        (7, "A", True),
        ("id", None, True),
        ("id", "A", 1),  # selected is a bool: a truthy 1 or "False" must not slip through
        ("id", "A", "False"),
        ("id", "A", None),
    ],
)
def test_malformed_records_are_refused(record) -> None:
    with pytest.raises(ValueError):
        four_fifths([record])  # type: ignore[list-item]


@pytest.mark.parametrize("bad", [0, -1])
def test_min_group_n_must_be_positive(bad: int) -> None:
    with pytest.raises(ValueError):
        four_fifths(group("A", 10, 5), min_group_n=bad)


# --- the report --------------------------------------------------------------------------------


def test_report_is_json_serialisable_and_immutable() -> None:
    report = four_fifths(group("A", 100, 50) + group("B", 100, 30) + group("C", 3, 1))
    dumped = report.model_dump(mode="json")
    assert json.loads(json.dumps(dumped)) == dumped
    assert dumped["min_ratio_exact"] == "3/5"
    assert dumped["threshold"] == "4/5"
    assert dumped["min_group_n"] == 100
    with pytest.raises(ValidationError):
        report.passed = False  # type: ignore[misc]


def test_group_statistics_hold_the_counts_behind_every_ratio() -> None:
    report = four_fifths(group("A", 100, 50) + group("B", 100, 30))
    a, b = report.groups
    assert (a.group, a.n_people, a.n_records, a.selected) == ("A", 100, 100, 50)
    assert (a.rate, a.rate_exact, a.ratio, a.ratio_exact) == (0.5, "1/2", 1.0, "1/1")
    assert (b.rate, b.rate_exact, b.ratio, b.ratio_exact) == (0.3, "3/10", pytest.approx(0.6), "3/5")


# --- property: the verdict is the exact rational comparison -----------------------------------


@st.composite
def boundary_cases(draw):
    """Two groups whose lower selection rate sits at, just below or just above 4/5 of the higher."""
    n_high = draw(st.integers(min_value=100, max_value=400))
    n_low = draw(st.integers(min_value=100, max_value=400))
    s_high = draw(st.integers(min_value=1, max_value=n_high))
    exact_low = Fraction(4 * s_high * n_low, 5 * n_high)  # the selected count that gives exactly 4/5
    s_low = int(exact_low) + draw(st.integers(min_value=-1, max_value=2))
    s_low = max(0, min(n_low, s_low))
    return n_high, s_high, n_low, s_low


@given(boundary_cases())
def test_the_verdict_matches_integer_cross_multiplication(case) -> None:
    n_high, s_high, n_low, s_low = case
    report = four_fifths(group("H", n_high, s_high) + group("L", n_low, s_low))
    # rate_L / rate_H >= 4/5   <=>   5 * s_L * n_H >= 4 * s_H * n_L   (when L is the lower rate)
    # and the symmetric statement when H is the lower one; no division anywhere.
    low_is_lower = s_low * n_high <= s_high * n_low
    if low_is_lower:
        expected = 5 * s_low * n_high >= 4 * s_high * n_low
    else:
        expected = 5 * s_high * n_low >= 4 * s_low * n_high
    assert report.passed is expected


@given(
    st.lists(
        st.tuples(st.integers(min_value=100, max_value=250), st.integers(min_value=0, max_value=250)),
        min_size=2,
        max_size=5,
    )
)
def test_the_best_group_always_has_ratio_one_and_no_ratio_exceeds_it(sizes) -> None:
    records: list[tuple[str, str, bool]] = []
    for index, (n, picked) in enumerate(sizes):
        records.extend(group(f"G{index}", n, min(n, picked)))
    report = four_fifths(records)
    assert all(0.0 <= g.ratio <= 1.0 for g in report.groups)
    if report.reference_group is not None:
        assert by_name(report)[report.reference_group].ratio_exact == "1/1"
