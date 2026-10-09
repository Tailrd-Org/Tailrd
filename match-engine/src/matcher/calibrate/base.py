"""The calibrator protocol and the helpers every calibrator shares (SPEC §7).

A calibrator turns the engine's raw score in [0, 1] into the integer people see and reports how it
did so (`CalibrationInfo`: method, scope, n, version), so `bands.basis[scope]` can name the
reference group honestly (DECISIONS D-50). Calibrators are pure and deterministic: the same raw
score always gives the same answer, and a higher raw score never gives a lower one.
"""

from __future__ import annotations

import math
from numbers import Real
from typing import Protocol, runtime_checkable

from matcher.models import CalibrationInfo


@runtime_checkable
class Calibrator(Protocol):
    """Raw score in, (integer in [0, 100], how it was calibrated) out."""

    def calibrate(self, raw: float) -> tuple[int, CalibrationInfo]: ...


def check_raw(raw: float, what: str = "a raw score") -> float:
    """`raw` as a float. A non-number (a bool included) is a TypeError; NaN or infinity a ValueError.

    NaN would otherwise compare false against everything and quietly rank at the bottom.
    """
    if isinstance(raw, bool) or not isinstance(raw, Real):
        raise TypeError(f"{what} must be a number, got {type(raw).__name__}")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{what} must be finite, got {raw!r}")
    return value


def positive_int(value: object, what: str) -> int:
    """`value` if it is an integer of at least 1 (a bool is not one), else a ValueError."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{what} must be a positive integer, got {value!r}")
    return value


def unit_to_score(value: float) -> int:
    """round(100 x value) clamped to [0, 100]; halves go to the even integer, as Python's round.

    The clamp comes first (it commutes with the rounding), so no finite value can overflow.
    """
    return round(100 * min(1.0, max(0.0, value)))


_TEST_INFO = CalibrationInfo(method="identity", scope="test", n=0, version="test")


class FixedCalibrator:
    """Tests only: round(100 x raw) clamped to [0, 100], reported as the `test` identity calibration.

    Lets the engine be exercised with no reference data. It is never wired into a product path:
    `bands.basis["test"]` says "Test calibration".
    """

    def calibrate(self, raw: float) -> tuple[int, CalibrationInfo]:
        return unit_to_score(check_raw(raw)), _TEST_INFO
