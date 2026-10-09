"""Percentile calibration (SPEC §7 phase 1; DECISIONS D-50).

Hand-computed percentiles first (ties included), then reference construction, then the fallback
chain posting -> family -> global -> panel by DISTINCT candidates, then properties. Expected
values are closed-form arithmetic from the decision, independent of the implementation.
"""

from __future__ import annotations

import json
import random
from bisect import bisect_left, bisect_right
from fractions import Fraction

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from pydantic import ValidationError

from matcher.calibrate import (
    GLOBAL_KEY,
    MAX_SAMPLES,
    Calibrator,
    PercentileCalibrator,
    ReferenceDistribution,
    build_reference,
    percentile,
)
from matcher.models import CalibrationInfo

MIN_N = 30  # config/scoring.yaml calibration.min_reference_n (checked against the config below)
PROBE = 0.5


def people(n: int) -> list[str]:
    return [f"person-{i}" for i in range(n)]


def reference(samples, *, scope="global", key=GLOBAL_KEY, version="ref-1") -> ReferenceDistribution:
    """Every score belongs to its own candidate: n_candidates == n_pairs == len(samples)."""
    scores = list(samples)
    return build_reference(scope, key, scores, people(len(scores)), version)


def tier(scope: str, key: str, n: int, value: float, version: str) -> ReferenceDistribution:
    """`n` distinct people, each scored once, all at `value`."""
    return build_reference(scope, key, [value] * n, people(n), version)


def mixed(scope: str, key: str, n: int, version: str) -> ReferenceDistribution:
    """`n` people scored 0.1, 0.2, 0.3, 0.7, 0.1, ...: for n = 40, ten of each."""
    scores = [(0.1, 0.2, 0.3, 0.7)[i % 4] for i in range(n)]
    return build_reference(scope, key, scores, people(n), version)


def percentile_by_formula(raw: float, samples) -> int:
    """The decision's formula, written the plain way: round(100 x (below + 0.5 x equal) / n)."""
    below = sum(1 for s in samples if s < raw)
    equal = sum(1 for s in samples if s == raw)
    return min(100, max(0, round(100 * (below + 0.5 * equal) / len(samples))))


# ─── percentile(): hand-computed ───────────────────────────────────────────────────────────

TIED = (0.2, 0.4, 0.4, 0.4, 0.8)  # n = 5, a three-way tie in the middle


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0.1, 0),  # below 0, equal 0 -> 0 / 5
        (0.2, 10),  # below 0, equal 1 -> 0.5 / 5 = 10 %
        (0.3, 20),  # below 1, equal 0 -> 1 / 5 = 20 %
        (0.4, 50),  # below 1, equal 3 -> (1 + 1.5) / 5 = 50 %
        (0.5, 80),  # below 4, equal 0 -> 4 / 5 = 80 %
        (0.8, 90),  # below 4, equal 1 -> (4 + 0.5) / 5 = 90 %
        (0.9, 100),  # below 5 -> 5 / 5
    ],
)
def test_percentile_counts_below_plus_half_of_the_ties(raw: float, expected: int) -> None:
    assert percentile(raw, reference(TIED)) == expected


def test_percentile_rounds_to_the_nearest_integer() -> None:
    ref = reference([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8])  # n = 8, all distinct
    # A raw equal to the k-th score (k = 1..8) has below = k - 1, equal = 1:
    # (k - 0.5) / 8 = 6.25 %, 18.75 %, 31.25 %, 43.75 %, 56.25 %, 68.75 %, 81.25 %, 93.75 %.
    assert [percentile(s, ref) for s in ref.samples] == [6, 19, 31, 44, 56, 69, 81, 94]


def test_exact_halves_round_to_the_even_integer() -> None:
    # D-50 says "rounded"; the brief writes round(...): Python's round, halves to even.
    ref = reference([i / 100 for i in range(100)])  # 100 distinct scores
    # The k-th smallest (k = 0, 1, 2, 3) has below = k, equal = 1: k + 0.5 % = 0.5, 1.5, 2.5, 3.5.
    assert [percentile(ref.samples[k], ref) for k in range(4)] == [0, 2, 2, 4]
    # Between two scores there is no tie: below = 1 -> exactly 1 %.
    assert percentile(0.005, ref) == 1


