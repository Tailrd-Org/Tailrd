"""Drift alarms (EVAL section 6) and the population stability index.

EVAL section 6 pages on any of these, and each is one alarm here with a plain message:

    taxonomy coverage                 below 92 %  (SPEC section 16; the same number as
                                                  taxonomy.coverage_alert_threshold in config/scoring.yaml)
    abstention rate                   above 20 %, or above 2x the 30-day baseline
    score distribution PSI            above 0.25 against the 30-day baseline
    four-fifths ratio, any group      below 0.80
    p95 latency                       above 300 ms

Every comparison is strict: a metric sitting exactly on its limit is healthy. A metric that is
None was not measured and raises no alarm; a metric that is NaN, infinite, negative or out of its
range (a percentage where a fraction belongs) raises ValueError, because silently passing a broken
measurement is the failure a monitor exists to prevent. The thresholds are module constants, not
parameters; the fairness floor in particular is never a knob (CLAUDE.md).
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections.abc import Sequence
from typing import Final, Optional

from matcher.audit.fairness import FOUR_FIFTHS
from matcher.models import Frozen

__all__ = [
    "ABSTENTION_BASELINE_FACTOR",
    "ABSTENTION_CEILING",
    "ABSTENTION_HIGH",
    "ABSTENTION_RISING",
    "COVERAGE_FLOOR",
    "COVERAGE_LOW",
    "FAIRNESS_FLOOR",
    "FOUR_FIFTHS_VIOLATION",
    "LATENCY_HIGH",
    "LATENCY_P95_CEILING_MS",
    "PSI_CEILING",
    "PSI_EPSILON",
    "SCORE_DRIFT",
    "DriftAlarm",
    "drift_alarms",
    "psi",
]

COVERAGE_FLOOR: Final = 0.92
ABSTENTION_CEILING: Final = 0.20
ABSTENTION_BASELINE_FACTOR: Final = 2.0
PSI_CEILING: Final = 0.25
FAIRNESS_FLOOR: Final = float(FOUR_FIFTHS)  # 0.80: one definition of four-fifths, in fairness.py
LATENCY_P95_CEILING_MS: Final = 300.0

# An empty bin has no logarithm; PSI floors every bin's share at this value, the usual convention.
PSI_EPSILON: Final = 1e-4

COVERAGE_LOW: Final = "COVERAGE_LOW"
ABSTENTION_HIGH: Final = "ABSTENTION_HIGH"
ABSTENTION_RISING: Final = "ABSTENTION_RISING"
SCORE_DRIFT: Final = "SCORE_DRIFT"
FOUR_FIFTHS_VIOLATION: Final = "FOUR_FIFTHS_VIOLATION"
LATENCY_HIGH: Final = "LATENCY_HIGH"


class DriftAlarm(Frozen):
    """One tripped limit: what was measured, the limit it crossed, and a sentence for a person."""

    code: str
    value: float
    threshold: float
    message: str


# --- population stability index ----------------------------------------------------------------


def _sample(values: Sequence[float], name: str) -> list[float]:
    numbers = list(values)
    if not numbers:
        raise ValueError(f"{name} sample is empty")
    for number in numbers:
        try:
            finite = math.isfinite(number)
        except TypeError:
            raise ValueError(f"{name} sample holds a non-number") from None
        if not finite:
            raise ValueError(f"{name} sample holds a NaN or an infinity")
    return numbers


def psi(reference: Sequence[float], current: Sequence[float], *, bins: int = 10) -> float:
    """Population stability index of `current` against `reference`: sum((a - e) * ln(a / e)).

    The reference defines the bins: its `bins - 1` quantile edges (nearest rank, so each bin holds
    about 1/bins of the reference), with duplicate edges collapsed when ties make bins
    indistinguishable. Bins are right-closed, `(edge_{i-1}, edge_i]`, with an open bottom and top, so
    values outside the reference's range fall in the end bins. `e` and `a` are each bin's share of
    the reference and of the current sample; a share below 1e-4 (an empty bin) is floored at 1e-4.
    Because the reference sets the bins, `psi(a, b)` and `psi(b, a)` differ. 0 means identical
    shares; EVAL section 6 alarms above 0.25. Raises ValueError for an empty or non-finite sample
    or `bins < 2`. Small samples make PSI noisy: compare windows of a few hundred scores or more.
    """
    if isinstance(bins, bool) or not isinstance(bins, int) or bins < 2:
        raise ValueError(f"bins must be an integer of at least 2, got {bins!r}")
    ref = sorted(_sample(reference, "reference"))
    cur = _sample(current, "current")
    n = len(ref)
    # k-th quantile edge: the ceil(k * n / bins)-th smallest reference value.
    edges = sorted({ref[-(-k * n // bins) - 1] for k in range(1, bins)})
    expected = [0] * (len(edges) + 1)
    actual = [0] * (len(edges) + 1)
    for value in ref:
        expected[bisect_left(edges, value)] += 1
    for value in cur:
        actual[bisect_left(edges, value)] += 1
    terms = []
    for e_count, a_count in zip(expected, actual):
        e = max(e_count / n, PSI_EPSILON)
        a = max(a_count / len(cur), PSI_EPSILON)
        terms.append((a - e) * math.log(a / e))
    return math.fsum(terms)


# --- alarms ------------------------------------------------------------------------------------


def _check(name: str, value: Optional[float], upper: Optional[float] = None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number or None, got {value!r}")
    if value < 0 or (upper is not None and value > upper):
        bound = f"between 0 and {upper:g}" if upper is not None else "at least 0"
        raise ValueError(f"{name} must be {bound}, got {value!r}")


def drift_alarms(
    *,
    coverage: Optional[float],
    abstention_rate: Optional[float],
    abstention_baseline: Optional[float],
    psi_value: Optional[float],
    min_fairness_ratio: Optional[float],
    p95_latency_ms: Optional[float],
) -> tuple[DriftAlarm, ...]:
    """The alarms EVAL section 6 pages on, in a fixed order: coverage, abstention (absolute, then
    against the baseline), score drift, four-fifths, latency.

    `coverage`, `abstention_rate`, `abstention_baseline` are fractions in [0, 1];
    `min_fairness_ratio` is `FairnessReport.min_ratio` (computed in exact arithmetic, never by float
    division); `psi_value` comes from `psi`; `p95_latency_ms` is in milliseconds. None skips a check.
    """
    _check("coverage", coverage, 1.0)
    _check("abstention_rate", abstention_rate, 1.0)
    _check("abstention_baseline", abstention_baseline, 1.0)
    _check("psi_value", psi_value)
    _check("min_fairness_ratio", min_fairness_ratio, 1.0)
    _check("p95_latency_ms", p95_latency_ms)

    alarms: list[DriftAlarm] = []
    if coverage is not None and coverage < COVERAGE_FLOOR:
        alarms.append(
            DriftAlarm(
                code=COVERAGE_LOW,
                value=coverage,
                threshold=COVERAGE_FLOOR,
                message=(
                    f"Taxonomy coverage is {coverage:.2%}, below the {COVERAGE_FLOOR:.0%} floor: "
                    "more of the skills in job postings are going unrecognised than expected."
                ),
            )
        )
    if abstention_rate is not None:
        if abstention_rate > ABSTENTION_CEILING:
            alarms.append(
                DriftAlarm(
                    code=ABSTENTION_HIGH,
                    value=abstention_rate,
                    threshold=ABSTENTION_CEILING,
                    message=(
                        f"The engine withheld a score on {abstention_rate:.2%} of pairs, "
                        f"above the {ABSTENTION_CEILING:.0%} ceiling."
                    ),
                )
            )
        if abstention_baseline is not None:
            line = ABSTENTION_BASELINE_FACTOR * abstention_baseline  # doubling is exact in binary
            if abstention_rate > line:
                alarms.append(
                    DriftAlarm(
                        code=ABSTENTION_RISING,
                        value=abstention_rate,
                        threshold=line,
                        message=(
                            f"The engine withheld a score on {abstention_rate:.2%} of pairs, more than "
                            f"twice the 30-day baseline of {abstention_baseline:.2%}."
                        ),
                    )
                )
    if psi_value is not None and psi_value > PSI_CEILING:
        alarms.append(
            DriftAlarm(
                code=SCORE_DRIFT,
                value=psi_value,
                threshold=PSI_CEILING,
                message=(
                    "The score distribution has moved away from the 30-day baseline "
                    f"(PSI {psi_value:.4f}, limit {PSI_CEILING:.2f})."
                ),
            )
        )
    # `min_fairness_ratio` must come from FairnessReport.min_ratio: exact arithmetic, then one correctly
    # rounded float(). The double nearest 0.80 lies just above 4/5, so for such a value `< 0.80` is
    # true exactly when the exact ratio is below 4/5 (a ratio of exactly 4/5 arrives as 0.8 and is
    # healthy). A caller who divides floats itself can get 0.7999999999999999 for 4/5 and a false alarm.
    if min_fairness_ratio is not None and min_fairness_ratio < FAIRNESS_FLOOR:
        alarms.append(
            DriftAlarm(
                code=FOUR_FIFTHS_VIOLATION,
                value=min_fairness_ratio,
                threshold=FAIRNESS_FLOOR,
                message=(
                    f"The lowest group selection ratio is {min_fairness_ratio:.4f}, below the "
                    f"four-fifths floor of {FAIRNESS_FLOOR:.2f}. This is a fairness failure: "
                    "stop and surface it, do not tune around it."
                ),
            )
        )
    if p95_latency_ms is not None and p95_latency_ms > LATENCY_P95_CEILING_MS:
        alarms.append(
            DriftAlarm(
                code=LATENCY_HIGH,
                value=p95_latency_ms,
                threshold=LATENCY_P95_CEILING_MS,
                message=(
                    f"p95 scoring latency is {p95_latency_ms:.1f} ms, "
                    f"above the {LATENCY_P95_CEILING_MS:.0f} ms limit."
                ),
            )
        )
    return tuple(alarms)
