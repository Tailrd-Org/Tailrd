"""Layer 3, golden (EVAL section 1) and layer 4, holdout.

Layer 3 runs when `eval/corpora/golden/labels.jsonl` exists (format: that folder's README.md; an
optional `group` per pair feeds the four-fifths guardrail, which is "not assessed" below n = 100):
Kendall's tau-b between the engine's raw score and the human band within each posting, averaged
over postings with at least two pairs; band accuracy (exact, off by one, off by two or more: the one
to alarm on) of the engine's band against the consensus band; inter-rater agreement (exact share and
quadratic-weighted Cohen's kappa over the first two raters) when pairs carry two labels. Each has a
bootstrap CI over postings (`metrics["summary"]`). Off-by-two stays a metric: EVAL.md calls it the
one to alarm on but gives no threshold. Without the
file it reports `GOLDEN: skipped (no labels)`.

Layer 4 never reads `eval/corpora/holdout/` here. It asks `eval.holdout.find_key` for the CI key
and reports `HOLDOUT: skipped (no key)` without it; the look itself is `make eval-holdout`.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Optional

from eval.metrics import PairScore, band_accuracy, bootstrap_ci, group_by_posting, kendall_tau_b
from eval.suites import BULK, ROOT, SuiteResult, Timer, fairness_guardrail, pair_record, skipped
from matcher.models import JobPosting, Resume

GOLDEN_LABELS = ROOT / "eval" / "corpora" / "golden" / "labels.jsonl"
GOLDEN_SKIPPED = "GOLDEN: skipped (no labels)"
HOLDOUT_SKIPPED = "HOLDOUT: skipped (no key)"

ParseFn = Callable[[bytes, str], Optional[Resume]]


def consensus(bands: Sequence[int]) -> int:
    """The median band; an even split rounds down (the cautious reading)."""
    return int(math.floor(median(bands)))


def weighted_kappa(a: Sequence[int], b: Sequence[int], categories: int = 4) -> Optional[float]:
    """Quadratic-weighted Cohen's kappa; None for fewer than two items or no expected disagreement."""
    n = len(a)
    if n < 2 or n != len(b):
        return None
    observed = [[0.0] * categories for _ in range(categories)]
    for x, y in zip(a, b):
        observed[x][y] += 1
    rows = [sum(r) for r in observed]
    cols = [sum(observed[i][j] for i in range(categories)) for j in range(categories)]
    weight = [[(i - j) ** 2 / (categories - 1) ** 2 for j in range(categories)] for i in range(categories)]
    disagree = math.fsum(weight[i][j] * observed[i][j] for i in range(categories) for j in range(categories))
    chance = math.fsum(weight[i][j] * rows[i] * cols[j] / n for i in range(categories) for j in range(categories))
    return None if chance == 0 else 1.0 - disagree / chance


def _tau(group: Sequence[PairScore]) -> Optional[float]:
    scored = [p for p in group if p.score is not None]
    if len(scored) < 2:
        return None
    return kendall_tau_b([p.score for p in scored], [float(p.relevance) for p in scored])


