"""Evaluation metrics (EVAL.md section 3): ranking, calibration, fairness and bootstrap CIs.

Every function takes `PairScore` records and returns None, never 0, when a metric is undefined on
the data it was given.
"""

from eval.metrics.bootstrap import CI, DeltaCI, bootstrap_ci, bootstrap_delta
from eval.metrics.calibration import abstention_stats, band_accuracy, expected_calibration_error
from eval.metrics.fairness import (
    FOUR_FIFTHS_THRESHOLD,
    FairnessResult,
    GroupRate,
    four_fifths,
    selection_rates,
)
from eval.metrics.pairs import Metric, PairScore, group_by_posting
from eval.metrics.ranking import auc_roc, kendall_tau_b, ndcg_at_k, per_posting, precision_at_k

__all__ = [
    "CI",
    "DeltaCI",
    "FOUR_FIFTHS_THRESHOLD",
    "FairnessResult",
    "GroupRate",
    "Metric",
    "PairScore",
    "abstention_stats",
    "auc_roc",
    "band_accuracy",
    "bootstrap_ci",
    "bootstrap_delta",
    "expected_calibration_error",
    "four_fifths",
    "group_by_posting",
    "kendall_tau_b",
    "ndcg_at_k",
    "per_posting",
    "precision_at_k",
    "selection_rates",
]
