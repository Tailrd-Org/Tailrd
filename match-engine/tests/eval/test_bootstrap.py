"""Bootstrap confidence intervals and paired deltas (EVAL.md section 3 and 5.3).

Postings are resampled, not pairs, because pairs inside a posting are correlated. Several tests use
zero-variance data (every posting identical) so the interval collapses to a point whatever the
random stream does; the others assert properties that hold for any stream, never an exact
percentile of a particular numpy release.
"""

from __future__ import annotations

import inspect
import math
import random

import pytest

from eval.metrics.bootstrap import CI, DeltaCI, bootstrap_ci, bootstrap_delta
from eval.metrics.pairs import PairScore
from eval.metrics.ranking import ndcg_at_k

LOG2_3 = math.log2(3)
# NDCG of a posting whose relevance 3, 2, 0 is shown worst-first (see test_ranking.py):
REVERSED_NDCG = (3 / LOG2_3 + 3.5) / (7 + 3 / LOG2_3)  # 0.60642


def mean_score(pairs) -> float | None:
    scores = [p.score for p in pairs if p.score is not None]
    return math.fsum(scores) / len(scores) if scores else None


def distinct_postings(pairs) -> float:
    return float(len({p.posting_id for p in pairs}))


def pair_count(pairs) -> float:
    return float(len(pairs))


def posting(pid: str, scores: list[float], rels: list[int] | None = None) -> list[PairScore]:
    rels = rels or [2] * len(scores)
    return [
        PairScore(posting_id=pid, pair_id=f"{pid}-{i}", score=s, relevance=r)
        for i, (s, r) in enumerate(zip(scores, rels, strict=True))
    ]


def ranked_posting(pid: str, *, best_first: bool) -> list[PairScore]:
    """Relevance 3, 2, 0: NDCG 1.0 when shown best-first, 0.60642 when shown worst-first."""
    rels = {"a": 3, "b": 2, "c": 0}
    scores = {"a": 0.9, "b": 0.5, "c": 0.1} if best_first else {"a": 0.1, "b": 0.5, "c": 0.9}
    return [
        PairScore(posting_id=pid, pair_id=f"{pid}-{k}", score=scores[k], relevance=rels[k]) for k in "abc"
    ]


def corpus(ids: list[str], *, best_first: bool | list[bool]) -> list[PairScore]:
    flags = best_first if isinstance(best_first, list) else [best_first] * len(ids)
    return [p for pid, flag in zip(ids, flags, strict=True) for p in ranked_posting(pid, best_first=flag)]


def noisy_corpus(n_postings: int, seed: int) -> list[PairScore]:
    """Five pairs per posting, scores loosely following relevance, so NDCG sits well inside (0, 1)."""
    rng = random.Random(seed)
    pairs = []
    for i in range(n_postings):
        for j in range(5):
            rel = rng.randint(0, 3)
            score = min(1.0, max(0.0, rel / 3 + rng.uniform(-0.5, 0.5)))
            pairs.append(
                PairScore(posting_id=f"post{i:03d}", pair_id=f"pair{i:03d}-{j}", score=score, relevance=rel)
            )
    return pairs


IDS30 = [f"p{i:02d}" for i in range(30)]


# --------------------------------------------------------------------------------------------
# bootstrap_ci
# --------------------------------------------------------------------------------------------


def test_signature_defaults_match_the_contract():
    ci_params = inspect.signature(bootstrap_ci).parameters
    assert (ci_params["n_resamples"].default, ci_params["seed"].default, ci_params["alpha"].default) == (
        1000,
        0,
        0.05,
    )
    d_params = inspect.signature(bootstrap_delta).parameters
    assert d_params["n_resamples"].default == 1000
    assert d_params["seed"].default == 0
    assert d_params["min_effect"].default == 0.01
    assert d_params["higher_is_better"].default is True
    for params in (ci_params, d_params):  # tuning knobs are keyword-only
        assert params["n_resamples"].kind is inspect.Parameter.KEYWORD_ONLY
        assert params["seed"].kind is inspect.Parameter.KEYWORD_ONLY


def test_same_seed_gives_identical_output_and_a_different_seed_does_not():
    pairs = noisy_corpus(40, seed=1)
    a = bootstrap_ci(pairs, ndcg_at_k, n_resamples=200, seed=11)
    b = bootstrap_ci(pairs, ndcg_at_k, n_resamples=200, seed=11)
    assert isinstance(a, CI)
    assert a == b
    assert a.low < a.high  # genuinely resampled, not a collapsed interval
    assert bootstrap_ci(pairs, ndcg_at_k, n_resamples=200, seed=12) != a


