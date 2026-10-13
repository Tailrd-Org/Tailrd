"""Weight sensitivity (SPEC section 10): perturb each weight by +/-20 %, renormalise, and report the
band-change rate on the evaluation pairs. A dimension whose 20 % change flips more than 15 % of
bands is over-leveraged.

A band "changes" when a pair's band index differs, an abstention (band None) included. The
perturbed vector is renormalised onto the 4-decimal simplex (`constraints.normalise`); a vector that
leaves [0.03, 0.45] is still evaluated and marked `outside_bounds` (it could never be shipped, but
the leverage it measures is real).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from eval.metrics import PairScore
from eval.tune import constraints

FACTORS = (0.8, 1.2)
OVER_LEVERAGED = 0.15


def perturb(weights: Mapping[str, float], dimension: str, factor: float) -> dict[str, float]:
    if dimension not in constraints.DIMENSIONS:
        raise ValueError(f"unknown dimension {dimension!r}")
    return constraints.normalise({d: weights[d] * (factor if d == dimension else 1.0) for d in constraints.DIMENSIONS})


def band_change_rate(before: Sequence[PairScore], after: Sequence[PairScore]) -> float:
    """Share of pairs (matched by pair id) whose band index differs."""
    old = {p.pair_id: p.band_pred for p in before}
    shared = [p for p in after if p.pair_id in old]
    if not shared:
        return 0.0
    return sum(1 for p in shared if p.band_pred != old[p.pair_id]) / len(shared)


@dataclass(frozen=True)
class Row:
    dimension: str
    factor: float
    weight_before: float
    weight_after: float
    rate: float
    outside_bounds: bool

    @property
    def over_leveraged(self) -> bool:
        return self.rate > OVER_LEVERAGED


def analyse(pairs: Callable[[dict[str, float]], list[PairScore]], weights: Mapping[str, float]) -> list[Row]:
    """`pairs(weights)` scores the evaluation pairs under a weight vector."""
    weights = dict(weights)
    base = pairs(weights)
    rows = []
    for dimension in constraints.DIMENSIONS:
        for factor in FACTORS:
            moved = perturb(weights, dimension, factor)
            rows.append(Row(dimension, factor, weights[dimension], moved[dimension],
                            band_change_rate(base, pairs(moved)), bool(constraints.bound_violations(moved))))
    return rows


def markdown(rows: Sequence[Row], weights: Mapping[str, float], *, pairs_label: str = "evaluation pairs") -> str:
    lines = [
        "# Sensitivity (SPEC section 10)",
        "",
        f"Each weight of the vector below moved by -20 % and +20 %, renormalised to sum 1, and every "
        f"{pairs_label} re-scored. Band-change rate = share of pairs whose band (or abstention) changes. "
        f"Over-leveraged: rate > {OVER_LEVERAGED:.0%}.",
        "",
        "Vector: " + ", ".join(f"{d} {weights[d]:.4f}" for d in constraints.DIMENSIONS),
        "",
        "| Dimension | Weight | -20 % (renormalised) | Band changes | +20 % (renormalised) | Band changes | Flag |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    by = {(r.dimension, r.factor): r for r in rows}
    flagged = []
    for d in constraints.DIMENSIONS:
        down, up = by.get((d, 0.8)), by.get((d, 1.2))
        if down is None or up is None:
            continue
        over = down.over_leveraged or up.over_leveraged
        if over:
            flagged.append(d)
        notes = []
        if over:
            notes.append("**over-leveraged**")
        if down.outside_bounds or up.outside_bounds:
            notes.append("perturbed weight outside [0.03, 0.45]")
        lines.append(f"| {d} | {weights[d]:.4f} | {down.weight_after:.4f} | {down.rate:.1%} | {up.weight_after:.4f} "
                     f"| {up.rate:.1%} | {'; '.join(notes) or 'ok'} |")
    lines.append("")
    if flagged:
        lines.append(f"Over-leveraged (rebalance before adopting): {', '.join(flagged)}.")
    else:
        lines.append("No dimension is over-leveraged.")
    return "\n".join(lines) + "\n"
