"""Thirty hand-built résumé / posting pairs scored end to end (SPEC §11 milestone 2, §10, §14.5).

Each `tests/fixtures/profiles/*.json` holds a pair, its `as_of` and what the engine must return:
the outcome, the raw score (null when no score is computed), every dimension's value (null =
abstained) and, where the pair is about them, the confidence, caps, reasons, flags, gates and
effective weights. Every expected number was derived by hand from SPEC §5, §6, §14.5 and §17.1
and the fixture graph's IDF data, never copied from the engine (EVAL §2.2); the arithmetic is in
each fixture's `derivation`. Scores are compared at abs_tol 0.001.
"""

from __future__ import annotations

import json
import math
from datetime import date
from functools import lru_cache
from pathlib import Path

import pytest

from matcher.calibrate import FixedCalibrator
from matcher.config import EngineConfig, load_config
from matcher.graph.loader import load_graph
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, JobPosting, MatchResult, Outcome, Resume, ScoreOptions
from matcher.score.engine import score
from tests.fixtures.build_graph_small import FIXTURE_DIR

PROFILES = Path(__file__).resolve().parent / "fixtures" / "profiles"
TOL = 0.001
FIXTURES = sorted(PROFILES.glob("*.json"))
EXPECTED_KEYS = {"outcome", "raw_score", "derivation", "dimensions"}
OPTIONAL_KEYS = {"band", "confidence", "caps", "reasons", "flags", "gate_status", "gates", "effective_weights"}


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def engine_inputs() -> tuple[EngineConfig, SkillGraph]:
    """The shipped config and the graph_small fixture, loaded once (other test modules reuse them)."""
    return load_config(), load_graph(FIXTURE_DIR)


def run_fixture(fixture: dict, **options) -> MatchResult:
    """The fixture scored in strict mode with the identity test calibration."""
    options.setdefault("strict", True)
    config, graph = engine_inputs()
    return score(
        Resume.model_validate(fixture["resume"]),
        JobPosting.model_validate(fixture["posting"]),
        as_of=date.fromisoformat(fixture["as_of"]),
        config=config,
        graph=graph,
        calibration=FixedCalibrator(),
        options=ScoreOptions(**options),
    )


def _close(actual, expected) -> bool:
    return math.isclose(actual, expected, rel_tol=0.0, abs_tol=TOL)


def test_there_are_thirty_named_pairs() -> None:
    assert len(FIXTURES) == 30
    names = [load(path)["name"] for path in FIXTURES]
    assert len(set(names)) == 30
    for path, name in zip(FIXTURES, names):
        assert path.stem.split("_", 1)[1] == name


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_fixture_shape(path: Path) -> None:
    fixture = load(path)
    assert set(fixture) == {"name", "description", "as_of", "resume", "posting", "expected"}
    expected = fixture["expected"]
    assert EXPECTED_KEYS <= set(expected) <= EXPECTED_KEYS | OPTIONAL_KEYS
    assert len(expected["derivation"]) > 80
    assert chr(0x2014) not in json.dumps(fixture, ensure_ascii=False)  # no em dashes in repo text
    if expected["outcome"] == Outcome.GATE_FAIL.value:
        assert expected["raw_score"] is None and expected["dimensions"] == {}
    else:
        assert set(expected["dimensions"]) == {name.value for name in DimensionName}


