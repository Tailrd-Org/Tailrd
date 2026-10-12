"""The totality contract (SPEC §13; DECISIONS D-46): the scorer is a total function.

Arbitrary bytes, arbitrary unicode, arbitrary JSON-ish structures, models built without validation
and valid-but-random models all map to exactly one of the four outcomes, never an exception, a None
or a NaN. For VALID random models the outcome is never an engine fault (INTERNAL_ERROR or
NUMERIC_FAULT) and the raw score lies in [0, 1]; strict mode (tests) would raise on any fault, and
the valid-model test runs in both modes.

`max_examples` comes from the environment variable TOTALITY_EXAMPLES (default 300, D-48). These tests
run on every commit and are never skipped (§13); `make gate` (the Milestone 3 gate) runs
them with TOTALITY_EXAMPLES=50000.
"""

from __future__ import annotations

import math
import os
from datetime import date

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from matcher.calibrate import FixedCalibrator
from matcher.config import load_config
from matcher.errors import Flag
from matcher.graph.loader import load_graph
from matcher.models import GateStatus, JobPosting, MatchResult, Outcome, Resume, ScoreOptions
from matcher.score.engine import score
from tests.strategies import (
    AS_OF,
    POSTING_FIELDS,
    RESUME_FIELDS,
    as_of_values,
    garbage,
    postings,
    resumes,
)
from tests.fixtures.build_graph_small import FIXTURE_DIR

EXAMPLES = int(os.environ.get("TOTALITY_EXAMPLES", "300"))
FAULTS = {Flag.INTERNAL_ERROR, Flag.NUMERIC_FAULT}
TOTAL = settings(
    max_examples=EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large, HealthCheck.filter_too_much],
)
FIXED = FixedCalibrator()
# Loaded once at import, not as pytest fixtures: Hypothesis prints every argument of a failing
# example, and the config's repr would bury the input that failed.
CONFIG = load_config()
GRAPH = load_graph(FIXTURE_DIR)


def run(resume, posting, as_of=AS_OF, **options) -> MatchResult:
    return _check(score(resume, posting, as_of=as_of, config=CONFIG, graph=GRAPH, calibration=FIXED,
                        options=ScoreOptions(**options)))


def _check(result: object) -> MatchResult:
    """One of the four outcomes, with a well-formed score whenever one is carried."""
    assert isinstance(result, MatchResult)
    assert result.outcome in set(Outcome)
    if result.raw_score is not None:
        assert math.isfinite(result.raw_score) and 0.0 <= result.raw_score <= 1.0
    for value in (result.calibrated_score, result.display_score):
        assert value is None or 0 <= value <= 100
    if result.outcome is Outcome.GATE_FAIL:
        assert result.raw_score is None and result.display_score is None
    if result.outcome is Outcome.NEEDS_REVIEW:
        assert result.reasons and result.display_score is None
    return result


@TOTAL
@given(resume=garbage(RESUME_FIELDS, Resume), posting=garbage(POSTING_FIELDS, JobPosting), as_of=as_of_values)
def test_garbage_in_one_outcome_out(resume, posting, as_of) -> None:
    # Production mode: whatever arrives, one of the four outcomes and never an engine fault. Most of
    # it is INVALID_INPUT; the odd dict that happens to validate ({} is an empty résumé) is scored.
    result = run(resume, posting, as_of)
    assert not FAULTS & set(result.reasons), result.reasons


@TOTAL
@given(data=st.binary(max_size=400) | st.text(max_size=400))
def test_bytes_and_unicode_on_either_side(data) -> None:
    for args in ((data, JobPosting()), (Resume(), data), (data, data)):
        for strict in (False, True):  # invalid input is an outcome in both modes (D-46)
            result = run(*args, strict=strict)
            assert result.outcome is Outcome.NEEDS_REVIEW and result.reasons == (Flag.INVALID_INPUT,)
            assert result.input_digest == "" and result.gate_status is GateStatus.PASS


@TOTAL
@given(resume=resumes(), posting=postings(), with_interval=st.booleans(),
       as_of=st.dates(min_value=date(1960, 1, 1), max_value=date(2040, 12, 31)) | st.just(AS_OF))
def test_valid_models_never_fault(resume, posting, with_interval, as_of) -> None:
    result = run(resume, posting, as_of, with_interval=with_interval)
    assert not FAULTS & set(result.reasons), result.reasons
    assert Flag.INVALID_INPUT not in result.reasons
    if result.outcome in (Outcome.SCORED, Outcome.SCORED_LOW_CONFIDENCE):
        assert result.raw_score is not None and result.band is not None
        assert result.gate_status is not GateStatus.FAIL
    # The same pair in strict mode raises on any engine fault (D-46) and agrees with production.
    assert run(resume, posting, as_of, strict=True, with_interval=with_interval) == result


@TOTAL
@given(resume=resumes(review_flags=False), posting=postings(review_flags=False, min_requirements=1))
def test_valid_models_with_requirements_reach_the_scorer(resume, posting) -> None:
    # No review flags and at least one requirement: the pair gets past the early exits as often as
    # the dates allow, so the dimension scorers, the aggregator and confidence see random models.
    result = run(resume, posting, strict=True)
    assert not FAULTS & set(result.reasons)
    if result.raw_score is not None:
        assert result.outcome is not Outcome.GATE_FAIL and len(result.dimensions) == 7


def test_most_valid_pairs_with_requirements_reach_the_scorer() -> None:
    # The test above is only worth its examples if they get past the early exits: on a fixed-seed
    # sample, at least half of the pairs carry a raw score (the scorers, the aggregator and
    # confidence ran). Measured share: about 0.8.
    reached = []

    @settings(max_examples=200, deadline=None, derandomize=True, database=None,
              suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
    @given(resume=resumes(review_flags=False), posting=postings(review_flags=False, min_requirements=1))
    def sample(resume, posting) -> None:
        reached.append(run(resume, posting, strict=True).raw_score is not None)

    sample()
    assert len(reached) >= 150 and sum(reached) / len(reached) >= 0.5, (sum(reached), len(reached))
