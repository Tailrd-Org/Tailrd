"""Layer 2, synthetic (EVAL section 2): analytic correctness, ranking, parser fidelity and format
invariance.

(a) Analytic correctness: `closed_form` pairs, engine raw vs the spec's arithmetic to 0.001.

(b) Ranking over the corpus: every profile's oracle résumé (`expected_resume`, what a perfect parser
recovers) scored against its target posting, `fit_level` as graded relevance. NDCG@10, P@10 (with
the abstention rate next to it), AUC (advanced = fit >= 2), band accuracy, ECE (calibrated / 100
against fit >= 2) and abstention rate and precision, each with a bootstrap CI over postings.

(c) Parser fidelity and format invariance, with the parser injected as `parse(bytes, filename) ->
Resume | None`: every sampled profile is rendered in the 8 templates x 6 formats, parsed and scored.

* recovered skills: the share of the profile's skills (those the taxonomy can name, `IdMap.known`)
  found anywhere in the parsed résumé, pooled over renderings; >= 0.95.
* date error: per rendering, the largest month error over the oracle's role starts and ends (a role
  the parser missed, or a date it lost, counts as unbounded); every rendering <= 1 month.
  `functional` is excluded (it prints no dates, by design).
* format invariance (orchestrator ruling on EVAL 2.2 vs SPEC 14.3): (a) the 6 formats within each
  template, (b) the 42 renderings of the 7 information-equivalent templates (all but `functional`),
  each max - min raw <= 0.02 at p99 over profiles; `functional` renderings are held to the score of
  `expected_resume(template="functional")`, |delta| <= 0.02 at p99. A rendering that yields no
  résumé or no raw score has an unbounded spread.

Profiles' industry codes are left out of the oracle comparisons (`expected_resume` sets codes no
page prints, Task 5.1). Parsing is slow (OCR of a scanned page takes seconds), so results are cached
by render identity and parser fingerprint under `.cache/eval/parse/` (git-ignored) and cold runs fan
out over processes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from eval.metrics import (
    PairScore,
    abstention_stats,
    auc_roc,
    band_accuracy,
    bootstrap_ci,
    expected_calibration_error,
    ndcg_at_k,
    precision_at_k,
)
from eval.suites import (
    BULK,
    ROOT,
    TAXONOMY_DIR,
    Corpus,
    CorpusPair,
    Failure,
    Guardrail,
    IdMap,
    SuiteResult,
    Timer,
    fairness_guardrail,
    pair_record,
    percentile,
)
from matcher.models import Resume

ParseFn = Callable[[bytes, str], Optional[Resume]]
CACHE_DIR = ROOT / ".cache" / "eval" / "parse"
INVARIANCE_MAX = 0.02
RECOVERY_MIN = 0.95
DATE_ERROR_MAX = 1
UNBOUNDED = math.inf


# ─── (b) Ranking metrics ───────────────────────────────────────────────────────────────────


def _ece(pairs: Sequence[PairScore]) -> Optional[float]:
    scored = [p for p in pairs if p.score is not None and p.advanced is not None]
    if not scored:
        return None
    probs = [round(100 * min(1.0, max(0.0, p.score))) / 100 for p in scored]  # FixedCalibrator
    return expected_calibration_error(probs, [bool(p.advanced) for p in scored])


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    fn: Callable[[Sequence[PairScore]], Optional[float]]
    higher_is_better: bool
    min_effect: float
    percent: bool = False


RANKING_METRICS: tuple[MetricSpec, ...] = (
    MetricSpec("ndcg@10", "NDCG@10", lambda ps: ndcg_at_k(ps, 10), True, 0.01),
    MetricSpec("precision@10", "Precision@10", lambda ps: precision_at_k(ps, 10), True, 0.01),
    MetricSpec("abstention_rate", "Abstention rate", lambda ps: abstention_stats(ps)["rate"], False, 0.01, True),
    MetricSpec("auc", "AUC (advanced = fit >= 2)", auc_roc, True, 0.01),
    MetricSpec("band_exact", "Band exact", lambda ps: band_accuracy(ps)["exact"], True, 0.01, True),
    MetricSpec("band_off_by_one", "Band off-by-one", lambda ps: band_accuracy(ps)["off_by_one"], False, 0.01, True),
    MetricSpec("band_off_by_two", "Band off-by-two", lambda ps: band_accuracy(ps)["off_by_two_plus"], False, 0.005,
               True),
    MetricSpec("ece", "ECE (10 bins)", _ece, False, 0.01),
    MetricSpec("abstention_precision", "Abstention precision", lambda ps: abstention_stats(ps)["precision"], True,
               0.01, True),
)


def ranking_metrics(pairs: Sequence[PairScore], *, n_resamples: int = 1000, seed: int = 0) -> dict[str, dict]:
    out = {}
    for spec in RANKING_METRICS:
        ci = bootstrap_ci(pairs, spec.fn, n_resamples=n_resamples, seed=seed)
        out[spec.key] = {"point": ci.point, "low": ci.low, "high": ci.high}
    return out


def score_corpus(engine: Any, pairs: Sequence[CorpusPair], as_of: date) -> list[PairScore]:
    return [pair_record(cp.posting_id, cp.case, engine.score(cp.resume, cp.posting, as_of=as_of, options=BULK),
                        cp.profile.fit_level) for cp in pairs]


# ─── (c) Parsing: cache, workers ───────────────────────────────────────────────────────────


def _hash_files(paths: Sequence[Path], root: Path = ROOT, extra: bytes = b"") -> str:
    digest = hashlib.sha256(extra)
    for path in sorted(paths):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def generator_fingerprint() -> str:
    """Render identity: the generator's code and templates."""
    folder = ROOT / "eval" / "generate"
    return _hash_files([p for p in folder.rglob("*") if p.is_file() and p.suffix in (".py", ".j2")])[:16]


