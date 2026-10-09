"""Ranking metrics against hand-computed values (EVAL.md section 3: NDCG@10, P@10, AUC, tau).

Every expected number is written out as arithmetic first so a reader can check the test without
trusting the implementation. DCG uses gain 2^rel - 1 and discount log2(rank + 1); log2(3) is
1.58496, log2(4) is 2, log2(12) is 3.58496.

tests/eval/ deliberately has no __init__.py: with pytest's default import mode it would then be
imported as a package named `eval` and shadow the real eval/ package these tests exercise.
"""

from __future__ import annotations

import itertools
import math
import random

import pytest
from pydantic import ValidationError

from eval.metrics.pairs import PairScore
from eval.metrics.ranking import auc_roc, kendall_tau_b, ndcg_at_k, per_posting, precision_at_k

LOG2_3 = math.log2(3)


def pair(posting: str, pair_id: str, score: float | None, rel: int, **extra) -> PairScore:
    return PairScore(posting_id=posting, pair_id=pair_id, score=score, relevance=rel, **extra)


# --------------------------------------------------------------------------------------------
# PairScore: the record every metric consumes
# --------------------------------------------------------------------------------------------


def test_pairscore_defaults_and_abstention():
    p = pair("p", "a", None, 2)
    assert p.score is None  # None means abstained
    assert (p.advanced, p.band_pred, p.band_true, p.group, p.hard) == (None, None, None, None, False)


@pytest.mark.parametrize(
    "bad",
    [
        {"relevance": 4},
        {"relevance": -1},
        {"band_pred": 4},
        {"band_true": -1},
        {"score": float("nan")},
        {"score": float("inf")},
        {"posting_id": ""},
        {"pair_id": ""},
        {"group": ""},
    ],
)
def test_pairscore_rejects_out_of_contract_values(bad):
    fields = {"posting_id": "p", "pair_id": "a", "score": 0.5, "relevance": 2} | bad
    with pytest.raises(ValidationError):
        PairScore(**fields)


def test_pairscore_is_frozen():
    p = pair("p", "a", 0.5, 2)
    with pytest.raises(ValidationError):
        p.score = 0.9  # type: ignore[misc]


def test_score_is_required_so_a_forgotten_score_cannot_pass_as_an_abstention():
    with pytest.raises(ValidationError):
        PairScore(posting_id="p", pair_id="a", relevance=2)  # type: ignore[call-arg]


# --------------------------------------------------------------------------------------------
# NDCG@k
# --------------------------------------------------------------------------------------------


def test_ndcg_perfect_ranking_is_exactly_one():
    # Relevance 3, 2, 0 shown best-first.
    # DCG  = 7/log2(2) + 3/log2(3) + 0/log2(4) = 7 + 1.89279 + 0 = 8.89279
    # IDCG = the ideal order is the same list, so the same terms: NDCG = 1.0
    pairs = [pair("p", "a", 0.9, 3), pair("p", "b", 0.5, 2), pair("p", "c", 0.1, 0)]
    assert ndcg_at_k(pairs) == 1.0


def test_ndcg_reversed_ranking_known_value():
    # Shown worst-first: relevance 0, 2, 3 at ranks 1, 2, 3.
    # DCG  = 0/1 + 3/log2(3) + 7/log2(4) = 0 + 1.89279 + 3.5 = 5.39279
    # IDCG = 7/1 + 3/log2(3) + 0/2       = 7 + 1.89279     = 8.89279
    # NDCG = 5.39279 / 8.89279 = 0.60642
    pairs = [pair("p", "a", 0.1, 3), pair("p", "b", 0.5, 2), pair("p", "c", 0.9, 0)]
    assert ndcg_at_k(pairs) == pytest.approx(0.60642, abs=1e-5)
    assert ndcg_at_k(pairs) == pytest.approx((3 / LOG2_3 + 3.5) / (7 + 3 / LOG2_3))


