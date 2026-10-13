"""Tuning proposals (EVAL.md section 5.1): `proposals/<date>-tune/`, never `config/`.

    weights.diff     current vs proposed config/weights.yaml (a reviewer applies it by hand)
    rationale.md     which dimensions moved, which cases improved, which broke (the human-signed draft)
    report.md        the search, the metric table with bootstrap CIs, the accept rules
    sensitivity.md   +/-20 % perturbation of the proposed vector (SPEC section 10)
    proposal.json    the same numbers for tools (`eval.holdout evaluate --proposal` reads it)

Accept rules (EVAL 5.3), evaluated and printed, never acted on:

* rule 2: the NDCG@10 improvement's 95 % CI excludes zero AND the point estimate exceeds +0.01;
* rule 4: dev gain > 2 x holdout gain -> OVERFIT_SUSPECTED, reject. The holdout gain exists only
  inside CI on a release branch (`make eval-holdout PROPOSAL=...`); here it reads "not assessed".

A synthetic-only proposal and one whose fairness was not assessed are never adoptable
(DECISIONS D-119), whatever the numbers say.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

from eval.tune import constraints

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
SYNTHETIC_BANNER = "synthetic-only: do not adopt"
FAIRNESS_BANNER = "fairness not assessed: proposal cannot be adopted until a grouped labelled set exists"
MIN_GAIN = 0.01  # EVAL 5.3 rule 2
OVERFIT_FACTOR = 2.0  # EVAL 5.3 rule 4
TOP_CASES = 20


# ─── accept rules ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AcceptDecision:
    dev_delta: Mapping[str, Optional[float]]
    holdout_gain: Optional[float]
    improvement_real: bool
    overfit_suspected: Optional[bool]  # None: not assessed (no holdout look)
    synthetic_only: bool
    fairness_assessed: bool

    @property
    def verdict(self) -> str:
        if not self.improvement_real or self.overfit_suspected:
            return "REJECT"
        if self.synthetic_only or not self.fairness_assessed:
            return "NOT ADOPTABLE"
        return "READY FOR HUMAN REVIEW"

    def lines(self) -> list[str]:
        point, low, high = (self.dev_delta.get(k) for k in ("point", "low", "high"))
        if point is None or low is None or high is None:
            gain = "NDCG@10 gain undefined"
        else:
            gain = f"NDCG@10 gain {point:+.4f}, 95% CI [{low:+.4f}, {high:+.4f}]"
        out = [
            f"Rule 2 (CI excludes 0 and point > +{MIN_GAIN:.2f}): "
            f"{'PASS' if self.improvement_real else 'FAIL'}: {gain}"
            + ("" if self.improvement_real else "; the improvement is not real (noise or too small)"),
        ]
        if self.overfit_suspected is None:
            out.append("Rule 4 (dev gain > 2x holdout gain): not assessed: the holdout is evaluated only in CI on a "
                       "release branch (`make eval-holdout PROPOSAL=<folder>`)")
        else:
            out.append(f"Rule 4 (dev gain > 2x holdout gain): "
                       f"{'OVERFIT_SUSPECTED, reject' if self.overfit_suspected else 'PASS'}: "
                       f"dev {point:+.4f} vs holdout {self.holdout_gain:+.4f}")
        if self.synthetic_only:
            out.append(SYNTHETIC_BANNER)
        if not self.fairness_assessed:
            out.append(FAIRNESS_BANNER)
        out.append(f"Verdict: {self.verdict}")
        return out

    def text(self) -> str:
        return "\n".join(self.lines())


def accept_rules(dev_delta: Mapping[str, Optional[float]], *, holdout_gain: Optional[float], synthetic_only: bool,
                 fairness_assessed: bool) -> AcceptDecision:
    point, low = dev_delta.get("point"), dev_delta.get("low")
    real = point is not None and low is not None and low > 0.0 and point > MIN_GAIN
    if holdout_gain is None or point is None:
        overfit: Optional[bool] = None
    else:
        overfit = point > OVERFIT_FACTOR * holdout_gain
    return AcceptDecision(dict(dev_delta), holdout_gain, real, overfit, synthetic_only, fairness_assessed)


def ndcg_delta(result: Any, *, n_resamples: int) -> dict[str, Any]:
    from eval.baseline import metric_deltas
    from eval.suites.synthetic import RANKING_METRICS

    spec = [s for s in RANKING_METRICS if s.key == "ndcg@10"]
    return metric_deltas(result.baseline.pairs, result.proposed_evaluation.pairs, spec, n_resamples=n_resamples)[
        "ndcg@10"]


def decision_for(result: Any, *, n_resamples: int, holdout_gain: Optional[float] = None) -> AcceptDecision:
    return accept_rules(ndcg_delta(result, n_resamples=n_resamples), holdout_gain=holdout_gain,
                        synthetic_only=result.synthetic_only, fairness_assessed=result.fairness_assessed)


# ─── weights.yaml text ─────────────────────────────────────────────────────────────────────

_LINE = re.compile(r"^(?P<indent>\s+)(?P<name>[a-z_]+):(?P<gap>\s*)(?P<value>[0-9.]+)(?P<rest>.*)$")


def proposed_yaml(current_text: str, proposed: Mapping[str, float]) -> str:
    """`current_text` with each changed dimension's number replaced (4 decimals); comments kept."""
    out = []
    for line in current_text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        m = _LINE.match(body)
        if m and m.group("name") in proposed and float(m.group("value")) != proposed[m.group("name")]:
            value = f"{proposed[m.group('name')]:.4f}"
            body = f"{m.group('indent')}{m.group('name')}:{m.group('gap')}{value}{m.group('rest')}"
        out.append(body + ending)
    return "".join(out)