_PARSER_CONFIG = ("verbs.yaml", "negation.yaml", "curated_taxonomy.yaml", "curated_edges.yaml",
                  "engine_version.txt")
_PARSER_SCORING_SECTIONS = ("ingest", "taxonomy")  # the parts of scoring.yaml ingest and resolution read


_PARSER_ROOTS = ("parse.resume", "parse.config", "graph.loader", "config")  # what `real_parser` imports


def _module_file(src: Path, dotted: str) -> Optional[Path]:
    """`matcher.<dotted>` as a file under `src` (a package's __init__.py), or None."""
    base = src.joinpath(*dotted.split(".")) if dotted else src
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _import_closure(src: Path, roots: Sequence[str]) -> set[Path]:
    """Every `matcher` module reachable by static imports (module level or inside functions, absolute
    or relative) from `roots`, with the __init__.py of every package on the way."""
    import ast

    seen: set[Path] = set()
    todo = [r for r in roots]
    while todo:
        dotted = todo.pop()
        path = _module_file(src, dotted)
        if path is None or path in seen:
            continue
        seen.add(path)
        parts = dotted.split(".") if dotted else []
        for depth in range(len(parts)):  # the packages a module lives in run their __init__ first
            todo.append(".".join(parts[:depth]))
        package = parts if path.name == "__init__.py" else parts[:-1]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    anchor = package[: len(package) - node.level + 1]
                    base = ".".join(["matcher", *anchor, *(node.module.split(".") if node.module else [])])
                else:
                    base = node.module or ""
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            for name in names:
                if name == "matcher" or name.startswith("matcher."):
                    todo.append(name[len("matcher."):] if name != "matcher" else "")
    return seen


def parser_inputs(root: Path = ROOT) -> list[Path]:
    """What the résumé parser reads: its modules (ingest, parse, normalize, graph, plus the static
    import closure of what `real_parser` imports, which brings score/evidence.py, score/numeric.py,
    models, config, errors and every package __init__.py), config/parse/**, the verb,
    negation and curated taxonomy files, and the taxonomy's manifest and resolver tables. Weights,
    bands, gates and every other scoring module are not inputs (DECISIONS D-118)."""
    src = root / "src" / "matcher"
    files = [p for sub in ("ingest", "parse", "normalize", "graph") for p in (src / sub).rglob("*.py")]
    files += sorted(_import_closure(src, _PARSER_ROOTS))  # every module the parser imports, packages too
    files += [p for p in (root / "config" / "parse").rglob("*") if p.is_file()]
    files += [root / "config" / name for name in _PARSER_CONFIG]
    files += [root / "data" / "taxonomy" / name for name in ("MANIFEST.json", "resolver.json")]
    return [p for p in files if p.exists()]


def parser_fingerprint(root: Path = ROOT) -> str:
    """The parse cache's key part: `parser_inputs` plus the ingest and taxonomy sections of
    scoring.yaml (read as data, so a change elsewhere in that file keeps the cache warm)."""
    import yaml

    scoring = root / "config" / "scoring.yaml"
    sections = {}
    if scoring.exists():
        loaded = yaml.safe_load(scoring.read_text(encoding="utf-8")) or {}
        sections = {k: loaded.get(k) for k in _PARSER_SCORING_SECTIONS}
    extra = json.dumps(sections, sort_keys=True, default=str).encode("utf-8")
    return _hash_files(parser_inputs(root), root, extra)[:16]


