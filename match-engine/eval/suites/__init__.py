"""Evaluation suites (EVAL.md sections 1-3) and what they share.

Each suite returns a `SuiteResult`: metrics with bootstrap intervals, guardrails (hard pass/fail),
reproducible failure cases and, for the synthetic ranking suite, the per-pair scores that baselines
diff against. `eval.report` turns results into `reports/<timestamp>/`; `eval.run` is the CLI.

What the suites share:

* `Engine`: config + skill graph + `FixedCalibrator` (identity calibration, `calibrated = round(100 x
  raw)`) until Milestone 7's calibration lands; every raw-score metric is computed on it.
* `synthetic_corpus()`: Task 5.1's generator in memory (default vocabulary, 5 postings per family,
  500 profiles, seed 7, as of `GENERATION_AS_OF`).
* `IdMap`: the generator's vocabulary uses its own ids (`skill.<slug>`); the real taxonomy sometimes
  names the same skill differently ("Golang" is `skill.go`) or not at all ("Code Review"). Each
  vocabulary skill is translated to the id the real resolver gives its label or an alias, EXACT or
  ALIAS only (the methods the parser's extractor uses), so that oracle résumés, postings and parsed
  résumés all live in the taxonomy's namespace. A skill no spelling resolves is listed in
  `IdMap.unknown`: the taxonomy cannot know it and no parser can recover it, so translated résumés
  drop its mentions and translated postings carry it as an unresolved requirement token.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from eval.metrics import PairScore
from matcher.models import (
    Band,
    Education,
    EducationRequirement,
    JobPosting,
    JobRequirement,
    MatchResult,
    Necessity,
    Outcome,
    Resume,
    ScoreOptions,
    SkillRef,
)

ROOT = Path(__file__).resolve().parents[2]  # match-engine/
TAXONOMY_DIR = ROOT / "data" / "taxonomy"

CORPUS_SEED = 7
POSTINGS_PER_FAMILY = 5
N_PROFILES = 500

# Bulk sweeps skip the explanation layer (Task 9.1 carry note); latency is measured on the default.
BULK = ScoreOptions(with_explanation=False)

BAND_INDEX = {Band.LIMITED: 0, Band.PARTIAL: 1, Band.GOOD: 2, Band.STRONG: 3}


# ─── Results ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Guardrail:
    """A hard check (EVAL section 3): `passed` None means it could not be assessed, never a pass."""

    name: str
    passed: Optional[bool]
    value: Optional[float]
    threshold: str
    detail: str = ""
    kind: str = ""  # the `Failure.kind` of the cases that back it ("" = none)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "value": self.value,
                "threshold": self.threshold, "detail": self.detail, "kind": self.kind}


@dataclass(frozen=True)
class Failure:
    """One failed case with everything needed to replay it (`eval.report` writes it to
    `failures/<case>.json`)."""

    case: str
    kind: str
    detail: dict[str, Any]


@dataclass
class SuiteResult:
    name: str
    layer: int
    status: str = "ran"  # "ran" or "skipped"
    reason: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)
    guardrails: list[Guardrail] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    pairs: list[PairScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def failed(self) -> bool:
        return any(g.passed is False for g in self.guardrails)


def skipped(name: str, layer: int, reason: str) -> SuiteResult:
    return SuiteResult(name=name, layer=layer, status="skipped", reason=reason)


class Timer:
    def __enter__(self) -> "Timer":
        self.start = time.perf_counter()
        self.seconds = 0.0
        return self

    def __exit__(self, *exc: object) -> None:
        self.seconds = time.perf_counter() - self.start


def percentile(values: Sequence[float], q: float) -> Optional[float]:
    """The q-th percentile (0..100) with linear interpolation between order statistics (numpy's
    default), computed without numpy; None for no values."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * q / 100.0
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    if fraction == 0.0 or ordered[high] == ordered[low]:
        return float(ordered[low])
    if math.isinf(ordered[high]):
        return math.inf  # interpolating towards an unbounded value is unbounded, never NaN
    return float(ordered[low] + (ordered[high] - ordered[low]) * fraction)


# ─── The engine under test ─────────────────────────────────────────────────────────────────


class Engine:
    """`matcher.score.engine.score` with a fixed config, graph and calibrator."""

    def __init__(self, config: Any, graph: Any, calibration: Any = None) -> None:
        from matcher.calibrate.base import FixedCalibrator

        self.config = config
        self.graph = graph
        self.calibration = calibration if calibration is not None else FixedCalibrator()

    def score(self, resume: Any, posting: Any, *, as_of: Any, options: Any = BULK) -> MatchResult:
        from matcher.score.engine import score

        return score(resume, posting, as_of=as_of, config=self.config, graph=self.graph,
                     calibration=self.calibration, options=options)

    @property
    def config_hash(self) -> str:
        from matcher.config import combined_hash

        return combined_hash(self.config, self.graph.version)


