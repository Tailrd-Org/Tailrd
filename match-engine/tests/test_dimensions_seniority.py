"""§5.4 Seniority alignment (Task 3.3).

SPEC §5.3 (over-qualification is never deducted), §5.4; DECISIONS D-38, D-39. Closed forms with
the spec's step 0.28 and track-switch penalty 0.20, and D-38's offset 2 (M n sits level with
IC n+2): `max(0, 1 − 0.28 × gap − penalty)`, a gap below 0 (above the posting's level) counting
as 0 and raising OVERQUALIFIED.
"""

from __future__ import annotations

import math
import random
from datetime import date
from typing import Optional

import pytest

from matcher.config import load_config
from matcher.errors import EngineFault, Flag
from matcher.models import DimensionName, JobPosting, Resume, Role, SeniorityLevel, Track
from matcher.score.dimensions import score_seniority
from matcher.score.dimensions.seniority import candidate_level
from matcher.score.prepare import prepare_resume
from tests.builders import AS_OF, on, tiny_graph, with_scoring

STEP, PENALTY, OFFSET = 0.28, 0.20, 2  # SPEC §5.4; DECISIONS D-38
W_SENIORITY = 0.10  # SPEC §5.4


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12)


def level(code: str) -> SeniorityLevel:
    return SeniorityLevel(track=Track.MGMT if code.startswith("M") else Track.IC, level=int(code.lstrip("ICM")))


def current(code: Optional[str]) -> Role:
    return Role(start=on(date(2022, 1, 1)), is_current=True, seniority=level(code) if code else None)


def ended(code: Optional[str], end: date) -> Role:
    return Role(start=on(date(2010, 1, 1)), end=on(end), seniority=level(code) if code else None)


def _score(roles, job: Optional[str], graph, config):
    prepared = prepare_resume(Resume(roles=tuple(roles)), graph, config, AS_OF)
    return score_seniority(prepared, JobPosting(seniority=level(job) if job else None), graph, config)


def _codes(dim) -> list[str]:
    return [f.code for f in dim.facts]


# ─── The closed forms (plan Task 3.3) ────────────────────────────────────────────────────


@pytest.mark.parametrize("candidate,job,expected,above", [
    ("IC2", "IC3", 1 - STEP * 1, False),  # 0.72
    ("IC1", "IC3", 1 - STEP * 2, False),  # 0.44
    ("IC0", "IC4", 0.0, False),  # max(0, 1 − 0.28 × 4) = max(0, −0.12)
    ("IC3", "IC3", 1.0, False),
    ("IC4", "IC2", 1.0, True),  # two above: counted as 0 (D-39)
    ("M2", "IC3", 1 - PENALTY, True),  # M2 ≈ IC4, one above: gap 0, track switch → 0.80
    ("M1", "IC3", 1 - PENALTY, False),  # M1 ≈ IC3: gap 0 → 0.80
    ("IC3", "M2", 1 - STEP * 1 - PENALTY, False),  # IC3 ≈ M1, one below → 0.52
    ("IC1", "M1", 1 - STEP * 2 - PENALTY, False),  # IC1 ≈ M(−1), two below → 0.24
    ("M3", "M4", 1 - STEP * 1, False),  # 0.72
    ("IC0", "M6", 0.0, False),  # IC0 ≈ M(−2): eight below
])
def test_closed_forms(small_graph, config, candidate, job, expected, above) -> None:
    dim = _score([current(candidate)], job, small_graph, config)
    assert dim.name is DimensionName.SENIORITY and dim.abstained is False
    assert _close(dim.value, expected)
    assert _close(dim.weight, W_SENIORITY) and _close(dim.effective_weight, W_SENIORITY)
    assert _close(dim.contribution, W_SENIORITY * expected)
    assert ("OVERQUALIFIED" in _codes(dim)) is above


def test_the_named_closed_forms_round_as_the_brief_states(small_graph, config) -> None:
    for candidate, job, value in (("IC2", "IC3", 0.72), ("IC1", "IC3", 0.44), ("IC0", "IC4", 0.0),
                                  ("IC4", "IC2", 1.0), ("M2", "IC3", 0.80), ("IC3", "M2", 0.52),
                                  ("IC1", "M1", 0.24)):
        assert _close(_score([current(candidate)], job, small_graph, config).value, value)


def test_facts_name_the_gap_and_the_overqualification(small_graph, config) -> None:
    dim = _score([current("M2")], "IC3", small_graph, config)
    gap, over = dim.facts
    assert (gap.code, gap.subject) == ("SENIORITY_GAP", "IC3") and _close(gap.value, -1.0)  # IC3 − (2 + 2)
    assert gap.detail == {"candidate": "M2", "cross_track": True}
    assert (over.code, over.subject) == ("OVERQUALIFIED", "M2") and _close(over.value, 1.0)
    assert over.detail == {"job": "IC3"}
    dim = _score([current("IC1")], "IC3", small_graph, config)
    assert [(f.code, f.subject, f.value) for f in dim.facts] == [("SENIORITY_GAP", "IC3", 2.0)]
    assert dim.facts[0].detail == {"candidate": "IC1", "cross_track": False}