class ParseCache:
    """Parsed résumés keyed by render identity + parser fingerprint, one JSON file each."""

    def __init__(self, root: Optional[Path], parser_print: Optional[str], corpus_seed: int) -> None:
        self.root = root if parser_print else None
        self.prefix = f"{generator_fingerprint()}:{parser_print}:{corpus_seed}"

    def key(self, profile: Any, template: str, fmt: str) -> str:
        identity = f"{self.prefix}:{profile.model_dump_json()}:{template}:{fmt}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> tuple[bool, Optional[Resume]]:
        if self.root is None:
            return False, None
        path = self._path(key)
        if not path.exists():
            return False, None
        data = json.loads(path.read_text(encoding="utf-8"))
        return True, None if data["resume"] is None else Resume.model_validate(data["resume"])

    def put(self, key: str, resume: Optional[Resume]) -> None:
        if self.root is None:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"resume": None if resume is None else resume.model_dump(mode="json")}
        path.write_text(json.dumps(payload), encoding="utf-8")


def parser_available() -> tuple[bool, str]:
    try:
        import pymupdf  # noqa: F401
        import docx  # noqa: F401
        import pdfplumber  # noqa: F401

        from matcher.parse.resume import parse_resume_bytes  # noqa: F401
    except Exception as exc:  # any import failure means the parser cannot run here
        return False, f"{type(exc).__name__}: {exc}"
    return True, ""


def real_parser() -> ParseFn:
    """`matcher.parse.resume.parse_resume_bytes` with the shipped config and taxonomy."""
    from matcher.config import load_config
    from matcher.graph.loader import load_resolver
    from matcher.parse.config import load_parse_config
    from matcher.parse.resume import load_resume_resources, parse_resume_bytes
    from eval.generate.profiles import GENERATION_AS_OF

    config = load_config()
    parse_config = load_parse_config()
    resolver = load_resolver(TAXONOMY_DIR, config)
    resources = load_resume_resources(TAXONOMY_DIR, parse_config=parse_config)

    def parse(data: bytes, filename: str) -> Optional[Resume]:
        return parse_resume_bytes(data, filename, as_of=GENERATION_AS_OF, resolver=resolver, config=config,
                                  parse_config=parse_config, resources=resources).resume

    return parse


_WORKER_PARSE: Optional[ParseFn] = None


_THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")


def _init_worker(factory: Callable[[], ParseFn]) -> None:
    """One thread per worker process: the pool is the parallelism, and N workers each running a
    full-width BLAS / OpenCV thread pool oversubscribe the machine many times over."""
    global _WORKER_PARSE
    for name in _THREAD_VARS:
        os.environ[name] = "1"
    try:
        import cv2

        cv2.setNumThreads(1)
    except Exception:  # OpenCV is rapidocr's dependency; without it there is nothing to limit
        pass
    _WORKER_PARSE = factory()


def _render_and_parse(profile: Any, combos: Sequence[tuple[str, str]], seed: int, parse: ParseFn
                      ) -> list[tuple[str, str, Optional[str]]]:
    from eval.generate.corpus import render_seed
    from eval.generate.render import FORMAT_EXTENSIONS, build_document, render_document
    from eval.generate.vocabulary import default_vocabulary

    vocab = default_vocabulary()
    out = []
    documents: dict[str, Any] = {}
    for template, fmt in combos:
        s = render_seed(seed, profile.profile_id, template)
        if template not in documents:
            documents[template] = build_document(profile, template, vocab, seed=s)
        data = render_document(documents[template], fmt, seed=s, template=template, profile_id=profile.profile_id)
        resume = parse(data, f"{profile.profile_id}_{template}.{FORMAT_EXTENSIONS[fmt]}")
        out.append((template, fmt, None if resume is None else resume.model_dump_json()))
    return out


def _worker_task(task: tuple[str, tuple[tuple[str, str], ...], int]) -> list[tuple[str, str, Optional[str]]]:
    from eval.generate.profiles import CandidateProfile

    profile_json, combos, seed = task
    return _render_and_parse(CandidateProfile.model_validate_json(profile_json), combos, seed, _WORKER_PARSE)


Rendered = dict[tuple[str, str, str], Optional[Resume]]  # (profile id, template, format) -> résumé


