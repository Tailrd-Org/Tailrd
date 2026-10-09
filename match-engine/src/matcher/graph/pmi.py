"""Related edges mined from posting co-occurrence (SPEC §3.2, DECISIONS D-22).

Edge weight is normalised PMI: `NPMI(a, b) = PMI(a, b) / -ln P(a, b)`, i.e. PMI divided by its
per-pair maximum. A global PMI_max would rank pairs by rarity, not by association.
"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Mapping, Optional

_NO_CURATED: Mapping[str, Mapping[str, float]] = MappingProxyType({})


def npmi(n: int, df_a: int, df_b: int, df_ab: int) -> Optional[float]:
    """Normalised PMI of two skills over `n` postings, clamped to [-1, 1].

    None when the pair never co-occurs (`df_ab == 0`) or co-occurs in every posting
    (`P(a, b) == 1`, where -ln P(a, b) is 0). Impossible counts raise ValueError.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")
    if not (0 <= df_ab <= min(df_a, df_b) and max(df_a, df_b) <= n and df_a + df_b - df_ab <= n):
        raise ValueError(f"impossible counts: n={n}, df_a={df_a}, df_b={df_b}, df_ab={df_ab}")
    if df_ab == 0 or df_ab == n:
        return None
    # PMI = ln(p_ab / (p_a * p_b)) = ln(df_ab * n / (df_a * df_b)); -ln p_ab = ln(n / df_ab).
    # Integer products are exact, so each logarithm sees one correctly rounded quotient.
    pmi = math.log((df_ab * n) / (df_a * df_b))
    return max(-1.0, min(1.0, pmi / math.log(n / df_ab)))


def build_edges(
    df: Mapping[str, int],
    co_df: Mapping[tuple[str, str], int],
    n: int,
    *,
    threshold: float,
    min_cooccurrence: int,
    max_out_degree: int,
    curated: Mapping[str, Mapping[str, float]] = _NO_CURATED,
) -> dict[str, dict[str, float]]:
    """Related edges `{a: {b: weight}}` with weights in [0, 1] (DECISIONS D-22).

    A pair is kept when it co-occurs in at least `min_cooccurrence` postings and its NPMI is at
    least `threshold`; its weight is NPMI clamped to [0, 1], stored in both directions. Each node
    then keeps its `max_out_degree` heaviest edges (ties: smaller target id first). Curated edges
    are laid over the result last: their weight wins, in both directions, and the degree cap never
    drops them. Self-pairs are ignored. `co_df` keys are unordered pairs.
    """
    if max_out_degree < 0:
        raise ValueError(f"max_out_degree must be >= 0, got {max_out_degree}")

    pairs: dict[tuple[str, str], int] = {}
    for (a, b), count in co_df.items():
        if a == b:
            continue
        pair = (a, b) if a < b else (b, a)
        if pairs.get(pair, count) != count:
            raise ValueError(f"conflicting co-occurrence counts for {pair}: {pairs[pair]} and {count}")
        pairs[pair] = count

    candidates: dict[str, list[tuple[float, str]]] = {}
    for a, b in sorted(pairs):
        count = pairs[(a, b)]
        if count < min_cooccurrence:
            continue
        if a not in df or b not in df:
            raise ValueError(f"no document frequency for {a if a not in df else b}")
        value = npmi(n, df[a], df[b], count)
        if value is None or value < threshold:
            continue
        weight = max(0.0, min(1.0, value))
        candidates.setdefault(a, []).append((weight, b))
        candidates.setdefault(b, []).append((weight, a))

    edges: dict[str, dict[str, float]] = {}
    for node, outgoing in candidates.items():
        outgoing.sort(key=lambda edge: (-edge[0], edge[1]))
        kept = outgoing[:max_out_degree]
        if kept:
            edges[node] = {target: weight for weight, target in kept}

    curated_pairs: dict[tuple[str, str], float] = {}
    for a in sorted(curated):
        for b in sorted(curated[a]):
            if a == b:
                continue
            weight = float(curated[a][b])
            if not 0.0 <= weight <= 1.0:  # also rejects NaN
                raise ValueError(f"curated edge {a}->{b} weight {weight} outside [0, 1]")
            pair = (a, b) if a < b else (b, a)
            if pair not in curated_pairs:
                curated_pairs[pair] = weight
            elif not math.isclose(curated_pairs[pair], weight):
                raise ValueError(f"curated edge {pair} given two weights: {curated_pairs[pair]} and {weight}")
    for (a, b), weight in sorted(curated_pairs.items()):
        edges.setdefault(a, {})[b] = weight
        edges.setdefault(b, {})[a] = weight

    return {node: dict(sorted(edges[node].items())) for node in sorted(edges)}