def test_overqualification_is_deducted_only_when_configured(small_graph, config) -> None:
    penalize = with_scoring(config, "seniority", penalize_overqualification=True)
    dim = _score([current("IC4")], "IC2", small_graph, penalize)
    assert _close(dim.value, 1 - STEP * 2) and "OVERQUALIFIED" in _codes(dim)  # 0.44
    dim = _score([current("M2")], "IC3", small_graph, penalize)
    assert _close(dim.value, 1 - STEP * 1 - PENALTY)  # 0.52: one above, and the track switch


def test_values_follow_the_config(small_graph, config) -> None:
    tuned = with_scoring(config, "seniority", step=0.25, cross_track_penalty=0.10, cross_track_offset=1)
    assert _close(_score([current("IC1")], "IC3", small_graph, tuned).value, 1 - 0.25 * 2)
    assert _close(_score([current("IC3")], "M2", small_graph, tuned).value, 1 - 0.10)  # IC3 ≈ M2 at offset 1


# ─── The candidate's level (DECISIONS D-38) ──────────────────────────────────────────────


def test_candidate_level_is_the_highest_current_level() -> None:
    roles = [current("IC2"), current("IC3"), ended("IC5", date(2021, 6, 30))]
    rng = random.Random(1)
    for _ in range(6):
        rng.shuffle(roles)
        assert candidate_level(Resume(roles=tuple(roles))) == level("IC3")


def test_without_a_current_role_the_most_recent_role_counts() -> None:
    roles = (ended("IC4", date(2019, 6, 30)), ended("IC2", date(2023, 6, 30)))
    assert candidate_level(Resume(roles=roles)) == level("IC2")  # the latest end, not the highest level
    tie = (ended("IC2", date(2023, 6, 30)), ended("IC3", date(2023, 6, 30)), ended("IC5", date(2020, 1, 31)))
    assert candidate_level(Resume(roles=tie)) == level("IC3")  # same end: the higher level


def test_only_roles_that_carry_a_seniority_count() -> None:
    roles = (ended(None, date(2025, 6, 30)), ended("IC3", date(2022, 6, 30)))
    assert candidate_level(Resume(roles=roles)) == level("IC3")
    roles = (current(None), ended("IC2", date(2022, 6, 30)))
    assert candidate_level(Resume(roles=roles)) == level("IC2")
    assert candidate_level(Resume(roles=(current(None), ended(None, date(2020, 1, 31))))) is None
    assert candidate_level(Resume()) is None
    # A level with no end and no current flag cannot be placed in time.
    assert candidate_level(Resume(roles=(Role(seniority=level("IC3")),))) is None


def test_concurrent_roles_on_two_tracks_compare_after_alignment(small_graph, config) -> None:
    roles = Resume(roles=(current("M2"), current("IC3")))
    assert candidate_level(roles) == level("IC3")  # ladder index: 3 > 2
    assert candidate_level(roles, cross_track_offset=OFFSET) == level("M2")  # M2 ≈ IC4 > IC3
    # A tie after alignment goes to the management level (M n ≥ IC n under any offset).
    assert candidate_level(Resume(roles=(current("IC3"), current("M1"))), cross_track_offset=OFFSET) == level("M1")
    assert candidate_level(Resume(roles=(current("IC2"), current("M2")))) == level("M2")
    # score_seniority aligns with the configured offset: a manager who also holds a senior IC role
    # is a manager for a manager posting (raw indices would place them at IC3 ≈ M1: 0.52).
    dim = _score([current("M2"), current("IC3")], "M2", small_graph, config)
    assert dim.value == 1.0 and dim.facts[0].detail == {"candidate": "M2", "cross_track": False}


# ─── Abstention, binding ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("roles,job", [
    ([current("IC3")], None),
    ([current(None)], "IC3"),
    ([], "IC3"),
    ([], None),
])
def test_abstains_when_either_level_is_unknown(small_graph, config, roles, job) -> None:
    dim = _score(roles, job, small_graph, config)
    assert dim.abstained and dim.value is None and dim.abstain_reason is Flag.SENIORITY_UNKNOWN
    assert _close(dim.weight, W_SENIORITY) and dim.effective_weight == 0.0 and dim.contribution == 0.0
    assert dim.facts == ()


def test_a_prepared_resume_is_bound_to_its_graph_and_config(small_graph, config) -> None:
    prepared = prepare_resume(Resume(roles=(current("IC2"),)), small_graph, config, AS_OF)
    other_graph = tiny_graph({"skill.python": 0.5})
    other_config = with_scoring(config, "seniority", step=0.30)
    for post in (JobPosting(seniority=level("IC3")), JobPosting()):  # even where it would abstain
        score_seniority(prepared, post, small_graph, config)
        score_seniority(prepared, post, small_graph, load_config())
        for graph, cfg in ((other_graph, config), (small_graph, other_config)):
            with pytest.raises(EngineFault) as err:
                score_seniority(prepared, post, graph, cfg)
            assert err.value.flag is Flag.INTERNAL_ERROR