def parse_renderings(profiles: Sequence[Any], seed: int, templates: Sequence[str], formats: Sequence[str], *,
                     parse: Optional[ParseFn] = None, parse_factory: Optional[Callable[[], ParseFn]] = None,
                     cache: Optional[ParseCache] = None, jobs: int = 1, log: Callable[[str], None] = lambda _: None
                     ) -> tuple[Rendered, dict[str, int]]:
    """Every (profile, template, format) parsed, from the cache when possible. Misses run in this
    process with `parse`, or over `jobs` worker processes built by `parse_factory`."""
    results: Rendered = {}
    todo: list[tuple[Any, list[tuple[str, str]], list[str]]] = []  # one entry per (profile, template)
    stats = {"renderings": 0, "cached": 0, "parsed": 0}
    for profile in profiles:
        for template in templates:
            missing, keys = [], []
            for fmt in formats:
                stats["renderings"] += 1
                key = cache.key(profile, template, fmt) if cache else ""
                found, resume = cache.get(key) if cache else (False, None)
                if found:
                    stats["cached"] += 1
                    results[(profile.profile_id, template, fmt)] = resume
                else:
                    missing.append((template, fmt))
                    keys.append(key)
            if missing:
                todo.append((profile, missing, keys))

    def store(profile: Any, keys: list[str], rows: list[tuple[str, str, Optional[str]]]) -> None:
        for key, (template, fmt, payload) in zip(keys, rows):
            resume = None if payload is None else Resume.model_validate_json(payload)
            results[(profile.profile_id, template, fmt)] = resume
            stats["parsed"] += 1
            if cache:
                cache.put(key, resume)  # stored as each task finishes: an interrupted run keeps its work

    def progress(done: int) -> None:
        if done % 25 == 0 or done == len(todo):
            log(f"parsed {done}/{len(todo)} (profile, template) groups")

    if todo and jobs > 1 and parse_factory is not None:
        with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker, initargs=(parse_factory,)) as pool:
            futures = {pool.submit(_worker_task, (p.model_dump_json(), tuple(combos), seed)): (p, keys)
                       for p, combos, keys in todo}
            for done, future in enumerate(as_completed(futures), start=1):
                profile, keys = futures[future]
                store(profile, keys, future.result())
                progress(done)
    elif todo:
        parse = parse or (parse_factory() if parse_factory else None)
        if parse is None:
            raise ValueError("parse_renderings needs `parse` or `parse_factory`")
        for done, (profile, combos, keys) in enumerate(todo, start=1):
            store(profile, keys, _render_and_parse(profile, combos, seed, parse))
            progress(done)
    return results, stats


# ─── (c) Fidelity and invariance measures ──────────────────────────────────────────────────


def skill_ids(resume: Resume) -> set[str]:
    refs = [*resume.skills_blob, *resume.certifications]
    refs += [r for role in resume.roles for r in role.skills]
    refs += [r for project in resume.projects for r in project.skills]
    return {r.canonical_id for r in refs}


def expected_ids(profile: Any, corpus: Corpus, id_map: IdMap, template: str) -> set[str]:
    """The taxonomy ids of the skills this rendering shows that the taxonomy can name."""
    from eval.generate.profiles import expected_resume

    oracle = expected_resume(profile, corpus.vocab, corpus.as_of,
                             template="functional" if template == "functional" else None)
    return {id_map(i) for i in skill_ids(oracle) if id_map.known(i)}


def _month(d: date) -> int:
    return d.year * 12 + d.month - 1


def _norm(text: str) -> str:
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def date_error(oracle: Resume, parsed: Optional[Resume], as_of: date) -> tuple[float, list[str]]:
    """The largest month error over the oracle's role dates, with what went wrong."""
    if parsed is None:
        return UNBOUNDED, ["no résumé"]
    worst = 0.0
    notes: list[str] = []
    used: set[int] = set()
    for index, role in enumerate(oracle.roles):
        match = None
        for j, candidate in enumerate(parsed.roles):
            if j not in used and _norm(candidate.employer) and _norm(candidate.employer) == _norm(role.employer):
                match = j
                break
        if match is None:
            for j, candidate in enumerate(parsed.roles):
                if j not in used and _norm(candidate.title_raw) == _norm(role.title_raw):
                    match = j
                    break
        if match is None:
            worst = UNBOUNDED
            notes.append(f"role {index} ({role.title_raw} at {role.employer}) not found")
            continue
        used.add(match)
        got = parsed.roles[match]
        if got.start is None:
            worst = UNBOUNDED
            notes.append(f"role {index}: start lost")
        else:
            error = abs(_month(got.start.value) - _month(role.start.value))
            worst = max(worst, error)
            if error > DATE_ERROR_MAX:
                notes.append(f"role {index}: start {got.start.value} vs {role.start.value}")
        want_end = _month(as_of) if role.is_current else _month(role.end.value)
        if got.is_current:
            got_end: Optional[int] = _month(as_of)
        else:
            got_end = None if got.end is None else _month(got.end.value)
        if got_end is None:
            worst = UNBOUNDED
            notes.append(f"role {index}: end lost")
        else:
            error = abs(got_end - want_end)
            worst = max(worst, error)
            if error > DATE_ERROR_MAX:
                notes.append(f"role {index}: end {'current' if got.is_current else got.end.value} vs "
                             f"{'current' if role.is_current else role.end.value}")
    return worst, notes


