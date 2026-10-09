"""Calibration, band accuracy and abstention statistics (EVAL.md section 3, secondary metrics)."""

from __future__ import annotations

import pytest

from eval.metrics.calibration import abstention_stats, band_accuracy, expected_calibration_error
from eval.metrics.pairs import PairScore


def banded(pred: int | None, true: int | None, i: int = 0) -> PairScore:
    return PairScore(posting_id="p", pair_id=f"b{i}", score=0.5, relevance=2, band_pred=pred, band_true=true)


def abstainer(i: int, *, abstained: bool, hard: bool) -> PairScore:
    return PairScore(
        posting_id="p", pair_id=f"s{i}", score=None if abstained else 0.5, relevance=2, hard=hard
    )


# --------------------------------------------------------------------------------------------
# Expected calibration error
# --------------------------------------------------------------------------------------------


def test_ece_two_bin_toy_example():
    # bins=2 -> [0, 0.5) and [0.5, 1].
    # probs [0.2, 0.4, 0.6, 0.8], outcomes [F, T, T, F].
    # Bin 0 holds 0.2 and 0.4: mean prob 0.3, observed rate 1/2 -> gap 0.2, weight 2/4 -> 0.10.
    # Bin 1 holds 0.6 and 0.8: mean prob 0.7, observed rate 1/2 -> gap 0.2, weight 2/4 -> 0.10.
    # ECE = 0.10 + 0.10 = 0.20
    ece = expected_calibration_error([0.2, 0.4, 0.6, 0.8], [False, True, True, False], bins=2)
    assert ece == pytest.approx(0.20)


def test_ece_is_zero_when_every_bin_is_calibrated():
    # Four predictions of 0.25, one of which happens: mean prob 0.25 == observed rate 0.25.
    assert expected_calibration_error([0.25] * 4, [True, False, False, False]) == pytest.approx(0.0)


def test_ece_is_one_when_confidently_wrong():
    assert expected_calibration_error([1.0, 1.0], [False, False]) == pytest.approx(1.0)
    assert expected_calibration_error([0.0, 0.0], [True, True]) == pytest.approx(1.0)


def test_ece_lower_edges_open_upwards_and_the_last_bin_is_closed():
    # bins=2: 0.5 belongs to the UPPER bin [0.5, 1], so [0.5, 0.9] with outcomes [F, T] share a
    # bin: mean prob 0.7, observed 0.5 -> gap 0.2, weight 1 -> ECE 0.2.
    # (If 0.5 fell in the lower bin the result would be 0.5*0.5 + 0.5*0.1 = 0.3.)
    assert expected_calibration_error([0.5, 0.9], [False, True], bins=2) == pytest.approx(0.2)
    # p = 1.0 is in the last bin, not a phantom bin past the end.
    assert expected_calibration_error([1.0], [True], bins=10) == pytest.approx(0.0)
    assert expected_calibration_error([1.0, 0.0], [True, False], bins=10) == pytest.approx(0.0)


def test_ece_defaults_to_ten_equal_width_bins():
    # probs 0.05 (bin 0) and 0.15 (bin 1), outcomes [F, T].
    # Bin 0: |0.05 - 0| = 0.05, weight 1/2 -> 0.025.  Bin 1: |0.15 - 1| = 0.85, weight 1/2 -> 0.425.
    # ECE = 0.45.  (With 5 bins both fall in one bin: |0.10 - 0.5| = 0.4.)
    assert expected_calibration_error([0.05, 0.15], [False, True]) == pytest.approx(0.45)


def test_ece_is_none_for_empty_input():
    assert expected_calibration_error([], []) is None


@pytest.mark.parametrize("bad", [-0.01, 1.01, float("nan"), float("inf")])
def test_ece_rejects_probabilities_outside_the_unit_interval(bad):
    with pytest.raises(ValueError):
        expected_calibration_error([0.5, bad], [True, False])


def test_ece_rejects_mismatched_lengths_and_nonpositive_bins():
    with pytest.raises(ValueError):
        expected_calibration_error([0.5, 0.6], [True])
    with pytest.raises(ValueError):
        expected_calibration_error([0.5], [True], bins=0)


# --------------------------------------------------------------------------------------------
# Band accuracy
# --------------------------------------------------------------------------------------------


def test_band_accuracy_shares():
    # (pred, true): (2,2) exact; (1,2) and (3,2) off by one; (0,2) and (0,3) off by two or more.
    # A pair missing either band is not counted. 5 counted pairs -> 1/5, 2/5, 2/5.
    pairs = [
        banded(2, 2, 0),
        banded(1, 2, 1),
        banded(3, 2, 2),
        banded(0, 2, 3),
        banded(0, 3, 4),
        banded(None, 2, 5),
        banded(2, None, 6),
    ]
    acc = band_accuracy(pairs)
    assert set(acc) == {"exact", "off_by_one", "off_by_two_plus"}
    assert acc["exact"] == pytest.approx(0.2)
    assert acc["off_by_one"] == pytest.approx(0.4)
    assert acc["off_by_two_plus"] == pytest.approx(0.4)
    assert sum(acc.values()) == pytest.approx(1.0)


def test_band_accuracy_with_no_banded_pairs_reports_undefined_not_zero():
    # "0% off by two" on no data would read as a perfect result; None cannot be mistaken for it.
    empty = band_accuracy([banded(None, None)])
    assert empty == {"exact": None, "off_by_one": None, "off_by_two_plus": None}
    assert band_accuracy([]) == empty


# --------------------------------------------------------------------------------------------
# Abstention rate and precision
# --------------------------------------------------------------------------------------------


def test_abstention_rate_and_precision():
    # 10 pairs, 4 abstained (score None), 3 of those flagged hard.
    # rate = 4/10 = 0.4; precision = abstentions that were hard = 3/4 = 0.75.
    # Scored pairs that are hard do not enter the precision.
    pairs = (
        [abstainer(i, abstained=True, hard=True) for i in range(3)]
        + [abstainer(3, abstained=True, hard=False)]
        + [abstainer(i, abstained=False, hard=True) for i in range(4, 6)]
        + [abstainer(i, abstained=False, hard=False) for i in range(6, 10)]
    )
    stats = abstention_stats(pairs)
    assert set(stats) == {"rate", "precision"}
    assert stats["rate"] == pytest.approx(0.4)
    assert stats["precision"] == pytest.approx(0.75)


def test_abstention_precision_is_undefined_without_abstentions():
    stats = abstention_stats([abstainer(i, abstained=False, hard=True) for i in range(3)])
    assert stats == {"rate": 0.0, "precision": None}


def test_abstention_stats_on_empty_input():
    assert abstention_stats([]) == {"rate": None, "precision": None}
