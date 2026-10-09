"""Calibration: the raw score becomes the integer people see (SPEC §7; DECISIONS D-50, D-51, D-52).

- `base`: the `Calibrator` protocol and the test-only `FixedCalibrator`.
- `reference`, `percentile`: phase 1, the percentile of a raw score in a reference group, chosen by
  the chain posting -> family -> global -> panel by DISTINCT candidates.
- `isotonic`: phase 2, a monotone curve from raw score to P(advance), off below 500 labels.

Plain Python (standard library and pydantic only), deterministic; `load_panel` is the one reader of
a file and takes an explicit path.
"""

from matcher.calibrate.base import Calibrator, FixedCalibrator
from matcher.calibrate.isotonic import IsotonicCalibrator, IsotonicCurve, fit_isotonic
from matcher.calibrate.percentile import PercentileCalibrator, percentile
from matcher.calibrate.reference import (
    GLOBAL_KEY,
    MAX_SAMPLES,
    PanelError,
    ReferenceDistribution,
    build_reference,
    load_panel,
)

__all__ = [
    "GLOBAL_KEY",
    "MAX_SAMPLES",
    "Calibrator",
    "FixedCalibrator",
    "IsotonicCalibrator",
    "IsotonicCurve",
    "PanelError",
    "PercentileCalibrator",
    "ReferenceDistribution",
    "build_reference",
    "fit_isotonic",
    "load_panel",
    "percentile",
]