_WRITTEN = 10  # failure files written per guardrail (the worst first); each states the true count


def unknown_ids(profile: Any, corpus: Corpus, id_map: IdMap, template: str) -> set[str]:
    """The vocabulary ids this rendering shows that the taxonomy cannot name."""
    from eval.generate.profiles import expected_resume

    oracle = expected_resume(profile, corpus.vocab, corpus.as_of,
                             template="functional" if template == "functional" else None)
    return {i for i in skill_ids(oracle) if not id_map.known(i)}


COVERAGE_MIN = 0.92  # SPEC 16 coverage alert threshold (scoring.yaml taxonomy.coverage_alert_threshold)


def taxonomy_coverage(postings: Any) -> Optional[float]:
    """SPEC 16 coverage over the postings' requirement tokens: resolved / all, pooled."""
    total = resolved = 0
    for posting in postings:
        tokens = posting.requirement_token_count or (len(posting.requirements)
                                                     + len(posting.unresolved_requirement_tokens))
        total += tokens
        resolved += max(0, tokens - len(posting.unresolved_requirement_tokens))
    return resolved / total if total else None


def coverage_guardrail(postings: Any) -> Guardrail:
    postings = list(postings)
    value = taxonomy_coverage(postings)
    unresolved = sorted({t for p in postings for t in p.unresolved_requirement_tokens})
    return Guardrail("Taxonomy coverage (posting requirement tokens)",
                     None if value is None else value >= COVERAGE_MIN, value, f">= {COVERAGE_MIN}",
                     f"{len(postings)} postings; {len(unresolved)} distinct unresolved tokens", "coverage")


def _without_industry(resume: Resume) -> Resume:
    return resume.model_copy(update={"roles": tuple(r.model_copy(update={"industry_codes": ()})
                                                    for r in resume.roles)})


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return "unbounded" if math.isinf(value) else f"{value:g}"


def _spread(values: Sequence[Optional[float]]) -> float:
    if not values or any(v is None for v in values):
        return UNBOUNDED
    return max(values) - min(values)


def _p99(values: Sequence[float]) -> Optional[float]:
    return percentile(values, 99)


@dataclass
class FidelityReport:
    metrics: dict[str, Any] = field(default_factory=dict)
    guardrails: list[Guardrail] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    worst: dict[str, list[dict]] = field(default_factory=dict)


