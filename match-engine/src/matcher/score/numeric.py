"""Numeric guards for the scorer (SPEC §13 numeric safety rules; DECISIONS D-46).

Intermediates pass through `finite` and results through `clamp01`, so a NaN or an infinity is
never silently coerced into a score: it raises `EngineFault(Flag.NUMERIC_FAULT)`, which the entry
point lets escape in strict mode (tests) and turns into NEEDS_REVIEW in production.
"""

from __future__ import annotations

import math

from matcher.errors import EngineFault, Flag


def finite(x: float) -> float:
    """`x` as a float. NaN, ±inf, a non-number (bool included) or an int beyond float range
    raises `EngineFault(Flag.NUMERIC_FAULT)`."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise EngineFault(Flag.NUMERIC_FAULT, f"not a number: {type(x).__name__}")
    try:
        value = float(x)
    except OverflowError as exc:
        raise EngineFault(Flag.NUMERIC_FAULT, "integer beyond float range") from exc
    if not math.isfinite(value):
        raise EngineFault(Flag.NUMERIC_FAULT, f"non-finite value: {value!r}")
    return value


def clamp01(x: float) -> float:
    """`x` clamped to [0, 1]; non-finite input raises as in `finite`. -0.0 comes back as 0.0, so
    serialised output stays byte-identical."""
    value = finite(x)
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    return value


TIE_DECIMALS = 12


def tie_key(x: float) -> float:
    """`x` rounded to `TIE_DECIMALS` places, for ORDERING candidates only: products that are equal
    in exact arithmetic but multiplied in another order (0.85 × (0.65 × R) and 0.65 × (0.85 × R))
    can differ in the last bit, and that bit must not decide a tie. Callers keep and return the
    unrounded value of the candidate they pick. Non-finite input raises as in `finite`."""
    return round(finite(x), TIE_DECIMALS)
