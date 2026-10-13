"""Layer 3, golden (EVAL section 1) and layer 4, holdout.

Layer 3 runs when `eval/corpora/golden/labels.jsonl` exists (format: that folder's README.md):
Kendall's tau-b between the engine's raw score and the human band within each posting, averaged
over postings with at least two pairs; band accuracy (exact, off by one, off by two or more: the one
to alarm on) of the engine's band against the consensus band; inter-rater agreement (exact share and
quadratic-weighted Cohen's kappa over the first two raters) when pairs carry two labels. Without the
file it reports `GOLDEN: skipped (no labels)`.

Layer 4 never reads `eval/corpora/holdout/`. It asks Task 8.1's tooling (`eval.holdout.find_key`)
for the CI key and reports `HOLDOUT: skipped (no key)` when that tooling or the key is absent.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any, Optional

from eval.metrics import PairScore, band_accuracy, kendall_tau_b
from eval.suites import BAND_INDEX, BULK, ROOT, SuiteResult, Timer, score_for_ranking, skipped
from matcher.models import Band, JobPosting, Resume

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


def _load(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_golden(engine: Any, as_of: date, labels: Path = GOLDEN_LABELS, parse: Optional[ParseFn] = None) -> SuiteResult:
    if not labels.exists():
        result = skipped("golden", 3, "no labels")
        result.notes.append(GOLDEN_SKIPPED)
        return result
    result = SuiteResult(name="golden", layer=3)
    with Timer() as timer:
        rows = _load(labels)
        pairs: list[PairScore] = []
        rater_pairs: list[tuple[int, int]] = []
        by_posting: dict[str, list[tuple[float, float]]] = {}
        unscorable = 0
        for row in rows:
            bands = [int(label["band"]) for label in row["labels"]]
            if len(bands) >= 2:
                rater_pairs.append((bands[0], bands[1]))
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
            score = score_for_ranking(scored)
            band = BAND_INDEX.get(Band(scored.band)) if scored.band is not None else None
            pairs.append(PairScore(posting_id=row["posting_id"], pair_id=row["pair_id"], score=score,
                                   relevance=truth, band_pred=band, band_true=truth,
                                   advanced=row.get("advanced")))
            if score is not None:
                by_posting.setdefault(row["posting_id"], []).append((score, float(truth)))
        taus = [t for t in (kendall_tau_b([s for s, _ in v], [h for _, h in v]) for v in by_posting.values()
                            if len(v) >= 2) if t is not None]
        bands = band_accuracy(pairs)
        result.pairs = pairs
        result.metrics.update({
            "golden_pairs": len(rows),
            "golden_unscorable": unscorable,
            "kendall_tau": (math.fsum(taus) / len(taus)) if taus else None,
            "kendall_postings": len(taus),
            "band_exact": bands["exact"],
            "band_off_by_one": bands["off_by_one"],
            "band_off_by_two": bands["off_by_two_plus"],
        })
        if rater_pairs:
            result.metrics["rater_exact_agreement"] = sum(1 for a, b in rater_pairs if a == b) / len(rater_pairs)
            result.metrics["rater_weighted_kappa"] = weighted_kappa([a for a, _ in rater_pairs],
                                                                    [b for _, b in rater_pairs])
        else:
            result.notes.append("GOLDEN: inter-rater agreement not assessed (no dual labels)")
        off_two = [p.pair_id for p in pairs if p.band_pred is not None and abs(p.band_pred - p.band_true) >= 2]
        if off_two:
            result.notes.append(f"GOLDEN ALARM: {len(off_two)} pairs off by two or more bands: {', '.join(off_two[:20])}")
        result.notes.append(f"GOLDEN: {len(rows)} labelled pairs scored")
    result.seconds = timer.seconds
    return result


def run_holdout() -> SuiteResult:
    """Layer 4: only Task 8.1's tooling may open the holdout, and only with the CI key."""
    try:
        from eval import holdout  # type: ignore[attr-defined]  # Task 8.1
    except ImportError:
        holdout = None
    key = holdout.find_key() if holdout is not None and hasattr(holdout, "find_key") else None
    if key is None:
        result = skipped("holdout", 4, "no key")
        result.notes.append(HOLDOUT_SKIPPED)
        return result
    result = skipped("holdout", 4, "key found, but holdout evaluation belongs to Task 8.1's tooling")
    result.notes.append("HOLDOUT: skipped (Task 8.1 runs it)")
    return result
