"""Build the shipped calibration reference panel (Task 7.1; SPEC §7; DECISIONS D-50).

Run from match-engine/ after scripts/pull_corpus.py:

    .venv/Scripts/python.exe scripts/build_reference_panel.py [--corpus FILE] [--out DIR]
        [--taxonomy DIR] [--seed N] [--postings-per-family N] [--profiles-per-posting N]

Until Tailrd has 30 distinct people per job family, every displayed score is a percentile against
this panel: synthetic candidate profiles scored offline against real postings (D-50). Steps:

1. Read the corpus (`role_category` is the job family), collapse exact-duplicate descriptions
   (`corpus_stats.corpus_texts`: decoded HTML, whitespace collapsed, first id kept).
2. For each non-blank family, shuffle its postings (seeded, from id order) and parse them with
   `parse_posting` in that order until POSTINGS_PER_FAMILY scorable ones are found. A posting with
   no requirement, or flagged POSTING_UNSCORABLE, is never scored by the engine (it leaves as
   NEEDS_REVIEW before calibration), so it has no place in a calibration reference and is skipped.
3. Build one generator vocabulary from the graph (`vocabulary_from_graph`) over every skill the
   sampled postings name; a skill's generator "families" are the job families whose sampled
   postings name it, so a level-0 candidate really comes from another job family.
4. Generate PROFILES_PER_POSTING profiles per posting with Task 5.1's `generate_profiles` (fit
   levels 0..3 spread uniformly: each posting gets every level PROFILES_PER_POSTING / 4 times),
   turn each into its `expected_resume` and score it against its target posting with
   `FixedCalibrator` (the raw score is what is kept). The generator writes to a posting's REQUIRED
   skills; a posting that marks none required gets them from its IMPLIED skills, else from its
   PREFERRED ones (`targeting_copy`), and is still scored as parsed.
5. Every result that carries a raw score (the results the live engine calibrates: SCORED,
   SCORED_LOW_CONFIDENCE and a low-confidence NEEDS_REVIEW) enters its family's reference and the
   pooled one (key "*"). GATE_FAIL and early NEEDS_REVIEW results are counted, never sampled.

Outputs (in --out, default data/calibration/):

    panel.json      {"format": 1, "references": [...]}: one `scope="panel"` ReferenceDistribution per
                    family plus the pooled "*" group; n_candidates = distinct synthetic profiles
    MANIFEST.json   panel.json's sha256 and version, the inputs (corpus hash, taxonomy MANIFEST hash,
                    engine version, config hash), the parameters, and per family the counts, the
                    sampled posting ids and the mean raw score per fit level

Only aggregates of synthetic scores and ids are written: no posting text, no personal data
(DECISIONS D-04). The build is deterministic: the same inputs give the same bytes whatever
PYTHONHASHSEED is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT, Path(__file__).resolve().parent):
    if str(_path) not in sys.path:  # run as a script: `matcher`, `eval` and corpus_stats importable
        sys.path.insert(0, str(_path))

import corpus_stats  # noqa: E402
from eval.generate.profiles import GENERATION_AS_OF, expected_resume, generate_profiles  # noqa: E402
from eval.generate.vocabulary import Vocabulary, vocabulary_from_graph  # noqa: E402
from matcher.calibrate import GLOBAL_KEY, FixedCalibrator, build_reference  # noqa: E402
from matcher.calibrate.reference import PANEL_FORMAT  # noqa: E402
from matcher.config import EngineConfig, combined_hash, load_config  # noqa: E402
from matcher.errors import Flag  # noqa: E402
from matcher.graph.loader import load_graph, load_resolver  # noqa: E402
from matcher.graph.model import SkillGraph  # noqa: E402
from matcher.models import JobPosting, Necessity, ScoreOptions  # noqa: E402
from matcher.parse.config import ParseConfig, load_parse_config  # noqa: E402
from matcher.parse.posting import PostingMeta, parse_posting  # noqa: E402
from matcher.parse.taxonomy_extras import TitleIndex, load_title_index  # noqa: E402
from matcher.score.engine import score  # noqa: E402

DEFAULT_OUT = ROOT / "data" / "calibration"
PANEL_JSON = "panel.json"
MANIFEST_JSON = "MANIFEST.json"
MANIFEST_FORMAT = 1
SEED = 71  # Task 7.1
POSTINGS_PER_FAMILY = 60
PROFILES_PER_POSTING = 8  # two of each fit level
AS_OF = GENERATION_AS_OF  # the generator's fixed "today"; parsing and scoring use the same day
VERSION_PREFIX = "panel-"
_TARGET_ORDER = (Necessity.REQUIRED, Necessity.IMPLIED, Necessity.PREFERRED)
_OPTIONS = ScoreOptions(with_explanation=False)
REBUILD = (".venv/Scripts/python.exe scripts/build_reference_panel.py "
           "[--corpus <path to postings.jsonl.gz>]")


# ─── resources ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Resources:
    """What parsing and scoring need, loaded once."""

    config: EngineConfig
    graph: SkillGraph
    resolver: Any
    parse_config: ParseConfig
    titles: TitleIndex

    @classmethod
    def load(cls, taxonomy_dir: Path) -> "Resources":
        config = load_config()
        parse_config = load_parse_config()
        return cls(config=config, graph=load_graph(taxonomy_dir), resolver=load_resolver(taxonomy_dir, config),
                   parse_config=parse_config, titles=load_title_index(taxonomy_dir, parse_config=parse_config))

    def parse(self, row: Mapping[str, Any], family: str) -> JobPosting:
        meta = PostingMeta(job_family=family, experience_level=_text(row.get("experience_level")))
        return parse_posting(_text(row.get("title")), _text(row.get("description")), meta=meta, as_of=AS_OF,
                             resolver=self.resolver, graph=self.graph, config=self.config,
                             parse_config=self.parse_config, titles=self.titles)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


# ─── sampling ──────────────────────────────────────────────────────────────────────────────


def families_of(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """Distinct postings by job family (`role_category`), each family in id order. A blank family
    is no family: those postings are left out."""
    out: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in corpus_stats.corpus_texts(rows).kept:  # id order, exact duplicates collapsed
        family = _text(row.get("role_category")).strip()
        if family:
            out[family].append(row)
    return {family: out[family] for family in sorted(out)}


def scorable(posting: JobPosting) -> bool:
    """Would the engine score this posting at all (SPEC §14.4)?"""
    return bool(posting.requirements) and Flag.POSTING_UNSCORABLE not in posting.flags


@dataclass
class FamilySample:
    family: str
    available: int
    postings: list[JobPosting] = field(default_factory=list)
    posting_ids: list[Any] = field(default_factory=list)
    parsed: int = 0
    skipped_unscorable: int = 0


def sample_family(family: str, rows: Sequence[Mapping[str, Any]], resources: Resources, *, seed: int,
                  limit: int) -> FamilySample:
    """Up to `limit` scorable postings of one family: a seeded shuffle of the id-ordered rows,
    parsed in that order until `limit` are kept."""
    order = list(rows)
    random.Random(f"panel:{seed}:{family}").shuffle(order)
    sample = FamilySample(family=family, available=len(order))
    for row in order:
        if len(sample.postings) >= limit:
            break
        posting = resources.parse(row, family)
        sample.parsed += 1
        if not scorable(posting):
            sample.skipped_unscorable += 1
            continue
        sample.postings.append(posting)
        sample.posting_ids.append(row.get("id"))
    return sample


def targeting_copy(posting: JobPosting) -> JobPosting:
    """The posting as the generator should read it. The generator builds fit levels on the REQUIRED
    skills; a posting that marks none required has its IMPLIED skills (else its PREFERRED ones) read
    as required. Unchanged when it has a required skill."""
    present = {r.necessity for r in posting.requirements}
    tier = next((n for n in _TARGET_ORDER if n in present), None)
    if tier is None or tier is Necessity.REQUIRED:
        return posting
    requirements = tuple(r.model_copy(update={"necessity": Necessity.REQUIRED}) if r.necessity is tier else r
                         for r in posting.requirements)
    return posting.model_copy(update={"requirements": requirements})


def panel_vocabulary(graph: SkillGraph, samples: Sequence[FamilySample]) -> Vocabulary:
    """The generator vocabulary: every skill the sampled postings name, its label and aliases from
    the graph, its families the job families that name it. An alias another skill already spells
    (in id order) is dropped, so every spelling names one skill; two skills with the same label
    cannot both be written about and stop the build."""
    named: dict[str, set[str]] = defaultdict(set)
    for sample in samples:
        for posting in sample.postings:
            for requirement in posting.requirements:
                named[requirement.skill_id].add(sample.family)
    base = vocabulary_from_graph(graph, sorted(named))
    spelled: dict[str, str] = {}
    skills = []
    for skill in base.skills:
        label = skill.label.casefold()
        if label in spelled:
            raise ValueError(f"{skill.id} and {spelled[label]} are both spelled {skill.label!r}")
        spelled[label] = skill.id
        aliases = []
        for alias in skill.aliases:
            if alias.casefold() not in spelled:
                spelled[alias.casefold()] = skill.id
                aliases.append(alias)
        skills.append(skill.model_copy(update={"families": tuple(sorted(named[skill.id])),
                                               "aliases": tuple(aliases)}))
    return Vocabulary(skills=tuple(skills), families=tuple(sorted(s.family for s in samples)))


# ─── scoring ───────────────────────────────────────────────────────────────────────────────


@dataclass
class FamilyScores:
    candidate_ids: list[str] = field(default_factory=list)
    raw: list[float] = field(default_factory=list)
    by_level: dict[int, list[float]] = field(default_factory=lambda: defaultdict(list))
    excluded: Counter = field(default_factory=Counter)
    promoted: int = 0


def score_family(sample: FamilySample, vocab: Vocabulary, resources: Resources, *, seed: int,
                 per_posting: int) -> FamilyScores:
    """Generate `per_posting` profiles per sampled posting and score each against its target."""
    out = FamilyScores()
    if not sample.postings:
        return out
    targets = [targeting_copy(p) for p in sample.postings]
    out.promoted = sum(t is not p for t, p in zip(targets, sample.postings))
    family_seed = int(hashlib.sha256(f"{seed}:{sample.family}".encode("utf-8")).hexdigest()[:8], 16)
    profiles = generate_profiles(vocab, targets, per_posting * len(targets), family_seed, as_of=AS_OF)
    for i, profile in enumerate(profiles):
        posting = sample.postings[i % len(sample.postings)]  # profile i targets posting i mod P
        resume = expected_resume(profile, vocab, AS_OF)
        result = score(resume, posting, as_of=AS_OF, config=resources.config, graph=resources.graph,
                       calibration=FixedCalibrator(), options=_OPTIONS)
        if result.raw_score is None:
            reasons = ",".join(reason.value for reason in result.reasons)
            out.excluded[f"{result.outcome.value}:{reasons}" if reasons else result.outcome.value] += 1
            continue
        out.candidate_ids.append(f"{sample.family}#{profile.profile_id}")
        out.raw.append(result.raw_score)
        out.by_level[profile.fit_level].append(result.raw_score)
    return out


# ─── the panel ─────────────────────────────────────────────────────────────────────────────


def _dumps(document: Any) -> bytes:
    return (json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 4) if values else None


@dataclass(frozen=True)
class Panel:
    panel: dict[str, Any]
    manifest: dict[str, Any]

    def panel_bytes(self) -> bytes:
        return _dumps(self.panel)

    def manifest_bytes(self) -> bytes:
        return _dumps(self.manifest)


def build_panel(rows: Sequence[Mapping[str, Any]], resources: Resources, *, seed: int = SEED,
                postings_per_family: int = POSTINGS_PER_FAMILY,
                profiles_per_posting: int = PROFILES_PER_POSTING,
                inputs: Optional[Mapping[str, Any]] = None) -> Panel:
    """The panel document and its manifest from corpus rows (see the module docstring)."""
    if postings_per_family < 1:
        raise ValueError("postings_per_family must be at least 1")
    if profiles_per_posting < 4 or profiles_per_posting % 4:
        raise ValueError("profiles_per_posting must be a positive multiple of 4 (every fit level equally)")
    by_family = families_of(rows)
    samples = [sample_family(family, family_rows, resources, seed=seed, limit=postings_per_family)
               for family, family_rows in by_family.items()]
    samples = [s for s in samples if s.postings]
    if not samples:
        raise ValueError("no family has a scorable posting: nothing to build a panel from")
    vocab = panel_vocabulary(resources.graph, samples)
    scores = {s.family: score_family(s, vocab, resources, seed=seed, per_posting=profiles_per_posting)
              for s in samples}

    all_ids = [cid for s in samples for cid in scores[s.family].candidate_ids]
    all_raw = [raw for s in samples for raw in scores[s.family].raw]
    groups = [(GLOBAL_KEY, all_raw, all_ids)]
    groups += [(s.family, scores[s.family].raw, scores[s.family].candidate_ids) for s in samples
               if scores[s.family].raw]
    unversioned = [build_reference("panel", key, raw, ids, "unversioned").model_dump(mode="json", exclude={"version"})
                   for key, raw, ids in groups]
    version = VERSION_PREFIX + _sha256(_dumps(unversioned))[:12]
    references = [build_reference("panel", key, raw, ids, version).model_dump(mode="json")
                  for key, raw, ids in groups]
    panel = {"format": PANEL_FORMAT, "references": references}
    panel_sha = _sha256(_dumps(panel))

    min_n = resources.config.scoring.calibration.min_reference_n
    families: dict[str, Any] = {}
    for sample in samples:
        got = scores[sample.family]
        families[sample.family] = {
            "postings_available": sample.available,
            "postings_parsed": sample.parsed,
            "postings_skipped_unscorable": sample.skipped_unscorable,
            "postings_sampled": len(sample.postings),
            "postings_targeted_on_implied_or_preferred": got.promoted,
            "posting_ids": sample.posting_ids,
            "profiles": len(sample.postings) * profiles_per_posting,
            "candidates": len(set(got.candidate_ids)),
            "excluded_without_raw_score": dict(sorted(got.excluded.items())),
            "mean_raw_by_fit_level": {str(level): _mean(got.by_level.get(level, [])) for level in range(4)},
            "meets_min_reference_n": len(set(got.candidate_ids)) >= min_n,
        }
    pooled_levels: dict[int, list[float]] = defaultdict(list)
    for got in scores.values():
        for level, values in got.by_level.items():
            pooled_levels[level].extend(values)
    manifest = {
        "format": MANIFEST_FORMAT,
        "panel": {"file": PANEL_JSON, "sha256": panel_sha, "version": version},
        "inputs": {
            **dict(inputs or {}),
            "engine_version": resources.config.engine_version,
            "config_hash": combined_hash(resources.config, resources.graph.version),
            "graph_version": resources.graph.version,
        },
        "parameters": {
            "seed": seed,
            "postings_per_family": postings_per_family,
            "profiles_per_posting": profiles_per_posting,
            "as_of": AS_OF.isoformat(),
            "min_reference_n": min_n,
            "families_from": "role_category",
            "generator": "eval.generate.profiles.generate_profiles + expected_resume",
            "calibration_for_raw": "FixedCalibrator (raw_score kept)",
        },
        "families": families,
        "pooled": {
            "key": GLOBAL_KEY,
            "candidates": len(set(all_ids)),
            "pairs": len(all_raw),
            "mean_raw_by_fit_level": {str(level): _mean(pooled_levels.get(level, [])) for level in range(4)},
        },
        "rebuild": REBUILD,
        "content": "aggregates of synthetic scores and posting ids only; no posting text, no personal data",
    }
    return Panel(panel=panel, manifest=manifest)


def corpus_inputs(corpus: Path, rows: Sequence[Mapping[str, Any]], taxonomy_dir: Path) -> dict[str, Any]:
    """What the manifest records about the inputs (hashes and counts, never content)."""
    taxonomy_manifest = taxonomy_dir / "MANIFEST.json"
    return {
        "corpus": {"file": corpus.name, "sha256": _file_sha256(corpus), "rows": len(rows),
                   "distinct_descriptions": len(corpus_stats.corpus_texts(rows).kept)},
        "taxonomy_manifest_sha256": _file_sha256(taxonomy_manifest) if taxonomy_manifest.exists() else None,
    }


def write_panel(panel: Panel, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / PANEL_JSON).write_bytes(panel.panel_bytes())
    (out_dir / MANIFEST_JSON).write_bytes(panel.manifest_bytes())


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=corpus_stats.DEFAULT_CORPUS)
    parser.add_argument("--taxonomy", type=Path, default=corpus_stats.DEFAULT_TAXONOMY)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--postings-per-family", type=int, default=POSTINGS_PER_FAMILY)
    parser.add_argument("--profiles-per-posting", type=int, default=PROFILES_PER_POSTING)
    args = parser.parse_args(argv)
    started = time.perf_counter()
    if not args.corpus.exists():
        raise SystemExit(f"build_reference_panel: no corpus at {args.corpus} (run scripts/pull_corpus.py)")
    rows = corpus_stats.read_postings(args.corpus)
    resources = Resources.load(args.taxonomy)
    panel = build_panel(rows, resources, seed=args.seed, postings_per_family=args.postings_per_family,
                        profiles_per_posting=args.profiles_per_posting,
                        inputs=corpus_inputs(args.corpus, rows, args.taxonomy))
    write_panel(panel, args.out)
    families = panel.manifest["families"]
    for family, info in families.items():
        print(f"{family:<24} {info['postings_sampled']:>3} postings  {info['candidates']:>4} candidates"
              f"{'' if info['meets_min_reference_n'] else '  (under the minimum: falls back to *)'}")
    pooled = panel.manifest["pooled"]
    print(f"pooled '*': {pooled['candidates']:,} candidates; version {panel.manifest['panel']['version']}; "
          f"{time.perf_counter() - started:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
