"""Saved baselines and the diff against one (EVAL section 4).

`eval/baselines/<NAME>/` holds `metrics.json` (the run's metrics and its identity: commit, config
hash, corpus) and `pairs.jsonl` (one `PairScore` per synthetic ranking pair, sorted by pair id).
Only synthetic material is ever saved here (the repository is public, DECISIONS D-04).

The diff has two parts:

* metric deltas: `bootstrap_delta` per ranking metric on the postings both runs share, with the
  metric's own `min_effect` (0.01 for NDCG and the other shares, 0.005 for band off-by-two);
* per-pair regressions: pairs whose score moved away from their label by more than
  `REGRESSION_EPS` (a drop for an advanced pair, fit >= 2; a rise for one that is not), or that
  newly abstain, worst first. They feed `regressions.md`.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from eval.metrics import PairScore, bootstrap_delta

BASELINES_DIR = Path(__file__).resolve().parent / "baselines"
REGRESSION_EPS = 0.005


@dataclass(frozen=True)
class Baseline:
    name: str
    metrics: dict[str, Any]
    pairs: tuple[PairScore, ...]

    @property
    def label(self) -> str:
        commit = (self.metrics.get("meta") or {}).get("git_commit") or "unknown"
        return f"{self.name}@{commit[:7]}"


def save_baseline(name: str, metrics: dict[str, Any], pairs: Sequence[PairScore],
                  root: Path = BASELINES_DIR) -> Path:
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "metrics.json").write_bytes(
        (json.dumps(metrics, indent=2, sort_keys=True, default=str) + "\n").encode("utf-8"))
    lines = [json.dumps(p.model_dump(mode="json"), sort_keys=True) for p in sorted(pairs, key=lambda p: p.pair_id)]
    (folder / "pairs.jsonl").write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return folder


def load_baseline(name: str, root: Path = BASELINES_DIR) -> Baseline:
    folder = root / name
    if not (folder / "metrics.json").exists():
        raise FileNotFoundError(f"no baseline {name!r} under {root}")
    metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
    rows = (folder / "pairs.jsonl").read_text(encoding="utf-8").splitlines()
    pairs = tuple(PairScore.model_validate(json.loads(line)) for line in rows if line.strip())
    return Baseline(name=name, metrics=metrics, pairs=pairs)


def metric_deltas(baseline: Sequence[PairScore], current: Sequence[PairScore], specs: Sequence[Any], *,
                  n_resamples: int = 1000, seed: int = 0) -> dict[str, dict[str, Any]]:
    """`bootstrap_delta` per metric spec (`key`, `fn`, `higher_is_better`, `min_effect`)."""
    out = {}
    for spec in specs:
        delta = bootstrap_delta(baseline, current, spec.fn, n_resamples=n_resamples, seed=seed,
                                min_effect=spec.min_effect, higher_is_better=spec.higher_is_better)
        out[spec.key] = {"point": delta.point, "low": delta.low, "high": delta.high, "verdict": delta.verdict,
                         "baseline": spec.fn(baseline)}
    return out


@dataclass(frozen=True)
class Regression:
    pair_id: str
    posting_id: str
    relevance: int
    before: Optional[float]
    after: Optional[float]
    severity: float  # how far it moved the wrong way; a new abstention counts 1.0

    def line(self) -> str:
        def fmt(v: Optional[float]) -> str:
            return "abstained" if v is None else f"{v:.2f}"

        return f"{self.pair_id}  {fmt(self.before)} -> {fmt(self.after)}  fit {self.relevance}, posting {self.posting_id}"


def regressions(baseline: Sequence[PairScore], current: Sequence[PairScore],
                eps: float = REGRESSION_EPS) -> list[Regression]:
    before = {p.pair_id: p for p in baseline}
    out = []
    for pair in current:
        old = before.get(pair.pair_id)
        if old is None:
            continue
        if old.score is not None and pair.score is None:
            out.append(Regression(pair.pair_id, pair.posting_id, pair.relevance, old.score, None, 1.0))
            continue
        if old.score is None or pair.score is None:
            continue
        moved = pair.score - old.score
        wrong = -moved if pair.relevance >= 2 else moved
        if wrong > eps:
            out.append(Regression(pair.pair_id, pair.posting_id, pair.relevance, old.score, pair.score, wrong))
    return sorted(out, key=lambda r: (-r.severity, r.pair_id))
