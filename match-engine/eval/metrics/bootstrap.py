"""Bootstrap confidence intervals over postings, and paired deltas against a baseline.

EVAL.md section 3: "Always report confidence intervals. Bootstrap 1,000 resamples over postings
(not pairs - pairs within a posting are correlated)." Section 5.3: a gain counts only if its CI
excludes zero and the point estimate clears a minimum effect; "an NDCG improvement of 0.004 with a
CI of +-0.02 is noise, and the harness should say so".

Every random draw comes from `numpy.random.default_rng(seed)` with an explicit seed, postings are
indexed in ascending id order and every mean is summed with `math.fsum`, so the same data and seed
give the same numbers whatever order the pairs arrive in. numpy is imported inside the functions
that need it (DECISIONS D-02).
"""

from __future__ import annotations

import operator
from collections.abc import Iterator, Sequence
from typing import Literal

from eval.metrics.pairs import Metric, PairScore, group_by_posting
from matcher.models import Frozen

DEFAULT_ALPHA = 0.05  # 95% intervals, as in the EVAL.md section 4 table

Verdict = Literal["PASS", "FAIL", "NOISE"]


class CI(Frozen):
    """A metric's value on the full data and its percentile interval over posting resamples.

    All three are None when the metric is undefined (no data, or no qualifying posting).
    `low` and `high` are also None if the metric was undefined on every resample.
    """

    point: float | None
    low: float | None
    high: float | None


class DeltaCI(Frozen):
    """current - baseline on the postings both runs share, with its interval and a verdict.

    PASS: the interval lies entirely on the good side of zero and the effect is at least
    `min_effect`. FAIL: the interval lies entirely on the bad side. NOISE: anything else,
    including a delta that is undefined.
    """

    point: float | None
    low: float | None
    high: float | None
    verdict: Verdict


class _Resampler:
    """Builds the concatenated pairs of a with-replacement draw of postings.

    A posting drawn more than once appears as separate postings: its first copy keeps its id and
    later copies get a suffixed one. Without that, a repeated draw would merge into one larger
    posting and any per-posting metric (NDCG, P@k) would quietly measure something else. The suffix
    separator is a run of '#' longer than any run found in the real ids, so a relabelled id can
    never equal a real one or another relabelled one. Copies are built once and reused.
    """

    def __init__(self, groups: Sequence[Sequence[PairScore]]) -> None:
        self._groups = [list(group) for group in groups]
        separator = "#"
        while any(separator in group[0].posting_id for group in self._groups):
            separator += "#"
        self._separator = separator
        self._copies: dict[tuple[int, int], list[PairScore]] = {}

    def __len__(self) -> int:
        return len(self._groups)

    def sample(self, draw: Sequence[int]) -> list[PairScore]:
        times_drawn: dict[int, int] = {}
        out: list[PairScore] = []
        for index in draw:
            occurrence = times_drawn.get(index, 0)
            times_drawn[index] = occurrence + 1
            out.extend(self._groups[index] if occurrence == 0 else self._copy(index, occurrence))
        return out

    def _copy(self, index: int, occurrence: int) -> list[PairScore]:
        key = (index, occurrence)
        if key not in self._copies:
            self._copies[key] = [
                p.model_copy(update={"posting_id": f"{p.posting_id}{self._separator}{occurrence}"})
                for p in self._groups[index]
            ]
        return self._copies[key]


def _check_resampling(n_resamples: int, seed: int, alpha: float) -> None:
    if operator.index(n_resamples) < 1:
        raise ValueError(f"n_resamples must be at least 1, got {n_resamples}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be strictly between 0 and 1, got {alpha}")
    operator.index(seed)  # TypeError for None or a float: an unseeded draw is not reproducible


def _draws(n_postings: int, n_resamples: int, seed: int) -> Iterator[list[int]]:
    """`n_resamples` draws of `n_postings` posting indexes, with replacement."""
    import numpy as np

    rng = np.random.default_rng(seed)
    for _ in range(n_resamples):
        yield rng.integers(0, n_postings, size=n_postings).tolist()


