"""Four-fifths monitoring on opaque-id records (SPEC section 9; EVAL section 3).

The monitor sees `(opaque_id, group, selected)` and nothing else: no user id, no score, no text.
Demographic groups are separate, voluntary data keyed by opaque id and never reach the scorer
(SPEC section 9), so this module knows groups only as labels.

* A group's selection rate is `selected records / records`: every scored pair of every person in
  the group counts, and an abstained or gated pair is a record that was not selected.
* A group is assessed only when it holds at least `min_group_n` PEOPLE (distinct opaque ids), not
  records. One person scored against 2,000 postings is one person (DECISIONS D-50 applies the same
  rule to calibration). Smaller groups are listed as excluded and take no part in the verdict.
* The verdict compares each assessed group's rate with the best assessed rate in exact rational
  arithmetic (`fractions.Fraction`), so float rounding can neither hide a violation nor invent one:
  5/100 against 4/100 is exactly 4/5 and passes, though 0.04 / 0.05 is 0.7999999999999999.
* The threshold is 4/5 and is not a parameter. A fairness failure is terminal (CLAUDE.md): nobody
  tunes around it, moves the threshold or drops the failing group.
* Fewer than two assessed groups means nothing was compared: `passed` is True (no violation was
  found) and `assessed` is False, so a report can say "not assessed" rather than "fair".
"""

from __future__ import annotations

from collections.abc import Iterable
from fractions import Fraction
from typing import Optional

from matcher.models import Frozen

__all__ = ["FOUR_FIFTHS", "MIN_GROUP_N", "ExcludedGroup", "FairnessReport", "GroupSelection", "four_fifths"]

FOUR_FIFTHS = Fraction(4, 5)
MIN_GROUP_N = 100  # EVAL section 3: groups with n >= 100


def _exact(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


class GroupSelection(Frozen):
    """One assessed group: its size, its selection rate, and that rate relative to the best group's."""

    group: str
    n_people: int
    n_records: int
    selected: int
    rate: float
    rate_exact: str  # "selected/records" reduced, e.g. "9/20"
    ratio: float
    ratio_exact: str
    meets: bool  # ratio >= 4/5


class ExcludedGroup(Frozen):
    """A group below the size floor. Reported, never counted in the verdict."""

    group: str
    n_people: int
    n_records: int


class FairnessReport(Frozen):
    """The four-fifths verdict.

    `min_ratio` is the lowest assessed ratio (None when no group was assessed); `passed` is
    `min_ratio >= 4/5`, or True when nothing was assessed. `reference_group` is the group with the
    best rate (the smallest id on a tie), None when nobody was selected anywhere.
    """

    threshold: str
    min_group_n: int
    groups: tuple[GroupSelection, ...]
    excluded: tuple[ExcludedGroup, ...]
    reference_group: Optional[str]
    min_ratio: Optional[float]
    min_ratio_exact: Optional[str]
    passed: bool

    @property
    def assessed(self) -> bool:
        """True when at least two groups were large enough to be compared with each other."""
        return len(self.groups) >= 2


def _unpack(record: object) -> tuple[str, str, bool]:
    if not isinstance(record, (tuple, list)) or len(record) != 3:
        raise ValueError("each record must be (opaque_id, group, selected)")
    opaque, label, picked = record
    if not isinstance(opaque, str) or not opaque:
        raise ValueError("opaque_id must be a non-empty string")
    if not isinstance(label, str) or not label:
        raise ValueError("group must be a non-empty string")
    if not isinstance(picked, bool):
        raise ValueError("selected must be True or False")
    return opaque, label, picked


def four_fifths(
    records: Iterable[tuple[str, str, bool]], *, min_group_n: int = MIN_GROUP_N
) -> FairnessReport:
    """Every assessed group's selection rate against the best rate, against exactly 4/5.

    `records` are `(opaque_id, group, selected)`, one per scored pair; a person may appear in many
    records but only in one group. Raises ValueError for a malformed record, for a person assigned
    to two groups, and for `min_group_n < 1`. Output is ordered by group id, so it does not depend
    on the order of `records`.
    """
    if isinstance(min_group_n, bool) or not isinstance(min_group_n, int) or min_group_n < 1:
        raise ValueError(f"min_group_n must be a positive integer, got {min_group_n!r}")
    group_of: dict[str, str] = {}
    people: dict[str, set[str]] = {}
    n_records: dict[str, int] = {}
    n_selected: dict[str, int] = {}
    for record in records:
        opaque, label, picked = _unpack(record)
        if group_of.setdefault(opaque, label) != label:
            raise ValueError("an opaque id is assigned to two groups; each person belongs to exactly one")
        people.setdefault(label, set()).add(opaque)
        n_records[label] = n_records.get(label, 0) + 1
        n_selected[label] = n_selected.get(label, 0) + (1 if picked else 0)

    eligible = sorted(label for label in people if len(people[label]) >= min_group_n)
    excluded = tuple(
        ExcludedGroup(group=label, n_people=len(people[label]), n_records=n_records[label])
        for label in sorted(people)
        if len(people[label]) < min_group_n
    )
    if not eligible:
        return FairnessReport(
            threshold=_exact(FOUR_FIFTHS),
            min_group_n=min_group_n,
            groups=(),
            excluded=excluded,
            reference_group=None,
            min_ratio=None,
            min_ratio_exact=None,
            passed=True,
        )

    rates = {label: Fraction(n_selected[label], n_records[label]) for label in eligible}
    best = max(rates.values())
    # Nobody selected anywhere: every group is at parity, there is no best rate to be measured against.
    ratios = {label: (rates[label] / best if best else Fraction(1)) for label in eligible}
    lowest = min(ratios.values())
    return FairnessReport(
        threshold=_exact(FOUR_FIFTHS),
        min_group_n=min_group_n,
        groups=tuple(
            GroupSelection(
                group=label,
                n_people=len(people[label]),
                n_records=n_records[label],
                selected=n_selected[label],
                rate=float(rates[label]),
                rate_exact=_exact(rates[label]),
                ratio=float(ratios[label]),
                ratio_exact=_exact(ratios[label]),
                meets=ratios[label] >= FOUR_FIFTHS,
            )
            for label in eligible
        ),
        excluded=excluded,
        reference_group=next((label for label in eligible if rates[label] == best), None) if best else None,
        min_ratio=float(lowest),
        min_ratio_exact=_exact(lowest),
        passed=lowest >= FOUR_FIFTHS,
    )
