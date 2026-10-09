"""Isotonic calibration (SPEC §7 phase 2; DECISIONS D-51).

Pool-adjacent-violators on (raw score, advanced) pairs gives a monotone step function that
`apply` interpolates; the calibrator built on it stays off below `isotonic_min_labels` (500).
PAVA is checked against two textbook examples worked by hand and, as an independent oracle,
against the min-max formula for isotonic regression (Barlow et al., 1972).
"""

from __future__ import annotations

import math
import random
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from matcher.calibrate import Calibrator, IsotonicCalibrator, IsotonicCurve, fit_isotonic
from matcher.models import CalibrationInfo


def labels(x: float, advanced: int, total: int) -> list[tuple[float, bool]]:
    """`total` labelled applications that all scored `x`; `advanced` of them advanced."""
    return [(x, True)] * advanced + [(x, False)] * (total - advanced)


def random_labels(n: int, seed: int = 7) -> list[tuple[float, bool]]:
    """n labels where a higher raw score advances more often (P(advance) = raw)."""
    rng = random.Random(seed)
    return [(raw, rng.random() < raw) for raw in (rng.random() for _ in range(n))]


# ─── PAVA, worked by hand ──────────────────────────────────────────────────────────────────

# Eight applications, one per raw score. Blocks are (first x .. last x: advanced / labels):
#   0.1 F  -> [0.1: 0/1]
#   0.2 T  -> [0.1: 0/1] [0.2: 1/1]                       0 <= 1: no violation
#   0.3 F  -> [0.1: 0/1] [0.2-0.3: 1/2]                    1 > 0 violates: pool -> 1/2; 0 <= 1/2 ok
#   0.4 T  -> ... [0.4: 1/1]                               1/2 <= 1 ok
#   0.5 T  -> ... [0.4-0.5: 2/2]                           equal neighbours are pooled too (one plateau)
#   0.6 F  -> ... [0.4-0.6: 2/3]                           1 > 0 violates: pool -> 2/3; 1/2 <= 2/3 ok
#   0.7 T  -> ... [0.7: 1/1]                               2/3 <= 1 ok
#   0.8 T  -> ... [0.7-0.8: 2/2]                           pooled with its equal neighbour
# Final blocks 0.1: 0 | 0.2-0.3: 1/2 | 0.4-0.6: 2/3 | 0.7-0.8: 1. A block contributes its first and
# last x as knots (one knot if it is a single x), both at the block's value.
EXAMPLE_A = [
    (0.1, False),
    (0.2, True),
    (0.3, False),
    (0.4, True),
    (0.5, True),
    (0.6, False),
    (0.7, True),
    (0.8, True),
]
A_XS = (0.1, 0.2, 0.3, 0.4, 0.6, 0.7, 0.8)
A_YS = (0.0, 0.5, 0.5, 2 / 3, 2 / 3, 1.0, 1.0)


def test_pava_on_a_binary_example_worked_by_hand() -> None:
    curve = fit_isotonic(EXAMPLE_A, "iso-1")
    assert curve.xs == A_XS
    assert curve.ys == pytest.approx(A_YS, abs=1e-12)
    assert curve.n_labels == 8
    assert (curve.version, curve.previous) == ("iso-1", None)


def test_pava_on_the_textbook_example() -> None:
    # The classic y = (1, 3, 2, 4, 3, 5) (Barlow et al.), scaled to labels: at x = 0.1 ... 0.6,
    # 10 applications each, y_i of which advanced. Block means: 0.1, 0.3, 0.2, 0.4, 0.3, 0.5.
    #   0.3 > 0.2 violates: pool (3 + 2) / 20 = 0.25, and 0.1 <= 0.25 holds.
    #   0.4 > 0.3 violates: pool (4 + 3) / 20 = 0.35, and 0.25 <= 0.35 holds.
    # Fitted y = (1, 2.5, 2.5, 3.5, 3.5, 5) / 10.
    pairs = [pair for i, y in enumerate((1, 3, 2, 4, 3, 5)) for pair in labels((i + 1) / 10, y, 10)]
    curve = fit_isotonic(pairs, "iso-1")
    assert curve.xs == (0.1, 0.2, 0.3, 0.4, 0.5, 0.6)
    assert curve.ys == pytest.approx((0.1, 0.25, 0.25, 0.35, 0.35, 0.5), abs=1e-12)
    assert curve.n_labels == 60


