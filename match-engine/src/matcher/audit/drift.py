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

PSI is noisy on small samples, so it can alarm only when both windows hold at least
`PSI_MIN_SAMPLE` (200) scores. The caller says how many scores each side holds
(`psi_reference_n`, `psi_current_n`); a PSI over fewer is not evaluated, and `evaluate_drift`
reports that as a skipped check with the reason "insufficient sample" instead of raising an alarm
(`drift_alarms` returns the alarms alone). A monitor that logs only alarms would hide the skip, so
the scheduled job should use `evaluate_drift` and log `skipped`.
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
    "INSUFFICIENT_SAMPLE",
    "LATENCY_HIGH",
    "LATENCY_P95_CEILING_MS",
    "PSI_CEILING",
    "PSI_EPSILON",
    "PSI_MIN_SAMPLE",
    "SCORE_DRIFT",
    "DriftAlarm",
    "DriftReport",
    "SkippedCheck",
    "drift_alarms",
    "evaluate_drift",
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
# PSI over fewer scores than this on either side is noise, not drift: it is not evaluated.
PSI_MIN_SAMPLE: Final = 200

COVERAGE_LOW: Final = "COVERAGE_LOW"
ABSTENTION_HIGH: Final = "ABSTENTION_HIGH"
ABSTENTION_RISING: Final = "ABSTENTION_RISING"
SCORE_DRIFT: Final = "SCORE_DRIFT"
FOUR_FIFTHS_VIOLATION: Final = "FOUR_FIFTHS_VIOLATION"
LATENCY_HIGH: Final = "LATENCY_HIGH"

INSUFFICIENT_SAMPLE: Final = "insufficient sample"


class DriftAlarm(Frozen):
    """One tripped limit: what was measured, the limit it crossed, and a sentence for a person."""

    code: str
    value: float
    threshold: float
    message: str


class SkippedCheck(Frozen):
    """A check that was not evaluated, and why. Not an alarm: nothing is wrong, nothing was learned."""

    code: str  # the alarm code that could not be evaluated
    reason: str
    message: str


class DriftReport(Frozen):
    """What `evaluate_drift` found: the alarms, and the checks it could not evaluate."""

    alarms: tuple[DriftAlarm, ...]
    skipped: tuple[SkippedCheck, ...]


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
    or `bins < 2`. This is plain arithmetic and works on any size, but a PSI over fewer than
    `PSI_MIN_SAMPLE` (200) scores on either side is noise: `evaluate_drift` will not alarm on it.
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


def _check_count(name: str, value: Optional[int]) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{name} must be a whole number of scores (0 or more) or None, got {value!r}")


def _count(value: Optional[int]) -> str:
    return "unknown" if value is None else str(value)


def evaluate_drift(
    *,
    coverage: Optional[float],
    abstention_rate: Optional[float],
    abstention_baseline: Optional[float],
    psi_value: Optional[float],
    min_fairness_ratio: Optional[float],
    p95_latency_ms: Optional[float],
    psi_reference_n: Optional[int] = None,
    psi_current_n: Optional[int] = None,
) -> DriftReport:
    """The alarms EVAL section 6 pages on, in a fixed order, plus the checks that were skipped.

    Order: coverage, abstention (absolute, then against the baseline), score drift, four-fifths,
    latency. `coverage`, `abstention_rate`, `abstention_baseline` are fractions in [0, 1];
    `min_fairness_ratio` is `FairnessReport.min_ratio` (computed in exact arithmetic, never by float
    division; None when the report was not assessed, which raises nothing and must be reported as
    "not assessed" by the caller); `psi_value` comes from `psi`; `p95_latency_ms` is in milliseconds.
    None skips a check. `psi_reference_n` and `psi_current_n` are the number of scores on each side
    of the PSI: if either is known and below `PSI_MIN_SAMPLE` the PSI is not evaluated and appears
    in `skipped` with the reason "insufficient sample". Without them the PSI is evaluated as given,
    so pass them.
    """
    _check("coverage", coverage, 1.0)
    _check("abstention_rate", abstention_rate, 1.0)
    _check("abstention_baseline", abstention_baseline, 1.0)
    _check("psi_value", psi_value)
    _check("min_fairness_ratio", min_fairness_ratio, 1.0)
    _check("p95_latency_ms", p95_latency_ms)
    _check_count("psi_reference_n", psi_reference_n)
    _check_count("psi_current_n", psi_current_n)

    alarms: list[DriftAlarm] = []
    skipped: list[SkippedCheck] = []
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
    if psi_value is not None:
        known = [n for n in (psi_reference_n, psi_current_n) if n is not None]
        if any(n < PSI_MIN_SAMPLE for n in known):
            skipped.append(
                SkippedCheck(
                    code=SCORE_DRIFT,
                    reason=INSUFFICIENT_SAMPLE,
                    message=(
                        f"PSI was not evaluated: {INSUFFICIENT_SAMPLE} (reference {_count(psi_reference_n)}, "
                        f"current {_count(psi_current_n)}; at least {PSI_MIN_SAMPLE} scores are needed "
                        "on each side)."
                    ),
                )
            )
        elif psi_value > PSI_CEILING:
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
    return DriftReport(alarms=tuple(alarms), skipped=tuple(skipped))


def drift_alarms(
    *,
    coverage: Optional[float],
    abstention_rate: Optional[float],
    abstention_baseline: Optional[float],
    psi_value: Optional[float],
    min_fairness_ratio: Optional[float],
    p95_latency_ms: Optional[float],
    psi_reference_n: Optional[int] = None,
    psi_current_n: Optional[int] = None,
) -> tuple[DriftAlarm, ...]:
    """The alarms of `evaluate_drift` and nothing else: a skipped check is not reported here."""
    return evaluate_drift(
        coverage=coverage,
        abstention_rate=abstention_rate,
        abstention_baseline=abstention_baseline,
        psi_value=psi_value,
        min_fairness_ratio=min_fairness_ratio,
        p95_latency_ms=p95_latency_ms,
        psi_reference_n=psi_reference_n,
        psi_current_n=psi_current_n,
    ).alarms