def test_percentile_of_a_single_score() -> None:
    ref = reference([0.5])
    assert [percentile(0.4, ref), percentile(0.5, ref), percentile(0.6, ref)] == [0, 50, 100]


def test_a_reference_where_everyone_scored_the_same() -> None:
    ref = reference([0.3] * 10)
    assert percentile(0.3, ref) == 50  # all equal: (0 + 0.5 x 10) / 10
    assert percentile(0.29, ref) == 0
    assert percentile(0.31, ref) == 100


def test_raw_scores_beyond_the_reference_clamp_to_the_ends() -> None:
    ref = reference([0.2, 0.6])
    assert percentile(-5.0, ref) == 0
    assert percentile(5.0, ref) == 100


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_percentile_refuses_a_non_finite_raw_score(bad: float) -> None:
    with pytest.raises(ValueError):
        percentile(bad, reference(TIED))


def test_percentile_refuses_things_that_are_not_numbers() -> None:
    for bad in ("0.5", None, True):
        with pytest.raises(TypeError):
            percentile(bad, reference(TIED))  # type: ignore[arg-type]


def test_an_empty_reference_has_no_percentile() -> None:
    empty = build_reference("panel", GLOBAL_KEY, [], [], "empty-v")
    assert (empty.n_candidates, empty.n_pairs, empty.samples) == (0, 0, ())
    with pytest.raises(ValueError):
        percentile(0.5, empty)


@given(
    samples=st.lists(st.floats(0.0, 1.0), min_size=1, max_size=60),
    a=st.floats(-0.5, 1.5),
    b=st.floats(-0.5, 1.5),
)
def test_percentile_is_monotone_bounded_and_matches_the_formula(samples, a, b) -> None:
    ref = reference(samples)
    low, high = sorted((a, b))
    p_low, p_high = percentile(low, ref), percentile(high, ref)
    assert 0 <= p_low <= p_high <= 100
    assert p_low == percentile_by_formula(low, samples)
    assert p_high == percentile_by_formula(high, samples)


@given(samples=st.lists(st.floats(0.0, 1.0), min_size=1, max_size=60), data=st.data())
def test_percentile_agrees_with_exact_arithmetic_at_the_scores_themselves(samples, data) -> None:
    ref = reference(samples)
    raw = data.draw(st.sampled_from(samples))
    below, equal, n = sum(s < raw for s in samples), sum(s == raw for s in samples), len(samples)
    assert percentile(raw, ref) == round(Fraction(100 * (2 * below + equal), 2 * n))


# ─── ReferenceDistribution and build_reference ─────────────────────────────────────────────


def test_build_reference_counts_distinct_candidates_not_pairs() -> None:
    scores = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    ids = ["ann", "ann", "bob", "bob", "bob", "cy"]
    ref = build_reference("family", "data", scores, ids, "v1")
    assert (ref.n_pairs, ref.n_candidates) == (6, 3)
    assert (ref.scope, ref.key, ref.version) == ("family", "data", "v1")
    assert ref.samples == tuple(scores)


def test_build_reference_sorts_the_scores() -> None:
    ref = build_reference("global", GLOBAL_KEY, [0.9, 0.1, 0.5, 0.1], list("abcd"), "v")
    assert ref.samples == (0.1, 0.1, 0.5, 0.9)


def test_build_reference_needs_one_candidate_id_per_score() -> None:
    with pytest.raises(ValueError):
        build_reference("global", GLOBAL_KEY, [0.1, 0.2, 0.3], ["a", "b"], "v")
    with pytest.raises(ValueError):
        build_reference("global", GLOBAL_KEY, [0.1], ["a", "b"], "v")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -0.01, 1.01])
def test_build_reference_refuses_scores_outside_the_unit_interval(bad: float) -> None:
    with pytest.raises(ValueError):
        build_reference("global", GLOBAL_KEY, [0.5, bad], ["a", "b"], "v")


