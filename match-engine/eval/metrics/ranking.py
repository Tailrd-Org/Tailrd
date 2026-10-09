"""Ranking metrics: NDCG@k, Precision@k, AUC-ROC and Kendall tau-b (EVAL.md section 3).

The product ranks résumés for a posting, so ordering is what is measured. Within one posting the
scored pairs are ordered by score, highest first, and equal scores are broken by `pair_id`
ascending, so no result depends on the order the pairs arrive in. Abstained pairs (score None) are
never ranked. Every function returns None, not 0, when the metric is undefined on the data.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from itertools import chain

from eval.metrics.pairs import Metric, PairScore, group_by_posting


def _check_k(k: int) -> None:
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")


def _check_unique_pair_ids(group: Sequence[PairScore]) -> None:
    ids = [p.pair_id for p in group]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate pair_id inside posting {group[0].posting_id!r}")


def _ranked(group: Sequence[PairScore]) -> list[PairScore]:
    """The scored pairs of one posting, best first; ties fall back to pair_id ascending."""
    _check_unique_pair_ids(group)
    scored = [p for p in group if p.score is not None]
    scored.sort(key=lambda p: (-p.score, p.pair_id))
    return scored


def _mean(values: Iterable[float]) -> float | None:
    values = list(values)
    return math.fsum(values) / len(values) if values else None


def per_posting(pairs: Sequence[PairScore], metric: Metric) -> dict[str, float]:
    """`metric` applied to each posting's pairs; postings where it is undefined are left out.

    Keys come back in ascending posting id order.
    """
    result: dict[str, float] = {}
    for posting_id, group in group_by_posting(pairs).items():
        value = metric(group)
        if value is not None:
            result[posting_id] = value
    return result


def _dcg(relevances: Sequence[int], k: int) -> float:
    # Gain 2^rel - 1, discount log2(rank + 1); enumerate is 0-based so rank + 1 is i + 2.
    return math.fsum((2**rel - 1) / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))


def _ndcg_of_posting(group: Sequence[PairScore], k: int) -> float | None:
    ranked = _ranked(group)  # also validates the posting, whether or not it ends up skipped
    # The ideal ordering uses every pair of the posting, abstained ones included: a pair the
    # engine declined to rank still deserved a place, so abstaining cannot raise NDCG.
    ideal = _dcg(sorted((p.relevance for p in group), reverse=True), k)
    if ideal == 0.0:
        return None
    return _dcg([p.relevance for p in ranked], k) / ideal


def ndcg_at_k(pairs: Sequence[PairScore], k: int = 10) -> float | None:
    """NDCG@k per posting, averaged over the postings that have anything relevant in them.

    DCG = sum over ranks i = 1..k of (2^rel_i - 1) / log2(i + 1). A posting whose ideal DCG is 0
    (no pair with relevance above 0) cannot be scored and is skipped. None when no posting
    qualifies.
    """
    _check_k(k)
    return _mean(per_posting(pairs, lambda group: _ndcg_of_posting(group, k)).values())


def _precision_of_posting(group: Sequence[PairScore], k: int, good_threshold: int) -> float | None:
    top = _ranked(group)[:k]
    if not top:
        return None  # nothing shown, so nothing to be precise about
    return sum(1 for p in top if p.relevance >= good_threshold) / len(top)


def precision_at_k(pairs: Sequence[PairScore], k: int = 10, good_threshold: int = 2) -> float | None:
    """Share of the top k that is good (relevance >= `good_threshold`), averaged over postings.

    The denominator is min(k, scored pairs in the posting), so a posting with fewer than k scored
    pairs is not punished for being short. Postings with no scored pair are skipped.
    """
    _check_k(k)
    return _mean(per_posting(pairs, lambda group: _precision_of_posting(group, k, good_threshold)).values())


def auc_roc(pairs: Sequence[PairScore]) -> float | None:
    """P(a random advanced pair outscores a random not-advanced one); a tie counts one half.

    Pairs are pooled across postings. Abstained pairs and pairs with no known outcome are left
    out. None when either class is empty. This is the Mann-Whitney U statistic computed from
    average ranks, in integers (ranks doubled) so the only rounding is the final division.
    """
    labelled = sorted(
        ((p.score, bool(p.advanced)) for p in pairs if p.score is not None and p.advanced is not None),
        key=lambda item: item[0],
    )
    n_pos = sum(1 for _, advanced in labelled if advanced)
    n_neg = len(labelled) - n_pos
    if n_pos == 0 or n_neg == 0:
        return None
    twice_rank_sum = 0  # twice the sum of the positives' average ranks
    start = 0
    while start < len(labelled):
        end = start
        while end + 1 < len(labelled) and labelled[end + 1][0] == labelled[start][0]:
            end += 1
        # Tied scores share the average of the 1-based ranks start+1 .. end+1, which is
        # (start + end + 2) / 2; doubled, that is the integer start + end + 2.
        positives_here = sum(1 for _, advanced in labelled[start : end + 1] if advanced)
        twice_rank_sum += positives_here * (start + end + 2)
        start = end + 1
    twice_u = twice_rank_sum - n_pos * (n_pos + 1)
    return twice_u / (2 * n_pos * n_neg)


def _tied_pairs(values: Iterable[object]) -> int:
    return sum(count * (count - 1) // 2 for count in Counter(values).values())


def _strict_inversions(ranks: Sequence[int], size: int) -> int:
    """Pairs i < j with ranks[i] > ranks[j], counted with a Fenwick tree over the values 1..size."""
    tree = [0] * (size + 1)
    inversions = 0
    for seen, rank in enumerate(ranks):
        at_most = 0
        i = rank
        while i > 0:
            at_most += tree[i]
            i -= i & -i
        inversions += seen - at_most
        i = rank
        while i <= size:
            tree[i] += 1
            i += i & -i
    return inversions


def kendall_tau_b(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Kendall's tau-b with the standard tie correction, in O(n log n).

    tau_b = (C - D) / sqrt((n0 - n1)(n0 - n2)) where n0 = n(n-1)/2 and n1, n2 count the pairs tied
    in xs and in ys. A pair tied in either variable is neither concordant nor discordant. None for
    fewer than two points or when either input is constant (the denominator is zero).
    """
    if len(xs) != len(ys):
        raise ValueError(f"xs and ys differ in length ({len(xs)} vs {len(ys)})")
    if not all(math.isfinite(v) for v in chain(xs, ys)):
        raise ValueError("kendall_tau_b needs finite values")
    n = len(xs)
    if n < 2:
        return None
    n0 = n * (n - 1) // 2
    ties_x, ties_y = _tied_pairs(xs), _tied_pairs(ys)
    if ties_x == n0 or ties_y == n0:
        return None
    ties_both = _tied_pairs(zip(xs, ys, strict=True))
    # Ordered by (x, y), a pair of positions i < j is discordant exactly when y strictly drops:
    # equal x never counts because y is ascending inside an x tie.
    y_rank = {y: rank for rank, y in enumerate(sorted(set(ys)), start=1)}
    ordered = sorted(zip(xs, ys, strict=True))
    discordant = _strict_inversions([y_rank[y] for _, y in ordered], len(y_rank))
    concordant = n0 - ties_x - ties_y + ties_both - discordant
    tau = (concordant - discordant) / math.sqrt((n0 - ties_x) * (n0 - ties_y))
    return max(-1.0, min(1.0, tau))
