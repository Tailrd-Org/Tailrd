"""Report writer, baselines and the run CLI (Task 5.3)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from eval import baseline, report, run
from eval.metrics import PairScore
from eval.suites import Failure, Guardrail, SuiteResult
from eval.suites.synthetic import RANKING_METRICS

STAMP = datetime(2026, 10, 12, 14, 22, tzinfo=timezone.utc)


def _pairs(shift: float = 0.0, postings: int = 6) -> list[PairScore]:
    return [PairScore(posting_id=f"post{k}", pair_id=f"post{k}-{r}", score=min(1.0, 0.2 + 0.2 * r + shift),
                      relevance=r, advanced=r >= 2, band_pred=r, band_true=r, hard=r in (1, 2))
            for k in range(postings) for r in range(4)]


def _record(suites, base=None) -> report.RunRecord:
    return report.RunRecord(timestamp=STAMP, config_hash="a3f9c21", engine_version="1.0.0", graph_version="g1",
                            git_commit="0411563", layers=(1, 2), suites=suites, baseline=base)


def _synthetic(pairs, guardrails=(), failures=()) -> SuiteResult:
    from eval.suites.synthetic import ranking_metrics

    suite = SuiteResult(name="synthetic", layer=2, pairs=list(pairs), guardrails=list(guardrails),
                        failures=list(failures))
    suite.metrics["ranking"] = ranking_metrics(pairs, n_resamples=50)
    return suite


# ─── formatting ────────────────────────────────────────────────────────────────────────────


def test_numbers_are_formatted_as_in_eval_section_4() -> None:
    assert report.fmt_value(0.7123, percent=False) == "0.712"
    assert report.fmt_value(0.021, percent=True) == "2.1%"
    assert report.fmt_delta(0.036, percent=False) == "+.036"
    assert report.fmt_delta(-0.007, percent=True) == "-0.7%"
    assert report.fmt_ci(0.018, 0.054, percent=False, signed=True) == "[+.018, +.054]"
    assert report.fmt_ci(-0.013, -0.001, percent=True, signed=True) == "[-1.3, -0.1]"
    assert report.fmt_value(None, percent=False) == "-"


def test_undefined_deltas_and_unassessed_fairness_are_not_green() -> None:
    assert report.verdict_text(None) == "not assessed"
    assert report.verdict_text({"point": None, "low": None, "high": None, "verdict": "NOISE"}) == "not assessed"
    assert report.verdict_text({"point": 0.02, "low": 0.01, "high": 0.03, "verdict": "PASS"}) == "PASS"
    assert report.verdict_text({"point": -0.02, "low": -0.03, "high": -0.01, "verdict": "FAIL"}) == "**FAIL**"
    assert report.fairness_status(None) == "not assessed"
    assert report.fairness_status(0.79) == "**FAIL**" and report.fairness_status(0.80) == "PASS"


# ─── writing a report ──────────────────────────────────────────────────────────────────────


def test_a_clean_run_writes_every_file_and_exits_zero(tmp_path) -> None:
    suites = [SuiteResult(name="properties", layer=1, guardrails=[Guardrail("Totality", True, 0.0, "0 exceptions")]),
              _synthetic(_pairs())]
    folder, code = report.write_report(_record(suites), tmp_path)
    assert code == 0
    assert folder.name == "2026-10-12T142200Z"
    for name in ("summary.md", "metrics.json", "regressions.md", "config_hash.txt"):
        assert (folder / name).exists(), name
    assert (folder / "failures").is_dir()
    summary = (folder / "summary.md").read_text(encoding="utf-8")
    assert summary.startswith("# Eval - 2026-10-12T14:22Z - config a3f9c21 - no baseline")
    assert "| Metric | Baseline | Current | Δ | 95% CI | Verdict |" in summary
    assert "| Four-fifths (min) |" in summary and "not assessed" in summary
    assert "## FAIL" not in summary and "**FAIL**" not in summary
    rows = [line for line in summary.splitlines() if line.startswith("| ")]
    labels = [line.split("|")[1].strip() for line in rows]
    assert labels.index("Abstention rate") == labels.index("Precision@10") + 1  # next to P@10
    metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["config_hash"] == "a3f9c21" and metrics["exit_code"] == 0
    assert (folder / "config_hash.txt").read_text(encoding="utf-8").startswith("a3f9c21")


def test_a_guardrail_failure_is_a_fail_section_and_exit_one(tmp_path) -> None:
    failing = Guardrail("Format invariance: formats within a template", False, 0.31, "p99 <= 0.02", "worst p0001",
                        "invariance within")
    other = Failure("totality__1", "totality", {})
    failure = Failure("invariance__p0001__chronological", "invariance within", {"spread": 0.31})
    suites = [_synthetic(_pairs(), guardrails=[failing], failures=[failure, other])]
    folder, code = report.write_report(_record(suites), tmp_path)
    assert code == 1
    summary = (folder / "summary.md").read_text(encoding="utf-8")
    assert "## FAIL: Format invariance: formats within a template" in summary
    case = json.loads((folder / "failures" / "invariance__p0001__chronological.json").read_text(encoding="utf-8"))
    assert case["kind"] == "invariance within" and case["detail"]["spread"] == 0.31
    fail_section = summary.split("## FAIL:")[1].split("## ")[0]
    assert "totality__1" not in fail_section  # only the cases behind this guardrail


def test_skipped_layers_are_reported_by_name(tmp_path) -> None:
    from eval.suites import skipped

    golden = skipped("golden", 3, "no labels")
    golden.notes.append("GOLDEN: skipped (no labels)")
    folder, _ = report.write_report(_record([_synthetic(_pairs()), golden]), tmp_path)
    assert "GOLDEN: skipped (no labels)" in (folder / "summary.md").read_text(encoding="utf-8")


def test_a_baseline_diff_gives_verdicts_and_worst_first_regressions(tmp_path) -> None:
    before = _pairs()
    after = [p.model_copy(update={"score": p.score - (0.3 if p.pair_id == "post2-3" else 0.0)
                                  + (0.1 if p.pair_id == "post4-3" else 0.0)}) for p in before]
    after[0] = after[0].model_copy(update={"score": None})  # post0-0 now abstains
    base = baseline.Baseline("main", {"meta": {"git_commit": "8e2d1aa"}}, tuple(before))
    folder, _ = report.write_report(_record([_synthetic(after)], base), tmp_path)
    summary = (folder / "summary.md").read_text(encoding="utf-8")
    assert summary.startswith("# Eval - 2026-10-12T14:22Z - config a3f9c21 - vs baseline main@8e2d1aa")
    regressions = (folder / "regressions.md").read_text(encoding="utf-8")
    assert regressions.index("post0-0") < regressions.index("post2-3")  # a new abstention counts as worst
    assert "post4-3" not in regressions  # rising for an advanced pair is not a regression
    assert "## Top regressions (2)" in summary


# ─── baselines ─────────────────────────────────────────────────────────────────────────────


def test_baseline_round_trip(tmp_path) -> None:
    pairs = _pairs()
    folder = baseline.save_baseline("main", {"meta": {"git_commit": "abc1234"}, "x": 1}, pairs, root=tmp_path)
    loaded = baseline.load_baseline("main", root=tmp_path)
    assert folder == tmp_path / "main" and loaded.label == "main@abc1234"
    assert sorted(loaded.pairs, key=lambda p: p.pair_id) == sorted(pairs, key=lambda p: p.pair_id)
    with pytest.raises(FileNotFoundError):
        baseline.load_baseline("nope", root=tmp_path)


def test_metric_deltas_use_each_metrics_min_effect() -> None:
    worse = [p.model_copy(update={"score": 1.0 - p.score}) for p in _pairs(postings=30)]
    deltas = baseline.metric_deltas(_pairs(postings=30), worse, RANKING_METRICS, n_resamples=100)
    assert deltas["ndcg@10"]["verdict"] == "FAIL" and deltas["ndcg@10"]["point"] < 0
    same = baseline.metric_deltas(_pairs(postings=30), _pairs(postings=30), RANKING_METRICS, n_resamples=100)
    assert same["ndcg@10"]["verdict"] == "NOISE" and same["ndcg@10"]["point"] == 0.0
    assert same["abstention_precision"]["point"] is None  # undefined: no abstentions on either side


def test_regressions_move_away_from_the_label() -> None:
    before = _pairs(postings=1)
    after = [p.model_copy(update={"score": p.score + (0.2 if p.relevance == 0 else -0.004)}) for p in before]
    found = baseline.regressions(before, after)
    assert [r.pair_id for r in found] == ["post0-0"] and found[0].severity == pytest.approx(0.2)


# ─── the CLI ───────────────────────────────────────────────────────────────────────────────


def test_cli_arguments() -> None:
    args = run.parse_args(["--layers", "1,2"])
    assert args.layers == (1, 2) and not run.full_mode(args) and args.baseline is None
    args = run.parse_args(["--layers", "1,2,3", "--baseline", "main", "--save-baseline", "main", "--sample", "4"])
    assert args.layers == (1, 2, 3) and run.full_mode(args) and args.sample == 4
    assert run.settings(run.parse_args(["--layers", "1,2"]))["fuzz_n"] == 5000
    assert run.settings(run.parse_args(["--layers", "1,2,3"]))["fuzz_n"] == 50000
    assert run.settings(run.parse_args(["--layers", "1,2"]))["parser_profiles"] == 10
    with pytest.raises(SystemExit):
        run.parse_args(["--layers", "1,5"])