def _interval(values: Sequence[float], alpha: float) -> tuple[float | None, float | None]:
    """Percentile interval at alpha/2 and 1 - alpha/2 (linear interpolation between order stats)."""
    if not values:
        return None, None
    import numpy as np

    low, high = np.quantile(np.asarray(values, dtype=float), [alpha / 2, 1 - alpha / 2])
    return float(low), float(high)


def bootstrap_ci(
    pairs: Sequence[PairScore],
    metric: Metric,
    *,
    n_resamples: int = 1000,
    seed: int = 0,
    alpha: float = DEFAULT_ALPHA,
) -> CI:
    """Percentile bootstrap interval for `metric`, resampling postings with replacement.

    Each resample draws as many postings as there are, recomputes `metric` on their concatenated
    pairs and keeps the value unless it is None. `point` is the metric on the full data.
    """
    _check_resampling(n_resamples, seed, alpha)
    everything = list(pairs)
    groups = group_by_posting(everything)
    if not groups:
        return CI(point=None, low=None, high=None)
    resampler = _Resampler(list(groups.values()))
    values: list[float] = []
    for draw in _draws(len(resampler), n_resamples, seed):
        value = metric(resampler.sample(draw))
        if value is not None:
            values.append(value)
    low, high = _interval(values, alpha)
    return CI(point=metric(everything), low=low, high=high)


def _difference(metric: Metric, baseline: Sequence[PairScore], current: Sequence[PairScore]) -> float | None:
    before, after = metric(baseline), metric(current)
    return None if before is None or after is None else after - before


def _verdict(
    point: float | None, low: float | None, high: float | None, min_effect: float, higher_is_better: bool
) -> Verdict:
    if point is None or low is None or high is None:
        return "NOISE"
    # Express everything as "bigger is better": flip the sign when lower is better.
    effect, good_low, bad_high = (point, low, high) if higher_is_better else (-point, -high, -low)
    if good_low > 0.0 and effect >= min_effect:
        return "PASS"
    if bad_high < 0.0:
        return "FAIL"
    return "NOISE"


def bootstrap_delta(
    baseline_pairs: Sequence[PairScore],
    current_pairs: Sequence[PairScore],
    metric: Metric,
    *,
    n_resamples: int = 1000,
    seed: int = 0,
    min_effect: float = 0.01,
    higher_is_better: bool = True,
) -> DeltaCI:
    """Paired bootstrap of `metric(current) - metric(baseline)` with a 95% interval and a verdict.

    Baseline and current are matched by posting id and only postings present in both are used. Each
    resample draws the same postings for both sides, which is what makes the interval about the
    change rather than about the sample.

    PASS needs the interval to exclude zero on the good side and an effect of at least
    `min_effect` (EVAL.md section 5.3: smaller wins are not real). FAIL needs only the interval to
    exclude zero on the bad side: a detectable regression is reported at any size. Otherwise NOISE.
    With no posting in common, or the metric undefined, the numbers are None and the verdict NOISE.
    """
    _check_resampling(n_resamples, seed, DEFAULT_ALPHA)
    if min_effect < 0.0:
        raise ValueError(f"min_effect must not be negative, got {min_effect}")
    baseline_groups = group_by_posting(baseline_pairs)
    current_groups = group_by_posting(current_pairs)
    shared = sorted(baseline_groups.keys() & current_groups.keys())
    if not shared:
        return DeltaCI(point=None, low=None, high=None, verdict="NOISE")
    baseline = _Resampler([baseline_groups[posting_id] for posting_id in shared])
    current = _Resampler([current_groups[posting_id] for posting_id in shared])
    deltas: list[float] = []
    for draw in _draws(len(shared), n_resamples, seed):
        delta = _difference(metric, baseline.sample(draw), current.sample(draw))
        if delta is not None:
            deltas.append(delta)
    low, high = _interval(deltas, DEFAULT_ALPHA)
    everyone = range(len(shared))
    point = _difference(metric, baseline.sample(everyone), current.sample(everyone))
    return DeltaCI(
        point=point, low=low, high=high, verdict=_verdict(point, low, high, min_effect, higher_is_better)
    )
