"""Phase 2 calibration: isotonic regression from raw score to P(advance) (SPEC §7; DECISIONS D-51).

Fitted offline from outcome labels by pool-adjacent-violators (PAVA), applied at run time as a
monotone interpolation. Isotonic keeps order, so a better candidate can never be shown a lower
score. The curve is built and versioned today but stays off (`IsotonicCalibrator` refuses it)
until `calibration.isotonic_min_labels` (500) outcome labels exist; each curve records the
version it replaced in `previous`, so a bad refit can be rolled back by loading that version.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from typing import Annotated, Optional

from pydantic import Field, field_validator, model_validator

from matcher.calibrate.base import check_raw, positive_int, unit_to_score
from matcher.models import CalibrationInfo, Frozen

_UNIT = Annotated[float, Field(ge=0.0, le=1.0)]
_NAME = Annotated[str, Field(min_length=1, max_length=200)]


class IsotonicCurve(Frozen):
    """A monotone step function from raw score to P(advance), as knots.

    Between knots `apply` interpolates linearly; outside them it holds the end values. Each fitted
    plateau contributes its first and last raw score as knots at the plateau's value, so the curve
    is flat across a plateau and rises between plateaus. `n_labels` is how many outcome labels it
    was fitted on; `previous` is the version of the curve it replaced.
    """

    xs: tuple[_UNIT, ...]
    ys: tuple[_UNIT, ...]
    version: _NAME
    n_labels: int = Field(ge=0)
    previous: Optional[_NAME] = None

    @field_validator("xs", "ys")
    @classmethod
    def _positive_zero(cls, values: tuple[float, ...]) -> tuple[float, ...]:
        return tuple(value + 0.0 for value in values)  # one spelling of zero in the serialised bytes

    @model_validator(mode="after")
    def _monotone(self) -> "IsotonicCurve":
        if not self.xs:
            raise ValueError("a curve needs at least one knot")
        if len(self.xs) != len(self.ys):
            raise ValueError(f"xs has {len(self.xs)} knots but ys has {len(self.ys)}")
        if any(not low < high for low, high in zip(self.xs, self.xs[1:])):
            raise ValueError("xs must be strictly increasing")
        if any(low > high for low, high in zip(self.ys, self.ys[1:])):
            raise ValueError("ys must not decrease")
        if self.previous is not None and self.previous == self.version:
            raise ValueError("a curve cannot be its own previous version")
        return self

    def apply(self, raw: float) -> float:
        """P(advance) at `raw`, in [0, 1], non-decreasing in `raw`.

        Linear between the two knots around `raw`, clamped into their values so rounding can never
        make the function dip at a knot (with tiny raw scores the weight rounds to 1.0 and
        y0 + (y1 - y0) can land one ulp above y1); the first / last value outside the knots.
        """
        x = check_raw(raw)
        xs, ys = self.xs, self.ys
        if x <= xs[0]:
            return ys[0]
        if x >= xs[-1]:
            return ys[-1]
        i = bisect_right(xs, x)  # xs[i - 1] <= x < xs[i], and 1 <= i <= len(xs) - 1 here
        x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
        if x == x0:
            return y0
        interpolated = y0 + (y1 - y0) * ((x - x0) / (x1 - x0))
        return min(max(interpolated, y0), y1)


def fit_isotonic(
    pairs: Sequence[tuple[float, bool]], version: str, *, previous: Optional[str] = None
) -> IsotonicCurve:
    """Pool-adjacent-violators on (raw score, advanced) pairs. Offline: this is the refit job.

    Labels with the same raw score are pooled first (one value per distinct score). Scores are then
    walked in ascending order, each starting a block of (advanced, labels); while the block before
    has an average at least as high, the two are pooled. Equal neighbours are pooled too, so every
    plateau is one block and the curve has no redundant knots. Averages are compared as integer
    cross-products, so ties and violations never depend on float rounding, and the result does not
    depend on the order of `pairs`. `previous` is the version this curve replaces.
    """
    cells: dict[float, list[int]] = {}  # raw score -> [advanced, labels]
    total = 0
    for index, pair in enumerate(pairs):
        try:
            raw, advanced = pair
        except (TypeError, ValueError) as exc:
            raise ValueError(f"pairs[{index}] must be a (raw score, advanced) pair, got {pair!r}") from exc
        score = check_raw(raw, f"pairs[{index}] raw score")
        if not 0.0 <= score <= 1.0:
            raise ValueError(f"pairs[{index}] raw score {raw!r} is outside [0, 1]")
        if not isinstance(advanced, int) or advanced not in (0, 1):  # bool is an int
            raise ValueError(f"pairs[{index}] outcome must be True/False (or 0/1), got {advanced!r}")
        cell = cells.setdefault(score, [0, 0])
        cell[0] += int(advanced)
        cell[1] += 1
        total += 1
    if total == 0:
        raise ValueError("cannot fit an isotonic curve without any labelled pairs")

    blocks: list[list] = []  # [first raw score, last raw score, advanced, labels]
    for score in sorted(cells):
        advanced, labels = cells[score]
        blocks.append([score, score, advanced, labels])
        while len(blocks) > 1 and blocks[-2][2] * blocks[-1][3] >= blocks[-1][2] * blocks[-2][3]:
            last = blocks.pop()
            blocks[-1][1] = last[1]
            blocks[-1][2] += last[2]
            blocks[-1][3] += last[3]

    xs: list[float] = []
    ys: list[float] = []
    for first, last, advanced, labels in blocks:
        value = advanced / labels
        xs.append(first)
        ys.append(value)
        if last != first:
            xs.append(last)
            ys.append(value)
    return IsotonicCurve(xs=tuple(xs), ys=tuple(ys), version=version, n_labels=total, previous=previous)


class IsotonicCalibrator:
    """Calibrates with an isotonic curve: round(100 x P(advance)), reported as method "isotonic".

    The curve is fitted over every labelled application, so its scope is "global". Refuses (ValueError)
    a curve fitted on fewer than `isotonic_min_labels` labels: D-51 keeps isotonic off until 500
    outcome labels exist, and a refit that has not reached that must not go live.

    The score is a probability of advancing, not a rank, so the percentile wording of
    `bands.basis["global"]` does not describe it: the explanation has to key on
    `CalibrationInfo.method` before isotonic goes live.
    """

    def __init__(self, curve: IsotonicCurve, *, isotonic_min_labels: int) -> None:
        minimum = positive_int(isotonic_min_labels, "isotonic_min_labels")
        if curve.n_labels < minimum:
            raise ValueError(
                f"isotonic calibration stays off below {minimum} outcome labels (D-51): "
                f"curve {curve.version!r} was fitted on {curve.n_labels}"
            )
        self._curve = curve
        self._info = CalibrationInfo(
            method="isotonic", scope="global", n=curve.n_labels, version=curve.version
        )

    def calibrate(self, raw: float) -> tuple[int, CalibrationInfo]:
        return unit_to_score(self._curve.apply(raw)), self._info
