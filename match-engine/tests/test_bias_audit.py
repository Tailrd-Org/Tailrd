"""scripts/bias_audit.py: the annual bias-audit artifacts from persisted traces (SPEC section 9).

The inputs are tests/fixtures/audit/ (synthetic, built by make_audit_fixture.py; the module
docstring there lists every person). All expected numbers below are worked out by hand from that
table, never read back from the script:

attr_pass, groups of 10 people with one trace each (p1: S92 S88 S85 S80 S74 S70 S66 S58 S40 NR;
p2: S90 S87 S85 S78 S72 LC62 S58 S45 GF S30), p3 has 2 people:
    >= 85:  p1 3/10, p2 3/10        ratios 1, 1        min 1/1
    >= 70:  p1 6/10, p2 5/10        ratios 1, 5/6      min 5/6
    >= 55:  p1 8/10, p2 7/10        ratios 1, 7/8      min 7/8
attr_fail, groups of 12 people with 13 traces each (q1 = p1 + persons 20, 21; q2 = p2 + 22, 23):
    >= 85:  q1 4/13, q2 3/13        ratios 1, 3/4      min 3/4   <- below four-fifths
    >= 70:  q1 7/13, q2 6/13        ratios 1, 6/7      min 6/7
    >= 55:  q1 9/13, q2 9/13        ratios 1, 1        min 1/1
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.audit import make_audit_fixture as fixture

ENGINE_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = fixture.FIXTURE_DIR
TRACES = FIXTURE_DIR / "traces.jsonl"
DEMOGRAPHICS = FIXTURE_DIR / "demographics.csv"

PASS, ERROR, FAIL, NOT_ASSESSED = 0, 1, 2, 3


@pytest.fixture(scope="module")
def bias_audit():
    path = ENGINE_ROOT / "scripts" / "bias_audit.py"
    spec = importlib.util.spec_from_file_location("bias_audit", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bias_audit"] = module
    spec.loader.exec_module(module)
    return module


def run(
    bias_audit, out_dir: Path, *extra: str, traces: Path = TRACES, demographics: Path = DEMOGRAPHICS
) -> int:
    return bias_audit.main(
        ["--traces", str(traces), "--demographics", str(demographics), "--out-dir", str(out_dir), *extra]
    )


def load(out_dir: Path) -> dict:
    return json.loads((out_dir / "bias_audit.json").read_text(encoding="utf-8"))


@pytest.fixture
def audit(bias_audit, tmp_path):
    """One exploratory run of the fixture (groups of 5 people and up are assessed)."""
    out = tmp_path / "out"
    code = run(bias_audit, out, "--min-group-n", "5")
    return code, load(out), (out / "bias_audit.md").read_text(encoding="utf-8"), out


# --- the fixture -------------------------------------------------------------------------------


def test_committed_fixture_is_current(tmp_path) -> None:
    fixture.write_fixture(tmp_path)
    for name in ("traces.jsonl", "demographics.csv"):
        assert (FIXTURE_DIR / name).read_bytes() == (tmp_path / name).read_bytes(), (
            f"{name} drifted from tests/fixtures/audit/make_audit_fixture.py; rerun the builder"
        )


def test_fixture_uses_opaque_ids_never_user_keys() -> None:
    text = TRACES.read_text(encoding="utf-8") + DEMOGRAPHICS.read_text(encoding="utf-8")
    assert "synthetic-user" not in text
    assert "user-17" in text  # the one deliberately bad key, in a line the audit must reject


# --- verdict and exit codes --------------------------------------------------------------------


def test_a_violation_is_reported_and_exits_2(audit) -> None:
    code, report, _, _ = audit
    assert code == FAIL
    assert report["status"] == "FAIL"
    assert report["violations"] == [
        {"attribute": "attr_fail", "threshold": 85, "min_ratio": 0.75, "min_ratio_exact": "3/4"}
    ]


def test_a_clean_attribute_passes_with_exit_0(bias_audit, tmp_path) -> None:
    code = run(bias_audit, tmp_path, "--min-group-n", "5", "--attributes", "attr_pass")
    report = load(tmp_path)
    assert code == PASS
    assert report["status"] == "PASS"
    assert report["violations"] == []
    assert sorted(report["attributes"]) == ["attr_pass"]


def test_nothing_assessable_is_not_a_pass(bias_audit, tmp_path) -> None:
    # No group in the fixture has 100 people: every check is "not assessed", which is neither a pass
    # nor a failure, and the exit status says so.
    code = run(bias_audit, tmp_path)
    report = load(tmp_path)
    assert code == NOT_ASSESSED
    assert report["status"] == "NOT_ASSESSED"
    assert report["parameters"]["exploratory"] is False
    assert report["violations"] == []
    for attribute in report["attributes"].values():
        assert attribute["groups"] == {}
        assert all(check["assessed"] is False for check in attribute["selection"].values())
    text = (tmp_path / "bias_audit.md").read_text(encoding="utf-8")
    assert "Status: NOT ASSESSED" in text
    assert "Exploratory" not in text


# --- parameters and inputs ---------------------------------------------------------------------


def test_thresholds_default_to_the_configured_band_minimums(audit, bias_audit, tmp_path) -> None:
    assert audit[1]["parameters"]["thresholds"] == [85, 70, 55]
    run(bias_audit, tmp_path, "--min-group-n", "5", "--thresholds", "55,85,85")
    assert load(tmp_path)["parameters"]["thresholds"] == [85, 55]


def test_parameters_record_how_the_audit_was_run(audit) -> None:
    assert audit[1]["parameters"] == {
        "min_group_n": 5,
        "standard_min_group_n": 100,
        "exploratory": True,
        "thresholds": [85, 70, 55],
        "ratio_threshold": "4/5",
    }


def test_input_summary_counts_good_bad_and_tampered_lines(audit) -> None:
    traces = audit[1]["inputs"]["traces"]
    assert traces == {
        "lines": 31,
        "usable": 27,
        "malformed": 3,  # not JSON; no trace; a user id where an opaque id belongs
        "integrity_failures": 1,  # a trace whose digest does not match it
        "people": 25,
        "as_of_first": "2026-09-01",
        "as_of_last": "2026-09-25",
        "engine_versions": ["1.0.0", "1.0.1"],
        "config_hashes": ["0f1e2d3c4b5a6978", "a1b2c3d4e5f60718"],
    }
    assert audit[1]["inputs"]["demographics"] == {"people": 24, "attributes": ["attr_fail", "attr_pass"]}


def test_a_tampered_trace_never_enters_the_statistics(audit) -> None:
    # The tampered line claims a score of 99 for a person in p1: p1 would have 4 selected at >= 85.
    p1 = audit[1]["attributes"]["attr_pass"]["selection"]["85"]["groups"][0]
    assert (p1["group"], p1["selected"], p1["n_records"]) == ("p1", 3, 10)


def test_traces_without_a_digest_are_accepted(bias_audit, tmp_path) -> None:
    lines = [json.loads(line) for line in TRACES.read_text(encoding="utf-8").splitlines()[:27]]
    for line in lines:
        del line["digest"]
    stripped = tmp_path / "stripped.jsonl"
    stripped.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    run(bias_audit, out, "--min-group-n", "5", traces=stripped)
    assert load(out)["inputs"]["traces"]["usable"] == 27
    assert load(out)["inputs"]["traces"]["integrity_failures"] == 0


# --- the numbers -------------------------------------------------------------------------------


def test_coverage_counts_who_could_be_placed_in_a_group(audit) -> None:
    attributes = audit[1]["attributes"]
    assert attributes["attr_pass"]["coverage"] == {
        "traces": 27,
        "with_group": 23,
        "undisclosed": 3,
        "no_record": 1,
    }
    assert attributes["attr_fail"]["coverage"] == {
        "traces": 27,
        "with_group": 26,
        "undisclosed": 0,
        "no_record": 1,
    }


def test_small_groups_are_suppressed_not_described(audit) -> None:
    attribute = audit[1]["attributes"]["attr_pass"]
    assert sorted(attribute["groups"]) == ["p1", "p2"]
    assert attribute["suppressed"] == [{"group": "p3", "people": 2}]
    assert audit[1]["attributes"]["attr_fail"]["suppressed"] == []


@pytest.mark.parametrize(
    "attribute,threshold,expected",
    [
        ("attr_pass", "85", ("1/1", [("p1", 3, "3/10", "1/1"), ("p2", 3, "3/10", "1/1")])),
        ("attr_pass", "70", ("5/6", [("p1", 6, "3/5", "1/1"), ("p2", 5, "1/2", "5/6")])),
        ("attr_pass", "55", ("7/8", [("p1", 8, "4/5", "1/1"), ("p2", 7, "7/10", "7/8")])),
        ("attr_fail", "85", ("3/4", [("q1", 4, "4/13", "1/1"), ("q2", 3, "3/13", "3/4")])),
        ("attr_fail", "70", ("6/7", [("q1", 7, "7/13", "1/1"), ("q2", 6, "6/13", "6/7")])),
        ("attr_fail", "55", ("1/1", [("q1", 9, "9/13", "1/1"), ("q2", 9, "9/13", "1/1")])),
    ],
)
def test_selection_rates_and_ratios_per_group_per_threshold(audit, attribute, threshold, expected) -> None:
    min_ratio, groups = expected
    check = audit[1]["attributes"][attribute]["selection"][threshold]
    assert check["min_ratio_exact"] == min_ratio
    assert [(g["group"], g["selected"], g["rate_exact"], g["ratio_exact"]) for g in check["groups"]] == groups


def test_the_violating_check_in_full(audit) -> None:
    check = audit[1]["attributes"]["attr_fail"]["selection"]["85"]
    assert check == {
        "threshold": "4/5",
        "min_group_n": 5,
        "assessed": True,
        "passed": False,
        "reference_group": "q1",
        "min_ratio": 0.75,
        "min_ratio_exact": "3/4",
        "excluded": [],
        "groups": [
            {
                "group": "q1",
                "n_people": 12,
                "n_records": 13,
                "selected": 4,
                "rate": 0.307692,
                "rate_exact": "4/13",
                "ratio": 1.0,
                "ratio_exact": "1/1",
                "meets": True,
            },
            {
                "group": "q2",
                "n_people": 12,
                "n_records": 13,
                "selected": 3,
                "rate": 0.230769,
                "rate_exact": "3/13",
                "ratio": 0.75,
                "ratio_exact": "3/4",
                "meets": False,
            },
        ],
    }


def test_abstained_and_gated_traces_count_in_the_denominator_and_are_never_selected(audit) -> None:
    # p1 has 10 traces of which one is NEEDS_REVIEW; p2 has 10 of which one is GATE_FAIL. Both are in
    # n_records, so p1's rate is 3/10 and not 3/9.
    groups = audit[1]["attributes"]["attr_pass"]["selection"]["85"]["groups"]
    assert [g["n_records"] for g in groups] == [10, 10]


def test_group_outcomes_abstention_and_score_distribution(audit) -> None:
    groups = audit[1]["attributes"]["attr_pass"]["groups"]
    p1, p2 = groups["p1"], groups["p2"]
    assert (p1["people"], p1["traces"]) == (10, 10)
    assert p1["outcomes"] == {"NEEDS_REVIEW": 1, "SCORED": 9}
    assert p1["abstention"] == {"count": 1, "rate": 0.1, "reasons": {"UNREADABLE": 1}}
    # scored 92 88 85 80 74 70 66 58 40: sum 653, median 74
    assert p1["scores"] == {
        "n": 9,
        "mean": 72.56,
        "median": 74,
        "min": 40,
        "max": 92,
        "histogram": {
            "0-9": 0,
            "10-19": 0,
            "20-29": 0,
            "30-39": 0,
            "40-49": 1,
            "50-59": 1,
            "60-69": 1,
            "70-79": 2,
            "80-89": 3,
            "90-100": 1,
        },
    }
    assert (p2["people"], p2["traces"]) == (10, 10)
    assert p2["outcomes"] == {"GATE_FAIL": 1, "SCORED": 8, "SCORED_LOW_CONFIDENCE": 1}
    # a low-confidence result withholds its percentage: it counts as abstained, and its internal
    # calibrated score (62) still sits in the distribution
    assert p2["abstention"] == {"count": 1, "rate": 0.1, "reasons": {"LOW_EVIDENCE_DENSITY": 1}}
    assert p2["scores"] == {
        "n": 9,
        "mean": 67.44,
        "median": 72,
        "min": 30,
        "max": 90,
        "histogram": {
            "0-9": 0,
            "10-19": 0,
            "20-29": 0,
            "30-39": 1,
            "40-49": 1,
            "50-59": 1,
            "60-69": 1,
            "70-79": 2,
            "80-89": 2,
            "90-100": 1,
        },
    }


# --- the markdown ------------------------------------------------------------------------------


def test_markdown_states_the_verdict_and_the_evidence(audit) -> None:
    _, _, text, _ = audit
    assert text.startswith("# Bias audit")
    assert "Status: FAIL" in text
    assert "## attr_fail" in text and "## attr_pass" in text
    assert "3/4" in text  # the violating ratio, exact
    assert "attr_fail" in text.split("Violations", 1)[1]
    assert "p3 (2 people)" in text  # suppressed group, named with its size only
    assert "Exploratory run" in text  # min group size 5 is below the standard 100
    for needle in ("calibrated score >=", "employment attorney"):
        assert needle in text


def section_of(text: str, heading: str) -> str:
    """The body of one `## heading` section (up to the next `## `)."""
    return text.split(f"\n## {heading}\n", 1)[1].split("\n## ", 1)[0]


def test_markdown_tables_have_one_row_per_group(audit) -> None:
    _, _, text, _ = audit
    body = section_of(text, "attr_pass")
    # one row for each of: the group table, three selection tables, the score distribution, abstention
    assert body.count("| p1 |") == 6 and body.count("| p2 |") == 6
    assert "| p3 |" not in body  # suppressed groups get no row
    assert body.count("| q") == 0 and "attr_fail" not in body


# --- determinism and privacy -------------------------------------------------------------------


def test_two_runs_write_identical_bytes(bias_audit, tmp_path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    run(bias_audit, first, "--min-group-n", "5")
    run(bias_audit, second, "--min-group-n", "5")
    for name in ("bias_audit.json", "bias_audit.md"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_the_artifacts_hold_no_person_level_identifier(audit) -> None:
    _, _, text, out = audit
    blob = text + (out / "bias_audit.json").read_text(encoding="utf-8")
    for index in range(25):
        assert fixture.person_id(index) not in blob
        assert fixture.user_key(index) not in blob
    assert "user-17" not in blob


def test_the_output_directory_is_created(bias_audit, tmp_path) -> None:
    out = tmp_path / "deep" / "nested" / "dir"
    run(bias_audit, out, "--min-group-n", "5")
    assert (out / "bias_audit.json").is_file() and (out / "bias_audit.md").is_file()


def test_summary_goes_to_stdout(bias_audit, tmp_path, capsys) -> None:
    run(bias_audit, tmp_path, "--min-group-n", "5")
    printed = capsys.readouterr().out
    assert "FAIL" in printed and "attr_fail" in printed and "bias_audit.json" in printed


def test_runs_as_a_script_without_installing_the_package(tmp_path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    done = subprocess.run(
        [
            sys.executable,
            str(ENGINE_ROOT / "scripts" / "bias_audit.py"),
            "--traces",
            str(TRACES),
            "--demographics",
            str(DEMOGRAPHICS),
            "--out-dir",
            str(tmp_path / "out"),
            "--min-group-n",
            "5",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
        timeout=120,
    )
    assert done.returncode == FAIL, done.stderr
    assert (tmp_path / "out" / "bias_audit.json").is_file()


# --- bad inputs --------------------------------------------------------------------------------


def write_demographics(path: Path, header: str, *rows: str) -> Path:
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


def test_demographics_must_be_keyed_by_opaque_id_not_user_id(bias_audit, tmp_path, capsys) -> None:
    keyed_by_user = write_demographics(tmp_path / "d.csv", "user_id,attr_pass", "44,p1", "45,p2")
    assert run(bias_audit, tmp_path / "out", demographics=keyed_by_user) == ERROR
    assert "opaque_id" in capsys.readouterr().err


def test_a_key_that_is_not_an_opaque_id_stops_the_audit_without_echoing_it(
    bias_audit, tmp_path, capsys
) -> None:
    bad = write_demographics(tmp_path / "d.csv", "opaque_id,attr_pass", "jane.doe@example.com,p1")
    assert run(bias_audit, tmp_path / "out", demographics=bad) == ERROR
    err = capsys.readouterr().err
    assert "line 2" in err and "jane.doe" not in err


def test_conflicting_duplicate_keys_stop_the_audit(bias_audit, tmp_path, capsys) -> None:
    key = fixture.person_id(0)
    bad = write_demographics(tmp_path / "d.csv", "opaque_id,attr_pass", f"{key},p1", f"{key},p2")
    assert run(bias_audit, tmp_path / "out", demographics=bad) == ERROR
    assert "twice" in capsys.readouterr().err


def test_identical_duplicate_rows_are_harmless(bias_audit, tmp_path) -> None:
    rows = DEMOGRAPHICS.read_text(encoding="utf-8").splitlines()
    doubled = tmp_path / "d.csv"
    doubled.write_text("\n".join(rows + rows[1:]) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    assert run(bias_audit, out, "--min-group-n", "5", demographics=doubled) == FAIL
    assert load(out)["inputs"]["demographics"]["people"] == 24


def test_a_demographics_file_with_no_attribute_column_is_refused(bias_audit, tmp_path, capsys) -> None:
    bare = write_demographics(tmp_path / "d.csv", "opaque_id", fixture.person_id(0))
    assert run(bias_audit, tmp_path / "out", demographics=bare) == ERROR
    assert "attribute" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["--attributes", "nonexistent"],
        ["--thresholds", "eighty"],
        ["--thresholds", "0"],
        ["--thresholds", "101"],
        ["--min-group-n", "0"],
        ["--min-group-n", "many"],
    ],
)
def test_bad_options_exit_1(bias_audit, tmp_path, args) -> None:
    assert run(bias_audit, tmp_path / "out", *args) == ERROR


def test_missing_files_exit_1(bias_audit, tmp_path) -> None:
    assert run(bias_audit, tmp_path / "out", traces=tmp_path / "nope.jsonl") == ERROR
    assert run(bias_audit, tmp_path / "out", demographics=tmp_path / "nope.csv") == ERROR


def test_no_usable_traces_exit_1(bias_audit, tmp_path, capsys) -> None:
    junk = tmp_path / "junk.jsonl"
    junk.write_text("not json\n{}\n", encoding="utf-8")
    assert run(bias_audit, tmp_path / "out", traces=junk) == ERROR
    assert "no usable traces" in capsys.readouterr().err.lower()


def test_argument_errors_exit_1_not_2(bias_audit, tmp_path) -> None:
    # argparse's own exit status is 2, which here means "a four-fifths violation was found".
    assert bias_audit.main(["--traces", str(TRACES)]) == ERROR


def test_a_column_that_gives_everyone_a_different_value_is_an_identifier_not_an_attribute(
    bias_audit, tmp_path, capsys
) -> None:
    rows = [f"{fixture.person_id(i)},label-{i}" for i in range(12)]
    ids = write_demographics(tmp_path / "d.csv", "opaque_id,email", *rows)
    assert run(bias_audit, tmp_path / "out", demographics=ids) == ERROR
    err = capsys.readouterr().err
    assert "identifier" in err and "label-3" not in err
    # a real attribute with few categories over the same people is fine
    ok = write_demographics(
        tmp_path / "ok.csv", "opaque_id,attr", *[f"{fixture.person_id(i)},g{i % 2}" for i in range(12)]
    )
    assert run(bias_audit, tmp_path / "out2", "--min-group-n", "2", demographics=ok) in (PASS, FAIL)


# --- reading trace lines -----------------------------------------------------------------------

GOOD_ID = "0123456789abcdef01234567"


def trace_file(tmp_path: Path, *lines: object, bom: bool = False) -> Path:
    path = tmp_path / "t.jsonl"
    text = "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n"
    path.write_text(text, encoding="utf-8-sig" if bom else "utf-8")
    return path


def rec(**trace) -> dict:
    return {"opaque_id": GOOD_ID, "trace": {"outcome": "SCORED", "calibrated": 80, **trace}}


@pytest.mark.parametrize(
    "line",
    [
        rec(calibrated=101),
        rec(calibrated=-1),
        rec(calibrated=True),
        rec(calibrated="88"),
        rec(calibrated=88.5),
        rec(outcome="MAYBE"),
        rec(outcome=None),
        rec(reasons="UNREADABLE"),
        rec(reasons=[1, 2]),
        {**rec(), "digest": 123},
        {**rec(), "opaque_id": GOOD_ID.upper()},
        {**rec(), "opaque_id": GOOD_ID[:-1]},
        {**rec(), "opaque_id": 44},
        {"opaque_id": GOOD_ID, "trace": "not an object"},
        {"trace": rec()["trace"]},
        [1, 2, 3],
        "a string",
        7,
        None,
        '{"opaque_id": "'
        + GOOD_ID
        + '", "digest": "x", "trace": {"outcome": "SCORED", "calibrated": 80, "raw": NaN}}',
    ],
)
def test_a_trace_line_that_cannot_be_read_is_counted_malformed(bias_audit, tmp_path, line) -> None:
    load = bias_audit.load_traces(trace_file(tmp_path, line, rec()))
    assert (load.lines, load.malformed, load.integrity_failures, len(load.rows)) == (2, 1, 0, 1)


def test_a_minimal_trace_is_enough_and_blank_lines_and_a_bom_are_harmless(bias_audit, tmp_path) -> None:
    minimal = {"opaque_id": GOOD_ID, "trace": {"outcome": "NEEDS_REVIEW"}}
    load = bias_audit.load_traces(trace_file(tmp_path, "", minimal, "   ", rec(), bom=True))
    assert (load.lines, load.malformed, len(load.rows)) == (2, 0, 2)
    assert load.rows[0].calibrated is None and load.rows[0].reasons == ()
    assert load.rows[1].calibrated == 80


def test_the_score_histogram_has_ten_buckets_and_keeps_the_edges(bias_audit) -> None:
    def member(score):
        return bias_audit.TraceRow(GOOD_ID, "SCORED", score, (), None, None, None)

    scores = [0, 9, 10, 19, 20, 89, 90, 99, 100]
    histogram = bias_audit._group_details([member(s) for s in scores])["scores"]["histogram"]
    assert histogram == {
        "0-9": 2,
        "10-19": 2,
        "20-29": 1,
        "30-39": 0,
        "40-49": 0,
        "50-59": 0,
        "60-69": 0,
        "70-79": 0,
        "80-89": 1,
        "90-100": 3,
    }


def test_the_median_of_an_even_count_is_the_mean_of_the_middle_two(bias_audit) -> None:
    rows = [bias_audit.TraceRow(GOOD_ID, "SCORED", s, (), None, None, None) for s in (41, 10, 30, 20)]
    scores = bias_audit._group_details(rows)["scores"]
    assert (scores["median"], scores["mean"], scores["min"], scores["max"]) == (25.0, 25.25, 10, 41)


def test_ratios_in_the_json_are_rounded_to_six_places_and_the_exact_fraction_is_kept(audit) -> None:
    check = audit[1]["attributes"]["attr_pass"]["selection"]["70"]
    assert check["min_ratio"] == 0.833333 and check["min_ratio_exact"] == "5/6"
    assert check["groups"][1]["ratio"] == 0.833333
    assert audit[1]["attributes"]["attr_fail"]["selection"]["70"]["min_ratio"] == 0.857143  # 6/7


def test_blank_lines_in_the_demographics_file_are_skipped(bias_audit, tmp_path) -> None:
    rows = DEMOGRAPHICS.read_text(encoding="utf-8").splitlines()
    spaced = tmp_path / "spaced.csv"
    spaced.write_text("\n".join([*rows[:5], "", ",,", *rows[5:], "", ""]) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    assert run(bias_audit, out, "--min-group-n", "5", demographics=spaced) == FAIL
    assert load(out)["inputs"]["demographics"]["people"] == 24


def test_a_row_with_the_wrong_number_of_columns_stops_the_audit(bias_audit, tmp_path, capsys) -> None:
    short = write_demographics(
        tmp_path / "d.csv", "opaque_id,attr_pass,attr_fail", f"{fixture.person_id(0)},p1"
    )
    assert run(bias_audit, tmp_path / "out", demographics=short) == ERROR
    assert "expected 3 columns" in capsys.readouterr().err


def test_a_group_with_no_score_has_empty_statistics(bias_audit) -> None:
    row = bias_audit.TraceRow(GOOD_ID, "NEEDS_REVIEW", None, ("UNREADABLE",), None, None, None)
    details = bias_audit._group_details([row, row])
    assert details["scores"]["n"] == 0
    assert details["scores"]["mean"] is None and details["scores"]["median"] is None
    assert details["abstention"] == {"count": 2, "rate": 1.0, "reasons": {"UNREADABLE": 2}}