def test_ndcg_three_pair_posting_known_answer():
    # Scores 0.9, 0.8, 0.7 put relevance 2, 3, 1 at ranks 1, 2, 3.
    # DCG  = (2^2-1)/log2(2) + (2^3-1)/log2(3) + (2^1-1)/log2(4)
    #      = 3 + 7/1.58496 + 1/2 = 3 + 4.41651 + 0.5 = 7.91651
    # IDCG = ideal order 3, 2, 1: 7/1 + 3/log2(3) + 1/2 = 7 + 1.89279 + 0.5 = 9.39279
    # NDCG = 7.91651 / 9.39279 = 0.84283
    pairs = [pair("p", "a", 0.9, 2), pair("p", "b", 0.8, 3), pair("p", "c", 0.7, 1)]
    assert ndcg_at_k(pairs) == pytest.approx(0.84283, abs=1e-5)
    assert ndcg_at_k(pairs) == pytest.approx((3 + 7 / LOG2_3 + 0.5) / (7 + 3 / LOG2_3 + 0.5))


def test_ndcg_equal_scores_are_ordered_by_pair_id_ascending():
    # a (rel 0) and b (rel 3) tie at 0.5. pair_id 'a' < 'b', so a is ranked first.
    # DCG = 0/1 + 7/log2(3) = 4.41651; IDCG = 7/1 + 0 = 7; NDCG = 1/log2(3) = 0.63093.
    pairs = [pair("p", "a", 0.5, 0), pair("p", "b", 0.5, 3)]
    assert ndcg_at_k(pairs) == pytest.approx(1 / LOG2_3)
    assert ndcg_at_k(pairs[::-1]) == pytest.approx(1 / LOG2_3)  # input order does not matter
    # Swap which id carries the relevance: now the relevant pair wins the tie and NDCG is 1.0.
    flipped = [pair("p", "b", 0.5, 0), pair("p", "a", 0.5, 3)]
    assert ndcg_at_k(flipped) == 1.0


def test_ndcg_is_invariant_to_the_input_order_of_pairs():
    base = [
        pair("p", "a", 0.5, 1),
        pair("p", "b", 0.5, 3),
        pair("p", "c", 0.5, 0),
        pair("p", "d", 0.2, 2),
    ]
    expected = ndcg_at_k(base, k=3)
    assert all(ndcg_at_k(list(perm), k=3) == expected for perm in itertools.permutations(base))


def test_ndcg_abstained_pair_lowers_the_score_but_stays_in_the_ideal_ordering():
    # a (rel 3) abstains; b (rel 2, score 0.9) and c (rel 1, score 0.5) are ranked.
    # DCG  = 3/log2(2) + 1/log2(3) = 3 + 0.63093 = 3.63093
    # IDCG = 7/1 + 3/log2(3) + 1/log2(4) = 7 + 1.89279 + 0.5 = 9.39279   (a still counts)
    # NDCG = 3.63093 / 9.39279 = 0.38657
    pairs = [pair("p", "a", None, 3), pair("p", "b", 0.9, 2), pair("p", "c", 0.5, 1)]
    assert ndcg_at_k(pairs) == pytest.approx(0.38657, abs=1e-5)
    # The same posting with a scored on top is a perfect ranking.
    scored = [pair("p", "a", 0.95, 3), pair("p", "b", 0.9, 2), pair("p", "c", 0.5, 1)]
    assert ndcg_at_k(scored) == 1.0


def test_ndcg_posting_where_everything_abstains_is_zero_not_skipped():
    pairs = [pair("p", "a", None, 3), pair("p", "b", None, 1)]
    assert ndcg_at_k(pairs) == 0.0


def test_ndcg_skips_postings_with_zero_ideal_dcg_and_averages_the_rest():
    # p1 is perfect (1.0). p2 is the reversed posting (0.60642). p3 has nothing relevant, so its
    # IDCG is 0 and it is skipped, not averaged in as 0 or 1.
    # mean = (1.0 + 0.60642) / 2 = 0.80321
    pairs = [
        pair("p1", "a", 0.9, 3),
        pair("p1", "b", 0.5, 2),
        pair("p1", "c", 0.1, 0),
        pair("p2", "a", 0.1, 3),
        pair("p2", "b", 0.5, 2),
        pair("p2", "c", 0.9, 0),
        pair("p3", "a", 0.9, 0),
        pair("p3", "b", 0.1, 0),
    ]
    assert ndcg_at_k(pairs) == pytest.approx(0.80321, abs=1e-5)


