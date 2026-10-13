"""The tuning constraints (EVAL.md section 5.1) and the weight simplex.

    sum(weights) == 1.0
    each weight in [0.03, 0.45]     no dimension dominates or vanishes
    fairness_ratio >= 0.80          hard constraint, infeasible if violated
    abstention_rate <= 0.15         accuracy is not bought by refusing to score

Weights live on a 4-decimal grid so a proposal reads like `config/weights.yaml` and sums to exactly
1.0 as decimals: `normalise` divides by the sum, rounds every weight to 4 decimals and gives the
rounding residual to the largest weight (the first in dimension order on a tie).

Fairness that cannot be assessed (no group with n >= 100, as on the synthetic corpus) leaves a
trial feasible, and the proposal then says it cannot be adopted (DECISIONS D-119). A fairness FAIL
is infeasible; on the incumbent weights it is terminal (`FairnessFailure`): the tuner stops.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Optional

from matcher.config import WEIGHT_MAX, WEIGHT_MIN
from matcher.models import DimensionName

DIMENSIONS: tuple[str, ...] = tuple(d.value for d in DimensionName)
DECIMALS = 4
FAIRNESS_MIN = 0.80
ABSTENTION_MAX = 0.15

_QUANTUM = Decimal(1).scaleb(-DECIMALS)


class FairnessFailure(RuntimeError):
    """The four-fifths guardrail fails: terminal, never tuned around (CLAUDE.md, EVAL section 7)."""


def normalise(weights: Mapping[str, float]) -> dict[str, float]:
    """`weights` scaled to sum 1 on the 4-decimal grid, summing to exactly 1 as decimals."""
    missing = set(DIMENSIONS) - set(weights)
    if missing:
        raise ValueError(f"weights missing dimensions: {sorted(missing)}")
    values = [float(weights[d]) for d in DIMENSIONS]
    if any(not math.isfinite(v) or v <= 0.0 for v in values):
        raise ValueError(f"weights must be finite and positive: {dict(zip(DIMENSIONS, values))}")
    total = math.fsum(values)
    rounded = [Decimal(repr(v / total)).quantize(_QUANTUM, rounding=ROUND_HALF_EVEN) for v in values]
    largest = max(range(len(rounded)), key=lambda i: (rounded[i], -i))
    rounded[largest] += Decimal(1) - sum(rounded, Decimal(0))
    return {d: float(r) for d, r in zip(DIMENSIONS, rounded)}


def from_unit(u: Sequence[float]) -> dict[str, float]:
    """The sampler's point in (0, 1]^7 -> a weight vector.

    `-log(u)` normalised is a flat Dirichlet draw `d` on the simplex; `w = 0.03 + 0.79 d` keeps every
    weight at or above the lower bound and still sums to 1. The upper bound (0.45) can still be
    broken; the search rejects such a vector without scoring it."""
    if len(u) != len(DIMENSIONS):
        raise ValueError(f"need {len(DIMENSIONS)} coordinates, got {len(u)}")
    x = [-math.log(min(1.0, max(float(v), 1e-12))) + 1e-12 for v in u]
    total = math.fsum(x)
    spare = 1.0 - len(DIMENSIONS) * WEIGHT_MIN
    return normalise({d: WEIGHT_MIN + spare * xi / total for d, xi in zip(DIMENSIONS, x)})


def bound_violations(weights: Mapping[str, float]) -> list[str]:
    out = []
    for d in DIMENSIONS:
        w = weights[d]
        if not (WEIGHT_MIN <= w <= WEIGHT_MAX):
            out.append(f"{d}={w} outside [{WEIGHT_MIN}, {WEIGHT_MAX}]")
    total = sum((Decimal(repr(weights[d])) for d in DIMENSIONS), Decimal(0))
    if abs(total - 1) > Decimal("1e-9"):
        out.append(f"weights sum to {total}, not 1")
    return out


@dataclass(frozen=True)
class Feasibility:
    feasible: bool
    reasons: tuple[str, ...]
    fairness: str  # "pass", "fail" or "not assessed"
    fairness_ratio: Optional[float]
    abstention_rate: Optional[float]


def fairness_status(guardrail: Any) -> str:
    if guardrail is None or guardrail.passed is None:
        return "not assessed"
    return "pass" if guardrail.passed else "fail"


def assess(weights: Mapping[str, float], *, abstention_rate: Optional[float], fairness: Any) -> Feasibility:
    """Every constraint of EVAL 5.1 on one evaluated vector. `fairness` is the four-fifths
    `Guardrail` (passed None = not assessed)."""
    reasons = list(bound_violations(weights))
    status = fairness_status(fairness)
    ratio = None if fairness is None else fairness.value
    if status == "fail":
        reasons.append(f"fairness: four-fifths ratio {ratio} < {FAIRNESS_MIN:.2f}")
    if abstention_rate is None:
        reasons.append("abstention rate undefined")
    elif abstention_rate > ABSTENTION_MAX:
        reasons.append(f"abstention rate {abstention_rate:.3f} > {ABSTENTION_MAX:.2f}")
    return Feasibility(not reasons, tuple(reasons), status, ratio, abstention_rate)


def out_of_bounds(weights: Mapping[str, float]) -> Feasibility:
    """A vector rejected before scoring: only its bounds are known."""
    return Feasibility(False, tuple(bound_violations(weights)), "not evaluated", None, None)