def weights_diff(current_text: str, proposed: Mapping[str, float], header: Sequence[str] = ()) -> str:
    """The unified diff, after `# ` comment lines (`git apply` ignores text before the first `---`), so
    the do-not-adopt markers travel with the diff itself."""
    new = proposed_yaml(current_text, proposed)
    diff = "".join(difflib.unified_diff(current_text.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile="a/match-engine/config/weights.yaml",
                                        tofile="b/match-engine/config/weights.yaml"))
    prefix = "".join(f"# {line}\n" for line in header)
    return prefix + (diff or "# no change proposed: no feasible vector beat the current weights\n")


# ─── writing ───────────────────────────────────────────────────────────────────────────────


def _inside(path: Path, folder: Path) -> bool:
    path, folder = path.resolve(), folder.resolve()
    return path == folder or folder in path.parents


def _fmt(value: Optional[float], digits: int = 4) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _banners(result: Any) -> list[str]:
    out = []
    if result.synthetic_only:
        out.append(f"> **{SYNTHETIC_BANNER}.** Tuned on the synthetic split only (no golden or dev labels exist). "
                   "The synthetic labels are constructed fit levels, not human judgements or outcomes.")
    if not result.fairness_assessed:
        out.append(f"> **{FAIRNESS_BANNER}.** The four-fifths guardrail needs groups of n >= 100; these pairs "
                   "carry no group (DECISIONS D-119).")
    return [line for banner in out for line in (banner, "")][:-1]


def _show(value: Optional[float]) -> str:
    return "abstained" if value is None else f"{value:.3f}"


def _case_lines(items: Sequence[Any], *, improved: bool) -> list[str]:
    lines = []
    for r in items[:TOP_CASES]:
        # `regressions(a, b)` lists pairs that got worse going from a to b. For improvements it is called
        # with (proposed, current), so its `after` is the current score and its `before` the proposed one.
        current, proposed = (r.after, r.before) if improved else (r.before, r.after)
        lines.append(f"{len(lines) + 1}. {r.pair_id}  {_show(current)} -> {_show(proposed)}  fit {r.relevance}, "
                     f"posting {r.posting_id}")
    if len(items) > TOP_CASES:
        lines.append(f"... and {len(items) - TOP_CASES} more")
    return lines or ["(none)"]


