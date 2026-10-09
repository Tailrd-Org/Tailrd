"""Audit and monitoring (SPEC section 9; EVAL section 6).

* `trace`: the feature trace persisted with every score, its digest, and the opaque id that keys
  demographic data (SPEC section 0.4 and 9; DECISIONS D-14).
* `fairness`: the four-fifths check over opaque-id records.
* `drift`: the EVAL section 6 alarms and the population stability index.

Runtime code: standard library and pydantic only. No numpy, no `eval`, no I/O, no clock, so it can
run next to the scorer. The annual audit artifacts are written by `scripts/bias_audit.py`.
"""

from matcher.audit.drift import DriftAlarm, drift_alarms, psi
from matcher.audit.fairness import FairnessReport, four_fifths
from matcher.audit.trace import build_trace, opaque_id, trace_digest

__all__ = [
    "DriftAlarm",
    "FairnessReport",
    "build_trace",
    "drift_alarms",
    "four_fifths",
    "opaque_id",
    "psi",
    "trace_digest",
]