def test_negative_zero_is_stored_as_zero() -> None:
    # Otherwise the same scores in another order would serialise to different bytes.
    ref = build_reference("global", GLOBAL_KEY, [0.5, -0.0, 0.0], list("abc"), "v")
    assert ref.samples == (0.0, 0.0, 0.5)
    assert "-0.0" not in ref.model_dump_json()
    flipped = build_reference("global", GLOBAL_KEY, [0.0, 0.5, -0.0], list("abc"), "v")
    assert flipped.model_dump_json() == ref.model_dump_json()


def test_a_reference_of_exactly_the_maximum_size_is_kept_whole() -> None:
    scores = [i / MAX_SAMPLES for i in range(MAX_SAMPLES)]
    ref = build_reference("global", GLOBAL_KEY, scores, people(MAX_SAMPLES), "v")
    assert MAX_SAMPLES == 5_000
    assert ref.samples == tuple(scores) and ref.n_pairs == MAX_SAMPLES


def test_downsampling_takes_the_middle_of_equal_mass_blocks() -> None:
    # 10,000 scores into 5,000 blocks of two: block j is {2j, 2j + 1}, and its representative is
    # index floor((j + 0.5) x 10,000 / 5,000) = 2j + 1.
    scores = [i / 10_000 for i in range(10_000)]
    ref = build_reference("global", GLOBAL_KEY, scores, people(10_000), "v")
    assert ref.n_pairs == 10_000 and ref.n_candidates == 10_000
    assert ref.samples == tuple(scores[1::2])

    # 7,500 scores: blocks of 1.5, representative index floor((2j + 1) x 3 / 4) = 0, 2, 3, 5, 6, 8, ...
    scores = [i / 7_500 for i in range(7_500)]
    ref = build_reference("global", GLOBAL_KEY, scores, people(7_500), "v")
    assert len(ref.samples) == MAX_SAMPLES
    assert ref.samples[:6] == tuple(scores[i] for i in (0, 2, 3, 5, 6, 8))
    assert ref.samples[-1] == scores[-1]


def test_downsampling_is_deterministic_and_ignores_input_order() -> None:
    rng = random.Random(2026_10_09)
    scores = [round(rng.random(), 4) for _ in range(12_345)]
    ids = people(len(scores))
    first = build_reference("global", GLOBAL_KEY, scores, ids, "v")
    shuffled = list(zip(scores, ids))
    rng.shuffle(shuffled)
    second = build_reference("global", GLOBAL_KEY, [s for s, _ in shuffled], [i for _, i in shuffled], "v")
    assert first.model_dump_json() == second.model_dump_json()
    assert len(first.samples) == MAX_SAMPLES and first.n_pairs == 12_345
    assert list(first.samples) == sorted(first.samples)


def test_downsampled_percentiles_stay_within_a_point_of_the_full_data() -> None:
    rng = random.Random(20261009)
    scores = [round(rng.random() ** 2, 3) for _ in range(23_456)]  # skewed, with many exact ties
    ref = build_reference("global", GLOBAL_KEY, scores, people(len(scores)), "v")
    assert len(ref.samples) == MAX_SAMPLES
    full = sorted(scores)
    for step in range(201):
        raw = step / 200
        below = bisect_left(full, raw)
        equal = bisect_right(full, raw) - below
        exact = 100 * (below + 0.5 * equal) / len(full)
        # 0.5 for rounding to an integer, at most 0.01 for sampling 5,000 of the 23,456 (D-50 asks
        # for an integer, so this is far below what a person can see).
        assert abs(percentile(raw, ref) - exact) <= 0.51 + 1e-9, raw


@given(
    pairs=st.lists(st.tuples(st.floats(0.0, 1.0), st.sampled_from("abcdef")), max_size=80),
    data=st.data(),
)
def test_build_reference_invariants(pairs, data) -> None:
    scores = [score for score, _ in pairs]
    ids = [who for _, who in pairs]
    ref = build_reference("family", "f", scores, ids, "v")
    assert ref.n_pairs == len(pairs)
    assert ref.n_candidates == len(set(ids))
    assert list(ref.samples) == sorted(scores)
    order = data.draw(st.permutations(range(len(pairs))))
    again = build_reference("family", "f", [scores[i] for i in order], [ids[i] for i in order], "v")
    assert again == ref