def kendall_by_posting(pairs: Sequence[PairScore]) -> Optional[float]:
    """Kendall's tau-b between raw score and the consensus band within each posting, averaged over
    the postings where it is defined (two scored pairs and neither side constant)."""
    taus = [t for t in (_tau(g) for g in group_by_posting(pairs).values()) if t is not None]
    return math.fsum(taus) / len(taus) if taus else None


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_golden(engine: Any, as_of: date, labels: Path = GOLDEN_LABELS, parse: Optional[ParseFn] = None, *,
               n_resamples: int = 1000) -> SuiteResult:
    if not labels.exists():
        result = skipped("golden", 3, "no labels")
        result.notes.append(GOLDEN_SKIPPED)
        return result
    result = SuiteResult(name="golden", layer=3)
    with Timer() as timer:
        rows = _load(labels)
        pairs: list[PairScore] = []
        raters: dict[str, tuple[int, int]] = {}  # pair id -> the first two raters' bands
        unscorable = 0
        for row in rows:
            bands = [int(label["band"]) for label in row["labels"]]
            if len(bands) >= 2:
                raters[row["pair_id"]] = (bands[0], bands[1])
            truth = consensus(bands)
            resume: Optional[Resume]
            if "resume" in row:
                resume = Resume.model_validate(row["resume"])
            elif parse is not None:
                file = labels.parent / row["resume_file"]
                resume = parse(file.read_bytes(), file.name)
            else:
                resume = None
            if resume is None:
                unscorable += 1
                continue
            posting = JobPosting.model_validate(row["posting"])
            scored = engine.score(resume, posting, as_of=date.fromisoformat(row.get("as_of", as_of.isoformat())),
                                  options=BULK)
            record = pair_record(row["posting_id"], row["pair_id"], scored, truth)
            pairs.append(record.model_copy(update={"advanced": row.get("advanced"), "hard": False,
                                                   "group": row.get("group")}))
        bands = band_accuracy(pairs)
        result.pairs = pairs
        result.metrics.update({
            "golden_pairs": len(rows),
            "golden_unscorable": unscorable,
            "kendall_tau": kendall_by_posting(pairs),
            "kendall_postings": len([g for g in group_by_posting(pairs).values() if _tau(g) is not None]),
            "band_exact": bands["exact"],
            "band_off_by_one": bands["off_by_one"],
            "band_off_by_two": bands["off_by_two_plus"],
        })

        def kappa(sample: Sequence[PairScore]) -> Optional[float]:
            both = [raters[p.pair_id] for p in sample if p.pair_id in raters]
            return weighted_kappa([a for a, _ in both], [b for _, b in both])

        metrics = {"kendall_tau": kendall_by_posting,
                   "band_exact": lambda ps: band_accuracy(ps)["exact"],
                   "band_off_by_one": lambda ps: band_accuracy(ps)["off_by_one"],
                   "band_off_by_two": lambda ps: band_accuracy(ps)["off_by_two_plus"]}
        if raters:
            metrics["rater_weighted_kappa"] = kappa
        summary = {}
        for key, fn in metrics.items():
            ci = bootstrap_ci(pairs, fn, n_resamples=n_resamples, seed=0)
            summary[key] = {"point": ci.point, "low": ci.low, "high": ci.high}
        result.metrics["summary"] = summary
        result.guardrails.append(fairness_guardrail(pairs))
        if raters:
            both = list(raters.values())
            result.metrics["rater_exact_agreement"] = sum(1 for a, b in both if a == b) / len(both)
            result.metrics["rater_weighted_kappa"] = weighted_kappa([a for a, _ in both], [b for _, b in both])
        else:
            result.notes.append("GOLDEN: inter-rater agreement not assessed (no dual labels)")
        off_two = [p.pair_id for p in pairs if p.band_pred is not None and abs(p.band_pred - p.band_true) >= 2]
        if off_two:
            result.notes.append(f"GOLDEN ALARM: {len(off_two)} pairs off by two or more bands: {', '.join(off_two[:20])}")
        result.notes.append(f"GOLDEN: {len(rows)} labelled pairs scored")
    result.seconds = timer.seconds
    return result


def run_holdout() -> SuiteResult:
    """Layer 4 never decrypts here: `make eval-holdout` (`eval.holdout evaluate`) owns every look, so
    each one is budgeted and logged. This only reports whether the key is present."""
    from eval import holdout

    try:
        key = holdout.find_key()
    except holdout.HoldoutError as exc:
        result = skipped("holdout", 4, f"bad key: {exc}")
        result.notes.append(f"HOLDOUT: skipped (bad key: {exc})")
        return result
    if key is None:
        result = skipped("holdout", 4, "no key")
        result.notes.append(HOLDOUT_SKIPPED)
        return result
    result = skipped("holdout", 4, "key present; layer 4 runs only through `make eval-holdout`")
    result.notes.append("HOLDOUT: skipped here (key present; run `make eval-holdout` on a release branch)")
    return result