def test_result_does_not_depend_on_the_order_of_the_input():
    pairs = noisy_corpus(40, seed=2)
    shuffled = list(pairs)
    random.Random(99).shuffle(shuffled)
    assert bootstrap_ci(shuffled, ndcg_at_k, n_resamples=200, seed=5) == bootstrap_ci(
        pairs, ndcg_at_k, n_resamples=200, seed=5
    )


def test_interval_brackets_the_point_estimate_computed_on_the_full_data():
    pairs = noisy_corpus(60, seed=3)
    ci = bootstrap_ci(pairs, ndcg_at_k, n_resamples=500, seed=0)
    assert ci.point == ndcg_at_k(pairs)
    assert ci.low <= ci.point <= ci.high
    assert 0.0 < ci.low and ci.high < 1.0


def test_smaller_alpha_gives_a_wider_interval():
    pairs = noisy_corpus(40, seed=4)
    wide = bootstrap_ci(pairs, ndcg_at_k, n_resamples=300, seed=0, alpha=0.01)
    default = bootstrap_ci(pairs, ndcg_at_k, n_resamples=300, seed=0)
    narrow = bootstrap_ci(pairs, ndcg_at_k, n_resamples=300, seed=0, alpha=0.20)
    assert wide.low <= default.low <= narrow.low
    assert wide.high >= default.high >= narrow.high


def test_identical_postings_collapse_the_interval_to_the_point():
    pairs = corpus(IDS30, best_first=False)
    ci = bootstrap_ci(pairs, ndcg_at_k, n_resamples=100, seed=0)
    assert ci.point == pytest.approx(REVERSED_NDCG)
    assert ci.low == ci.point == ci.high


@pytest.mark.parametrize("alpha", [0.05, 0.10])
def test_interval_endpoints_are_the_alpha_over_two_percentiles_of_the_resample_values(alpha):
    # A metric that returns 1, 2, 3, ... on successive calls stands in for resample values whose
    # distribution is known exactly, whatever the random stream. One of the 401 calls is the
    # point estimate on the full data; the other 400 are the resamples.
    calls: list[int] = []

    def counting_metric(sample) -> float:
        calls.append(len(sample))
        return float(len(calls))

    pairs = [p for i in range(4) for p in posting(f"p{i}", [0.5])]
    ci = bootstrap_ci(pairs, counting_metric, n_resamples=400, seed=0, alpha=alpha)
    assert len(calls) == 401
    resample_values = [float(v) for v in range(1, 402) if float(v) != ci.point]
    assert len(resample_values) == 400
    # Linear interpolation between order statistics (numpy's default): for an arithmetic run
    # a, a+1, ..., a+399 the q-quantile is a + q * 399.
    first = resample_values[0]
    assert ci.low == pytest.approx(first + (alpha / 2) * 399)
    assert ci.high == pytest.approx(first + (1 - alpha / 2) * 399)


def test_postings_are_resampled_not_pairs():
    # One posting with 1 pair and one with 9. Resampling two postings gives sizes
    #   small+small = 2 (prob 1/4), a mix = 10 (prob 1/2), big+big = 18 (prob 1/4).
    # Resampling the 10 pairs individually would hover around 10 and never reach 2 or 18.
    pairs = posting("small", [0.5]) + posting("big", [0.5] * 9)
    ci = bootstrap_ci(pairs, pair_count, n_resamples=1000, seed=0)
    assert ci.point == 10.0
    assert (ci.low, ci.high) == (2.0, 18.0)


def test_a_posting_drawn_twice_counts_as_two_postings():
    # Each draw must be its own posting, otherwise a repeated draw would merge into one bigger
    # posting and per-posting metrics (NDCG, P@k) would silently change. 5 draws -> always 5.
    pairs = [p for i in range(5) for p in posting(f"p{i}", [0.1, 0.9])]
    ci = bootstrap_ci(pairs, distinct_postings, n_resamples=200, seed=0)
    assert (ci.point, ci.low, ci.high) == (5.0, 5.0, 5.0)


def test_relabelled_copies_never_collide_with_real_posting_ids():
    # "a" drawn twice must not be given the id of the real posting "a#1".
    pairs = posting("a", [0.5]) + posting("a#1", [0.5]) + posting("b", [0.5])
    ci = bootstrap_ci(pairs, distinct_postings, n_resamples=300, seed=0)
    assert (ci.point, ci.low, ci.high) == (3.0, 3.0, 3.0)


