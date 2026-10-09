"""Phase 1 calibration: the percentile of a raw score in a reference group (SPEC §7; DECISIONS D-50).

"Better fit than 82% of candidates" is the claim, so the number is a rank, not a rescaled sum. The
reference is chosen by a chain, first match wins:

    posting (this posting's scored candidates)
      -> family (the posting's job family)
      -> global (every posting)
      -> panel (the shipped synthetic profiles: the family's group, else the pooled group)

A group is only used when it holds at least `calibration.min_reference_n` (30) DISTINCT candidates.
People are counted, not (candidate, posting) pairs: five people each scored against 2,000 postings
make 10,000 pairs but still five people, and would mostly be compared with themselves. The result
names the group actually used (`CalibrationInfo.scope`), so `bands.basis[scope]` tells the truth.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Iterable

from matcher.calibrate.base import Calibrator, check_raw, positive_int
from matcher.calibrate.reference import GLOBAL_KEY, ReferenceDistribution
from matcher.models import CalibrationInfo


def _round_half_even(numerator: int, denominator: int) -> int:
    """round(numerator / denominator) for non-negative integers, exactly, halves to even.

    Python's round() does the same on floats, but 100 x (below + 0.5 x equal) / n is a ratio of
    small integers, so the integer form cannot be nudged by the order of float operations.
    """
    quotient, remainder = divmod(numerator, denominator)
    twice = 2 * remainder
    if twice > denominator or (twice == denominator and quotient % 2 == 1):
        quotient += 1
    return quotient


def percentile(raw: float, ref: ReferenceDistribution) -> int:
    """round(100 x (below + 0.5 x equal) / n), clamped to [0, 100] (D-50).

    `below` and `equal` count the reference scores under and equal to `raw`; `n` is the number of
    scores kept. Ties count half, so a raw score tied with the whole reference lands at 50.
    Monotone non-decreasing in `raw`: a better candidate is never shown a lower number.
    """
    value = check_raw(raw)
    samples = ref.samples
    n = len(samples)
    if n == 0:
        raise ValueError(f"the {ref.scope} reference {ref.key!r} has no scores to rank against")
    below = bisect_left(samples, value)
    equal = bisect_right(samples, value) - below
    # 100 x (below + equal / 2) / n  ==  100 x (2 x below + equal) / (2 x n)
    return max(0, min(100, _round_half_even(100 * (2 * below + equal), 2 * n)))


class _BoundPercentile:
    """One reference, chosen at `bind` time: a pure function of the raw score."""

    __slots__ = ("_reference", "_info")

    def __init__(self, reference: ReferenceDistribution) -> None:
        self._reference = reference
        self._info = CalibrationInfo(
            method="percentile", scope=reference.scope, n=reference.n_candidates, version=reference.version
        )

    def calibrate(self, raw: float) -> tuple[int, CalibrationInfo]:
        return percentile(raw, self._reference), self._info


class PercentileCalibrator:
    """The reference chain for percentile calibration; `bind` fixes it for one posting.

    `references` are every group known right now: live posting/family/global groups from scoring
    plus the shipped panel (scope "panel"). `min_reference_n` is `calibration.min_reference_n`.
    Scope "global" is a single pooled group (its key is only a label); every other group is
    identified by (scope, key), and two groups with the same identity are refused as ambiguous.
    """

    def __init__(self, references: Iterable[ReferenceDistribution], *, min_reference_n: int) -> None:
        self._min_n = positive_int(min_reference_n, "min_reference_n")
        self._groups: dict[tuple[str, str], ReferenceDistribution] = {}
        for reference in references:
            identity = (reference.scope, GLOBAL_KEY if reference.scope == "global" else reference.key)
            if identity in self._groups:
                raise ValueError(
                    f"more than one {reference.scope} reference for key {reference.key!r}: "
                    "the chain could not tell which one to use"
                )
            self._groups[identity] = reference

    def bind(self, *, posting_key: str | None, family: str | None) -> Calibrator:
        """The calibrator for one posting: the first group in the chain with enough candidates.

        `posting_key` and `family` may be None (a posting with no history, no family); that tier
        is skipped. Raises LookupError when no group qualifies, rather than inventing a score.
        """
        order: list[tuple[str, str]] = []
        if posting_key is not None:
            order.append(("posting", posting_key))
        if family is not None:
            order.append(("family", family))
        order.append(("global", GLOBAL_KEY))
        if family is not None:
            order.append(("panel", family))
        order.append(("panel", GLOBAL_KEY))

        tried: list[str] = []
        for identity in order:
            reference = self._groups.get(identity)
            if reference is not None and reference.n_candidates >= self._min_n:
                return _BoundPercentile(reference)
            found = "none" if reference is None else f"{reference.n_candidates} candidates"
            tried.append(f"{identity[0]} {identity[1]!r}: {found}")
        raise LookupError(
            f"no reference group has at least {self._min_n} distinct candidates for posting "
            f"{posting_key!r}, family {family!r} (tried {'; '.join(tried)})"
        )