def test_reference_round_trips_through_json() -> None:
    ref = reference(TIED, scope="family", key="data", version="ref-9")
    assert ReferenceDistribution.model_validate_json(ref.model_dump_json()) == ref
    assert ref.model_dump(mode="json")["samples"] == list(TIED)


GOOD = {
    "scope": "global",
    "key": GLOBAL_KEY,
    "n_candidates": 3,
    "n_pairs": 3,
    "samples": (0.1, 0.5, 0.9),
    "version": "v",
}


def test_the_good_reference_used_by_the_validation_tests_is_good() -> None:
    assert ReferenceDistribution(**GOOD).samples == (0.1, 0.5, 0.9)


@pytest.mark.parametrize(
    "change",
    [
        {"samples": (0.5, 0.1, 0.9)},  # not sorted
        {"samples": (0.1, 0.5, 1.5)},  # above 1
        {"samples": (-0.1, 0.5, 0.9)},  # below 0
        {"samples": (0.1, float("nan"), 0.9)},  # NaN
        {"samples": (0.1, 0.5, float("inf"))},  # infinity
        {"n_candidates": 4},  # more people than (person, posting) pairs
        {"n_candidates": 1, "n_pairs": 2},  # more samples than pairs
        {"n_candidates": 0, "n_pairs": 0},  # samples without any pair
        {"n_candidates": 1, "n_pairs": 3, "samples": ()},  # pairs without any sample
        {"n_candidates": -1},
        {"scope": "test"},  # a reference is never the test calibration
        {"scope": "nonsense"},
        {"version": ""},
        {"key": "k" * 201},
        {"unexpected": 1},
    ],
)
def test_reference_distribution_rejects_inconsistent_data(change) -> None:
    with pytest.raises(ValidationError):
        ReferenceDistribution(**{**GOOD, **change})


def test_reference_distribution_allows_at_most_5000_samples() -> None:
    ok = {**GOOD, "n_candidates": 1, "n_pairs": 5_000, "samples": (0.5,) * 5_000}
    assert len(ReferenceDistribution(**ok).samples) == 5_000
    with pytest.raises(ValidationError):
        ReferenceDistribution(**{**ok, "n_pairs": 5_001, "samples": (0.5,) * 5_001})


def test_reference_distribution_is_immutable() -> None:
    ref = ReferenceDistribution(**GOOD)
    with pytest.raises(ValidationError):
        ref.n_pairs = 99  # type: ignore[misc]


# ─── PercentileCalibrator: the chain posting -> family -> global -> panel ──────────────────

# With 40 people per tier, each tier is told apart by what the probe raw (0.5) is worth in it:
#   posting: everyone scored 0.1   -> percentile 100      family: everyone scored 0.5 -> 50
#   global:  everyone scored 0.9   -> percentile 0        panel:  0.1/0.2/0.3/0.7     -> 75
PROBE_SCORES = {"posting": 100, "family": 50, "global": 0, "panel": 75}
SCOPES = ("posting", "family", "global", "panel")


def chain_references(n_posting: int, n_family: int, n_global: int, n_panel: int):
    return [
        tier("posting", "job-1", n_posting, 0.1, "posting-v"),
        tier("family", "data", n_family, 0.5, "family-v"),
        tier("global", GLOBAL_KEY, n_global, 0.9, "global-v"),
        mixed("panel", GLOBAL_KEY, n_panel, "panel-v"),
    ]


def info_of(scope: str, n: int) -> CalibrationInfo:
    return CalibrationInfo(method="percentile", scope=scope, n=n, version=f"{scope}-v")


def test_each_tier_reads_its_own_samples() -> None:
    for ref in chain_references(40, 40, 40, 40):
        bound = PercentileCalibrator([ref], min_reference_n=MIN_N).bind(posting_key="job-1", family="data")
        assert bound.calibrate(PROBE) == (PROBE_SCORES[ref.scope], info_of(ref.scope, 40))