def test_ndcg_is_none_when_no_posting_qualifies():
    assert ndcg_at_k([]) is None
    assert ndcg_at_k([pair("p", "a", 0.9, 0), pair("p", "b", 0.1, 0)]) is None


def test_ndcg_k_truncates_ranking_and_ideal_alike():
    # a (0.9, rel 1) is ranked above b (0.8, rel 3).
    # k=1: DCG = 1/1 = 1; IDCG = 7/1 = 7; NDCG = 1/7 = 0.14286
    # k=2: DCG = 1 + 7/log2(3) = 5.41651; IDCG = 7 + 1/log2(3) = 7.63093; NDCG = 0.70981
    pairs = [pair("p", "a", 0.9, 1), pair("p", "b", 0.8, 3)]
    assert ndcg_at_k(pairs, k=1) == pytest.approx(1 / 7)
    assert ndcg_at_k(pairs, k=2) == pytest.approx(0.70981, abs=1e-5)


def test_ndcg_default_k_is_ten():
    # Twelve pairs, scores strictly decreasing with the id; the only relevant pair (rel 3) is the
    # 11th. k=10 (default) never sees it: DCG = 0. k=12 sees it: DCG = 7/log2(12), IDCG = 7,
    # NDCG = 1/log2(12) = 0.27894.
    pairs = [pair("p", f"p{i:02d}", 1.0 - i / 100, 3 if i == 11 else 0) for i in range(1, 13)]
    assert ndcg_at_k(pairs) == 0.0
    assert ndcg_at_k(pairs, k=12) == pytest.approx(0.27894, abs=1e-5)


@pytest.mark.parametrize("bad_k", [0, -1])
def test_ranking_functions_reject_a_non_positive_k(bad_k):
    pairs = [pair("p", "a", 0.9, 3)]
    with pytest.raises(ValueError):
        ndcg_at_k(pairs, k=bad_k)
    with pytest.raises(ValueError):
        precision_at_k(pairs, k=bad_k)


def test_duplicate_pair_ids_inside_a_posting_are_an_error_not_a_silent_tie():
    pairs = [pair("p", "a", 0.9, 3), pair("p", "a", 0.9, 0)]
    with pytest.raises(ValueError, match="duplicate"):
        ndcg_at_k(pairs)
    with pytest.raises(ValueError, match="duplicate"):
        precision_at_k(pairs)
    # A posting that NDCG would skip (nothing relevant) is still checked.
    with pytest.raises(ValueError, match="duplicate"):
        ndcg_at_k([pair("p", "a", 0.9, 0), pair("p", "a", 0.8, 0)])
    # The same pair id in two different postings is fine.
    assert ndcg_at_k([pair("p", "a", 0.9, 3), pair("q", "a", 0.9, 0)]) == 1.0


# --------------------------------------------------------------------------------------------
# Precision@k
# --------------------------------------------------------------------------------------------


def test_precision_at_k_counts_good_pairs_in_the_top_k():
    # Best-first relevance: 3, 0, 2, 1, 2. "Good" means relevance >= 2.
    # P@3 = good in {3, 0, 2} = 2 -> 2/3.   P@5 = good in all five = 3 -> 3/5.
    rels = [3, 0, 2, 1, 2]
    pairs = [pair("p", f"x{i}", 0.9 - i / 10, rel) for i, rel in enumerate(rels)]
    assert precision_at_k(pairs, k=3) == pytest.approx(2 / 3)
    assert precision_at_k(pairs, k=5) == pytest.approx(3 / 5)


