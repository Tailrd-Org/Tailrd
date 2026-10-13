"""Layer 1, properties (EVAL sections 1 and 3): checks that need no labels, over generated pairs.

Every check is seeded, so a failure replays from its case name. On the synthetic corpus's pairs
(oracle résumé against its target posting, in the taxonomy's namespace):

* boundedness: raw in [0, 1], calibrated and displayed in [0, 100], confidence and every dimension
  value in [0, 1], effective weights summing to 1 whenever anything scored;
* monotonicity: adding a skill the posting asks for never lowers raw, removing a REQUIRED skill
  the résumé holds never raises it. The skill is added as a skills-list entry and, separately, as
  an OWNERSHIP bullet of the most recent role;
* gate precedence: a failing gate (a professional licence the résumé does not hold) gives
  GATE_FAIL with no raw, calibrated or displayed score, however strong the pair (SPEC 4, 10);
* determinism: the same pair scored `runs` times (default options, explanation included) gives
  one sha256 of the canonical result JSON;
* totality: `n` fuzzed inputs (junk values, mutated résumé/posting JSON, odd `as_of` values, valid
  models with random content) never raise and always return a MatchResult (SPEC 13);
* latency: p95 per pair under 150 ms with the default options;
* stuffing defense: a résumé that is every requirement of a posting pasted as a skills list scores
  below 0.85 (EVAL section 3); with a parser, also the posting's requirement lines pasted as text.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from typing import Any, Optional

from eval.suites import BULK, Failure, Guardrail, SuiteResult, Timer, percentile
from matcher.models import (
    Evidence,
    GateStatus,
    JobPosting,
    LicenseGate,
    MatchResult,
    Necessity,
    Outcome,
    Resume,
    ScoreOptions,
    SkillRef,
    SkillSource,
    VerbClass,
    canonical_json,
)

LATENCY_P95_MS = 150.0
STUFFING_MAX = 0.85
EPS = 1e-9
UNHELD_LICENCE = "cred.registered_nurse"  # a professional licence (config/gates.yaml) nobody here holds

Pair = tuple[str, Resume, JobPosting]  # (case id, résumé, posting)
ParseFn = Callable[[bytes, str], Optional[Resume]]


def _ids(resume: Resume) -> set[str]:
    refs = [*resume.skills_blob, *resume.certifications]
    refs += [r for role in resume.roles for r in role.skills]
    refs += [r for project in resume.projects for r in project.skills]
    return {r.canonical_id for r in refs}


def _replay(case: str, resume: Any, posting: Any, as_of: Any) -> dict[str, Any]:
    def dump(value: Any) -> Any:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        try:
            json.dumps(value)
            return value
        except (TypeError, ValueError):
            return repr(value)[:2000]

    return {"case": case, "resume": dump(resume), "posting": dump(posting),
            "as_of": as_of.isoformat() if isinstance(as_of, date) else repr(as_of)}


# ─── Boundedness ───────────────────────────────────────────────────────────────────────────


def bound_problems(result: MatchResult) -> list[str]:
    problems = []
    if result.raw_score is not None and not 0.0 <= result.raw_score <= 1.0:
        problems.append(f"raw {result.raw_score}")
    for name in ("calibrated_score", "display_score"):
        value = getattr(result, name)
        if value is not None and not 0 <= value <= 100:
            problems.append(f"{name} {value}")
    if result.confidence is not None and not 0.0 <= result.confidence.value <= 1.0:
        problems.append(f"confidence {result.confidence.value}")
    for key, dimension in result.dimensions.items():
        if dimension.value is not None and not 0.0 <= dimension.value <= 1.0:
            problems.append(f"{key} {dimension.value}")
    if result.dimensions and any(not d.abstained for d in result.dimensions.values()):
        total = math.fsum(d.effective_weight for d in result.dimensions.values())
        if not math.isclose(total, 1.0, abs_tol=1e-9):
            problems.append(f"effective weights sum to {total}")
    return problems


def check_bounded(engine: Any, pairs: Sequence[Pair], as_of: date) -> tuple[Guardrail, list[Failure]]:
    failures = []
    for case, resume, posting in pairs:
        problems = bound_problems(engine.score(resume, posting, as_of=as_of, options=BULK))
        if problems:
            failures.append(Failure(f"bounded__{case}", "boundedness",
                                    {**_replay(case, resume, posting, as_of), "problems": problems}))
    return Guardrail("Boundedness", not failures, float(len(failures)), "0 out-of-range values",
                     f"{len(pairs)} pairs", "boundedness"), failures


# ─── Monotonicity ──────────────────────────────────────────────────────────────────────────


def _add_listed(resume: Resume, skill_id: str) -> Resume:
    ref = SkillRef(canonical_id=skill_id, surface_form=skill_id, evidence=Evidence.SKILLS_BLOB,
                   source=SkillSource.SKILLS_SECTION)
    return resume.model_copy(update={"skills_blob": resume.skills_blob + (ref,)})


def _add_bullet(resume: Resume, skill_id: str) -> Optional[Resume]:
    if not resume.roles:
        return None
    ref = SkillRef(canonical_id=skill_id, surface_form=skill_id, evidence=Evidence.ROLE_BULLET,
                   verb_class=VerbClass.OWNERSHIP, source=SkillSource.ROLE)
    first, *rest = resume.roles
    return resume.model_copy(update={"roles": (first.model_copy(update={"skills": first.skills + (ref,)}), *rest)})


def _remove(resume: Resume, skill_id: str) -> Resume:
    def keep(refs: tuple[SkillRef, ...]) -> tuple[SkillRef, ...]:
        return tuple(r for r in refs if r.canonical_id != skill_id)

    return resume.model_copy(update={
        "roles": tuple(r.model_copy(update={"skills": keep(r.skills)}) for r in resume.roles),
        "projects": tuple(p.model_copy(update={"skills": keep(p.skills)}) for p in resume.projects),
        "skills_blob": keep(resume.skills_blob),
        "certifications": keep(resume.certifications),
    })


def check_monotone(engine: Any, pairs: Sequence[Pair], as_of: date, seed: int = 0) -> tuple[
        list[Guardrail], list[Failure]]:
    """Add a missing requirement (listed, then as a bullet) never lowers raw; removing a held
    REQUIRED skill never raises it. One random choice per pair, seeded by the case id."""
    failures: list[Failure] = []
    checked = {"add": 0, "remove": 0}
    broken = {"add": 0, "remove": 0}
    for case, resume, posting in pairs:
        rng = random.Random(f"monotone:{seed}:{case}")
        base = engine.score(resume, posting, as_of=as_of, options=BULK).raw_score
        if base is None:
            continue
        held = _ids(resume)
        missing = sorted({r.skill_id for r in posting.requirements} - held)
        held_required = sorted({r.skill_id for r in posting.requirements
                                if Necessity(r.necessity) is Necessity.REQUIRED} & held)
        variants: list[tuple[str, str, Optional[Resume]]] = []
        if missing:
            skill = rng.choice(missing)
            variants += [("add", f"listed {skill}", _add_listed(resume, skill)),
                         ("add", f"bullet {skill}", _add_bullet(resume, skill))]
        if held_required:
            skill = rng.choice(held_required)
            variants.append(("remove", f"remove {skill}", _remove(resume, skill)))
        for kind, what, changed in variants:
            if changed is None:
                continue
            after = engine.score(changed, posting, as_of=as_of, options=BULK).raw_score
            if after is None:
                continue
            checked[kind] += 1
            wrong = after < base - EPS if kind == "add" else after > base + EPS
            if wrong:
                broken[kind] += 1
                failures.append(Failure(
                    f"monotone_{kind}__{case}", "monotonicity",
                    {**_replay(case, changed, posting, as_of), "change": what, "raw_before": base,
                     "raw_after": after, "resume_before": resume.model_dump(mode="json")}))
    guardrails = [
        Guardrail("Monotonicity: add a matching skill", not broken["add"], float(broken["add"]),
                  "0 decreases", f"{checked['add']} additions", "monotonicity"),
        Guardrail("Monotonicity: remove a required skill", not broken["remove"], float(broken["remove"]),
                  "0 increases", f"{checked['remove']} removals", "monotonicity"),
    ]
    return guardrails, failures


# ─── Gate precedence ───────────────────────────────────────────────────────────────────────


def check_gate_precedence(engine: Any, pairs: Sequence[Pair], as_of: date) -> tuple[Guardrail, list[Failure]]:
    failures = []
    gate = LicenseGate(credential_ids=(UNHELD_LICENCE,), surface_form="Registered Nurse licence")
    for case, resume, posting in pairs:
        if UNHELD_LICENCE in _ids(resume):
            continue
        gated = posting.model_copy(update={"gates": posting.gates + (gate,)})
        result = engine.score(resume, gated, as_of=as_of, options=ScoreOptions())
        ok = (Outcome(result.outcome) is Outcome.GATE_FAIL and GateStatus(result.gate_status) is GateStatus.FAIL
              and result.raw_score is None and result.calibrated_score is None and result.display_score is None)
        if not ok:
            failures.append(Failure(f"gate_precedence__{case}", "gate precedence",
                                    {**_replay(case, resume, gated, as_of), "outcome": result.outcome.value,
                                     "raw": result.raw_score}))
    return Guardrail("Gate precedence", not failures, float(len(failures)), "0 scored ineligible pairs",
                     f"{len(pairs)} pairs with an unheld licence gate", "gate precedence"), failures


# ─── Determinism ───────────────────────────────────────────────────────────────────────────


def result_hash(result: MatchResult) -> str:
    return hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()


def check_determinism(engine: Any, pairs: Sequence[Pair], as_of: date, runs: int = 1000) -> tuple[
        Guardrail, list[Failure]]:
    failures = []
    for case, resume, posting in pairs:
        hashes = {result_hash(engine.score(resume, posting, as_of=as_of, options=ScoreOptions()))
                  for _ in range(runs)}
        if len(hashes) != 1:
            failures.append(Failure(f"determinism__{case}", "determinism",
                                    {**_replay(case, resume, posting, as_of), "distinct_hashes": sorted(hashes)}))
    return Guardrail("Determinism", not failures, float(len(failures)), "1 hash per pair",
                     f"{len(pairs)} pairs x {runs} runs", "determinism"), failures


# ─── Totality ──────────────────────────────────────────────────────────────────────────────

_JUNK: tuple[Any, ...] = (
    None, 0, -1, 1, 10**20, 1e308, -1e308, float("nan"), float("inf"), "", "x" * 5000, "\x00�‮",
    b"", b"\xff\xfe\x00", [], {}, [1, "a", None], {"roles": 5}, {"requirements": "x"}, "{", "null", "[]",
    "{\"roles\": [{\"start\": 7}]}", True, False, object(), (1, 2), {1: 2}, date(2026, 1, 1), 3.5,
)
_DATES: tuple[Any, ...] = (
    date(2026, 10, 1), date.min, date.max, date(1900, 1, 1), datetime(2026, 10, 1, 12, 0), "2026-10-01", None,
    0, float("nan"), date(2000, 2, 29),
)
_ODD_STRINGS = ("", " ", "2026-13-45", "9999-12-31", "0001-01-01", "-5", "NaN", "\u0000", "é" * 300,
                "IC9", "M0", "REQUIRED ", "skill." + "a" * 300)


def _paths(value: Any, prefix: tuple = ()) -> list[tuple]:
    out = [prefix]
    if isinstance(value, dict):
        for key, child in value.items():
            out += _paths(child, prefix + (key,))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            out += _paths(child, prefix + (i,))
    return out


def _get(value: Any, path: tuple) -> Any:
    for step in path:
        value = value[step]
    return value


def _mutate_json(rng: random.Random, data: Any, ops: int) -> Any:
    data = json.loads(json.dumps(data, default=repr))
    for _ in range(ops):
        paths = _paths(data)
        path = rng.choice(paths)
        if not path:
            return rng.choice(_JUNK)
        parent, key = _get(data, path[:-1]), path[-1]
        current = parent[key]
        op = rng.random()
        if op < 0.2 and isinstance(parent, dict):
            del parent[key]
        elif op < 0.2 and isinstance(parent, list):
            parent.pop(key)
        elif op < 0.4:
            parent[key] = rng.choice(_JUNK)
        elif op < 0.55 and isinstance(current, (int, float)) and not isinstance(current, bool):
            parent[key] = rng.choice((-current, current * 1e9, -1, 0, 1e-300, 61, 1201, 100001))
        elif op < 0.7:
            parent[key] = rng.choice(_ODD_STRINGS)
        elif op < 0.8 and isinstance(current, list) and current:
            parent[key] = current + current * rng.randint(1, 3)
        elif op < 0.9:
            parent[key] = [] if isinstance(current, dict) else ({} if isinstance(current, list) else [current])
        else:
            parent[key] = rng.choice((True, False, None))
    return data


def _random_model(rng: random.Random, pairs: Sequence[Pair]) -> tuple[Resume, JobPosting]:
    """Two pairs' parts crossed, skills reassigned to random evidence and polarity: valid models
    with content no generator would write."""
    _, resume, _ = rng.choice(pairs)
    _, _, posting = rng.choice(pairs)
    all_ids = sorted(_ids(resume) | {r.skill_id for r in posting.requirements})

    def shuffled(refs: tuple[SkillRef, ...]) -> tuple[SkillRef, ...]:
        return tuple(r.model_copy(update={
            "canonical_id": rng.choice(all_ids) if all_ids else r.canonical_id,
            "evidence": rng.choice(tuple(Evidence)),
            "verb_class": rng.choice(tuple(VerbClass)),
            "polarity": rng.choice(tuple(type(r.polarity))),
        }) for r in refs)

    resume = resume.model_copy(update={
        "roles": tuple(role.model_copy(update={"skills": shuffled(role.skills)}) for role in resume.roles),
        "skills_blob": shuffled(resume.skills_blob),
    })
    return resume, posting


def fuzz_inputs(rng: random.Random, pairs: Sequence[Pair], as_of: date) -> tuple[Any, Any, Any]:
    _, resume, posting = rng.choice(pairs)
    kind = rng.random()
    if kind < 0.1:
        return rng.choice(_JUNK), rng.choice(_JUNK), as_of
    if kind < 0.2:
        return resume, posting, rng.choice(_DATES)
    if kind < 0.35:
        return _mutate_json(rng, resume.model_dump(mode="json"), rng.randint(1, 4)), posting, as_of
    if kind < 0.5:
        return resume, _mutate_json(rng, posting.model_dump(mode="json"), rng.randint(1, 4)), as_of
    if kind < 0.6:
        both = (_mutate_json(rng, resume.model_dump(mode="json"), rng.randint(1, 3)),
                _mutate_json(rng, posting.model_dump(mode="json"), rng.randint(1, 3)))
        as_json = rng.random() < 0.5
        return ((json.dumps(both[0], default=repr) if as_json else both[0]),
                (json.dumps(both[1], default=repr) if as_json else both[1]), as_of)
    model = _random_model(rng, pairs)
    return model[0], model[1], rng.choice((as_of, as_of, as_of, date(2015, 1, 1)))


def check_totality(engine: Any, pairs: Sequence[Pair], as_of: date, n: int, seed: int = 0) -> tuple[
        Guardrail, list[Failure]]:
    """0 exceptions over `n` fuzzed inputs; every answer a MatchResult. One case in four runs with
    the default options (explanation on), the rest with the bulk options."""
    rng = random.Random(f"totality:{seed}")
    failures: list[Failure] = []
    exceptions = 0
    for i in range(n):
        resume, posting, day = fuzz_inputs(rng, pairs, as_of)
        options = ScoreOptions() if i % 4 == 0 else BULK
        try:
            result = engine.score(resume, posting, as_of=day, options=options)
            if not isinstance(result, MatchResult):
                raise TypeError(f"returned {type(result).__name__}, not a MatchResult")
        except Exception as exc:  # the property under test: nothing may escape
            exceptions += 1
            if len(failures) < 25:
                failures.append(Failure(f"totality__{seed}_{i}", "totality",
                                        {**_replay(f"totality:{seed}:{i}", resume, posting, day),
                                         "exception": f"{type(exc).__name__}: {exc}"[:2000]}))
    return Guardrail("Totality", exceptions == 0, float(exceptions), "0 exceptions",
                     f"{n} fuzzed inputs", "totality"), failures


# ─── Latency ───────────────────────────────────────────────────────────────────────────────


def check_latency(engine: Any, pairs: Sequence[Pair], as_of: date) -> tuple[Guardrail, dict[str, float]]:
    timings: list[float] = []
    bulk: list[float] = []
    for _, resume, posting in pairs:
        start = time.perf_counter()
        engine.score(resume, posting, as_of=as_of, options=ScoreOptions())
        timings.append((time.perf_counter() - start) * 1000.0)
        start = time.perf_counter()
        engine.score(resume, posting, as_of=as_of, options=BULK)
        bulk.append((time.perf_counter() - start) * 1000.0)
    p95 = percentile(timings, 95)
    stats = {"latency_p50_ms": percentile(timings, 50), "latency_p95_ms": p95,
             "latency_bulk_p95_ms": percentile(bulk, 95)}
    passed = None if p95 is None else p95 < LATENCY_P95_MS
    return Guardrail("Latency p95 (ms/pair)", passed, p95, f"< {LATENCY_P95_MS:.0f}",
                     f"{len(pairs)} pairs, default options (bulk p95 {stats['latency_bulk_p95_ms']:.1f} ms)"
                     if p95 is not None else "no pairs"), stats


# ─── Stuffing defense ──────────────────────────────────────────────────────────────────────


def pasted_posting_text(posting: JobPosting) -> str:
    lines = [posting.title_raw or "Job posting", "", "Requirements"]
    lines += [f"- {r.surface_form or r.skill_id}" for r in posting.requirements]
    return "\n".join(lines) + "\n"


def check_stuffing(engine: Any, postings: Sequence[tuple[str, JobPosting]], as_of: date,
                   parse: Optional[ParseFn] = None) -> tuple[Guardrail, list[Failure], dict[str, float]]:
    failures = []
    worst = 0.0
    worst_parsed: Optional[float] = None
    for posting_id, posting in postings:
        pasted = Resume(skills_blob=tuple(
            SkillRef(canonical_id=r.skill_id, surface_form=r.surface_form, evidence=Evidence.SKILLS_BLOB,
                     source=SkillSource.SKILLS_SECTION) for r in posting.requirements))
        readings: list[tuple[str, Optional[Resume]]] = [("listed", pasted)]
        if parse is not None:
            readings.append(("parsed", parse(pasted_posting_text(posting).encode("utf-8"), "posting.txt")))
        for how, resume in readings:
            if resume is None:
                continue
            raw = engine.score(resume, posting, as_of=as_of, options=BULK).raw_score
            if raw is None:
                continue
            if how == "listed":
                worst = max(worst, raw)
            else:
                worst_parsed = raw if worst_parsed is None else max(worst_parsed, raw)
            if raw >= STUFFING_MAX:
                failures.append(Failure(f"stuffing__{how}__{posting_id}", "stuffing defense",
                                        {**_replay(posting_id, resume, posting, as_of), "raw": raw}))
    top = max(worst, worst_parsed or 0.0)
    detail = f"{len(postings)} postings; listed max {worst:.3f}"
    if worst_parsed is not None:
        detail += f", parsed text max {worst_parsed:.3f}"
    metrics = {"stuffing_listed_max": worst}
    if worst_parsed is not None:
        metrics["stuffing_parsed_max"] = worst_parsed
    return Guardrail("Stuffing defense (max raw)", not failures if postings else None, top,
                     f"< {STUFFING_MAX}", detail, "stuffing defense"), failures, metrics


# ─── The suite ─────────────────────────────────────────────────────────────────────────────


def run_properties(engine: Any, pairs: Sequence[Pair], postings: Sequence[tuple[str, JobPosting]], as_of: date, *,
                   fuzz_n: int, determinism_runs: int = 1000, determinism_sample: int = 3,
                   monotone_sample: int = 200, latency_sample: int = 200, seed: int = 0,
                   parse: Optional[ParseFn] = None) -> SuiteResult:
    rng = random.Random(f"properties:{seed}")
    sample = list(pairs)
    rng.shuffle(sample)
    result = SuiteResult(name="properties", layer=1)
    with Timer() as timer:
        checks = [
            check_bounded(engine, pairs, as_of),
            check_gate_precedence(engine, sample[:monotone_sample], as_of),
            check_determinism(engine, sample[:determinism_sample], as_of, determinism_runs),
            check_totality(engine, pairs, as_of, fuzz_n, seed),
        ]
        monotone, monotone_failures = check_monotone(engine, sample[:monotone_sample], as_of, seed)
        latency, stats = check_latency(engine, sample[:latency_sample], as_of)
        stuffing, stuffing_failures, stuffing_metrics = check_stuffing(engine, postings, as_of, parse)
    for guardrail, failures in checks:
        result.guardrails.append(guardrail)
        result.failures.extend(failures)
    result.guardrails[1:1] = monotone
    result.failures.extend(monotone_failures + stuffing_failures)
    result.guardrails += [latency, stuffing]
    result.metrics.update(stats)
    result.metrics.update(stuffing_metrics)
    result.metrics["fuzzed_inputs"] = float(fuzz_n)
    result.seconds = timer.seconds
    return result
