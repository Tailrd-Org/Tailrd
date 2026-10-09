"""Selection rates and the four-fifths guardrail (EVAL.md section 3: ratio >= 0.80 for n >= 100).

A guardrail failure is terminal in this project (CLAUDE.md), so the edges matter: the threshold is
inclusive, a small group can never decide the verdict, and float rounding must not turn an exact
0.80 into a failure.
"""

from __future__ import annotations

import pytest

from eval.metrics.fairness import four_fifths, selection_rates
from eval.metrics.pairs import PairScore


def selected(p: PairScore) -> bool:
    """The shortlist rule used by these tests: scored at or above 0.5."""
    return p.score is not None and p.score >= 0.5


def group_of(group: str, n: int, n_selected: int) -> list[PairScore]:
    """n pairs in `group`; the first n_selected score 0.9 (shortlisted), the rest 0.1."""
    return [
        PairScore(
            posting_id="p",
            pair_id=f"{group}{i}",
            score=0.9 if i < n_selected else 0.1,
            relevance=2,
            group=group,
        )
        for i in range(n)
    ]


def test_selection_rates_report_group_size_and_rate_for_eligible_groups_only():
    pairs = group_of("A", 200, 100) + group_of("B", 150, 60) + group_of("small", 40, 10)
    assert selection_rates(pairs, selected) == {"A": (200, 0.5), "B": (150, 0.4)}


def test_min_group_n_is_inclusive_and_configurable():
    pairs = group_of("A", 100, 50) + group_of("B", 99, 50)
    assert set(selection_rates(pairs, selected)) == {"A"}  # 100 is enough, 99 is not
    assert set(selection_rates(pairs, selected, min_group_n=99)) == {"A", "B"}


def test_four_fifths_fails_at_ratio_079_and_a_small_group_is_excluded_not_counted():
    # A: 100/200 = 0.500 (the best rate).  B: 79/200 = 0.395 -> ratio 0.395 / 0.5 = 0.79 < 0.80.
    # C has only 40 pairs and a rate of 0; it is reported as excluded and never decides.
    pairs = group_of("A", 200, 100) + group_of("B", 200, 79) + group_of("C", 40, 0)
    result = four_fifths(pairs, selected)
    assert result.passed is False
    assert result.min_ratio == pytest.approx(0.79)
    assert result.groups_excluded == ("C",)
    assert set(result.per_group) == {"A", "B"}
    assert result.per_group["A"].ratio == pytest.approx(1.0)
    assert result.per_group["B"].ratio == pytest.approx(0.79)
    assert (result.per_group["B"].n, result.per_group["B"].rate) == (200, pytest.approx(0.395))


def test_a_small_group_cannot_fail_an_otherwise_fair_selection():
    # A 0.50, B 0.45 -> ratio 0.90 passes. C (n=40, rate 0) would fail it if it were counted.
    pairs = group_of("A", 200, 100) + group_of("B", 200, 90) + group_of("C", 40, 0)
    result = four_fifths(pairs, selected)
    assert result.passed is True
    assert result.min_ratio == pytest.approx(0.90)
    assert result.groups_excluded == ("C",)
    # Lowering the size floor is what brings C into the verdict, and then it fails.
    assert four_fifths(pairs, selected, min_group_n=40).passed is False


def test_a_ratio_of_exactly_point_eight_passes_even_where_float_division_would_say_less():
    # A: 5/100 = 0.05, B: 4/100 = 0.04: the exact ratio is 4/5. In binary floating point
    # 0.04 / 0.05 is 0.7999999999999999, which would fail a naive `>= 0.80` and wrongly stop a
    # build on a terminal guardrail.
    assert (4 / 100) / (5 / 100) < 0.80
    result = four_fifths(group_of("A", 100, 5) + group_of("B", 100, 4), selected)
    assert result.passed is True
    assert result.min_ratio == 0.8


def test_just_below_the_threshold_fails():
    # 79/100 against 100/100 is 0.79: no rounding can hide it.
    result = four_fifths(group_of("A", 100, 100) + group_of("B", 100, 79), selected)
    assert result.passed is False


def test_three_groups_are_compared_to_the_best_groups_rate():
    # A 0.50, B 0.45, C 0.30 -> ratios 1.00, 0.90, 0.60; the minimum (0.60) is C.
    pairs = group_of("A", 200, 100) + group_of("B", 200, 90) + group_of("C", 100, 30)
    result = four_fifths(pairs, selected)
    assert {g: r.ratio for g, r in result.per_group.items()} == pytest.approx({"A": 1.0, "B": 0.9, "C": 0.6})
    assert result.min_ratio == pytest.approx(0.6)
    assert result.passed is False
    assert result.groups_excluded == ()


def test_no_eligible_group_passes_with_no_ratio():
    pairs = group_of("A", 40, 20) + group_of("B", 30, 5)
    result = four_fifths(pairs, selected)
    assert result.passed is True
    assert result.min_ratio is None
    assert result.per_group == {}
    assert result.groups_excluded == ("A", "B")
    assert four_fifths([], selected).groups_excluded == ()


def test_a_single_eligible_group_is_its_own_reference():
    result = four_fifths(group_of("A", 120, 30), selected)
    assert result.min_ratio == 1.0
    assert result.passed is True


def test_when_nobody_is_selected_anywhere_the_groups_are_at_parity():
    result = four_fifths(group_of("A", 100, 0) + group_of("B", 100, 0), selected)
    assert result.min_ratio == 1.0
    assert result.passed is True
    assert {r.ratio for r in result.per_group.values()} == {1.0}


def test_pairs_without_a_group_are_not_assessable_and_are_ignored():
    pairs = group_of("A", 100, 50) + group_of("B", 100, 50)
    ungrouped = [PairScore(posting_id="p", pair_id=f"u{i}", score=0.1, relevance=2) for i in range(500)]
    assert selection_rates(pairs + ungrouped, selected) == {"A": (100, 0.5), "B": (100, 0.5)}
    assert four_fifths(pairs + ungrouped, selected).groups_excluded == ()


def test_abstained_pairs_count_toward_group_size():
    # Group B: 100 pairs, 40 of them abstain (score None, never shortlisted), 30 shortlisted.
    # Its rate is 30/100 = 0.30, not 30/60.
    abstained = [
        PairScore(posting_id="p", pair_id=f"B-abstain{i}", score=None, relevance=2, group="B")
        for i in range(40)
    ]
    scored = [
        PairScore(posting_id="p", pair_id=f"B{i}", score=0.9 if i < 30 else 0.1, relevance=2, group="B")
        for i in range(60)
    ]
    assert selection_rates(abstained + scored, selected) == {"B": (100, 0.30)}


def test_the_selected_rule_is_applied_to_each_pair_once_and_decides_membership():
    seen: list[str] = []

    def rule(p: PairScore) -> bool:
        seen.append(p.pair_id)
        return p.score is not None and p.score >= 0.5

    pairs = group_of("A", 100, 50)
    selection_rates(pairs, rule)
    assert sorted(seen) == sorted(p.pair_id for p in pairs)


def test_min_group_n_must_be_positive():
    with pytest.raises(ValueError):
        selection_rates(group_of("A", 10, 5), selected, min_group_n=0)
    with pytest.raises(ValueError):
        four_fifths(group_of("A", 10, 5), selected, min_group_n=0)