def test_resamples_where_the_metric_is_undefined_are_skipped():
    # A has 1 pair, B has 3. The metric is only defined when A is present; B+B is skipped.
    # Valid resamples: A+A = 2 (1/3 of valid), A+B or B+A = 4 (2/3 of valid) -> interval [2, 4].
    pairs = posting("A", [0.5]) + posting("B", [0.5] * 3)

    def only_when_a_present(sample) -> float | None:
        return float(len(sample)) if any(p.posting_id == "A" for p in sample) else None

    ci = bootstrap_ci(pairs, only_when_a_present, n_resamples=1000, seed=0)
    assert ci.point == 4.0
    assert (ci.low, ci.high) == (2.0, 4.0)


def test_interval_is_none_when_no_resample_is_defined_but_the_point_is():
    # The metric is defined only when the sample is exactly the original set of pairs, which a
    # with-replacement resample of 30 postings reproduces with probability ~2.6e-12.
    pairs = corpus(IDS30, best_first=False)
    original = {(p.posting_id, p.pair_id) for p in pairs}

    def only_the_original(sample) -> float | None:
        return 1.0 if {(p.posting_id, p.pair_id) for p in sample} == original else None

    ci = bootstrap_ci(pairs, only_the_original, n_resamples=50, seed=0)
    assert ci == CI(point=1.0, low=None, high=None)


def test_empty_input_gives_an_undefined_interval():
    assert bootstrap_ci([], ndcg_at_k) == CI(point=None, low=None, high=None)


def test_ci_arguments_are_validated():
    pairs = noisy_corpus(5, seed=0)
    with pytest.raises(ValueError):
        bootstrap_ci(pairs, ndcg_at_k, n_resamples=0)
    for alpha in (0.0, 1.0, -0.1, 1.5):
        with pytest.raises(ValueError):
            bootstrap_ci(pairs, ndcg_at_k, n_resamples=10, alpha=alpha)
    with pytest.raises(TypeError):  # an unseeded draw would make the harness non-deterministic
        bootstrap_ci(pairs, ndcg_at_k, n_resamples=10, seed=None)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------
# bootstrap_delta
# --------------------------------------------------------------------------------------------


def test_delta_passes_when_every_posting_improves():
    # Every posting goes from NDCG 0.60642 to 1.0, so every resample has delta 0.39358.
    baseline = corpus(IDS30, best_first=False)
    current = corpus(IDS30, best_first=True)
    result = bootstrap_delta(baseline, current, ndcg_at_k, n_resamples=200)
    assert isinstance(result, DeltaCI)
    assert result.verdict == "PASS"
    assert result.point == pytest.approx(1.0 - REVERSED_NDCG)
    assert result.low == pytest.approx(result.point)
    assert result.high == pytest.approx(result.point)


def test_delta_fails_when_every_posting_regresses():
    result = bootstrap_delta(
        corpus(IDS30, best_first=True), corpus(IDS30, best_first=False), ndcg_at_k, n_resamples=200
    )
    assert result.verdict == "FAIL"
    assert result.point == pytest.approx(REVERSED_NDCG - 1.0)
    assert result.high < 0.0


def test_delta_with_real_variance_still_excludes_zero_when_two_thirds_improve():
    # 40 of 60 postings improve by 0.39358, 20 are unchanged: point 0.26239. A resample would need
    # all 60 draws from the 20 unchanged postings to show no gain, so the interval clears zero.
    ids = [f"p{i:02d}" for i in range(60)]
    baseline = corpus(ids, best_first=False)
    current = corpus(ids, best_first=[i < 40 for i in range(60)])
    # postings 40..59 are reversed in `current` too, so only the first 40 improve
    result = bootstrap_delta(baseline, current, ndcg_at_k, n_resamples=500)
    assert result.point == pytest.approx((40 / 60) * (1.0 - REVERSED_NDCG))
    assert 0.0 < result.low < result.point < result.high
    assert result.verdict == "PASS"


def test_delta_is_noise_when_gains_and_losses_cancel():
    # 20 postings improve by 0.39358 and 20 regress by 0.39358: point exactly 0, wide interval.
    ids = [f"p{i:02d}" for i in range(40)]
    flags_base = [i >= 20 for i in range(40)]
    baseline = corpus(ids, best_first=flags_base)
    current = corpus(ids, best_first=[not f for f in flags_base])
    result = bootstrap_delta(baseline, current, ndcg_at_k, n_resamples=500)
    assert result.point == 0.0
    assert result.low < 0.0 < result.high
    assert result.verdict == "NOISE"


def test_delta_that_clears_zero_but_not_the_minimum_effect_is_noise():
    # EVAL.md: "an improvement of 0.004 ... is noise". Every pair rises by 0.004. The interval
    # excludes zero but the effect is below min_effect = 0.01 (the 5.3 guard), so NOISE.
    baseline = [p for i in range(20) for p in posting(f"p{i:02d}", [0.5, 0.5, 0.5])]
    current = [p for i in range(20) for p in posting(f"p{i:02d}", [0.504, 0.504, 0.504])]
    result = bootstrap_delta(baseline, current, mean_score, n_resamples=200)
    assert result.point == pytest.approx(0.004)
    assert result.low > 0.0
    assert result.verdict == "NOISE"
    assert bootstrap_delta(baseline, current, mean_score, n_resamples=200, min_effect=0.001).verdict == "PASS"


