"""Selection rates and the four-fifths guardrail (EVAL.md section 3: ratio >= 0.80, n >= 100).

A fairness guardrail failure is terminal in this project (CLAUDE.md): nothing here takes the
threshold as an argument, and the comparison is done in exact rational arithmetic so floating
point rounding can neither flip a verdict at the boundary nor hide one.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from fractions import Fraction

from eval.metrics.pairs import PairScore
from matcher.models import Frozen

# The four-fifths rule. Fixed on purpose: it is not a knob (CLAUDE.md hard rules).
_THRESHOLD = Fraction(4, 5)
FOUR_FIFTHS_THRESHOLD = float(_THRESHOLD)  # for reports; the verdict compares exact fractions


class GroupRate(Frozen):
    """One eligible group: how many pairs it has, its selection rate, and that rate relative to the
    best group's rate (the ratio the four-fifths rule is about)."""

    n: int
    rate: float
    ratio: float


class FairnessResult(Frozen):
    """Outcome of the four-fifths check.

    `min_ratio` is the lowest group ratio and `passed` is `min_ratio >= 0.80`. Groups below the
    size floor are listed in `groups_excluded` and take no part in the verdict. With no eligible
    group there is nothing to compare: `min_ratio` is None and `passed` is True.
    """

    min_ratio: float | None
    per_group: dict[str, GroupRate]
    passed: bool
    groups_excluded: tuple[str, ...]


def _check_min_group_n(min_group_n: int) -> None:
    if min_group_n < 1:
        raise ValueError(f"min_group_n must be at least 1, got {min_group_n}")


def _group_counts(
    pairs: Sequence[PairScore], selected: Callable[[PairScore], bool]
) -> dict[str, tuple[int, int]]:
    """group -> (pairs, selected pairs), groups in ascending id order. A pair with no group cannot
    be assessed and is skipped."""
    totals: dict[str, list[int]] = {}
    for pair in pairs:
        if pair.group is None:
            continue
        counts = totals.setdefault(pair.group, [0, 0])
        counts[0] += 1
        counts[1] += 1 if selected(pair) else 0
    return {group: (counts[0], counts[1]) for group, counts in sorted(totals.items())}


def selection_rates(
    pairs: Sequence[PairScore], selected: Callable[[PairScore], bool], min_group_n: int = 100
) -> dict[str, tuple[int, float]]:
    """group -> (pairs, selection rate) for the groups with at least `min_group_n` pairs.

    `selected` says whether a pair is shortlisted; abstained pairs count in the group's size.
    """
    _check_min_group_n(min_group_n)
    return {
        group: (n, hits / n)
        for group, (n, hits) in _group_counts(pairs, selected).items()
        if n >= min_group_n
    }


def four_fifths(
    pairs: Sequence[PairScore], selected: Callable[[PairScore], bool], min_group_n: int = 100
) -> FairnessResult:
    """Every eligible group's selection rate relative to the highest rate, against 0.80.

    If nobody is selected in any group the rates are all zero and the groups are at parity, so
    every ratio is 1.0.
    """
    _check_min_group_n(min_group_n)
    counts = _group_counts(pairs, selected)
    eligible = {group: c for group, c in counts.items() if c[0] >= min_group_n}
    excluded = tuple(group for group in counts if group not in eligible)
    if not eligible:
        return FairnessResult(min_ratio=None, per_group={}, passed=True, groups_excluded=excluded)
    rates = {group: Fraction(hits, n) for group, (n, hits) in eligible.items()}
    best = max(rates.values())
    ratios = {group: (rate / best if best else Fraction(1)) for group, rate in rates.items()}
    lowest = min(ratios.values())
    return FairnessResult(
        min_ratio=float(lowest),
        per_group={
            group: GroupRate(n=eligible[group][0], rate=float(rates[group]), ratio=float(ratios[group]))
            for group in eligible
        },
        passed=lowest >= _THRESHOLD,
        groups_excluded=excluded,
    )