def test_precision_good_threshold_is_configurable():
    rels = [3, 0, 2, 1, 2]
    pairs = [pair("p", f"x{i}", 0.9 - i / 10, rel) for i, rel in enumerate(rels)]
    assert precision_at_k(pairs, k=5, good_threshold=3) == pytest.approx(1 / 5)
    assert precision_at_k(pairs, k=5, good_threshold=1) == pytest.approx(4 / 5)


def test_precision_denominator_is_min_of_k_and_scored_pairs():
    # Two scored pairs (rel 3 and 0) and three abstained ones that WOULD be good.
    # P@10 = 1 good / min(10, 2 scored) = 1/2. Abstained pairs add nothing to either side.
    pairs = [
        pair("p", "a", 0.9, 3),
        pair("p", "b", 0.5, 0),
        pair("p", "c", None, 3),
        pair("p", "d", None, 3),
        pair("p", "e", None, 3),
    ]
    assert precision_at_k(pairs, k=10) == pytest.approx(0.5)


def test_precision_is_the_mean_over_postings():
    # p1: top-2 relevance 3, 2 -> 2/2 = 1.0.  p2: top-2 relevance 2, 0 -> 1/2 = 0.5.  mean = 0.75
    pairs = [
        pair("p1", "a", 0.9, 3),
        pair("p1", "b", 0.8, 2),
        pair("p2", "a", 0.9, 2),
        pair("p2", "b", 0.8, 0),
    ]
    assert precision_at_k(pairs, k=2) == pytest.approx(0.75)


def test_precision_ties_are_broken_by_pair_id_before_cutting_at_k():
    # a (rel 0) and b (rel 3) tie; 'a' takes the single slot at k=1, so P@1 = 0.
    pairs = [pair("p", "b", 0.5, 3), pair("p", "a", 0.5, 0)]
    assert precision_at_k(pairs, k=1) == 0.0
    flipped = [pair("p", "a", 0.5, 3), pair("p", "b", 0.5, 0)]
    assert precision_at_k(flipped, k=1) == 1.0


def test_precision_skips_postings_with_no_scored_pair_and_is_none_when_nothing_qualifies():
    pairs = [pair("p1", "a", 0.9, 3), pair("p2", "a", None, 3)]
    assert precision_at_k(pairs, k=5) == 1.0  # p2 shows nothing, so it is skipped
    assert precision_at_k([pair("p", "a", None, 3)], k=5) is None
    assert precision_at_k([], k=5) is None


# --------------------------------------------------------------------------------------------
# per_posting
# --------------------------------------------------------------------------------------------


def test_per_posting_returns_one_value_per_posting_in_id_order():
    pairs = [pair("q", "a", 0.9, 3), pair("p", "a", 0.9, 3), pair("p", "b", 0.1, 0), pair("p", "c", 0.5, 1)]
    result = per_posting(pairs, lambda group: float(len(group)))
    assert result == {"p": 3.0, "q": 1.0}
    assert list(result) == ["p", "q"]


def test_per_posting_drops_postings_where_the_metric_is_undefined():
    pairs = [pair("p", "a", 0.9, 3), pair("p", "b", 0.5, 2), pair("q", "a", 0.9, 0)]
    result = per_posting(pairs, ndcg_at_k)
    assert result == {"p": 1.0}  # q has IDCG 0 -> None -> dropped


# --------------------------------------------------------------------------------------------
# AUC-ROC
# --------------------------------------------------------------------------------------------


def _auc_pairs(scores, labels) -> list[PairScore]:
    return [
        pair("p", f"x{i}", s, 2, advanced=label)
        for i, (s, label) in enumerate(zip(scores, labels, strict=True))
    ]


def test_auc_textbook_example_with_ties_in_neither_class():
    # scores [0.1, 0.4, 0.35, 0.8], labels [F, F, T, T].
    # positives {0.35, 0.8}, negatives {0.1, 0.4}: 0.35>0.1 win, 0.35<0.4 loss, 0.8>0.1 win,
    # 0.8>0.4 win -> 3 / 4 = 0.75
    pairs = _auc_pairs([0.1, 0.4, 0.35, 0.8], [False, False, True, True])
    assert auc_roc(pairs) == pytest.approx(0.75)