def test_a_violation_pools_backwards_through_every_block_it_undercuts() -> None:
    # Means 5/10, 6/10, 7/10 rise, then 0/10 undercuts all three:
    #   pool with 7/10 -> 7/20 = 0.35 < 0.6: pool again -> 13/30 = 0.433 < 0.5: pool again -> 18/40.
    pairs = [p for x, y in ((0.1, 5), (0.2, 6), (0.3, 7), (0.4, 0)) for p in labels(x, y, 10)]
    curve = fit_isotonic(pairs, "iso-1")
    assert curve.xs == (0.1, 0.4)
    assert curve.ys == pytest.approx((0.45, 0.45), abs=1e-12)


def test_applications_with_the_same_raw_score_are_pooled_first() -> None:
    curve = fit_isotonic([(0.5, True), (0.5, False), (0.5, False), (0.5, False)], "iso-1")
    assert (curve.xs, curve.ys) == ((0.5,), (0.25,))
    # 0.5 holds 1 of 2, 0.7 holds 1 of 1: already non-decreasing, so two knots.
    curve = fit_isotonic([(0.7, True), (0.5, True), (0.5, False)], "iso-1")
    assert (curve.xs, curve.ys) == ((0.5, 0.7), (0.5, 1.0))


def test_a_single_label_and_single_class_label_sets() -> None:
    one = fit_isotonic([(0.3, True)], "iso-1")
    assert (one.xs, one.ys, one.n_labels) == ((0.3,), (1.0,), 1)
    everyone = fit_isotonic([(0.2, True), (0.8, True)], "iso-1")
    assert (everyone.xs, everyone.ys) == ((0.2, 0.8), (1.0, 1.0))
    nobody = fit_isotonic([(0.2, False), (0.5, False), (0.8, False)], "iso-1")
    assert (nobody.xs, nobody.ys) == ((0.2, 0.8), (0.0, 0.0))


def test_the_input_order_does_not_matter() -> None:
    expected = fit_isotonic(EXAMPLE_A, "iso-1")
    assert fit_isotonic(list(reversed(EXAMPLE_A)), "iso-1") == expected
    rng = random.Random(3)
    shuffled = list(EXAMPLE_A)
    rng.shuffle(shuffled)
    assert fit_isotonic(shuffled, "iso-1") == expected


def test_outcomes_may_be_zero_and_one() -> None:
    assert fit_isotonic([(0.2, 0), (0.8, 1)], "v") == fit_isotonic([(0.2, False), (0.8, True)], "v")
    for bad in (2, -1, 0.5, "yes", None):
        with pytest.raises(ValueError):
            fit_isotonic([(0.2, bad)], "v")  # type: ignore[list-item]


@pytest.mark.parametrize(
    "pairs",
    [
        [],
        [(float("nan"), True)],
        [(float("inf"), True)],
        [(-0.01, True)],
        [(1.01, False)],
        [(0.5,)],
        [0.5],
    ],
)
def test_fit_refuses_unusable_labels(pairs) -> None:
    with pytest.raises(ValueError):
        fit_isotonic(pairs, "iso-1")


def test_fit_refuses_an_empty_version() -> None:
    with pytest.raises(ValueError):
        fit_isotonic(EXAMPLE_A, "")


# ─── The independent oracle and the properties ─────────────────────────────────────────────


def min_max_fit(pairs) -> dict[float, Fraction]:
    """Isotonic regression by the min-max formula, in exact arithmetic:
    fit(i) = max over s <= i of min over t >= i of the label-weighted mean of groups s..t."""
    cells: dict[float, list[int]] = {}
    for raw, advanced in pairs:
        cell = cells.setdefault(raw + 0.0, [0, 0])
        cell[0] += int(advanced)
        cell[1] += 1
    xs = sorted(cells)
    positives = [cells[x][0] for x in xs]
    counts = [cells[x][1] for x in xs]

    def mean(s: int, t: int) -> Fraction:
        return Fraction(sum(positives[s : t + 1]), sum(counts[s : t + 1]))

    n = len(xs)
    return {xs[i]: max(min(mean(s, t) for t in range(i, n)) for s in range(i + 1)) for i in range(n)}


RAWS = st.one_of(st.sampled_from([0.0, 0.1, 0.25, 0.5, 0.75, 0.9, 1.0]), st.floats(0.0, 1.0))
PAIRS = st.lists(st.tuples(RAWS, st.booleans()), min_size=1, max_size=40)


