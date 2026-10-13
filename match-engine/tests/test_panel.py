"""Loading the shipped reference panel (DECISIONS D-50) and using it as the last link of the chain.

The panel is synthetic candidate profiles scored offline; its distributions load with
`load_panel(path)` from `{"format": 1, "references": [<ReferenceDistribution JSON>, ...]}`.
`tests/fixtures/panel_small.json` is a small hand-written one, independent of the code that will
build the real panel. Its distributions, by hand:

  "*"      40 candidates, scores 0.02, 0.04, ... 0.80
  "data"   30 candidates, ten scores each of 0.3, 0.5 and 0.7
  "design" 12 candidates (under the minimum of 30), 24 pairs, scores 0.15 ... 0.70 twice each

The last two parts (Task 7.1 part B) check the committed panel, `data/calibration/panel.json`
against its MANIFEST.json, and `scripts/build_reference_panel.py` on invented postings (never
scraped text, DECISIONS D-04).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

from matcher.calibrate import (
    GLOBAL_KEY,
    MAX_SAMPLES,
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


# ─── the committed panel (data/calibration, Task 7.1 part B) ──────────────────────────────

ROOT = Path(__file__).resolve().parents[1]
CALIBRATION = ROOT / "data" / "calibration"
PANEL = CALIBRATION / "panel.json"
MANIFEST = CALIBRATION / "MANIFEST.json"


def _builder():
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:  # the builder imports corpus_stats as a sibling
        sys.path.insert(0, scripts)
    return importlib.import_module("build_reference_panel")


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_the_committed_panel_is_the_one_its_manifest_pins() -> None:
    manifest = _manifest()
    data = PANEL.read_bytes()
    assert hashlib.sha256(data).hexdigest() == manifest["panel"]["sha256"]
    assert b"\r\n" not in data  # LF only: `* -text` keeps git from rewriting the hashed bytes
    assert "* -text" in (CALIBRATION / ".gitattributes").read_text(encoding="utf-8").splitlines()
    references = load_panel(PANEL)
    assert {r.version for r in references} == {manifest["panel"]["version"]}
    assert manifest["panel"]["version"].startswith("panel-")


def test_the_committed_panel_has_one_group_per_family_and_a_pooled_group() -> None:
    manifest = _manifest()
    references = load_panel(PANEL)
    assert references[0].key == GLOBAL_KEY
    by_key = {r.key: r for r in references}
    assert set(by_key) == {GLOBAL_KEY, *manifest["families"]}
    assert "" not in by_key  # a blank role_category is no family
    for family, info in manifest["families"].items():
        ref = by_key[family]
        # every synthetic profile is scored against its own target posting only: one pair each
        assert ref.n_candidates == ref.n_pairs == info["candidates"] == len(ref.samples)
        assert info["candidates"] <= info["profiles"] == info["postings_sampled"] * 8
        assert info["postings_sampled"] <= 60
        assert info["postings_sampled"] == len(info["posting_ids"]) == len(set(info["posting_ids"]))
        assert info["meets_min_reference_n"] is (ref.n_candidates >= MIN_N)
    pooled = by_key[GLOBAL_KEY]
    assert pooled.n_candidates == pooled.n_pairs == sum(i["candidates"] for i in manifest["families"].values())
    assert len(pooled.samples) == min(pooled.n_pairs, MAX_SAMPLES)
    assert pooled.n_candidates >= MIN_N  # the last link of the chain always answers (part A report)


def test_the_committed_panel_answers_for_every_family() -> None:
    references = load_panel(PANEL)
    by_key = {r.key: r for r in references}
    calibrator = PercentileCalibrator(references, min_reference_n=MIN_N)
    for family in [*_manifest()["families"], "A family nobody posted", None]:
        own = by_key.get(family) if family is not None else None
        expected = own if own is not None and own.n_candidates >= MIN_N else by_key[GLOBAL_KEY]
        calibrated, info = calibrator.bind(posting_key=None, family=family).calibrate(0.6)
        assert (info.scope, info.n, info.version) == ("panel", expected.n_candidates, expected.version)
        assert 0 <= calibrated <= 100


def test_the_committed_panel_orders_the_fit_levels() -> None:
    # The panel spreads fit levels 0..3 evenly; a better-fitting synthetic candidate scores higher
    # on average, in the pooled group and in every family that has enough candidates.
    manifest = _manifest()
    groups = [manifest["pooled"]] + [i for i in manifest["families"].values() if i["meets_min_reference_n"]]
    for group in groups:
        means = [group["mean_raw_by_fit_level"][str(level)] for level in range(4)]
        assert means == sorted(means) and len(set(means)) == 4, means


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_the_committed_panel_holds_no_posting_text() -> None:
    # D-04: scraped text never enters the repository. The panel holds numbers, keys and a version.
    manifest = _manifest()
    panel = json.loads(PANEL.read_text(encoding="utf-8"))
    allowed = {"format", "references", "scope", "key", "n_candidates", "n_pairs", "samples", "version",
               "panel", GLOBAL_KEY, manifest["panel"]["version"], *manifest["families"]}
    assert set(_strings(panel)) <= allowed
    for info in manifest["families"].values():
        assert all(isinstance(posting_id, int) for posting_id in info["posting_ids"])
    assert all(len(s) <= 120 and "\n" not in s for s in _strings(manifest))


# ─── the builder (scripts/build_reference_panel.py), on invented postings ────────────────

SOFTWARE = ["Python", "Kubernetes", "Docker", "Terraform", "React", "TypeScript", "PostgreSQL", "Kafka", "Redis",
            "GraphQL", "Django", "Flask", "Java", "Scala", "Rust", "Swift", "Angular", "Airflow", "Spark", "Jenkins"]
DATA = ["SQL", "Tableau", "Power BI", "Excel", "Statistics", "R", "Snowflake", "BigQuery", "Looker", "pandas",
        "NumPy", "Machine Learning", "Data Visualization", "ETL", "dbt", "Hadoop", "SAS", "SPSS", "MATLAB", "Alteryx"]


def _row(posting_id: int, family: str, description: str, title: str = "Analyst") -> dict:
    return {"id": posting_id, "title": title, "description": description, "role_category": family,
            "experience_level": "new_grad"}


def _requirements(names) -> str:
    return "Requirements\n" + "\n".join(f"- {name}" for name in names)


def invented_rows() -> list[dict]:
    """Invented postings (never scraped text): two families, a duplicate description, a posting
    with no requirement, a posting whose skills are only implied, and one with no family."""
    first = _requirements(SOFTWARE[:18])  # 18 skills each: a family the generator can staff alone
    return [
        _row(1, "Software", first, "Software Developer"),
        _row(2, "Software", _requirements(SOFTWARE[2:]), "Backend Developer"),
        _row(3, "Software", first.replace("\n", " \n "), "Platform Developer"),  # same text as 1
        _row(4, "Software", "We are a great company. Join our friendly team and grow with us!"),
        _row(5, "Data", _requirements(DATA[:18]), "Data Analyst"),
        _row(6, "Data", _requirements(DATA[2:]), "BI Analyst"),
        _row(7, "Data", "About the role\nYou will use Python, SQL and Excel daily in our analytics team."),
        _row(8, "", _requirements(["Python", "SQL"])),
    ]


@pytest.fixture(scope="module")
def resources():
    from tests.posting_helpers import env

    e = env()
    return _builder().Resources(config=e["config"], graph=e["graph"], resolver=e["resolver"],
                                parse_config=e["parse_config"], titles=e["titles"])


def _build(resources, rows=None, *, seed: int = 5, per_family: int = 3, per_posting: int = 4):
    return _builder().build_panel(invented_rows() if rows is None else rows, resources, seed=seed,
                                  postings_per_family=per_family, profiles_per_posting=per_posting)


@pytest.fixture(scope="module")
def small_panel(resources):
    return _build(resources)


def test_builder_samples_scorable_postings_per_family(small_panel) -> None:
    families = small_panel.manifest["families"]
    assert list(families) == ["Data", "Software"]  # the blank family is left out
    software, data = families["Software"], families["Data"]
    assert software["postings_available"] == 3  # posting 3 has posting 1's text: collapsed
    assert software["postings_skipped_unscorable"] == 1  # posting 4 names no skill
    assert sorted(software["posting_ids"]) == [1, 2]
    assert data["postings_available"] == 3 and data["postings_skipped_unscorable"] == 0
    assert sorted(data["posting_ids"]) == [5, 6, 7]
    assert data["postings_targeted_on_implied_or_preferred"] == 1  # posting 7: implied skills only
    assert software["profiles"] == 8 and data["profiles"] == 12


def test_builder_references_count_distinct_synthetic_people(small_panel) -> None:
    references = [ReferenceDistribution.model_validate(r) for r in small_panel.panel["references"]]
    assert [r.key for r in references] == [GLOBAL_KEY, "Data", "Software"]
    assert {r.scope for r in references} == {"panel"}
    by_key = {r.key: r for r in references}
    for family, info in small_panel.manifest["families"].items():
        excluded = sum(info["excluded_without_raw_score"].values())
        assert by_key[family].n_candidates == by_key[family].n_pairs == info["profiles"] - excluded
    assert by_key[GLOBAL_KEY].n_candidates == by_key["Data"].n_candidates + by_key["Software"].n_candidates
    assert by_key[GLOBAL_KEY].samples == tuple(sorted(by_key["Data"].samples + by_key["Software"].samples))
    assert small_panel.manifest["panel"]["sha256"] == hashlib.sha256(small_panel.panel_bytes()).hexdigest()


def test_builder_is_deterministic_and_seeded(resources, small_panel) -> None:
    again = _build(resources)
    assert again.panel_bytes() == small_panel.panel_bytes()
    assert again.manifest_bytes() == small_panel.manifest_bytes()
    # row order never matters: families are read in id order
    assert _build(resources, list(reversed(invented_rows()))).panel_bytes() == small_panel.panel_bytes()
    assert _build(resources, seed=6).panel_bytes() != small_panel.panel_bytes()


def test_builder_caps_the_postings_per_family(resources) -> None:
    # Without posting 7 (three implied skills, too few to staff a level-0 candidate's career from)
    rows = [row for row in invented_rows() if row["id"] != 7]
    panel = _build(resources, rows, per_family=1)
    for info in panel.manifest["families"].values():
        assert info["postings_sampled"] == 1 and info["profiles"] == 4


@pytest.mark.parametrize("per_posting", [0, 2, 6])
def test_builder_needs_whole_rounds_of_fit_levels(resources, per_posting) -> None:
    with pytest.raises(ValueError, match="multiple of 4"):
        _build(resources, per_posting=per_posting)


def test_targeting_copy_reads_implied_then_preferred_skills_as_required() -> None:
    from tests.posting_helpers import necessities, parse

    targeting_copy = _builder().targeting_copy
    required = parse(_requirements(["Python", "SQL"]))
    assert set(necessities(required).values()) == {"REQUIRED"}
    assert targeting_copy(required) is required

    implied = parse("About the role\nYou will use Python and SQL daily.\n\nNice to have\n- Kafka")
    assert necessities(implied) == {"skill.python": "IMPLIED", "skill.sql": "IMPLIED", "skill.kafka": "PREFERRED"}
    assert necessities(targeting_copy(implied)) == {"skill.python": "REQUIRED", "skill.sql": "REQUIRED",
                                                    "skill.kafka": "PREFERRED"}
    copy = targeting_copy(implied)  # everything else about the posting is kept
    assert copy.model_dump(exclude={"requirements"}) == implied.model_dump(exclude={"requirements"})

    preferred = parse("Nice to have\n- Kafka\n- Redis")
    assert set(necessities(preferred).values()) == {"PREFERRED"}
    assert set(necessities(targeting_copy(preferred)).values()) == {"REQUIRED"}


def test_panel_vocabulary_families_are_the_job_families_that_name_a_skill() -> None:
    from matcher.graph.model import SkillGraph, SkillNode
    from matcher.models import JobPosting, JobRequirement, Necessity, SkillKind

    builder = _builder()
    nodes = {
        "skill.a": SkillNode(id="skill.a", label="Alpha", kind=SkillKind.TECHNOLOGY, aliases=("Shared",)),
        "skill.b": SkillNode(id="skill.b", label="Beta", kind=SkillKind.TOOL, aliases=("Shared", "B2")),
        "skill.c": SkillNode(id="skill.c", label="Gamma", kind=SkillKind.DOMAIN),
    }

    def posting(*ids: str) -> JobPosting:
        return JobPosting(requirements=tuple(JobRequirement(skill_id=i, necessity=Necessity.REQUIRED) for i in ids))

    samples = [builder.FamilySample("Ops", 1, [posting("skill.b", "skill.c")]),
               builder.FamilySample("Dev", 1, [posting("skill.a", "skill.b")])]
    vocab = builder.panel_vocabulary(SkillGraph(nodes, {}, {}, "test"), samples)
    assert vocab.families == ("Dev", "Ops")
    by_id = {s.id: s for s in vocab.skills}
    assert [s.id for s in vocab.skills] == ["skill.a", "skill.b", "skill.c"]
    assert (by_id["skill.a"].families, by_id["skill.b"].families, by_id["skill.c"].families) == (
        ("Dev",), ("Dev", "Ops"), ("Ops",))
    assert by_id["skill.a"].aliases == ("Shared",) and by_id["skill.b"].aliases == ("B2",)  # one skill per spelling

    nodes["skill.d"] = SkillNode(id="skill.d", label="alpha", kind=SkillKind.TOOL)
    clash = [builder.FamilySample("Dev", 1, [posting("skill.a", "skill.d")])]
    with pytest.raises(ValueError, match="both spelled"):
        builder.panel_vocabulary(SkillGraph(nodes, {}, {}, "test"), clash)


@pytest.mark.slow  # two subprocesses load the taxonomy and build a small panel
def test_builder_output_does_not_depend_on_the_hash_seed(tmp_path) -> None:
    corpus = tmp_path / "postings.jsonl"
    corpus.write_text("".join(json.dumps(row) + "\n" for row in invented_rows()), encoding="utf-8")
    outputs = []
    for hash_seed in ("0", "4242"):
        out = tmp_path / f"out-{hash_seed}"
        environment = {**os.environ, "PYTHONHASHSEED": hash_seed}
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_reference_panel.py"), "--corpus", str(corpus),
             "--out", str(out), "--postings-per-family", "3", "--profiles-per-posting", "4"],
            capture_output=True, text=True, env=environment, cwd=ROOT, timeout=300,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(((out / "panel.json").read_bytes(), (out / "MANIFEST.json").read_bytes()))
    assert outputs[0] == outputs[1]
    assert load_panel(tmp_path / "out-0" / "panel.json")[0].key == GLOBAL_KEY
