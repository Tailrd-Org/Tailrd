"""`make tune`: constrained weight search that writes a proposal and never edits `config/`.

    python -m eval.tune.search --budget 200 --objective ndcg@10 --split dev [--seed 0] [--out proposals]

Optuna TPE (seeded) over the 7 dimension weights (EVAL.md section 5.1). Each trial samples a point
in (0, 1]^7, maps it onto the simplex with every weight >= 0.03 (`constraints.from_unit`), and
rejects a vector with a weight above 0.45 without scoring it. A scored trial is infeasible when the
four-fifths ratio fails or more than 15 % of pairs abstain; fairness that cannot be assessed leaves
it feasible and marks the proposal as not adoptable (DECISIONS D-119). A fairness FAIL on the
current weights is terminal: the search stops before any trial.

The objective is NDCG@10 on the chosen split: `dev` when `eval/corpora/dev/pairs.jsonl` exists,
otherwise the synthetic layer's ranking pairs (500 oracle résumés against their target postings,
scored by the real engine), and the proposal is then marked "synthetic-only: do not adopt". Weights
do not reach the parser, so no trial parses anything (DECISIONS D-120).

Output: `proposals/<date>-tune/` (weights.diff, rationale.md, report.md, sensitivity.md and
proposal.json), see `eval.tune.proposal`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "src") not in sys.path:  # `python -m eval.tune.search` from match-engine/ without an install
    sys.path.insert(0, str(_ROOT / "src"))

from eval.metrics import PairScore, abstention_stats, ndcg_at_k  # noqa: E402
from eval.suites import BULK, Guardrail, fairness_guardrail, pair_record  # noqa: E402
from eval.tune import constraints  # noqa: E402

OBJECTIVES = ("ndcg@10",)
SPLITS = ("dev", "synthetic")
DEV_LABELS = _ROOT / "eval" / "corpora" / "dev" / "pairs.jsonl"
PROPOSALS_DIR = _ROOT / "proposals"


# ─── evaluation of one weight vector ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Evaluation:
    pairs: list[PairScore]
    ndcg: Optional[float]
    abstention_rate: Optional[float]
    fairness: Guardrail


def evaluation_of(pairs: list[PairScore]) -> Evaluation:
    return Evaluation(pairs, ndcg_at_k(pairs, 10), abstention_stats(pairs)["rate"], fairness_guardrail(pairs))


def _unchecked_config(config: Any, weights: Mapping[str, float]) -> Any:
    """A config holding `weights` without the [0.03, 0.45] check: sensitivity analysis perturbs
    vectors past the tuning bounds and says so (`sensitivity.Row.outside_bounds`). Never persisted,
    never used by the search."""
    from matcher.config import EngineConfig, Weights
    from matcher.models import DimensionName

    dims = {DimensionName(k): float(v) for k, v in weights.items()}
    fields = {name: getattr(config, name) for name in EngineConfig.model_fields}
    fields["weights"] = Weights.model_construct(dimensions=dims)
    return EngineConfig.model_construct(**fields)


class EngineEvaluator:
    """Scores fixed (résumé, posting, label) items with the real engine under any weight vector.

    Items are `(posting_id, pair_id, resume, posting, as_of, relevance, advanced, group)`."""

    def __init__(self, split: str, items: Sequence[tuple], *, synthetic_only: bool) -> None:
        from eval.suites import real_engine

        self.split = split
        self.items = list(items)
        self.synthetic_only = synthetic_only
        self.engine = real_engine()

    @classmethod
    def synthetic(cls, limit: Optional[int] = None) -> "EngineEvaluator":
        from eval.suites import corpus_id_map, corpus_pairs, synthetic_corpus

        corpus = synthetic_corpus()
        pairs = corpus_pairs(corpus, corpus_id_map())
        if limit is not None:
            pairs = pairs[:limit]
        items = [(cp.posting_id, cp.case, cp.resume, cp.posting, corpus.as_of, cp.profile.fit_level, None, None)
                 for cp in pairs]
        return cls("synthetic", items, synthetic_only=True)

    @classmethod
    def dev(cls, path: Path = DEV_LABELS) -> "EngineEvaluator":
        """`eval/corpora/dev/pairs.jsonl` (format: that folder's README). Relevance from the outcome:
        hired 3, advanced 2, anything else 0."""
        from matcher.models import JobPosting, Resume

        items = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            outcome = str(row.get("outcome") or "").lower()
            advanced = bool(row.get("advanced")) or outcome in ("advanced", "hired")
            relevance = 3 if outcome == "hired" else 2 if advanced else 0
            items.append((row["posting_id"], row["pair_id"], Resume.model_validate(row["resume"]),
                          JobPosting.model_validate(row["posting"]), date.fromisoformat(row["as_of"]),
                          relevance, advanced, row.get("group")))
        return cls("dev", items, synthetic_only=False)

    def _engine(self, weights: Mapping[str, float], *, checked: bool) -> Any:
        from eval.suites import Engine
        from matcher.config import replace_weights

        config = (replace_weights(self.engine.config, dict(weights)) if checked
                  else _unchecked_config(self.engine.config, weights))
        return Engine(config, self.engine.graph, self.engine.calibration)

    def pairs(self, weights: Mapping[str, float], *, checked: bool = False) -> list[PairScore]:
        engine = self._engine(weights, checked=checked)
        out = []
        for posting_id, pair_id, resume, posting, as_of, relevance, advanced, group in self.items:
            record = pair_record(posting_id, pair_id, engine.score(resume, posting, as_of=as_of, options=BULK),
                                 min(3, relevance))
            if advanced is not None or group is not None:
                record = record.model_copy(update={"advanced": advanced if advanced is not None else record.advanced,
                                                   "group": group, "hard": False})
            out.append(record)
        return out

    def evaluate(self, weights: Mapping[str, float]) -> Evaluation:
        return evaluation_of(self.pairs(weights, checked=True))


# ─── the search ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Trial:
    number: int
    weights: dict[str, float]
    feasibility: constraints.Feasibility
    objective: Optional[float]  # None when the vector was rejected before scoring


@dataclass
class SearchResult:
    current: dict[str, float]
    baseline: Evaluation
    baseline_feasibility: constraints.Feasibility
    trials: list[Trial]
    best: Optional[Trial]
    best_evaluation: Optional[Evaluation]
    seed: int
    budget: int
    seconds: float
    split: str = "synthetic"
    synthetic_only: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def proposed_weights(self) -> dict[str, float]:
        """The best feasible vector, or the current one when no trial was feasible."""
        return dict(self.best.weights) if self.best is not None else dict(self.current)

    @property
    def proposed_evaluation(self) -> Evaluation:
        return self.best_evaluation if self.best_evaluation is not None else self.baseline

    @property
    def fairness_failures(self) -> int:
        return sum(1 for t in self.trials if t.feasibility.fairness == "fail")

    @property
    def feasible(self) -> list[Trial]:
        return [t for t in self.trials if t.feasibility.feasible]

    @property
    def fairness_assessed(self) -> bool:
        return constraints.fairness_status(self.proposed_evaluation.fairness) != "not assessed"


def run_search(evaluator: Any, current: Mapping[str, float], *, budget: int, seed: int = 0,
               log: Any = lambda _m: None) -> SearchResult:
    """TPE over the simplex. Raises `constraints.FairnessFailure` when the current weights fail the
    four-fifths guardrail (terminal, before any trial)."""
    import optuna

    started = time.perf_counter()
    current = constraints.normalise(current)
    baseline = evaluator.evaluate(current)
    baseline_feasibility = constraints.assess(current, abstention_rate=baseline.abstention_rate,
                                              fairness=baseline.fairness)
    if baseline_feasibility.fairness == "fail":
        raise constraints.FairnessFailure(
            f"four-fifths guardrail FAILS on the current weights ({baseline.fairness.value}, "
            f"{baseline.fairness.detail}); tuning is refused (terminal, EVAL section 7)")

    trials: list[Trial] = []
    evaluations: dict[int, Evaluation] = {}

    def objective(trial: Any) -> float:
        u = [trial.suggest_float(f"u{i}", 1e-6, 1.0) for i in range(len(constraints.DIMENSIONS))]
        weights = constraints.from_unit(u)
        if constraints.bound_violations(weights):
            feasibility = constraints.out_of_bounds(weights)
            trials.append(Trial(trial.number, weights, feasibility, None))
            trial.set_constraint("feasibility", 1.0)
            return 0.0
        evaluation = evaluator.evaluate(weights)
        feasibility = constraints.assess(weights, abstention_rate=evaluation.abstention_rate,
                                         fairness=evaluation.fairness)
        value = evaluation.ndcg if evaluation.ndcg is not None else 0.0
        trials.append(Trial(trial.number, weights, feasibility, evaluation.ndcg))
        evaluations[trial.number] = evaluation
        trial.set_constraint("feasibility", 0.0 if feasibility.feasible else 1.0)
        log(f"  trial {trial.number:3d}: ndcg@10 {value:.4f} "
            f"{'feasible' if feasibility.feasible else 'infeasible: ' + '; '.join(feasibility.reasons)}")
        return value

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    sampler = optuna.samplers.TPESampler(seed=seed)
    study = optuna.create_study(direction="maximize", sampler=sampler)
    study.optimize(objective, n_trials=budget)

    feasible = [t for t in trials if t.feasibility.feasible and t.objective is not None]
    best = max(feasible, key=lambda t: (t.objective, -t.number)) if feasible else None
    return SearchResult(
        current=current, baseline=baseline, baseline_feasibility=baseline_feasibility, trials=trials, best=best,
        best_evaluation=evaluations.get(best.number) if best is not None else None, seed=seed, budget=budget,
        seconds=time.perf_counter() - started, split=getattr(evaluator, "split", "synthetic"),
        synthetic_only=bool(getattr(evaluator, "synthetic_only", True)))


# ─── CLI ───────────────────────────────────────────────────────────────────────────────────


def choose_split(requested: str, *, dev_labels: Path = DEV_LABELS) -> tuple[str, str]:
    if requested == "dev" and not dev_labels.exists():
        shown = dev_labels.relative_to(_ROOT) if dev_labels.is_relative_to(_ROOT) else dev_labels
        return "synthetic", (f"no dev labels ({shown.as_posix()} missing): tuning on the synthetic split; "
                             "the proposal is synthetic-only")
    return requested, ""


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m eval.tune.search", description=__doc__.splitlines()[0])
    parser.add_argument("--budget", type=int, default=200, help="number of trials")
    parser.add_argument("--objective", choices=OBJECTIVES, default="ndcg@10")
    parser.add_argument("--split", choices=SPLITS, default="dev")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default=str(PROPOSALS_DIR), help="proposal root (never config/)")
    parser.add_argument("--date", default=None, help="proposal date, YYYY-MM-DD (default: today, UTC)")
    parser.add_argument("--resamples", type=int, default=1000, help="bootstrap resamples for the report")
    args = parser.parse_args(argv)
    if args.budget < 1:
        parser.error("--budget must be at least 1")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    from eval.suites import real_engine
    from eval.tune import proposal, sensitivity
    from matcher.models import DimensionName

    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    started = time.perf_counter()
    split, note = choose_split(args.split)
    if note:
        print(f"tune: {note}", flush=True)
    evaluator = EngineEvaluator.dev() if split == "dev" else EngineEvaluator.synthetic()
    current = {d.value: real_engine().config.weights.of(d) for d in DimensionName}
    print(f"tune: {args.budget} trials, TPE seed {args.seed}, objective {args.objective} on {split} "
          f"({len(evaluator.items)} pairs)", flush=True)
    try:
        result = run_search(evaluator, current, budget=args.budget, seed=args.seed,
                            log=lambda m: print(m, flush=True))
    except constraints.FairnessFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    if note:
        result.notes.append(note)
    print(f"tune: search {result.seconds:.1f} s; {len(result.feasible)} feasible of {len(result.trials)}", flush=True)
    rows = sensitivity.analyse(evaluator.pairs, result.proposed_weights)
    day = date.fromisoformat(args.date) if args.date else datetime.now(timezone.utc).date()
    wall = time.perf_counter() - started
    folder = proposal.write_proposal(result, evaluator, out_root=Path(args.out), day=day,
                                     sensitivity_rows=rows, n_resamples=args.resamples, wall_seconds=wall)
    decision = proposal.decision_for(result, n_resamples=args.resamples)
    print(decision.text(), flush=True)
    print(f"tune: proposal written to {folder}", flush=True)
    print(f"tune: wall time {time.perf_counter() - started:.1f} s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
