"""`make eval` / `make eval-fast`: run the evaluation layers and write `reports/<timestamp>/`.

    python -m eval.run --layers 1,2[,3,4] [--baseline main] [--save-baseline main] [--sample N]
                       [--jobs N] [--no-cache] [--out reports]

Without layer 3 the run is the fast one (`make eval-fast`): 5,000 fuzzed inputs, 300 closed-form
pairs and 10 profiles x 48 renderings through the parser. With layer 3 (`make eval`): 50,000 fuzzed
inputs, 2,000 closed-form pairs and every profile of the corpus. `--sample N` sets the number of
parser profiles. Parse results are cached under `.cache/eval/parse/` (git-ignored), so only a cold
cache pays for OCR. Exit code 1 when any guardrail fails.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:  # `python -m eval.run` from match-engine/ without an install
    sys.path.insert(0, str(_ROOT / "src"))

FAST = {"fuzz_n": 5000, "determinism_sample": 2, "closed_form_n": 300, "parser_profiles": 10}
FULL = {"fuzz_n": 50000, "determinism_sample": 5, "closed_form_n": 2000, "parser_profiles": None}  # None = all


def _layers(text: str) -> tuple[int, ...]:
    try:
        layers = tuple(sorted({int(part) for part in text.split(",") if part.strip()}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"layers are numbers 1-4: {text!r}") from exc
    if not layers or any(layer not in (1, 2, 3, 4) for layer in layers):
        raise argparse.ArgumentTypeError(f"layers are numbers 1-4: {text!r}")
    return layers


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m eval.run", description=__doc__.splitlines()[0])
    parser.add_argument("--layers", type=_layers, default=(1, 2), help="comma-separated layers, e.g. 1,2,3")
    parser.add_argument("--baseline", default=None, help="diff against eval/baselines/NAME")
    parser.add_argument("--save-baseline", default=None, help="write eval/baselines/NAME from this run")
    parser.add_argument("--sample", type=int, default=None, help="parser profiles (default: 10 fast, all full)")
    parser.add_argument("--jobs", type=int, default=None, help="parser worker processes")
    parser.add_argument("--no-cache", action="store_true", help="re-parse every rendering")
    parser.add_argument("--out", default=str(_ROOT / "reports"), help="report root")
    args = parser.parse_args(argv)
    if args.sample is not None and args.sample < 0:
        parser.error("--sample must be 0 or more")
    if args.sample == 0 and 3 in args.layers:
        parser.error("--sample 0 skips the parser pass; a full run (layer 3) must run it")
    return args


def full_mode(args: argparse.Namespace) -> bool:
    return 3 in args.layers


def settings(args: argparse.Namespace) -> dict[str, Any]:
    chosen = dict(FULL if full_mode(args) else FAST)
    if args.sample is not None:
        chosen["parser_profiles"] = args.sample
    chosen["n_resamples"] = 1000
    chosen["mode"] = "full" if full_mode(args) else "fast"
    return chosen


def _git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _sample(profiles: Sequence[Any], n: int | None) -> list[Any]:
    """`n` profiles spread evenly over the corpus (every fit level and family), or all of them."""
    if n is None or n >= len(profiles):
        return list(profiles)
    if n <= 0:
        return []  # --sample 0: no parser pass (ranking and closed form only)
    step = len(profiles) / n
    return [profiles[int(i * step)] for i in range(n)]


def log(message: str) -> None:
    print(message, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    from eval import baseline as baselines
    from eval import report
    from eval.suites import (
        Engine,
        corpus_id_map,
        corpus_pairs,
        real_engine,
        synthetic_corpus,
        translated_postings,
    )
    from eval.suites import closed_form, golden, properties, synthetic
    from matcher.models import DimensionName

    args = parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # the summary holds "Δ" and "é"
    chosen = settings(args)
    started = time.perf_counter()
    stamp = datetime.now(timezone.utc).replace(microsecond=0)
    log(f"eval: layers {','.join(map(str, args.layers))} ({chosen['mode']})")

    engine = real_engine()
    corpus = synthetic_corpus()
    id_map = corpus_id_map()
    pairs = corpus_pairs(corpus, id_map)
    log(f"corpus: {len(corpus.profiles)} profiles, {len(corpus.postings)} postings, "
        f"{len(id_map.unknown)} vocabulary skills unknown to the taxonomy ({time.perf_counter() - started:.1f} s)")

    available, why = synthetic.parser_available()
    parse_factory = synthetic.real_parser if available else None
    parse_once = synthetic.real_parser() if available else None
    suites = []
    if 1 in args.layers:
        log("layer 1: properties")
        suites.append(properties.run_properties(
            engine, [(cp.case, cp.resume, cp.posting) for cp in pairs], list(translated_postings(corpus, id_map).items()),
            corpus.as_of, fuzz_n=chosen["fuzz_n"], determinism_sample=chosen["determinism_sample"], parse=parse_once))
        log(f"  {suites[-1].seconds:.1f} s")
    if 2 in args.layers:
        log("layer 2: synthetic")
        cache = None if args.no_cache or not available else synthetic.ParseCache(
            synthetic.CACHE_DIR, synthetic.parser_fingerprint(), corpus.seed)
        cf_engine = Engine(engine.config, closed_form.closed_form_graph())
        suites.append(synthetic.run_synthetic(
            engine, cf_engine, corpus, id_map, pairs, closed_form_n=chosen["closed_form_n"],
            n_resamples=chosen["n_resamples"], parse_profiles=_sample(corpus.profiles, chosen["parser_profiles"]),
            parse=parse_once, parse_factory=parse_factory, parser_reason=why, cache=cache,
            jobs=args.jobs or synthetic.default_jobs(), log=lambda m: log(f"  {m}")))
        log(f"  {suites[-1].seconds:.1f} s")
    if 3 in args.layers:
        suites.append(golden.run_golden(engine, corpus.as_of, parse=parse_once))
    if 4 in args.layers or 3 in args.layers:
        suites.append(golden.run_holdout())
    for suite in suites:
        for note in suite.notes:
            if note.startswith(("GOLDEN", "HOLDOUT", "PARSER")):
                log(note)

    base = baselines.load_baseline(args.baseline) if args.baseline else None
    run = report.RunRecord(
        timestamp=stamp, config_hash=engine.config_hash, engine_version=engine.config.engine_version,
        graph_version=engine.graph.version, git_commit=_git_commit(), layers=args.layers, suites=suites,
        baseline=base, settings=chosen,
        weights={name.value: engine.config.weights.of(name) for name in DimensionName})
    folder, code = report.write_report(run, Path(args.out))
    synthetic_suite = run.suite("synthetic")
    if args.save_baseline and synthetic_suite is not None:
        meta = {"git_commit": run.git_commit, "timestamp": stamp.isoformat(), "config_hash": run.config_hash,
                "engine_version": run.engine_version, "graph_version": run.graph_version,
                "corpus": {"seed": corpus.seed, "profiles": len(corpus.profiles), "postings": len(corpus.postings)}}
        saved = baselines.save_baseline(args.save_baseline, {"meta": meta,
                                                             "ranking": synthetic_suite.metrics.get("ranking")},
                                        synthetic_suite.pairs)
        log(f"baseline saved: {saved}")
    print((folder / "summary.md").read_text(encoding="utf-8"))
    log(f"report: {folder}")
    log(f"eval: {'FAIL' if code else 'PASS'} in {time.perf_counter() - started:.1f} s")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