def test_delta_direction_flips_for_lower_is_better_metrics():
    high = [p for i in range(20) for p in posting(f"p{i:02d}", [0.5, 0.5])]
    low = [p for i in range(20) for p in posting(f"p{i:02d}", [0.45, 0.45])]
    # A metric that fell by 0.05: an improvement when lower is better, a regression otherwise.
    assert bootstrap_delta(high, low, mean_score, n_resamples=100, higher_is_better=False).verdict == "PASS"
    assert bootstrap_delta(high, low, mean_score, n_resamples=100).verdict == "FAIL"
    # And a rise is the mirror image.
    assert bootstrap_delta(low, high, mean_score, n_resamples=100, higher_is_better=False).verdict == "FAIL"
    assert bootstrap_delta(low, high, mean_score, n_resamples=100).verdict == "PASS"


def test_delta_uses_only_postings_present_in_both_runs():
    # Common postings b and c: 0.4 -> 0.5 in each, so delta 0.1 in every resample. The outliers
    # (a only in baseline at 0.0, d only in current at 1.0) would move the delta to 0.4 if used.
    baseline = posting("a", [0.0]) + posting("b", [0.4]) + posting("c", [0.4])
    current = posting("d", [1.0]) + posting("b", [0.5]) + posting("c", [0.5])
    result = bootstrap_delta(baseline, current, mean_score, n_resamples=100)
    assert result.point == pytest.approx(0.1)
    assert result.low == pytest.approx(0.1)
    assert result.high == pytest.approx(0.1)
    assert result.verdict == "PASS"


def test_both_sides_are_resampled_with_the_same_posting_draws():
    # Identical runs differ by exactly 0 in every resample only if baseline and current see the
    # same draws; independent draws would give a spread around 0.
    pairs = noisy_corpus(40, seed=6)
    result = bootstrap_delta(pairs, list(pairs), ndcg_at_k, n_resamples=200, seed=3)
    assert (result.point, result.low, result.high) == (0.0, 0.0, 0.0)
    assert result.verdict == "NOISE"
    # An interval that collapses onto zero does not exclude it, even with no minimum effect.
    assert bootstrap_delta(pairs, list(pairs), ndcg_at_k, n_resamples=50, min_effect=0.0).verdict == "NOISE"


def test_delta_is_deterministic_and_ignores_input_order():
    baseline = noisy_corpus(40, seed=7)
    current = noisy_corpus(40, seed=8)  # same posting ids, different scores
    a = bootstrap_delta(baseline, current, ndcg_at_k, n_resamples=200, seed=21)
    assert a == bootstrap_delta(baseline, current, ndcg_at_k, n_resamples=200, seed=21)
    shuffled = list(current)
    random.Random(1).shuffle(shuffled)
    assert a == bootstrap_delta(baseline, shuffled, ndcg_at_k, n_resamples=200, seed=21)
    assert a.point == pytest.approx(ndcg_at_k(current) - ndcg_at_k(baseline))
    assert a.low <= a.point <= a.high


def test_delta_with_no_posting_in_common_or_no_defined_metric_is_noise_with_no_numbers():
    undefined = DeltaCI(point=None, low=None, high=None, verdict="NOISE")
    assert bootstrap_delta(posting("a", [0.5]), posting("b", [0.5]), mean_score) == undefined
    assert bootstrap_delta([], [], mean_score) == undefined
    # Metric undefined on both sides (no relevant pair anywhere, so NDCG has no posting to average).
    zero = [PairScore(posting_id="p", pair_id="x", score=0.5, relevance=0)]
    assert bootstrap_delta(zero, zero, ndcg_at_k) == undefined


def test_delta_arguments_are_validated():
    pairs = noisy_corpus(5, seed=0)
    with pytest.raises(ValueError):
        bootstrap_delta(pairs, pairs, ndcg_at_k, n_resamples=0)
    with pytest.raises(ValueError):
        bootstrap_delta(pairs, pairs, ndcg_at_k, n_resamples=-5)
    with pytest.raises(ValueError):
        bootstrap_delta(pairs, pairs, ndcg_at_k, n_resamples=10, min_effect=-0.01)
    with pytest.raises(TypeError):
        bootstrap_delta(pairs, pairs, ndcg_at_k, n_resamples=10, seed=None)  # type: ignore[arg-type]