def test_auc_counts_a_tied_positive_negative_pair_as_one_half():
    # scores [0.2, 0.5, 0.5, 0.9], labels [F, T, F, T].
    # positives {0.5, 0.9}, negatives {0.2, 0.5}: (0.5,0.2) 1, (0.5,0.5) tie 0.5, (0.9,0.2) 1,
    # (0.9,0.5) 1 -> 3.5 / 4 = 0.875
    pairs = _auc_pairs([0.2, 0.5, 0.5, 0.9], [False, True, False, True])
    assert auc_roc(pairs) == pytest.approx(0.875)
    # Everything tied: no information, AUC is exactly 0.5.
    assert auc_roc(_auc_pairs([0.5, 0.5], [True, False])) == pytest.approx(0.5)


def test_auc_perfect_and_inverted_separation():
    scores, labels = [0.1, 0.2, 0.8, 0.9], [False, False, True, True]
    assert auc_roc(_auc_pairs(scores, labels)) == pytest.approx(1.0)
    assert auc_roc(_auc_pairs(scores, [not x for x in labels])) == pytest.approx(0.0)


def test_auc_ignores_abstained_and_unlabelled_pairs():
    # The textbook pairs (0.75) plus: an abstained positive (no score to rank) and a top-scored
    # pair with no outcome. Treating None as 0.0 or as "not advanced" would move AUC off 0.75.
    pairs = _auc_pairs([0.1, 0.4, 0.35, 0.8], [False, False, True, True]) + [
        pair("p", "abstained", None, 2, advanced=True),
        pair("p", "unlabelled", 0.99, 2, advanced=None),
    ]
    assert auc_roc(pairs) == pytest.approx(0.75)


def test_auc_pools_pairs_across_postings():
    pairs = [
        pair("p1", "a", 0.9, 2, advanced=True),
        pair("p1", "b", 0.1, 2, advanced=False),
        pair("p2", "a", 0.4, 2, advanced=True),
        pair("p2", "b", 0.6, 2, advanced=False),
    ]
    # positives {0.9, 0.4}, negatives {0.1, 0.6}: 0.9>0.1, 0.9>0.6, 0.4>0.1 win; 0.4<0.6 loss -> 3/4
    assert auc_roc(pairs) == pytest.approx(0.75)


def test_auc_is_none_when_a_class_is_missing():
    assert auc_roc([]) is None
    assert auc_roc(_auc_pairs([0.1, 0.9], [True, True])) is None
    assert auc_roc(_auc_pairs([0.1, 0.9], [False, False])) is None
    assert auc_roc([pair("p", "a", None, 2, advanced=True), pair("p", "b", 0.5, 2, advanced=False)]) is None


def test_auc_matches_a_brute_force_pair_count_on_tie_heavy_random_data():
    rng = random.Random(20261008)
    for _ in range(25):
        n = rng.randint(2, 40)
        scores = [rng.randint(0, 5) / 5 for _ in range(n)]  # few distinct values -> many ties
        labels = [rng.random() < 0.4 for _ in range(n)]
        pos = [s for s, y in zip(scores, labels, strict=True) if y]
        neg = [s for s, y in zip(scores, labels, strict=True) if not y]
        got = auc_roc(_auc_pairs(scores, labels))
        if not pos or not neg:
            assert got is None
            continue
        wins = sum(1.0 if a > b else 0.5 if a == b else 0.0 for a in pos for b in neg)
        assert got == pytest.approx(wins / (len(pos) * len(neg)))


# --------------------------------------------------------------------------------------------
# Kendall tau-b
# --------------------------------------------------------------------------------------------


def test_tau_b_without_ties():
    # x = 1..5, y = [3, 4, 1, 2, 5]. Of the 10 pairs 6 are concordant (1-2, 1-5, 2-5, 3-4, 3-5,
    # 4-5) and 4 are discordant (1-3, 1-4, 2-3, 2-4). tau = (6 - 4) / 10 = 0.2.
    assert kendall_tau_b([1, 2, 3, 4, 5], [3, 4, 1, 2, 5]) == pytest.approx(0.2)


