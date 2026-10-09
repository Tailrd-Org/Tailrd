"""Loading the shipped reference panel (DECISIONS D-50) and using it as the last link of the chain.

The panel is synthetic candidate profiles scored offline; its distributions load with
`load_panel(path)` from `{"format": 1, "references": [<ReferenceDistribution JSON>, ...]}`.
`tests/fixtures/panel_small.json` is a small hand-written one, independent of the code that will
build the real panel. Its distributions, by hand:

  "*"      40 candidates, scores 0.02, 0.04, ... 0.80
  "data"   30 candidates, ten scores each of 0.3, 0.5 and 0.7
  "design" 12 candidates (under the minimum of 30), 24 pairs, scores 0.15 ... 0.70 twice each
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from matcher.calibrate import (
    GLOBAL_KEY,
    PanelError,
    PercentileCalibrator,
    ReferenceDistribution,
    build_reference,
    load_panel,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "panel_small.json"
MIN_N = 30


def write(tmp_path: Path, document, name: str = "panel.json") -> Path:
    path = tmp_path / name
    text = document if isinstance(document, str) else json.dumps(document)
    path.write_text(text, encoding="utf-8")
    return path


def reference_json(**changes) -> dict:
    base = {
        "scope": "panel",
        "key": GLOBAL_KEY,
        "n_candidates": 3,
        "n_pairs": 3,
        "samples": [0.1, 0.5, 0.9],
        "version": "panel-x",
    }
    return {**base, **changes}


# ─── load_panel ────────────────────────────────────────────────────────────────────────────


def test_load_panel_reads_the_hand_written_fixture() -> None:
    panel = load_panel(FIXTURE)
    assert isinstance(panel, tuple)
    assert all(isinstance(ref, ReferenceDistribution) for ref in panel)
    assert [(r.scope, r.key, r.n_candidates, r.n_pairs, len(r.samples), r.version) for r in panel] == [
        ("panel", "*", 40, 40, 40, "panel-test-1"),
        ("panel", "data", 30, 30, 30, "panel-test-1"),
        ("panel", "design", 12, 24, 24, "panel-test-1"),
    ]
    assert panel[0].samples[:3] == (0.02, 0.04, 0.06) and panel[0].samples[-1] == 0.8
    assert GLOBAL_KEY == "*"


def test_load_panel_accepts_a_string_path() -> None:
    assert load_panel(str(FIXTURE)) == load_panel(FIXTURE)


def test_load_panel_round_trips_the_fixture() -> None:
    document = json.loads(FIXTURE.read_text(encoding="utf-8"))
    panel = load_panel(FIXTURE)
    assert [ref.model_dump(mode="json") for ref in panel] == document["references"]


def test_a_built_panel_survives_a_write_and_a_load(tmp_path: Path) -> None:
    rng = random.Random(41)
    small = build_reference("panel", "data", [0.1, 0.4, 0.4, 0.9], ["a", "a", "b", "c"], "panel-1")
    scores = [round(rng.random(), 6) for _ in range(12_000)]  # downsampled to 5,000
    big = build_reference("panel", GLOBAL_KEY, scores, [f"p{i}" for i in range(12_000)], "panel-1")
    document = {"format": 1, "references": [ref.model_dump(mode="json") for ref in (big, small)]}
    path = write(tmp_path, json.dumps(document, sort_keys=True, indent=1))
    assert load_panel(path) == (big, small)  # file order is kept


def test_unknown_top_level_keys_are_ignored_but_references_stay_strict(tmp_path: Path) -> None:
    document = {"format": 1, "built_by": "scripts/build_reference_panel.py", "references": [reference_json()]}
    assert len(load_panel(write(tmp_path, document))) == 1
    strict = {"format": 1, "references": [reference_json(unexpected=1)]}
    with pytest.raises(PanelError):
        load_panel(write(tmp_path, strict, "strict.json"))


def test_a_missing_file_or_a_directory_is_a_panel_error(tmp_path: Path) -> None:
    with pytest.raises(PanelError):
        load_panel(tmp_path / "nope.json")
    with pytest.raises(PanelError):
        load_panel(tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        "[1, 2",
        '{"format": 1, "references": [',
        "[]",  # a list, not an object
        '"panel"',
        '{"format": 1, "references": [{"scope": "panel", "key": "*", "n_candidates": 3, "n_pairs": 3,'
        ' "samples": [0.1, NaN, 0.9], "version": "v"}]}',  # NaN is not a score
    ],
)
def test_malformed_json_is_a_panel_error(tmp_path: Path, text: str) -> None:
    with pytest.raises(PanelError):
        load_panel(write(tmp_path, text))


def test_a_file_that_is_not_utf8_is_a_panel_error(tmp_path: Path) -> None:
    path = tmp_path / "panel.json"
    path.write_bytes(b"\xff\xfe{}")
    with pytest.raises(PanelError):
        load_panel(path)


@pytest.mark.parametrize(
    "document",
    [
        {"references": [reference_json()]},  # no format
        {"format": 2, "references": [reference_json()]},  # another format
        {"format": "1", "references": [reference_json()]},
        {"format": True, "references": [reference_json()]},  # true == 1 in Python, but it is not 1
        {"format": 1.0, "references": [reference_json()]},
        {"format": 1},  # no references
        {"format": 1, "references": {}},  # not a list
        {"format": 1, "references": []},  # nothing to calibrate with
        {"format": 1, "references": [reference_json(), "x"]},  # an entry that is not an object
    ],
)
def test_the_document_shape_is_checked(tmp_path: Path, document) -> None:
    with pytest.raises(PanelError):
        load_panel(write(tmp_path, document))


@pytest.mark.parametrize(
    "change",
    [
        {"samples": [0.5, 0.1, 0.9]},  # not sorted
        {"samples": [0.1, 0.5, 1.5]},  # outside [0, 1]
        {"n_candidates": 9},  # more people than pairs
        {"n_pairs": 0},
        {"version": ""},
    ],
)
def test_an_invalid_reference_is_a_panel_error(tmp_path: Path, change) -> None:
    document = {"format": 1, "references": [reference_json(), reference_json(key="data", **change)]}
    with pytest.raises(PanelError, match="data|references"):
        load_panel(write(tmp_path, document))


@pytest.mark.parametrize("scope", ["posting", "family", "global", "test"])
def test_a_panel_holds_only_panel_references(tmp_path: Path, scope: str) -> None:
    # A synthetic group labelled 'family' would let the basis sentence tell a person they were
    # compared with "candidates scored for similar roles" (D-50: it must say a reference group).
    document = {"format": 1, "references": [reference_json(scope=scope)]}
    with pytest.raises(PanelError, match="panel"):
        load_panel(write(tmp_path, document))


def test_a_repeated_reference_is_a_panel_error(tmp_path: Path) -> None:
    document = {"format": 1, "references": [reference_json(), reference_json(version="panel-y")]}
    with pytest.raises(PanelError):
        load_panel(write(tmp_path, document))


def test_a_panel_error_is_a_value_error() -> None:
    assert issubclass(PanelError, ValueError)


# ─── The panel in the chain ────────────────────────────────────────────────────────────────


def calibrator_for(panel) -> PercentileCalibrator:
    return PercentileCalibrator(panel, min_reference_n=MIN_N)


def test_the_panel_calibrates_by_family_where_it_has_enough_candidates() -> None:
    bound = calibrator_for(load_panel(FIXTURE)).bind(posting_key="job-1", family="data")
    # 'data': ten scores each of 0.3, 0.5, 0.7 (n = 30).
    #   0.2: nothing below                          -> 0
    #   0.3: (0 + 0.5 x 10) / 30 = 16.7 %           -> 17
    #   0.5: (10 + 0.5 x 10) / 30 = 50 %            -> 50
    #   0.6: 20 / 30 = 66.7 %                       -> 67
    #   0.8: everyone below                         -> 100
    scores = [bound.calibrate(raw)[0] for raw in (0.2, 0.3, 0.5, 0.6, 0.8)]
    assert scores == [0, 17, 50, 67, 100]
    info = bound.calibrate(0.5)[1]
    assert (info.method, info.scope, info.n, info.version) == ("percentile", "panel", 30, "panel-test-1")


@pytest.mark.parametrize("family", ["design", "ops", None])
def test_the_panel_falls_back_to_the_pooled_group(family) -> None:
    # 'design' has 12 candidates (under 30), 'ops' has no group, None means no family: all use '*'.
    bound = calibrator_for(load_panel(FIXTURE)).bind(posting_key=None, family=family)
    # '*': 0.02, 0.04 ... 0.80 (n = 40).
    #   0.45: 22 scores below (0.02 ... 0.44)               -> 22 / 40 = 55 %
    #   0.50: 24 below and one equal -> (24 + 0.5) / 40     -> 61.25 %, rounds to 61
    #   0.01 and 0.9: nothing below / everything below      -> 0 and 100
    assert [bound.calibrate(raw)[0] for raw in (0.01, 0.45, 0.5, 0.9)] == [0, 55, 61, 100]
    assert bound.calibrate(0.45)[1].n == 40


def test_with_a_handful_of_resumes_every_score_uses_the_panel() -> None:
    # D-50: until 30 people have scored résumés, every score uses the panel. Five résumés scored
    # against 2,000 postings of one family are 10,000 pairs from five people.
    people = [f"resume-{i}" for i in range(5)]
    ids = [people[i % 5] for i in range(10_000)]
    scores = [(i % 100) / 100 for i in range(10_000)]
    live = [
        build_reference("posting", "job-1", scores, ids, "live-1"),
        build_reference("family", "data", scores, ids, "live-1"),
        build_reference("global", GLOBAL_KEY, scores, ids, "live-1"),
    ]
    calibrator = calibrator_for([*live, *load_panel(FIXTURE)])
    for posting_key in ("job-1", "job-2", None):
        _, info = calibrator.bind(posting_key=posting_key, family="data").calibrate(0.5)
        assert (info.scope, info.n) == ("panel", 30)


def test_thirty_people_take_over_from_the_panel() -> None:
    thirty = [f"resume-{i}" for i in range(30)]
    scores = [(i % 100) / 100 for i in range(3_000)]
    ids = [thirty[i % 30] for i in range(3_000)]
    live = build_reference("global", GLOBAL_KEY, scores, ids, "live-2")
    calibrator = calibrator_for([live, *load_panel(FIXTURE)])
    info = calibrator.bind(posting_key=None, family="data").calibrate(0.5)[1]
    assert (info.scope, info.n, info.version) == ("global", 30, "live-2")