@pytest.mark.parametrize(
    "counts, scope",
    [
        ((40, 40, 40, 40), "posting"),
        ((30, 29, 29, 29), "posting"),  # n_candidates >= 30: the threshold is inclusive
        ((29, 40, 40, 40), "family"),
        ((29, 30, 29, 29), "family"),
        ((1, 29, 40, 40), "global"),
        ((29, 29, 30, 29), "global"),
        ((29, 29, 29, 40), "panel"),
        ((1, 1, 1, 30), "panel"),
    ],
)
def test_chain_takes_the_first_reference_with_enough_distinct_candidates(counts, scope) -> None:
    refs = chain_references(*counts)
    calibrator = PercentileCalibrator(refs, min_reference_n=MIN_N)
    used = refs[SCOPES.index(scope)]
    score, info = calibrator.bind(posting_key="job-1", family="data").calibrate(PROBE)
    assert info == info_of(scope, counts[SCOPES.index(scope)])
    assert score == percentile_by_formula(PROBE, used.samples)


def test_the_chain_is_exhausted_loudly_not_with_a_made_up_score() -> None:
    calibrator = PercentileCalibrator(chain_references(29, 29, 29, 29), min_reference_n=MIN_N)
    with pytest.raises(LookupError):
        calibrator.bind(posting_key="job-1", family="data")
    with pytest.raises(LookupError):
        PercentileCalibrator([], min_reference_n=MIN_N).bind(posting_key=None, family=None)


def bound_for(references, posting_key: str | None = "job-1", family: str | None = "data"):
    calibrator = PercentileCalibrator(references, min_reference_n=MIN_N)
    return calibrator.bind(posting_key=posting_key, family=family)


def test_the_threshold_counts_people_not_pairs() -> None:
    # D-50: five people each scored against 2,000 postings are 10,000 pairs but still only five
    # people, so their posting distribution compares each of them mostly with themselves.
    five = people(5)
    ids = [five[i % 5] for i in range(10_000)]
    crowded = build_reference("posting", "job-1", [0.1] * 10_000, ids, "posting-v")
    assert (crowded.n_pairs, crowded.n_candidates) == (10_000, 5)
    rest = chain_references(40, 40, 40, 40)[1:]
    assert bound_for([crowded, *rest]).calibrate(PROBE) == (PROBE_SCORES["family"], info_of("family", 40))

    # 29 people with 100 pairs each (2,900 pairs) still fall short ...
    p29 = people(29)
    ids = [p29[i % 29] for i in range(2_900)]
    short = build_reference("posting", "job-1", [0.1] * 2_900, ids, "posting-v")
    assert (short.n_pairs, short.n_candidates) == (2_900, 29)
    assert bound_for([short, *rest]).calibrate(PROBE)[1].scope == "family"

    # ... and 30 people with two pairs each (60 pairs) are enough.
    p30 = people(30)
    ids = [p30[i % 30] for i in range(60)]
    enough = build_reference("posting", "job-1", [0.1] * 60, ids, "posting-v")
    assert bound_for([enough, *rest]).calibrate(PROBE) == (PROBE_SCORES["posting"], info_of("posting", 30))


def test_the_threshold_is_the_one_the_caller_passes(config) -> None:
    assert config.scoring.calibration.min_reference_n == MIN_N
    refs = chain_references(40, 40, 40, 40)
    strict = PercentileCalibrator(refs, min_reference_n=41)  # every reference has 40 candidates
    with pytest.raises(LookupError):
        strict.bind(posting_key="job-1", family="data")
    loose = PercentileCalibrator(chain_references(1, 1, 1, 1), min_reference_n=1)
    assert loose.bind(posting_key="job-1", family="data").calibrate(PROBE)[1] == info_of("posting", 1)
    real = PercentileCalibrator(refs, min_reference_n=config.scoring.calibration.min_reference_n)
    assert real.bind(posting_key="job-1", family="data").calibrate(PROBE)[1].scope == "posting"