def fidelity(corpus: Corpus, id_map: IdMap, engine: Any, profiles: Sequence[Any], rendered: Rendered,
             templates: Sequence[str], formats: Sequence[str], postings: dict[str, Any]) -> FidelityReport:
    from eval.generate.profiles import expected_resume

    report = FidelityReport()
    as_of = corpus.as_of
    found = wanted = 0
    wanted_all = 0  # every vocabulary skill the rendering shows, those the taxonomy cannot name included
    extra = 0
    no_resume = 0
    date_errors: list[tuple[float, str, list[str]]] = []
    within: list[tuple[float, str, dict]] = []
    across: list[tuple[float, str, dict]] = []
    functional_dev: list[tuple[float, str, dict]] = []
    recoveries: list[tuple[float, str, list[str]]] = []
    parse_loss: list[float] = []
    for profile in profiles:
        posting = postings[profile.target_posting_id]
        oracle = id_map.resume(expected_resume(profile, corpus.vocab, as_of))
        comparable = _without_industry(oracle)  # no page prints the oracle's NAICS codes (Task 5.1)
        oracle_raw = engine.score(comparable, posting, as_of=as_of, options=BULK).raw_score
        raws: dict[tuple[str, str], Optional[float]] = {}
        dims: dict[tuple[str, str], dict[str, Optional[float]]] = {}
        stripped: dict[tuple[str, str], Optional[float]] = {}  # domain left out on both sides
        for template in templates:
            expected = expected_ids(profile, corpus, id_map, template)
            unnameable = len(unknown_ids(profile, corpus, id_map, template))
            for fmt in formats:
                parsed = rendered[(profile.profile_id, template, fmt)]
                name = f"{profile.profile_id}/{template}__{fmt}"
                if parsed is None:
                    no_resume += 1
                    raws[(template, fmt)] = stripped[(template, fmt)] = None
                else:
                    scored = engine.score(parsed, posting, as_of=as_of, options=BULK)
                    raws[(template, fmt)] = scored.raw_score
                    dims[(template, fmt)] = {k: d.value for k, d in scored.dimensions.items()}
                    stripped[(template, fmt)] = engine.score(_without_industry(parsed), posting, as_of=as_of,
                                                             options=BULK).raw_score
                got = skill_ids(parsed) if parsed is not None else set()
                hit = expected & got
                found += len(hit)
                wanted += len(expected)
                wanted_all += len(expected) + unnameable
                extra += len(got - expected)
                if expected:
                    recoveries.append((len(hit) / len(expected), name, sorted(expected - got)))
                if template != "functional":
                    error, notes = date_error(oracle, parsed, as_of)
                    date_errors.append((error, name, notes))
                    if stripped[(template, fmt)] is not None and oracle_raw is not None:
                        parse_loss.append(abs(stripped[(template, fmt)] - oracle_raw))
        for template in templates:
            values = [raws[(template, f)] for f in formats]
            within.append((_spread(values), f"{profile.profile_id}/{template}",
                           {"raws": {f: raws[(template, f)] for f in formats},
                            "dimensions": {f: dims.get((template, f)) for f in formats}}))
        equivalent = [t for t in templates if t != "functional"]
        if equivalent:
            values = [raws[(t, f)] for t in equivalent for f in formats]
            across.append((_spread(values), profile.profile_id,
                           {"raws": {f"{t}__{f}": raws[(t, f)] for t in equivalent for f in formats},
                            "dimensions": {f"{t}__{f}": dims.get((t, f)) for t in equivalent for f in formats}}))
        if "functional" in templates:
            functional_oracle = _without_industry(
                id_map.resume(expected_resume(profile, corpus.vocab, as_of, template="functional")))
            target = engine.score(functional_oracle, posting, as_of=as_of, options=BULK).raw_score
            for fmt in formats:
                got_raw = stripped[("functional", fmt)]
                dev = UNBOUNDED if got_raw is None or target is None else abs(got_raw - target)
                functional_dev.append((dev, f"{profile.profile_id}/functional__{fmt}",
                                       {"parsed": got_raw, "oracle": target,
                                        "dimensions": {fmt: dims.get(("functional", fmt))}}))

    recovery = found / wanted if wanted else None
    recovery_all = found / wanted_all if wanted_all else None
    dates = [e for e, _, _ in date_errors]
    report.metrics.update({
        "parser_profiles": len(profiles),
        "parser_renderings": len(profiles) * len(templates) * len(formats),
        "parser_no_resume": no_resume,
        "recovered_skills": recovery,
        "recovered_skills_all": recovery_all,
        "extra_skills_per_rendering": extra / max(1, len(profiles) * len(templates) * len(formats)),
        "date_error_max_months": max(dates) if dates else None,
        "date_error_p99_months": _p99(dates),
        "date_error_share_within_1": (sum(1 for e in dates if e <= DATE_ERROR_MAX) / len(dates)) if dates else None,
        "spread_within_template_p99": _p99([s for s, _, _ in within]),
        "spread_within_template_max": max((s for s, _, _ in within), default=None),
        "spread_across_templates_p99": _p99([s for s, _, _ in across]),
        "spread_across_templates_max": max((s for s, _, _ in across), default=None),
        "functional_deviation_p99": _p99([d for d, _, _ in functional_dev]),
        "parse_loss_mean": (math.fsum(parse_loss) / len(parse_loss)) if parse_loss else None,
        "taxonomy_unknown_skills": len(id_map.unknown),
    })
    report.worst = {
        "within_template": [{"case": c, "spread": s, "raws": r["raws"]}
                            for s, c, r in sorted(within, key=lambda x: -x[0])[:15]],
        "across_templates": [{"case": c, "spread": s} for s, c, _ in sorted(across, key=lambda x: -x[0])[:15]],
        "functional": [{"case": c, "deviation": d, **r} for d, c, r in sorted(functional_dev, key=lambda x: -x[0])[:15]],
        "date_error": [{"case": c, "error": e, "notes": n} for e, c, n in sorted(date_errors, key=lambda x: -x[0])[:15]],
        "recovery": [{"case": c, "recovered": r, "missing": m} for r, c, m in sorted(recoveries, key=lambda x: x[0])[:15]],
    }

    def p99_guard(name: str, rows: list, detail: str, kind: str) -> Guardrail:
        value = _p99([r[0] for r in rows])
        return Guardrail(name, None if value is None else value <= INVARIANCE_MAX, value,
                         f"p99 <= {INVARIANCE_MAX}", detail, kind)

    report.guardrails += [
        p99_guard("Format invariance: formats within a template", within,
                  f"{len(within)} (profile, template) groups of {len(formats)} formats", "invariance within"),
        p99_guard("Format invariance: across equivalent templates", across,
                  f"{len(across)} profiles x {len(templates) - ('functional' in templates)} templates x "
                  f"{len(formats)} formats", "invariance across"),
        p99_guard("Format invariance: functional vs its oracle", functional_dev, f"{len(functional_dev)} renderings",
                  "invariance functional"),
        Guardrail("Parser fidelity: recovered skills (all vocabulary)",
                  None if recovery_all is None else recovery_all >= RECOVERY_MIN, recovery_all,
                  f">= {RECOVERY_MIN:.0%}", f"{found}/{wanted_all} skills shown, the {len(id_map.unknown)} the "
                  f"taxonomy cannot name included (taxonomy-known only: {_fmt(recovery)}, a diagnostic)",
                  "recovery"),
        Guardrail("Parser fidelity: date error (months)", None if not dates else max(dates) <= DATE_ERROR_MAX,
                  max(dates) if dates else None, f"max <= {DATE_ERROR_MAX}",
                  f"{len(dates)} dated renderings; p99 {_fmt(report.metrics['date_error_p99_months'])}, "
                  f"{report.metrics['date_error_share_within_1']:.1%} within 1 month" if dates else "none",
                  "date error"),
    ]

    def replay(case: str) -> dict[str, Any]:
        profile_id, _, rest = case.partition("/")
        template, _, fmt = rest.partition("__")
        return {"profile_id": profile_id, "template": template or None, "format": fmt or None,
                "corpus_seed": corpus.seed, "how": "eval.suites.synthetic.parse_renderings on this profile "
                "(synthetic_corpus(500, 7)) with real_parser(), then score against its target posting"}

    def bounded(value: float) -> Any:
        return value if math.isfinite(value) else "unbounded"

    def counts(failing: int, unit: str) -> dict[str, Any]:
        written = min(failing, _WRITTEN)
        return {"failing_total": failing, "written": written,
                "summary": f"{failing:,} failing {unit}, {written} worst written"}

    for rows, kind, label, unit in ((within, "invariance within", "spread", "(profile, template) groups"),
                                    (across, "invariance across", "spread", "profiles"),
                                    (functional_dev, "invariance functional", "deviation", "renderings")):
        failing = sum(1 for row in rows if row[0] > INVARIANCE_MAX)
        for value, case, info in sorted(rows, key=lambda x: -x[0])[:_WRITTEN]:
            if value > INVARIANCE_MAX:
                report.failures.append(Failure(f"{kind.replace(' ', '_')}__{case.replace('/', '__')}", kind,
                                               {**replay(case), **counts(failing, unit), label: bounded(value),
                                                **info}))
    failing = sum(1 for row in date_errors if row[0] > DATE_ERROR_MAX)
    for error, case, notes in sorted(date_errors, key=lambda x: -x[0])[:_WRITTEN]:
        if error > DATE_ERROR_MAX:
            report.failures.append(Failure(f"date_error__{case.replace('/', '__')}", "date error",
                                           {**replay(case), **counts(failing, "renderings"),
                                            "error_months": bounded(error), "notes": notes}))
    failing = sum(1 for row in recoveries if row[0] < RECOVERY_MIN)
    for share, case, missing in sorted(recoveries, key=lambda x: x[0])[:_WRITTEN]:
        if share < RECOVERY_MIN:
            report.failures.append(Failure(f"recovery__{case.replace('/', '__')}", "recovery",
                                           {**replay(case), **counts(failing, "renderings"), "recovered": share,
                                            "missing": missing}))
    return report


