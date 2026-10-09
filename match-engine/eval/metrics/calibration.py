"""Calibration error, band accuracy and abstention statistics (EVAL.md section 3, secondary).

These answer "does 80 mean 80%?", "how far off are the bands?" and "are the abstentions the hard
ones?". Where a statistic is undefined on the data it is None, never 0.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from eval.metrics.pairs import PairScore


def expected_calibration_error(
    probs: Sequence[float], outcomes: Sequence[bool], bins: int = 10
) -> float | None:
    """ECE = sum over bins of (n_bin / N) * |mean(prob) - mean(outcome)|.

    Bins are equal-width on [0, 1]; a bin includes its lower edge and the last bin includes 1.0
    (bin index = min(floor(p * bins), bins - 1)). Probabilities outside [0, 1] raise ValueError,
    as does a length mismatch; empty input gives None.
    """
    if bins < 1:
        raise ValueError(f"bins must be at least 1, got {bins}")
    if len(probs) != len(outcomes):
        raise ValueError(f"probs and outcomes differ in length ({len(probs)} vs {len(outcomes)})")
    if not probs:
        return None
    bin_probs: list[list[float]] = [[] for _ in range(bins)]
    bin_hits = [0] * bins
    for prob, outcome in zip(probs, outcomes, strict=True):
        if not 0.0 <= prob <= 1.0:
            raise ValueError(f"probability outside [0, 1]: {prob!r}")
        index = min(int(prob * bins), bins - 1)
        bin_probs[index].append(prob)
        bin_hits[index] += 1 if outcome else 0
    total = len(probs)
    return math.fsum(
        (len(in_bin) / total) * abs(math.fsum(in_bin) / len(in_bin) - hits / len(in_bin))
        for in_bin, hits in zip(bin_probs, bin_hits, strict=True)
        if in_bin
    )


def band_accuracy(pairs: Sequence[PairScore]) -> dict[str, float | None]:
    """Share of pairs whose predicted band is exact, one off, or two or more off.

    Only pairs with both `band_pred` and `band_true` are counted. Off by two or more is the one to
    alarm on (EVAL.md section 3). With no such pair the shares are undefined and come back None.
    """
    gaps = [
        abs(p.band_pred - p.band_true) for p in pairs if p.band_pred is not None and p.band_true is not None
    ]
    if not gaps:
        return {"exact": None, "off_by_one": None, "off_by_two_plus": None}
    return {
        "exact": sum(1 for gap in gaps if gap == 0) / len(gaps),
        "off_by_one": sum(1 for gap in gaps if gap == 1) / len(gaps),
        "off_by_two_plus": sum(1 for gap in gaps if gap >= 2) / len(gaps),
    }


def abstention_stats(pairs: Sequence[PairScore]) -> dict[str, float | None]:
    """Abstention rate and abstention precision (SPEC section 17).

    rate is the share of pairs the engine declined to score. precision is the share of those
    abstentions that were on `hard` pairs, i.e. whether it refuses the right ones. precision is
    None when there are no abstentions; both are None for no pairs.
    """
    if not pairs:
        return {"rate": None, "precision": None}
    abstained = [p for p in pairs if p.score is None]
    return {
        "rate": len(abstained) / len(pairs),
        "precision": sum(1 for p in abstained if p.hard) / len(abstained) if abstained else None,
    }