def test_a_missing_key_or_an_unknown_one_skips_that_tier() -> None:
    calibrator = PercentileCalibrator(chain_references(40, 40, 40, 40), min_reference_n=MIN_N)

    def scope(**keys) -> str:
        return calibrator.bind(**keys).calibrate(PROBE)[1].scope

    assert scope(posting_key="job-1", family="data") == "posting"
    assert scope(posting_key=None, family="data") == "family"
    assert scope(posting_key="job-2", family="data") == "family"  # nothing scored for job-2
    assert scope(posting_key="job-1", family=None) == "posting"
    assert scope(posting_key="job-2", family="design") == "global"  # no 'design' reference
    assert scope(posting_key=None, family=None) == "global"


def test_the_panel_prefers_the_family_group_then_the_pooled_panel() -> None:
    by_family = tier("panel", "data", 40, 0.3, "panel-v")  # probe 0.5 -> 100
    pooled = tier("panel", GLOBAL_KEY, 40, 0.7, "panel-v")  # probe 0.5 -> 0
    short = tier("panel", "ops", 29, 0.3, "panel-v")
    calibrator = PercentileCalibrator([by_family, pooled, short], min_reference_n=MIN_N)

    def probe(family: str | None) -> tuple[int, CalibrationInfo]:
        return calibrator.bind(posting_key=None, family=family).calibrate(PROBE)

    assert probe("data") == (100, info_of("panel", 40))
    assert probe("design")[0] == 0  # no group for it: the pooled panel
    assert probe("ops")[0] == 0  # a group, but under 30 candidates: the pooled panel
    assert probe(None)[0] == 0
    only_groups = PercentileCalibrator([by_family], min_reference_n=MIN_N)
    with pytest.raises(LookupError):
        only_groups.bind(posting_key=None, family="design")


def test_the_same_key_in_two_scopes_is_two_different_references() -> None:
    # A 'panel' group keyed like a family is not a family reference, and the other way round.
    panel_data = tier("panel", "data", 40, 0.3, "panel-v")
    family_data = tier("family", "data", 40, 0.5, "family-v")
    only_panel = PercentileCalibrator([panel_data], min_reference_n=MIN_N)
    assert only_panel.bind(posting_key=None, family="data").calibrate(PROBE)[1].scope == "panel"
    both = PercentileCalibrator([panel_data, family_data], min_reference_n=MIN_N)
    assert both.bind(posting_key=None, family="data").calibrate(PROBE)[1].scope == "family"


def test_the_scope_names_the_reference_so_the_basis_sentence_tells_the_truth(config) -> None:
    expected = {
        "posting": "this posting",
        "family": "similar roles",
        "global": "all roles",
        "panel": "reference group of candidate profiles",
    }
    cases = [
        ((40, 40, 40, 40), "posting"),
        ((1, 40, 40, 40), "family"),
        ((1, 1, 40, 40), "global"),
        ((1, 1, 1, 40), "panel"),
    ]
    for counts, scope in cases:
        calibrator = PercentileCalibrator(chain_references(*counts), min_reference_n=MIN_N)
        score, info = calibrator.bind(posting_key="job-1", family="data").calibrate(PROBE)
        assert info.scope == scope
        sentence = config.bands.basis[info.scope].format(score=score)
        assert expected[scope] in sentence
        assert "applicants" not in sentence  # D-50: it never claims applicants


def test_with_only_a_handful_of_resumes_every_score_uses_the_panel() -> None:
    # D-50: "With Tailrd's current handful of résumés, every score therefore uses the panel until
    # 30 people have scored résumés." Five résumés scored against 2,000 postings in one family.
    five = people(5)
    ids = [five[i % 5] for i in range(10_000)]
    scores = [0.1 + 0.8 * (i % 100) / 100 for i in range(10_000)]
    live = [
        build_reference("posting", "job-1", scores, ids, "live-v"),
        build_reference("family", "data", scores, ids, "live-v"),
        build_reference("global", GLOBAL_KEY, scores, ids, "live-v"),
    ]
    panel = [mixed("panel", GLOBAL_KEY, 60, "panel-v")]
    calibrator = PercentileCalibrator([*live, *panel], min_reference_n=MIN_N)
    for posting_key in ("job-1", "job-2", None):
        info = calibrator.bind(posting_key=posting_key, family="data").calibrate(PROBE)[1]
        assert (info.scope, info.n, info.version) == ("panel", 60, "panel-v")