# ─── The suite ─────────────────────────────────────────────────────────────────────────────


_PARSER_GUARDRAILS = (
    ("Format invariance: formats within a template", f"p99 <= {INVARIANCE_MAX}", "invariance within"),
    ("Format invariance: across equivalent templates", f"p99 <= {INVARIANCE_MAX}", "invariance across"),
    ("Format invariance: functional vs its oracle", f"p99 <= {INVARIANCE_MAX}", "invariance functional"),
    ("Parser fidelity: recovered skills (all vocabulary)", f">= {RECOVERY_MIN:.0%}", "recovery"),
    ("Parser fidelity: date error (months)", f"max <= {DATE_ERROR_MAX}", "date error"),
)


def default_jobs() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 2))


def run_synthetic(engine: Any, cf_engine: Any, corpus: Corpus, id_map: IdMap, pairs: Sequence[CorpusPair], *,
                  closed_form_n: int, n_resamples: int = 1000, parse_profiles: Sequence[Any] = (),
                  parse: Optional[ParseFn] = None, parse_factory: Optional[Callable[[], ParseFn]] = None,
                  parser_reason: str = "", cache: Optional[ParseCache] = None, jobs: int = 1,
                  templates: Optional[Sequence[str]] = None, formats: Optional[Sequence[str]] = None,
                  log: Callable[[str], None] = lambda _: None) -> SuiteResult:
    from eval.generate.render import FORMATS, TEMPLATES
    from eval.suites import closed_form, mutations

    result = SuiteResult(name="synthetic", layer=2)
    with Timer() as timer:
        # (a) analytic correctness
        constructed = closed_form.constructed_pairs(closed_form_n, seed=0)
        mismatches, worst = closed_form.check(constructed, cf_engine)
        result.metrics["closed_form_pairs"] = closed_form_n
        result.metrics["closed_form_max_abs_error"] = worst
        result.guardrails.append(Guardrail(
            "Analytic correctness (closed form)", not mismatches, worst, f"abs(engine - closed form) <= "
            f"{closed_form.TOLERANCE}", f"{closed_form_n} constructed pairs, {len(mismatches)} mismatches",
            "analytic correctness"))
        for m in mismatches[:25]:
            result.failures.append(Failure(m.case.replace(":", "_"), "analytic correctness", {
                "engine_raw": m.engine_raw, "expected_raw": m.expected_raw,
                "engine_dimensions": m.engine_dimensions, "expected_dimensions": m.expected_dimensions,
                "resume": m.pair.resume.model_dump(mode="json"), "posting": m.pair.posting.model_dump(mode="json"),
                "as_of": m.pair.as_of.isoformat(), "graph": "eval.suites.closed_form.closed_form_graph()"}))
        # mutations (EVAL 2.3)
        outcomes = mutations.run_mutations(cf_engine)
        failed = [o for o in outcomes if not o.passed]
        result.metrics["mutations"] = {o.name: {"passed": o.passed, "before": o.before, "after": o.after,
                                                "delta": o.delta, "expectation": o.expectation} for o in outcomes}
        result.guardrails.append(Guardrail("Mutation suite", not failed, float(len(failed)), "0 failing cases",
                                           f"{len(outcomes)} cases" + (": " + ", ".join(o.name for o in failed)
                                                                       if failed else ""), "mutation"))
        for o in failed:
            result.failures.append(Failure(f"mutation__{o.name}", "mutation", {"problems": o.problems, **o.detail}))
        # (b) ranking
        result.pairs = score_corpus(engine, pairs, corpus.as_of)
        result.metrics["ranking"] = ranking_metrics(result.pairs, n_resamples=n_resamples)
        result.metrics["ranking_pairs"] = len(result.pairs)
        result.metrics["ranking_postings"] = len({p.posting_id for p in result.pairs})
        result.guardrails.append(fairness_guardrail(result.pairs))
        translated = [id_map.posting(p) for p in corpus.by_id.values()]
        result.guardrails.append(coverage_guardrail(translated))
        result.metrics["taxonomy_coverage"] = taxonomy_coverage(translated)
        labels = {s.id: s.label for s in (*corpus.vocab.skills, *corpus.vocab.fields)}
        result.metrics["taxonomy_unknown_labels"] = sorted(labels.get(i, i) for i in id_map.unknown)
        # (c) parser fidelity and format invariance
        if parse is None and parse_factory is None:
            result.notes.append(f"PARSER: skipped: parser not available{': ' + parser_reason if parser_reason else ''}")
            result.metrics["parser"] = "skipped: parser not available"
        elif not parse_profiles:
            result.notes.append("PARSER: skipped by --sample 0")
            result.metrics["parser"] = "skipped by --sample 0"
            result.guardrails += [Guardrail(name, None, None, threshold, "skipped by --sample 0", kind)
                                  for name, threshold, kind in _PARSER_GUARDRAILS]
        else:
            templates = tuple(templates or TEMPLATES)
            formats = tuple(formats or FORMATS)
            with Timer() as parse_timer:
                rendered, stats = parse_renderings(parse_profiles, corpus.seed, templates, formats, parse=parse,
                                                   parse_factory=parse_factory, cache=cache, jobs=jobs, log=log)
            postings = {pid: id_map.posting(p) for pid, p in corpus.by_id.items()}
            report = fidelity(corpus, id_map, engine, parse_profiles, rendered, templates, formats, postings)
            result.metrics["parser"] = {**report.metrics, **{f"parse_{k}": v for k, v in stats.items()},
                                        "parse_seconds": parse_timer.seconds}
            result.metrics["parser_worst"] = report.worst
            result.guardrails += report.guardrails
            result.failures += report.failures
            result.notes.append(f"PARSER: {len(parse_profiles)} profiles x {len(templates)} templates x "
                                f"{len(formats)} formats ({stats['cached']} cached, {stats['parsed']} parsed, "
                                f"{parse_timer.seconds:.1f} s)")
    result.seconds = timer.seconds
    return result