def write_proposal(result: Any, evaluator: Any, *, out_root: Path, day: date, sensitivity_rows: Sequence[Any],
                   n_resamples: int, wall_seconds: float, config_dir: Optional[Path] = None,
                   force: bool = False) -> Path:
    """Write `out_root/<day>-tune/`. Reads `config_dir/weights.yaml` (default `CONFIG_DIR`), never writes
    under it. An existing folder (perhaps holding a human-signed rationale.md) is refused unless `force`;
    a target file that is a symlink or resolves outside the folder is always refused."""
    from eval.baseline import metric_deltas, regressions
    from eval.report import fmt_ci, fmt_delta, fmt_value, verdict_text
    from eval.suites.synthetic import RANKING_METRICS
    from eval.tune import sensitivity

    config_dir = Path(config_dir) if config_dir is not None else CONFIG_DIR
    out_root = Path(out_root)
    if _inside(out_root, config_dir):
        raise ValueError(f"refusing to write a proposal under {config_dir}: proposals never touch config/")
    folder = out_root / f"{day.isoformat()}-tune"
    if _inside(folder, config_dir):
        raise ValueError(f"refusing to write a proposal under {config_dir}")
    if folder.exists() and not force:
        raise FileExistsError(f"refusing to overwrite {folder}: it may hold a signed rationale.md "
                              "(pass --force to replace it)")
    current_text = (config_dir / "weights.yaml").read_text(encoding="utf-8")
    current, proposed = result.current, result.proposed_weights
    base_pairs, new_pairs = result.baseline.pairs, result.proposed_evaluation.pairs
    deltas = metric_deltas(base_pairs, new_pairs, RANKING_METRICS, n_resamples=n_resamples)
    decision = accept_rules(deltas["ndcg@10"], holdout_gain=None, synthetic_only=result.synthetic_only,
                            fairness_assessed=result.fairness_assessed)
    broke = regressions(base_pairs, new_pairs)
    improved = regressions(new_pairs, base_pairs)
    banners = _banners(result)
    changed = result.best is not None and proposed != current
    rejected_bounds = sum(1 for t in result.trials if t.objective is None)
    abstain_bad = sum(1 for t in result.trials if any("abstention" in r for r in t.feasibility.reasons))

    files: dict[str, str] = {}
    header = [*([SYNTHETIC_BANNER] if result.synthetic_only else []),
              *([FAIRNESS_BANNER] if not result.fairness_assessed else []),
              f"verdict: {decision.verdict} (report.md, EVAL 5.3); never apply without a signed rationale.md"]
    files["weights.diff"] = weights_diff(current_text, proposed, header)

    moved = [d for d in constraints.DIMENSIONS if proposed[d] != current[d]]
    rationale = [f"# Tuning rationale draft, {day.isoformat()}", "", *banners, "",
                 "A human reviewer signs off on every weight change (EVAL 5.2: NYC LL144 and the EU AI Act need a "
                 "described and justified decision logic). This file is the draft of that justification.", "",
                 "## Dimensions moved", ""]
    if not changed:
        rationale += ["No change proposed: no feasible trial beat the current weights.", ""]
    rationale += ["| Dimension | Current | Proposed | Change |", "|---|---:|---:|---:|"]
    for d in constraints.DIMENSIONS:
        delta = proposed[d] - current[d]
        rationale.append(f"| {d} | {current[d]:.4f} | {proposed[d]:.4f} | {delta:+.4f} |")
    rationale += ["", ("Moved: " + ", ".join(f"{d} {'up' if proposed[d] > current[d] else 'down'}" for d in moved)
                       + ".") if moved else "Nothing moved.", "",
                  f"## Cases improved ({len(improved)})", "",
                  "Pairs whose score moved toward their label by more than 0.005 (an advanced pair up, a "
                  "non-advanced pair down), largest move first. Current -> proposed.", "",
                  *_case_lines(improved, improved=True), "",
                  f"## Cases broken ({len(broke)})", "",
                  "Pairs that regress under the proposal (DECISIONS D-115), worst first. Current -> proposed. Report "
                  "these even when the headline metric improves (EVAL 7).", "",
                  *_case_lines(broke, improved=False), "",
                  "## Why (reviewer to complete)", "",
                  "- Which product behaviour motivates each move:", "- Which broken cases are acceptable, and why:",
                  "- Sensitivity concerns (see sensitivity.md):", "",
                  "Reviewer: ____________  Date: ____________  Decision: adopt / reject", ""]
    files["rationale.md"] = "\n".join(rationale)

    table = ["| Metric | Current | Proposed | Δ | 95% CI | Verdict |", "|---|---:|---:|---:|---|---|"]
    for spec in RANKING_METRICS:
        d = deltas[spec.key]
        table.append(f"| {spec.label} | {fmt_value(spec.fn(base_pairs), percent=spec.percent)} | "
                     f"{fmt_value(spec.fn(new_pairs), percent=spec.percent)} | "
                     f"{fmt_delta(d['point'], percent=spec.percent)} | "
                     f"{fmt_ci(d['low'], d['high'], percent=spec.percent, signed=True)} | {verdict_text(d)} |")
    top = sorted(result.feasible, key=lambda t: (-(t.objective or 0.0), t.number))[:10]
    fairness = result.proposed_evaluation.fairness
    report = [f"# Tuning report, {day.isoformat()}", "", *banners, "",
              "## Search", "",
              f"- Split: {result.split} ({len(base_pairs)} pairs, {len({p.posting_id for p in base_pairs})} postings)",
              "- Objective: NDCG@10 (maximise)",
              f"- Sampler: Optuna TPE, seed {result.seed}, budget {result.budget} trials",
              f"- Trials: {len(result.trials)}; rejected before scoring (a weight above 0.45): {rejected_bounds}; "
              f"infeasible on abstention > 15%: {abstain_bad}; infeasible on fairness FAIL: "
              f"{result.fairness_failures}; feasible: {len(result.feasible)}",
              f"- Search time {result.seconds:.1f} s; wall time including sensitivity {wall_seconds:.1f} s",
              *[f"- Note: {n}" for n in result.notes], "",
              "## Current vs proposed", "", *table, "",
              f"Bootstrap: {n_resamples} resamples over postings. A CI that includes zero is noise.", "",
              "## Constraints on the proposed vector (EVAL 5.1)", "",
              f"- Weights in [0.03, 0.45], sum 1: {'yes' if not constraints.bound_violations(proposed) else 'NO'}",
              f"- Abstention rate {_fmt(result.proposed_evaluation.abstention_rate, 3)} (max 0.15)",
              f"- Four-fifths: {constraints.fairness_status(fairness)} ({fairness.detail})",
              f"- Current weights: {'feasible' if result.baseline_feasibility.feasible else 'infeasible: ' + '; '.join(result.baseline_feasibility.reasons)}",
              "", "## Accept rules (EVAL 5.3)", "", *[f"- {line}" for line in decision.lines()], "",
              "## Best feasible trials", "", "| Trial | NDCG@10 | Abstention | " +
              " | ".join(constraints.DIMENSIONS) + " |", "|---:|---:|---:|" + "---:|" * len(constraints.DIMENSIONS)]
    for t in top:
        report.append(f"| {t.number} | {_fmt(t.objective)} | {_fmt(t.feasibility.abstention_rate, 3)} | "
                      + " | ".join(f"{t.weights[d]:.4f}" for d in constraints.DIMENSIONS) + " |")
    report.append("")
    files["report.md"] = "\n".join(report)
    files["sensitivity.md"] = sensitivity.markdown(sensitivity_rows, proposed, pairs_label=f"{result.split} pair")
    summary = {
        "date": day.isoformat(), "split": result.split, "synthetic_only": result.synthetic_only,
        "fairness_assessed": result.fairness_assessed, "seed": result.seed, "budget": result.budget,
        "current": current, "proposed": proposed, "changed": changed,
        "dev_gain": deltas["ndcg@10"], "verdict": decision.verdict,
        "over_leveraged": sorted({r.dimension for r in sensitivity_rows if r.over_leveraged}),
    }
    files["proposal.json"] = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    folder.mkdir(parents=True, exist_ok=True)
    targets = {name: folder / name for name in files}
    for target in targets.values():
        if target.is_symlink() or not _inside(target, folder) or _inside(target, config_dir):
            raise ValueError(f"refusing to write {target}: a symlink or a path outside {folder}")
    for name, target in targets.items():
        target.write_text(files[name], encoding="utf-8", newline="\n")
    return folder