def test_bind_returns_something_the_engine_can_call() -> None:
    calibrator = PercentileCalibrator(chain_references(40, 40, 40, 40), min_reference_n=MIN_N)
    bound = calibrator.bind(posting_key="job-1", family="data")
    assert isinstance(bound, Calibrator)
    first = bound.calibrate(0.37)
    assert bound.calibrate(0.37) == first  # a pure function of the raw score
    assert isinstance(first[0], int) and not isinstance(first[0], bool)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_bound_calibrator_refuses_a_non_finite_raw_score(bad: float) -> None:
    bound = PercentileCalibrator(chain_references(40, 40, 40, 40), min_reference_n=MIN_N)
    with pytest.raises(ValueError):
        bound.bind(posting_key="job-1", family="data").calibrate(bad)


@pytest.mark.parametrize("bad", [0, -1, 1.5, "30", None, True])
def test_the_threshold_must_be_a_positive_integer(bad) -> None:
    with pytest.raises(ValueError):
        PercentileCalibrator(chain_references(40, 40, 40, 40), min_reference_n=bad)


def test_ambiguous_references_are_refused() -> None:
    first = tier("family", "data", 40, 0.5, "family-v")
    other_version = tier("family", "data", 50, 0.6, "family-w")
    with pytest.raises(ValueError):
        PercentileCalibrator([first, other_version], min_reference_n=MIN_N)
    two_globals = [tier("global", GLOBAL_KEY, 40, 0.5, "g-1"), tier("global", "everything", 40, 0.5, "g-2")]
    with pytest.raises(ValueError):
        PercentileCalibrator(two_globals, min_reference_n=MIN_N)


def test_any_key_is_fine_for_the_one_global_reference() -> None:
    odd = tier("global", "everything", 40, 0.9, "global-v")
    bound = PercentileCalibrator([odd], min_reference_n=MIN_N).bind(posting_key=None, family=None)
    assert bound.calibrate(PROBE) == (0, info_of("global", 40))


def test_references_may_come_from_a_generator() -> None:
    refs = (ref for ref in chain_references(40, 40, 40, 40))
    bound = PercentileCalibrator(refs, min_reference_n=MIN_N).bind(posting_key="job-1", family="data")
    assert bound.calibrate(PROBE)[1].scope == "posting"


@st.composite
def calibrators(draw):
    refs = []
    tiers = (("posting", "job-1"), ("family", "data"), ("global", GLOBAL_KEY), ("panel", GLOBAL_KEY))
    for scope, key in tiers:
        scores = draw(st.lists(st.floats(0.0, 1.0), min_size=1, max_size=40))
        distinct = draw(st.integers(min_value=1, max_value=len(scores)))
        ids = [f"p{i % distinct}" for i in range(len(scores))]
        refs.append(build_reference(scope, key, scores, ids, f"{scope}-v"))
    return PercentileCalibrator(refs, min_reference_n=draw(st.integers(min_value=1, max_value=40)))


@given(calibrator=calibrators(), a=st.floats(0.0, 1.0), b=st.floats(0.0, 1.0))
def test_a_bound_calibrator_is_monotone_whichever_tier_it_lands_on(calibrator, a, b) -> None:
    try:
        bound = calibrator.bind(posting_key="job-1", family="data")
    except LookupError:
        assume(False)
    low, high = sorted((a, b))
    (low_score, low_info), (high_score, high_info) = bound.calibrate(low), bound.calibrate(high)
    assert low_info == high_info
    assert 0 <= low_score <= high_score <= 100


def test_the_reference_models_serialise_without_losing_the_samples() -> None:
    refs = chain_references(40, 40, 40, 40)
    dumped = json.dumps([r.model_dump(mode="json") for r in refs])
    again = [ReferenceDistribution.model_validate(d) for d in json.loads(dumped)]
    assert again == refs