@lru_cache(maxsize=1)
def real_engine() -> Engine:
    """The shipped config and the committed taxonomy (`data/taxonomy`)."""
    from matcher.config import load_config
    from matcher.graph.loader import load_graph

    return Engine(load_config(), load_graph(TAXONOMY_DIR))


@lru_cache(maxsize=1)
def real_resolver() -> Any:
    from matcher.graph.loader import load_resolver

    return load_resolver(TAXONOMY_DIR, real_engine().config)


# ─── The synthetic corpus ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Corpus:
    vocab: Any
    postings: tuple[JobPosting, ...]
    by_id: dict[str, JobPosting]
    profiles: tuple[Any, ...]
    seed: int
    as_of: date


@lru_cache(maxsize=4)
def synthetic_corpus(n_profiles: int = N_PROFILES, seed: int = CORPUS_SEED) -> Corpus:
    from eval.generate import postings as gen_postings
    from eval.generate import profiles as gen_profiles
    from eval.generate.vocabulary import default_vocabulary

    vocab = default_vocabulary()
    posts = gen_postings.generate_postings(vocab, POSTINGS_PER_FAMILY, seed)
    people = gen_profiles.generate_profiles(vocab, posts, n_profiles, seed)
    by_id = {gen_postings.posting_id(p): p for p in posts}
    return Corpus(vocab=vocab, postings=posts, by_id=by_id, profiles=people, seed=seed,
                  as_of=gen_profiles.GENERATION_AS_OF)


# ─── Vocabulary ids -> taxonomy ids ────────────────────────────────────────────────────────


_STRENGTH = {Necessity.REQUIRED: 0, Necessity.PREFERRED: 1, Necessity.IMPLIED: 2}


@dataclass(frozen=True)
class IdMap:
    """Vocabulary ids -> taxonomy ids (DECISIONS D-111). Two vocabulary skills may resolve to one
    taxonomy id (on the default vocabulary: Market Research and Competitive Analysis -> market
    research, User Research and Usability Testing -> user research, Visual Design and Graphic Design
    -> graphic design): their mentions then count as one skill, and two posting requirements that land on one
    id merge into one requirement with the stronger necessity, as `parse_posting` would emit it.
    Skills in `unknown` are dropped from résumés and become unresolved tokens of postings."""

    skills: dict[str, str]  # vocabulary id -> taxonomy id, for every vocabulary skill and field
    unknown: frozenset[str]  # vocabulary ids no spelling of which resolves

    def __call__(self, skill_id: str) -> str:
        return self.skills.get(skill_id, skill_id)

    def known(self, skill_id: str) -> bool:
        return skill_id not in self.unknown

    def _ids(self, ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self(i) for i in ids if self.known(i)))

    def _refs(self, refs: Iterable[SkillRef]) -> tuple[SkillRef, ...]:
        """Translated; a mention of a skill the taxonomy cannot name is dropped (no parser could
        extract it)."""
        return tuple(r.model_copy(update={"canonical_id": self(r.canonical_id)}) for r in refs
                     if self.known(r.canonical_id))

    def resume(self, resume: Resume) -> Resume:
        return resume.model_copy(update={
            "roles": tuple(role.model_copy(update={"skills": self._refs(role.skills)}) for role in resume.roles),
            "projects": tuple(p.model_copy(update={"skills": self._refs(p.skills)}) for p in resume.projects),
            "certifications": self._refs(resume.certifications),
            "skills_blob": self._refs(resume.skills_blob),
            "education": tuple(self._education(e) for e in resume.education),
        })

    def _education(self, education: Education) -> Education:
        return education.model_copy(update={"field_ids": self._ids(education.field_ids)})

    def posting(self, posting: JobPosting) -> JobPosting:
        """Requirements translated; two requirements that land on one id merge, keeping the stronger
        necessity and the earlier position. A requirement the taxonomy cannot name becomes an
        unresolved requirement token, as `parse_posting` would leave it."""
        merged: dict[str, JobRequirement] = {}
        unresolved = list(posting.unresolved_requirement_tokens)
        for requirement in posting.requirements:
            if not self.known(requirement.skill_id):
                unresolved.append(requirement.surface_form or requirement.skill_id)
                continue
            moved = requirement.model_copy(update={"skill_id": self(requirement.skill_id)})
            kept = merged.get(moved.skill_id)
            if kept is None or _STRENGTH[Necessity(moved.necessity)] < _STRENGTH[Necessity(kept.necessity)]:
                merged[moved.skill_id] = moved if kept is None else kept.model_copy(
                    update={"necessity": moved.necessity})
        education: Optional[EducationRequirement] = posting.education_requirement
        if education is not None:
            education = education.model_copy(
                update={"field_ids": self._ids(education.field_ids)})
        return posting.model_copy(update={
            "requirements": tuple(merged.values()),
            "unresolved_requirement_tokens": tuple(unresolved),
            "education_requirement": education,
            "certifications_required": self._ids(posting.certifications_required),
            "certifications_preferred": self._ids(posting.certifications_preferred),
        })