@given(pairs=PAIRS)
def test_pava_equals_the_min_max_formula(pairs) -> None:
    curve = fit_isotonic(pairs, "v")
    expected = min_max_fit(pairs)
    for x, y in expected.items():
        assert curve.apply(x) == pytest.approx(float(y), abs=1e-12)


@given(pairs=PAIRS)
def test_the_fitted_curve_is_monotone_bounded_and_preserves_the_total(pairs) -> None:
    curve = fit_isotonic(pairs, "v")
    assert all(a < b for a, b in zip(curve.xs, curve.xs[1:]))
    assert all(a <= b for a, b in zip(curve.ys, curve.ys[1:]))
    assert all(0.0 <= y <= 1.0 for y in curve.ys)
    assert len(curve.xs) <= 2 * len({raw + 0.0 for raw, _ in pairs})
    assert curve.n_labels == len(pairs)
    # Isotonic regression keeps the number of advances: the fitted values add up to it.
    fitted_total = math.fsum(curve.apply(raw) for raw, _ in pairs)
    assert fitted_total == pytest.approx(sum(advanced for _, advanced in pairs), abs=1e-9)


@given(pairs=PAIRS, data=st.data())
def test_fit_ignores_the_order_of_the_labels(pairs, data) -> None:
    order = data.draw(st.permutations(range(len(pairs))))
    assert fit_isotonic([pairs[i] for i in order], "v") == fit_isotonic(pairs, "v")


@given(pairs=PAIRS, a=st.floats(-0.5, 1.5), b=st.floats(-0.5, 1.5))
def test_apply_never_decreases(pairs, a, b) -> None:
    curve = fit_isotonic(pairs, "v")
    low, high = sorted((a, b))
    assert 0.0 <= curve.apply(low) <= curve.apply(high) <= 1.0


def test_fit_and_apply_agree_with_scikit_learn() -> None:
    # An independent implementation for the whole curve, interpolation and end clamps included.
    # scikit-learn belongs to the optional `eval` extra, so without it this test is skipped.
    np = pytest.importorskip("numpy")
    sklearn_isotonic = pytest.importorskip("sklearn.isotonic")
    rng = random.Random(2026)
    for _ in range(60):
        n = rng.choice([5, 20, 100, 400])
        levels = rng.choice([3, 10, 1_000, 1_000_000])
        steps = [rng.randrange(levels) for _ in range(n)]
        pairs = [(k / levels, rng.random() < 0.1 + 0.7 * k / levels) for k in steps]
        curve = fit_isotonic(pairs, "v")
        reference = sklearn_isotonic.IsotonicRegression(
            y_min=0.0, y_max=1.0, increasing=True, out_of_bounds="clip"
        )
        reference.fit(np.array([raw for raw, _ in pairs]), np.array([float(a) for _, a in pairs]))
        grid = sorted({raw for raw, _ in pairs}) + [rng.random() for _ in range(40)]
        for x, expected in zip(grid, reference.predict(np.array(grid))):
            assert curve.apply(x) == pytest.approx(float(expected), abs=1e-12), (n, levels, x)


# ─── IsotonicCurve.apply ───────────────────────────────────────────────────────────────────


def test_apply_interpolates_between_steps_and_is_flat_inside_them() -> None:
    curve = fit_isotonic(EXAMPLE_A, "iso-1")
    cases = [
        (0.05, 0.0),  # left of every knot: the first value
        (0.15, 0.25),  # halfway between (0.1, 0) and (0.2, 0.5)
        (0.25, 0.5),  # inside the 0.5 step
        (0.35, 0.5 + (2 / 3 - 0.5) / 2),  # halfway between (0.3, 1/2) and (0.4, 2/3) = 7/12
        (0.5, 2 / 3),  # inside the 2/3 step
        (0.65, 2 / 3 + (1 - 2 / 3) / 2),  # halfway between (0.6, 2/3) and (0.7, 1) = 5/6
        (0.75, 1.0),
        (0.95, 1.0),  # right of every knot: the last value
    ]
    for raw, expected in cases:
        assert curve.apply(raw) == pytest.approx(expected, abs=1e-12), raw


def test_apply_returns_the_knot_values_exactly() -> None:
    curve = fit_isotonic(EXAMPLE_A, "iso-1")
    for x, y in zip(curve.xs, curve.ys):
        assert curve.apply(x) == y