@pytest.mark.parametrize("path", FIXTURES, ids=lambda path: path.stem)
def test_fixture_scores_as_derived(path: Path) -> None:
    fixture = load(path)
    expected = fixture["expected"]
    result = run_fixture(fixture)

    assert result.outcome.value == expected["outcome"]
    if expected["raw_score"] is None:
        assert result.raw_score is None and result.calibrated_score is None and result.display_score is None
    else:
        assert result.raw_score is not None and _close(result.raw_score, expected["raw_score"])
        assert result.calibrated_score == round(100 * result.raw_score)
        shown = result.outcome is Outcome.SCORED
        assert result.display_score == (result.calibrated_score if shown else None)

    assert set(result.dimensions) == set(expected["dimensions"])
    for name, value in expected["dimensions"].items():
        dimension = result.dimensions[name]
        if value is None:
            assert dimension.abstained, name
        else:
            assert not dimension.abstained and _close(dimension.value, value), (name, dimension.value, value)

    if "band" in expected:
        assert result.band is not None and result.band.value == expected["band"]
    if "confidence" in expected:
        assert result.confidence is not None and _close(result.confidence.value, expected["confidence"])
    if "caps" in expected:
        caps = [{"reason": cap.reason.value, "cap": cap.cap, "requirement_ids": list(cap.requirement_ids)}
                for cap in result.caps]
        assert caps == expected["caps"]
    if "reasons" in expected:
        assert [reason.value for reason in result.reasons] == expected["reasons"]
    if "flags" in expected:
        assert [flag.value for flag in result.flags] == expected["flags"]
    if "gate_status" in expected:
        assert result.gate_status.value == expected["gate_status"]
    if "gates" in expected:
        assert [[gate.kind.value, gate.status.value, gate.reason.value if gate.reason else None]
                for gate in result.gates] == expected["gates"]
    for name, weight in expected.get("effective_weights", {}).items():
        assert _close(result.dimensions[name].effective_weight, weight)


# ─── Pairs that name a §10 / §14.5 relation between two scorings ─────────────────────────────


def _fixture(name: str) -> dict:
    (path,) = [path for path in FIXTURES if path.stem.split("_", 1)[1] == name]
    return load(path)


def _without_digest(result: MatchResult) -> dict:
    dumped = result.model_dump(mode="json")
    dumped.pop("input_digest")
    return dumped


def test_one_skill_listed_fifty_times_scores_as_listed_once() -> None:
    # SPEC §10 adversarial: a skill listed 50 times scores identically to listed once.
    fixture = _fixture("one_skill_x50")
    once = json.loads(json.dumps(fixture))
    once["resume"]["skills_blob"] = once["resume"]["skills_blob"][:1]
    assert _without_digest(run_fixture(fixture)) == _without_digest(run_fixture(once))


def test_two_hundred_listed_skills_score_as_the_relevant_ten() -> None:
    # SPEC §14.5: IDF and the requirement-side sums handle a 200-skill list; the 193 skills that
    # relate to no requirement change no dimension and no raw score.
    fixture = _fixture("blob_200_skills")
    assert len(fixture["resume"]["skills_blob"]) == 200
    lean = json.loads(json.dumps(fixture))
    posting_ids = {requirement["skill_id"] for requirement in fixture["posting"]["requirements"]}
    lean["resume"]["skills_blob"] = [ref for ref in lean["resume"]["skills_blob"] if ref["canonical_id"] in posting_ids]
    stuffed, relevant = run_fixture(fixture), run_fixture(lean)
    assert stuffed.raw_score == relevant.raw_score
    assert {name: dim.value for name, dim in stuffed.dimensions.items()} == {
        name: dim.value for name, dim in relevant.dimensions.items()}


def test_the_pasted_posting_stays_below_0_85() -> None:
    # SPEC §10 / §14.5: the posting pasted into the résumé must score below 0.85.
    result = run_fixture(_fixture("posting_pasted_as_blob"))
    assert result.raw_score is not None and result.raw_score < 0.85


def test_a_location_warning_changes_no_score() -> None:
    # SPEC §4: gates never enter the weighted sum.
    warned, plain = run_fixture(_fixture("gate_warn_location")), run_fixture(_fixture("strong_all_round"))
    assert warned.raw_score == plain.raw_score and warned.dimensions == plain.dimensions


def test_zero_overlap_scores_valid_zeros_not_abstentions() -> None:
    result = run_fixture(_fixture("zero_overlap"))
    for name in ("required_skills", "preferred_skills", "tools", "experience"):
        dimension = result.dimensions[name]
        assert not dimension.abstained and dimension.value == 0.0