def build_id_map(vocab: Any, resolver: Any) -> IdMap:
    skills: dict[str, str] = {}
    unknown: set[str] = set()
    entries = [(s, False) for s in vocab.skills] + [(f, True) for f in vocab.fields]
    for skill, is_field in entries:
        target: Optional[str] = None
        for spelling in (skill.label, *skill.aliases):
            resolution = resolver.resolve(spelling, prefer_fields=is_field)
            if resolution.method in ("EXACT", "ALIAS") and resolution.canonical_id:
                target = resolution.canonical_id
                break
        if target is None:
            unknown.add(skill.id)
        else:
            skills[skill.id] = target
    return IdMap(skills=skills, unknown=frozenset(unknown))


@lru_cache(maxsize=1)
def corpus_id_map() -> IdMap:
    return build_id_map(synthetic_corpus().vocab, real_resolver())


@dataclass(frozen=True)
class CorpusPair:
    """One synthetic profile's oracle résumé against its target posting, both translated."""

    profile: Any
    resume: Resume
    posting_id: str
    posting: JobPosting

    @property
    def case(self) -> str:
        return self.profile.profile_id


def translated_postings(corpus: Corpus, id_map: IdMap) -> dict[str, JobPosting]:
    return {posting_id: id_map.posting(posting) for posting_id, posting in corpus.by_id.items()}


def corpus_pairs(corpus: Corpus, id_map: IdMap, *, template: Optional[str] = None) -> tuple[CorpusPair, ...]:
    from eval.generate.profiles import expected_resume

    postings = translated_postings(corpus, id_map)
    return tuple(
        CorpusPair(profile=p,
                   resume=id_map.resume(expected_resume(p, corpus.vocab, corpus.as_of, template=template)),
                   posting_id=p.target_posting_id, posting=postings[p.target_posting_id])
        for p in corpus.profiles
    )


SHORTLIST = 0.70  # four-fifths "selected": the Good band or better (bands.yaml, D-52)


def fairness_guardrail(pairs: Sequence[PairScore], *, name: str = "Four-fifths (min ratio)") -> Guardrail:
    """EVAL 3: selection-rate ratio >= 0.80 for every group with n >= 100, selected = raw >= 0.70.
    No eligible group is "not assessed" (passed None), never a pass (D-93)."""
    from eval.metrics import FOUR_FIFTHS_THRESHOLD, four_fifths

    result = four_fifths(pairs, lambda p: p.score is not None and p.score >= SHORTLIST)
    if result.min_ratio is None:
        groups = len({p.group for p in pairs if p.group is not None})
        detail = "no group with n >= 100" + (f" ({groups} smaller groups)" if groups else " (pairs carry no group)")
        return Guardrail(name, None, None, f">= {FOUR_FIFTHS_THRESHOLD:.2f}", detail, "fairness")
    worst = min(result.per_group, key=lambda g: result.per_group[g].ratio)
    return Guardrail(name, result.passed, result.min_ratio, f">= {FOUR_FIFTHS_THRESHOLD:.2f}",
                     f"{len(result.per_group)} groups; lowest {worst}", "fairness")


# ─── Results as metric records ─────────────────────────────────────────────────────────────


def score_for_ranking(result: MatchResult) -> Optional[float]:
    """The number a pair is ranked by. NEEDS_REVIEW abstains (None); a GATE_FAIL pair is
    ineligible and ranks last (0.0); a SCORED or SCORED_LOW_CONFIDENCE pair ranks by its raw score."""
    outcome = Outcome(result.outcome)
    if outcome is Outcome.NEEDS_REVIEW:
        return None
    if outcome is Outcome.GATE_FAIL:
        return 0.0
    return result.raw_score


def pair_record(posting_id: str, pair_id: str, result: MatchResult, fit_level: int) -> PairScore:
    """A synthetic pair as a `PairScore`. relevance and band_true are the constructed fit level,
    `advanced` is fit >= 2; `hard` marks the middle levels 1 and 2, the borderline candidates an
    abstention should concentrate on (DECISIONS D-113). A GATE_FAIL pair is ineligible: it ranks last
    and its band counts as Limited (0), so a gate never hides a pair from band accuracy."""
    if Outcome(result.outcome) is Outcome.GATE_FAIL:
        band: Optional[int] = BAND_INDEX[Band.LIMITED]
    else:
        band = BAND_INDEX.get(Band(result.band)) if result.band is not None else None
    return PairScore(
        posting_id=posting_id,
        pair_id=pair_id,
        score=score_for_ranking(result),
        relevance=fit_level,
        advanced=fit_level >= 2,
        band_pred=band,
        band_true=fit_level,
        hard=fit_level in (1, 2),
    )