def test_apply_never_dips_across_a_knot() -> None:
    curve = fit_isotonic(EXAMPLE_A, "iso-1")
    for knot in curve.xs:
        before, after = math.nextafter(knot, -math.inf), math.nextafter(knot, math.inf)
        assert curve.apply(before) <= curve.apply(knot) <= curve.apply(after)


def test_interpolation_cannot_overshoot_the_next_knot() -> None:
    # Regression for a float effect found by search: with tiny raw scores the interpolation weight
    # t = (x - x0) / (x1 - x0) rounds to exactly 1.0 one step below x1, and y0 + (y1 - y0) is then
    # 0.7222126503461719, one ulp ABOVE y1 = 0.7222126503461718. Unclamped, the curve would dip
    # when x steps onto the knot, so a better candidate could be shown a lower number.
    x0, x1 = 1.8182827275299898e-19, 4.172954706875437e-19
    y0, y1 = 0.17167994459052388, 0.7222126503461718
    curve = IsotonicCurve(xs=(x0, x1), ys=(y0, y1), version="v", n_labels=1, previous=None)
    below = math.nextafter(x1, 0.0)
    assert x0 < below < x1
    assert y0 + (y1 - y0) * ((below - x0) / (x1 - x0)) > y1  # the raw interpolation does overshoot
    assert curve.apply(below) <= curve.apply(x1) == y1


@given(pairs=PAIRS, data=st.data())
def test_apply_stays_between_the_values_of_the_knots_around_it(pairs, data) -> None:
    curve = fit_isotonic(pairs, "v")
    if len(curve.xs) < 2:
        return
    i = data.draw(st.integers(min_value=1, max_value=len(curve.xs) - 1))
    x = data.draw(st.floats(min_value=curve.xs[i - 1], max_value=curve.xs[i]))
    assert curve.ys[i - 1] <= curve.apply(x) <= curve.ys[i]


def test_apply_with_a_single_knot_is_constant() -> None:
    curve = IsotonicCurve(xs=(0.4,), ys=(0.3,), version="v", n_labels=1, previous=None)
    assert [curve.apply(raw) for raw in (0.0, 0.4, 1.0)] == [0.3, 0.3, 0.3]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_apply_refuses_a_non_finite_raw_score(bad: float) -> None:
    with pytest.raises(ValueError):
        fit_isotonic(EXAMPLE_A, "v").apply(bad)


# ─── IsotonicCurve: validation, versions, rollback ─────────────────────────────────────────

GOOD_CURVE = {
    "xs": (0.0, 0.5, 1.0),
    "ys": (0.1, 0.4, 0.9),
    "version": "iso-2",
    "n_labels": 600,
    "previous": "iso-1",
}


def test_the_good_curve_used_by_the_validation_tests_is_good() -> None:
    assert IsotonicCurve(**GOOD_CURVE).ys == (0.1, 0.4, 0.9)


@pytest.mark.parametrize(
    "change",
    [
        {"xs": (0.0, 0.5, 0.5)},  # not strictly increasing
        {"xs": (0.5, 0.0, 1.0)},
        {"ys": (0.1, 0.9, 0.4)},  # decreasing
        {"ys": (0.1, 0.4)},  # lengths differ
        {"xs": (), "ys": ()},  # empty
        {"xs": (0.0, 0.5, 1.5)},  # raw scores live in [0, 1]
        {"xs": (-0.5, 0.5, 1.0)},
        {"ys": (0.1, 0.4, 1.5)},  # probabilities live in [0, 1]
        {"ys": (-0.1, 0.4, 0.9)},
        {"ys": (0.1, float("nan"), 0.9)},
        {"version": ""},
        {"previous": ""},
        {"previous": "iso-2"},  # a curve cannot be its own predecessor
        {"n_labels": -1},
        {"unexpected": 1},
    ],
)
def test_a_curve_must_be_a_monotone_function_of_a_raw_score(change) -> None:
    with pytest.raises(ValidationError):
        IsotonicCurve(**{**GOOD_CURVE, **change})


def test_curves_are_versioned_with_the_previous_one_kept_for_rollback() -> None:
    first = fit_isotonic(EXAMPLE_A, "iso-2026-09")
    assert (first.version, first.previous) == ("iso-2026-09", None)
    second = fit_isotonic(EXAMPLE_A, "iso-2026-10", previous=first.version)
    assert (second.version, second.previous) == ("iso-2026-10", "iso-2026-09")
    assert (second.xs, second.ys) == (first.xs, first.ys)  # same labels, same curve; new lineage
    with pytest.raises(ValueError):
        fit_isotonic(EXAMPLE_A, "same", previous="same")


