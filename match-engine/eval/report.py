"""The report a run writes (EVAL section 4): `reports/<UTC timestamp>/`

    summary.md        the metric table (Metric | Baseline | Current | Δ | 95% CI | Verdict), the
                      guardrails, a FAIL section per failed guardrail, layer status, the parser and
                      format-invariance numbers and the top regressions
    metrics.json      everything, machine readable
    regressions.md    pairs that got worse against the baseline, worst first
    failures/         one JSON per failed case, with the inputs to replay it
    config_hash.txt   the config + graph hash that produced the scores, and the weights

Verdicts: PASS / NOISE / FAIL come from `bootstrap_delta` against the baseline; a metric with no
baseline, or whose delta is undefined (`point` None), and a four-fifths check with no eligible group
(`min_ratio` None) read "not assessed", never as a pass. Any failed guardrail gives a FAIL section and
exit code 1.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from eval.baseline import Baseline, metric_deltas, regressions
from eval.metrics import FOUR_FIFTHS_THRESHOLD, four_fifths
from eval.suites import SuiteResult
from eval.suites.synthetic import RANKING_METRICS

TOP_REGRESSIONS = 20
SHORTLIST = 0.70  # four-fifths "selected": the Good band or better (bands.yaml, D-52)


@dataclass
class RunRecord:
    timestamp: datetime
    config_hash: str
    engine_version: str
    graph_version: str
    git_commit: str
    layers: tuple[int, ...]
    suites: list[SuiteResult]
    baseline: Optional[Baseline] = None
    settings: dict[str, Any] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)

    def suite(self, name: str) -> Optional[SuiteResult]:
        return next((s for s in self.suites if s.name == name), None)


# ─── Formatting (EVAL section 4: 0.712, +.036, [+.018, +.054], 2.1%, -0.7%, [-1.3, -0.1]) ───


def _signed(value: float, digits: int) -> str:
    text = f"{value:+.{digits}f}"
    return text.replace("+0.", "+.").replace("-0.", "-.")


def fmt_value(value: Optional[float], *, percent: bool) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    if isinstance(value, float) and math.isinf(value):
        return "unbounded"
    return f"{100 * value:.1f}%" if percent else f"{value:.3f}"


def fmt_delta(value: Optional[float], *, percent: bool) -> str:
    if value is None:
        return "-"
    return f"{100 * value:+.1f}%" if percent else _signed(value, 3)


def fmt_ci(low: Optional[float], high: Optional[float], *, percent: bool, signed: bool) -> str:
    if low is None or high is None:
        return ""
    if percent:
        scale = 100.0
        text = (f"{scale * low:+.1f}", f"{scale * high:+.1f}") if signed else (f"{scale * low:.1f}", f"{scale * high:.1f}")
        return f"[{text[0]}, {text[1]}]"
    if signed:
        return f"[{_signed(low, 3)}, {_signed(high, 3)}]"
    return f"[{low:.3f}, {high:.3f}]"


def verdict_text(delta: Optional[dict[str, Any]]) -> str:
    if delta is None or delta.get("point") is None:
        return "not assessed"
    return "**FAIL**" if delta["verdict"] == "FAIL" else delta["verdict"]


def fairness_status(min_ratio: Optional[float]) -> str:
    if min_ratio is None:
        return "not assessed"
    return "PASS" if min_ratio >= FOUR_FIFTHS_THRESHOLD else "**FAIL**"


# ─── Building the content ──────────────────────────────────────────────────────────────────


def _deltas(run: RunRecord) -> dict[str, dict[str, Any]]:
    synthetic = run.suite("synthetic")
    if run.baseline is None or synthetic is None or not synthetic.pairs:
        return {}
    return metric_deltas(run.baseline.pairs, synthetic.pairs, RANKING_METRICS,
                         n_resamples=int(run.settings.get("n_resamples", 1000)))


def metric_table(run: RunRecord, deltas: dict[str, dict[str, Any]]) -> list[str]:
    synthetic = run.suite("synthetic")
    ranking = (synthetic.metrics.get("ranking") if synthetic else None) or {}
    lines = ["| Metric | Baseline | Current | Δ | 95% CI | Verdict |",
             "|---|---:|---:|---:|---|---|"]
    for spec in RANKING_METRICS:
        current = ranking.get(spec.key, {})
        delta = deltas.get(spec.key)
        if delta is not None:
            base = fmt_value(delta.get("baseline"), percent=spec.percent)
            change = fmt_delta(delta.get("point"), percent=spec.percent)
            interval = fmt_ci(delta.get("low"), delta.get("high"), percent=spec.percent, signed=True)
        else:
            base, change = "-", "-"
            interval = fmt_ci(current.get("low"), current.get("high"), percent=spec.percent, signed=False)
        lines.append(f"| {spec.label} | {base} | {fmt_value(current.get('point'), percent=spec.percent)} | "
                     f"{change} | {interval} | {verdict_text(delta)} |")
    fairness = four_fifths(synthetic.pairs, lambda p: p.score is not None and p.score >= SHORTLIST) \
        if synthetic else None
    ratio = fairness.min_ratio if fairness else None
    lines.append(f"| Four-fifths (min) | - | {fmt_value(ratio, percent=False)} | - |  | {fairness_status(ratio)} |")
    return lines


def _guardrails(run: RunRecord) -> list[Any]:
    return [g for s in run.suites for g in s.guardrails]


def _status(passed: Optional[bool]) -> str:
    return "not assessed" if passed is None else ("PASS" if passed else "**FAIL**")


def _value(value: Optional[float]) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and math.isinf(value):
        return "unbounded"
    return f"{value:.4g}"


def _cell(text: str) -> str:
    return str(text).replace("|", r"\|")


def _case_file(case: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", case)[:150] + ".json"


def summary_markdown(run: RunRecord, deltas: dict[str, dict[str, Any]], found: list[Any]) -> str:
    stamp = run.timestamp.strftime("%Y-%m-%dT%H:%MZ")
    against = f"vs baseline {run.baseline.label}" if run.baseline else "no baseline"
    out = [f"# Eval - {stamp} - config {run.config_hash[:7]} - {against}", ""]
    out += metric_table(run, deltas)
    out += ["", "Ranking metrics are over the synthetic corpus (oracle résumés vs their target postings, "
            "fit level as relevance); CIs are 1,000 bootstrap resamples over postings. Abstention rate sits "
            "next to Precision@10: P@10 skips abstained pairs. A CI that includes zero is noise.", ""]
    guards = _guardrails(run)
    out += ["## Guardrails", "", "| Guardrail | Value | Threshold | Status | Detail |", "|---|---:|---|---|---|"]
    out += [f"| {_cell(g.name)} | {_value(g.value)} | {_cell(g.threshold)} | {_status(g.passed)} | {_cell(g.detail)} |"
            for g in guards]
    out.append("")
    for suite in run.suites:
        for g in suite.guardrails:
            if g.passed is False:
                cases = [f.case for f in suite.failures if not g.kind or f.kind == g.kind]
                out += [f"## FAIL: {g.name}", "", f"{_value(g.value)} against {g.threshold}. {g.detail}"]
                if cases:
                    shown = ", ".join(f"failures/{_case_file(c)}" for c in cases[:10])
                    out.append(f"Failure cases ({len(cases)} in this suite): {shown}")
                out.append("")
    out += ["## Layers", ""]
    for suite in run.suites:
        if suite.status == "skipped":
            out.append(f"- Layer {suite.layer} {suite.name}: skipped ({suite.reason})")
        else:
            out.append(f"- Layer {suite.layer} {suite.name}: ran in {suite.seconds:.1f} s")
        out += [f"  - {note}" for note in suite.notes]
    out.append("")
    synthetic = run.suite("synthetic")
    parser = synthetic.metrics.get("parser") if synthetic else None
    if isinstance(parser, dict):
        out += ["## Parser fidelity and format invariance", "", "| Measure | Value |", "|---|---:|"]
        out += [f"| {k} | {_value(v) if isinstance(v, (int, float)) else v} |" for k, v in parser.items()]
        worst = synthetic.metrics.get("parser_worst", {})
        for title, key, field_name in (("Worst within-template spreads", "within_template", "spread"),
                                       ("Worst date errors", "date_error", "error"),
                                       ("Lowest skill recovery", "recovery", "recovered")):
            rows = worst.get(key, [])[:5]
            if rows:
                out += ["", f"{title}:"]
                for row in rows:
                    extra = row.get("notes") or row.get("missing") or row.get("raws") or ""
                    out.append(f"- {row['case']}: {_value(row[field_name])} {json.dumps(extra, default=str)[:300]}")
        out.append("")
    out += [f"## Top regressions ({len(found)})", ""]
    if run.baseline is None:
        out.append("No baseline given (`--baseline NAME`).")
    elif not found:
        out.append("None.")
    out += [f"{i}. {r.line()}" for i, r in enumerate(found[:TOP_REGRESSIONS], start=1)]
    return "\n".join(out) + "\n"


def _jsonable(value: Any) -> Any:
    if isinstance(value, float) and (math.isinf(value) or math.isnan(value)):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def build_metrics(run: RunRecord, deltas: dict[str, Any], found: list[Any], exit_code: int) -> dict[str, Any]:
    return _jsonable({
        "timestamp": run.timestamp.isoformat(),
        "config_hash": run.config_hash,
        "engine_version": run.engine_version,
        "graph_version": run.graph_version,
        "git_commit": run.git_commit,
        "layers": list(run.layers),
        "settings": run.settings,
        "baseline": run.baseline.label if run.baseline else None,
        "exit_code": exit_code,
        "suites": {s.name: {"layer": s.layer, "status": s.status, "reason": s.reason, "seconds": s.seconds,
                            "metrics": s.metrics, "notes": s.notes,
                            "guardrails": [g.to_dict() for g in s.guardrails],
                            "failures": [f.case for f in s.failures]} for s in run.suites},
        "deltas": deltas,
        "regressions": [r.__dict__ for r in found],
    })


def write_report(run: RunRecord, out_root: Path) -> tuple[Path, int]:
    folder = out_root / run.timestamp.strftime("%Y-%m-%dT%H%M%SZ")
    (folder / "failures").mkdir(parents=True, exist_ok=True)
    deltas = _deltas(run)
    synthetic = run.suite("synthetic")
    found = regressions(run.baseline.pairs, synthetic.pairs) if run.baseline and synthetic else []
    exit_code = 1 if any(g.passed is False for g in _guardrails(run)) else 0
    (folder / "summary.md").write_bytes(summary_markdown(run, deltas, found).encode("utf-8"))
    (folder / "metrics.json").write_bytes(
        (json.dumps(build_metrics(run, deltas, found, exit_code), indent=2, default=str) + "\n").encode("utf-8"))
    lines = [f"# Regressions vs {run.baseline.label if run.baseline else '(no baseline)'} ({len(found)})", ""]
    lines += [f"{i}. {r.line()}" for i, r in enumerate(found, start=1)] or ["None."]
    (folder / "regressions.md").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    for suite in run.suites:
        for failure in suite.failures:
            payload = {"case": failure.case, "kind": failure.kind, "suite": suite.name, "detail": failure.detail}
            (folder / "failures" / _case_file(failure.case)).write_bytes(
                (json.dumps(_jsonable(payload), indent=2, default=str) + "\n").encode("utf-8"))
    hash_lines = [run.config_hash, f"engine_version {run.engine_version}", f"graph_version {run.graph_version}"]
    hash_lines += [f"weight {k} {v}" for k, v in sorted(run.weights.items())]
    (folder / "config_hash.txt").write_bytes(("\n".join(hash_lines) + "\n").encode("utf-8"))
    return folder, exit_code