def test_tau_b_textbook_example_with_ties_in_both_variables():
    # The scipy.stats.kendalltau documentation example.
    # x = [12, 2, 1, 12, 2], y = [1, 4, 7, 1, 0]; n0 = 10 pairs.
    # concordant C = 2, discordant D = 6; ties in x: n1 = 2 (the two 12s, the two 2s);
    # ties in y: n2 = 1 (the two 1s); tied in both: 1 pair (indices 0 and 3).
    # tau_b = (C - D) / sqrt((n0 - n1)(n0 - n2)) = -4 / sqrt(8 * 9) = -4 / sqrt(72) = -0.471405
    assert kendall_tau_b([12, 2, 1, 12, 2], [1, 4, 7, 1, 0]) == pytest.approx(-4 / math.sqrt(72))
    assert kendall_tau_b([12, 2, 1, 12, 2], [1, 4, 7, 1, 0]) == pytest.approx(-0.4714045207910317)


def test_tau_b_pair_tied_in_both_variables_is_neither_concordant_nor_discordant():
    # x = [1, 1, 2], y = [5, 5, 6]: n0 = 3, n1 = 1, n2 = 1, one joint tie.
    # C = 2 (index 2 against each of the others), D = 0 -> tau_b = 2 / sqrt(2 * 2) = 1.0
    assert kendall_tau_b([1, 1, 2], [5, 5, 6]) == pytest.approx(1.0)


def test_tau_b_perfect_agreement_and_reversal():
    assert kendall_tau_b([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert kendall_tau_b([1, 2, 3, 4], [40, 30, 20, 10]) == -1.0


def test_tau_b_depends_only_on_order():
    xs, ys = [3.0, 1.0, 4.0, 1.5, 9.0], [2.0, 7.0, 1.0, 8.0, 2.8]
    assert kendall_tau_b([math.exp(x) for x in xs], [y**3 for y in ys]) == pytest.approx(
        kendall_tau_b(xs, ys)
    )


def test_tau_b_is_none_for_fewer_than_two_points_or_constant_input():
    assert kendall_tau_b([], []) is None
    assert kendall_tau_b([1.0], [2.0]) is None
    assert kendall_tau_b([1, 1, 1], [1, 2, 3]) is None
    assert kendall_tau_b([1, 2, 3], [4, 4, 4]) is None


def test_tau_b_rejects_mismatched_lengths_and_non_finite_values():
    with pytest.raises(ValueError):
        kendall_tau_b([1, 2, 3], [1, 2])
    with pytest.raises(ValueError):
        kendall_tau_b([1, float("nan"), 3], [1, 2, 3])


def _brute_tau_b(xs, ys) -> float:
    # tau_b = (P - Q) / sqrt((P + Q + T)(P + Q + U)); T = tied only in x, U = tied only in y.
    p = q = t = u = 0
    for (x1, y1), (x2, y2) in itertools.combinations(list(zip(xs, ys, strict=True)), 2):
        dx, dy = x1 - x2, y1 - y2
        if dx == 0 and dy == 0:
            continue
        if dx == 0:
            t += 1
        elif dy == 0:
            u += 1
        elif (dx > 0) == (dy > 0):
            p += 1
        else:
            q += 1
    return (p - q) / math.sqrt((p + q + t) * (p + q + u))


def test_tau_b_matches_a_brute_force_count_on_tie_heavy_random_data():
    rng = random.Random(7)
    checked = 0
    for _ in range(60):
        n = rng.randint(2, 45)
        xs = [rng.randint(0, 6) for _ in range(n)]
        ys = [rng.randint(0, 6) for _ in range(n)]
        got = kendall_tau_b(xs, ys)
        if len(set(xs)) == 1 or len(set(ys)) == 1:
            assert got is None
            continue
        assert got == pytest.approx(_brute_tau_b(xs, ys), abs=1e-12)
        checked += 1
    assert checked > 40