def test_a_curve_round_trips_through_json_and_is_immutable() -> None:
    curve = fit_isotonic(EXAMPLE_A, "iso-2", previous="iso-1")
    assert IsotonicCurve.model_validate_json(curve.model_dump_json()) == curve
    with pytest.raises(ValidationError):
        curve.version = "other"  # type: ignore[misc]


# ─── IsotonicCalibrator ────────────────────────────────────────────────────────────────────

STEP_CURVE = IsotonicCurve(xs=A_XS, ys=A_YS, version="iso-A", n_labels=500, previous="iso-0")


def test_isotonic_stays_off_below_the_label_minimum(config) -> None:
    minimum = config.scoring.calibration.isotonic_min_labels
    assert minimum == 500  # D-51
    with pytest.raises(ValueError, match="499"):
        IsotonicCalibrator(fit_isotonic(random_labels(499), "iso-499"), isotonic_min_labels=minimum)
    assert isinstance(
        IsotonicCalibrator(fit_isotonic(random_labels(500), "iso-500"), isotonic_min_labels=minimum),
        Calibrator,
    )


def test_a_curve_with_no_labels_is_never_active() -> None:
    empty = IsotonicCurve(xs=(0.0, 1.0), ys=(0.0, 1.0), version="v", n_labels=0, previous=None)
    with pytest.raises(ValueError):
        IsotonicCalibrator(empty, isotonic_min_labels=500)


@pytest.mark.parametrize("bad", [0, -5, 1.5, "500", None, True])
def test_the_label_minimum_must_be_a_positive_integer(bad) -> None:
    with pytest.raises(ValueError):
        IsotonicCalibrator(STEP_CURVE, isotonic_min_labels=bad)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0.05, 0),  # left of every knot: 0
        (0.25, 50),  # inside the 1/2 step
        (0.35, 58),  # 7/12 = 58.3 %
        (0.5, 67),  # inside the 2/3 step: 66.7 %
        (0.65, 83),  # 5/6 = 83.3 %
        (0.75, 100),
        (0.95, 100),  # right of every knot: 100
    ],
)
def test_calibrate_is_the_rounded_probability_of_advancing(raw: float, expected: int) -> None:
    score, info = IsotonicCalibrator(STEP_CURVE, isotonic_min_labels=500).calibrate(raw)
    assert score == expected
    assert info == CalibrationInfo(method="isotonic", scope="global", n=500, version="iso-A")


def test_the_calibration_info_names_the_curve_and_its_labels() -> None:
    curve = fit_isotonic(random_labels(650), "iso-650")
    _, info = IsotonicCalibrator(curve, isotonic_min_labels=500).calibrate(0.4)
    assert (info.method, info.scope, info.n, info.version) == ("isotonic", "global", 650, "iso-650")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_the_calibrator_refuses_a_non_finite_raw_score(bad: float) -> None:
    with pytest.raises(ValueError):
        IsotonicCalibrator(STEP_CURVE, isotonic_min_labels=500).calibrate(bad)


@given(pairs=PAIRS, a=st.floats(-0.5, 1.5), b=st.floats(-0.5, 1.5))
def test_a_better_candidate_is_never_shown_a_lower_score(pairs, a, b) -> None:
    # The reason SPEC §7 chose isotonic: monotone in the raw score, and always an integer in range.
    calibrator = IsotonicCalibrator(fit_isotonic(pairs, "v"), isotonic_min_labels=1)
    low, high = sorted((a, b))
    (low_score, _), (high_score, _) = calibrator.calibrate(low), calibrator.calibrate(high)
    assert isinstance(low_score, int) and isinstance(high_score, int)
    assert 0 <= low_score <= high_score <= 100


def test_a_fitted_curve_tracks_the_probability_that_generated_the_labels() -> None:
    # 5,000 labels where P(advance) = raw: the fitted probability should sit near raw. (Isotonic
    # fits are noisy where data are thin, so this checks the average gap, 0.02 to 0.03 for these
    # seeds, rather than any single point.)
    for seed in (11, 12, 13):
        curve = fit_isotonic(random_labels(5_000, seed=seed), "iso-big")
        gaps = [abs(curve.apply(step / 100) - step / 100) for step in range(5, 96)]
        assert math.fsum(gaps) / len(gaps) < 0.05, seed
