"""Hand-built résumés, postings and graphs for the scoring tests (no parsing involved).

The constants are the spec's numbers written out (SPEC §5.1 E and R, §5.1 relation table, §15.1
verbs, §15.2 aspiration, §16 provisional nodes, DECISIONS D-21 unseen IDF, D-43 critical miss,
config/weights.yaml as SPEC §5 states it). Tests compute expected values from these by closed-form
arithmetic, never from the engine's config objects (EVAL §2.2).
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Mapping
from datetime import date
from types import MappingProxyType
from typing import Optional

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph, SkillNode
from matcher.models import (
    DatePrecision,
    DateValue,
    DimensionName,
    DimensionScore,
    Evidence,
    Fact,
    JobPosting,
    JobRequirement,
    Necessity,
    Polarity,
    Project,
    Resume,
    Role,
    SkillKind,
    SkillRef,
    SkillSource,
    VerbClass,
)

# ─── SPEC constants ───────────────────────────────────────────────────────────────────────

E_BLOB = 0.65  # §5.1 SKILLS_BLOB
E_TITLE = 1.00  # §5.1 ROLE_TITLE
E_CERT = 1.00  # §5.1 CERTIFICATION
E_OWNERSHIP = 1.00  # §15.1
E_EXECUTION = 0.85
E_PARTICIPATION = 0.65
E_NO_VERB = 0.70
QUANTIFIED_BONUS = 1.15  # §15.1, capped at 1.0
DECAY_PER_YEAR = 0.15  # §5.1 R = max(0.55, exp(-0.15 × years))
R_FLOOR = 0.55
ASPIRATIONAL_M = 0.15  # §15.2
EXACT_CREDIT = 1.00  # §5.1 relation table, DECISIONS D-23
CHILD_CREDIT = 0.85
PARENT_CREDIT = 0.40
SIBLING_MIN = 0.40
SIBLING_MAX = 0.80
PROVISIONAL = 0.80  # §16, DECISIONS D-24
UNSEEN_IDF = 0.75  # DECISIONS D-21
IDF_CRITICAL = 0.70  # §5.1 critical miss, DECISIONS D-43
M_CRITICAL = 0.20
W_REQUIRED = 0.35  # §5.1
W_PREFERRED = 0.10  # §5.2
W_TOOLS = 0.10  # §5.7
SPEC_WEIGHTS: Mapping[DimensionName, float] = MappingProxyType({  # SPEC §5, every dimension
    DimensionName.REQUIRED_SKILLS: 0.35,
    DimensionName.PREFERRED_SKILLS: 0.10,
    DimensionName.EXPERIENCE: 0.15,
    DimensionName.SENIORITY: 0.10,
    DimensionName.DOMAIN: 0.10,
    DimensionName.EDUCATION: 0.10,
    DimensionName.TOOLS: 0.10,
})

AS_OF = date(2026, 10, 8)
FOUR_YEARS_AGO = date(2022, 10, 8)  # 1,461 days (one leap day) = exactly 4.0 years of 365.25 days
TWO_YEARS_AGO = date(2024, 10, 8)  # 730 days


def close(a: float, b: float, tol: float = 1e-12) -> bool:
    """Equal within an absolute tolerance: tests compare floats this way, never with == (SPEC §13)."""
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


def year(y: int) -> DateValue:
    """A bare year as parse/dates.py writes it: the midpoint, bounded by the year (SPEC §14.2). Year
    precision is imprecise (DECISIONS D-11), so score intervals move it to its bounds (D-45)."""
    return DateValue(value=date(y, 7, 2), earliest=date(y, 1, 1), latest=date(y, 12, 31),
                     precision=DatePrecision.YEAR)


def recency(days: int) -> float:
    """SPEC §5.1 R (DECISIONS D-34) for a skill last used `days` before as_of."""
    return max(R_FLOOR, math.exp(-DECAY_PER_YEAR * days / 365.25))


# ─── Résumé pieces ────────────────────────────────────────────────────────────────────────


def ref(
    skill_id: str,
    evidence: Evidence = Evidence.SKILLS_BLOB,
    *,
    verb: VerbClass = VerbClass.NO_VERB,
    polarity: Polarity = Polarity.AFFIRMED,
    source: SkillSource = SkillSource.SKILLS_SECTION,
    last_used: Optional[date] = None,
) -> SkillRef:
    return SkillRef(
        canonical_id=skill_id,
        evidence=evidence,
        verb_class=verb,
        polarity=polarity,
        source=source,
        last_used=last_used,
    )


def listed(*skill_ids: str, polarity: Polarity = Polarity.AFFIRMED) -> tuple[SkillRef, ...]:
    """Skills-section mentions (SKILLS_BLOB evidence)."""
    return tuple(ref(skill_id, polarity=polarity) for skill_id in skill_ids)


def bullet(
    skill_id: str,
    verb: VerbClass = VerbClass.OWNERSHIP,
    *,
    polarity: Polarity = Polarity.AFFIRMED,
    quantified: bool = False,
    last_used: Optional[date] = None,
    source: SkillSource = SkillSource.ROLE,
) -> SkillRef:
    evidence = Evidence.ROLE_BULLET_QUANTIFIED if quantified else Evidence.ROLE_BULLET
    return ref(skill_id, evidence, verb=verb, polarity=polarity, source=source, last_used=last_used)


def on(day: date) -> DateValue:
    return DateValue.exact(day)


def current_role(*skills: SkillRef, start: date = date(2024, 1, 15)) -> Role:
    return Role(start=on(start), is_current=True, skills=skills)


def ended_role(*skills: SkillRef, end: date, start: Optional[date] = None) -> Role:
    first = start if start is not None else date(end.year - 1, 1, 1)
    return Role(start=on(first), end=on(end), skills=skills)


def undated_role(*skills: SkillRef) -> Role:
    """A role with no end and not current: nothing dates its skills."""
    return Role(skills=skills)


def project(*skills: SkillRef, end: Optional[date] = None) -> Project:
    return Project(skills=skills, end=on(end) if end is not None else None)


def resume(
    *,
    roles: Iterable[Role] = (),
    projects: Iterable[Project] = (),
    blob: Iterable[SkillRef] = (),
    certifications: Iterable[SkillRef] = (),
) -> Resume:
    return Resume(
        roles=tuple(roles),
        projects=tuple(projects),
        skills_blob=tuple(blob),
        certifications=tuple(certifications),
    )


# ─── Posting pieces ───────────────────────────────────────────────────────────────────────


def req(
    skill_id: str,
    necessity: Necessity = Necessity.REQUIRED,
    *,
    or_group: Optional[str] = None,
    weight_multiplier: float = 1.0,
) -> JobRequirement:
    return JobRequirement(
        skill_id=skill_id, necessity=necessity, or_group=or_group, weight_multiplier=weight_multiplier
    )


def posting(*requirements: JobRequirement) -> JobPosting:
    return JobPosting(requirements=requirements)


# ─── Dimension scores (what the dimension scorers hand the aggregator) ──────────────────────


def dimension(name: DimensionName, value: float, *, facts: Iterable[Fact] = ()) -> DimensionScore:
    """A scored dimension as the scorers return it: the SPEC weight as configured and effective
    weight, contribution = weight × value (the aggregator redistributes, SPEC §14.5)."""
    weight = SPEC_WEIGHTS[name]
    return DimensionScore(name=name, value=value, weight=weight, effective_weight=weight,
                          contribution=weight * value, abstained=False, facts=tuple(facts))


def abstained_dimension(name: DimensionName, reason: Flag) -> DimensionScore:
    """An abstaining dimension: no value, no effective weight, no contribution, no facts."""
    return DimensionScore(name=name, value=None, weight=SPEC_WEIGHTS[name], effective_weight=0.0,
                          contribution=0.0, abstained=True, abstain_reason=reason)


# ─── Config and graphs ────────────────────────────────────────────────────────────────────


def with_scoring(config: EngineConfig, section: str, **changes: object) -> EngineConfig:
    """A fresh config with `scoring.<section>` fields replaced (never a model_copy of the whole
    config, which would carry the old cached content hash)."""
    part = getattr(config.scoring, section).model_copy(update=changes)
    scoring = config.scoring.model_copy(update={section: part})
    fields = {name: getattr(config, name) for name in EngineConfig.model_fields}
    fields["scoring"] = scoring
    return EngineConfig(**fields)


def with_gates(config: EngineConfig, section: str, **changes: object) -> EngineConfig:
    """A fresh config with `gates.<section>` fields replaced (same rule as `with_scoring`)."""
    part = getattr(config.gates, section).model_copy(update=changes)
    gates = config.gates.model_copy(update={section: part})
    fields = {name: getattr(config, name) for name in EngineConfig.model_fields}
    fields["gates"] = gates
    return EngineConfig(**fields)


def tiny_graph(
    idf: Mapping[str, float],
    edges: Iterable[tuple[str, str, float]] = (),
    *,
    one_way: Iterable[tuple[str, str, float]] = (),
    parents: Optional[Mapping[str, tuple[str, ...]]] = None,
    provisional: Iterable[str] = (),
) -> SkillGraph:
    """A hand-built graph: `edges` stored in both directions, `one_way` edges only a -> b (as
    the out-degree cap can leave them, DECISIONS D-22)."""
    edges, one_way, provisional = list(edges), list(one_way), set(provisional)
    parent_map = dict(parents or {})
    ids = set(idf) | provisional | set(parent_map)
    for a, b, _ in edges + one_way:
        ids |= {a, b}
    for linked in parent_map.values():
        ids |= set(linked)
    nodes = {
        node_id: SkillNode(
            id=node_id,
            label=node_id,
            kind=SkillKind.TECHNOLOGY,
            parents=parent_map.get(node_id, ()),
            provisional=node_id in provisional,
        )
        for node_id in sorted(ids)
    }
    related: dict[str, dict[str, float]] = {}
    for a, b, weight in edges:
        related.setdefault(a, {})[b] = weight
        related.setdefault(b, {})[a] = weight
    for a, b, weight in one_way:
        related.setdefault(a, {})[b] = weight
    return SkillGraph(nodes, related, dict(idf), "test")


def random_graph(
    seed: int,
    n_nodes: int = 300,
    *,
    edges_per_node: int = 6,
    one_way_share: float = 0.25,
    asymmetric_share: float = 0.10,
    provisional_share: float = 0.05,
) -> SkillGraph:
    """A seeded random graph with the shapes real data can take: a parent DAG up to several
    levels deep, related edges stored both ways, one way only (the out-degree cap, D-22) or both
    ways with different weights (hand-built data), weights on both sides of [0.40, 0.80],
    provisional nodes, and nodes the corpus never saw (no IDF)."""
    rng = random.Random(seed)
    ids = [f"skill.r{i:05d}" for i in range(n_nodes)]
    nodes = {}
    for i, node_id in enumerate(ids):
        parents = tuple(sorted(set(rng.sample(ids[:i], k=min(i, rng.choice((0, 1, 1, 2))))))) if i >= 5 else ()
        nodes[node_id] = SkillNode(id=node_id, label=node_id, kind=SkillKind.TECHNOLOGY, parents=parents,
                                   provisional=rng.random() < provisional_share)
    related: dict[str, dict[str, float]] = {}
    for a in ids:
        for b in rng.sample(ids, k=edges_per_node):
            if a == b:
                continue
            weight = round(rng.uniform(0.2, 1.0), 3)
            shape = rng.random()
            related.setdefault(a, {})[b] = weight
            if shape < one_way_share:
                continue
            if shape < one_way_share + asymmetric_share:
                related.setdefault(b, {})[a] = round(rng.uniform(0.2, 1.0), 3)
            else:
                related.setdefault(b, {})[a] = weight
    idf = {node_id: round(rng.random(), 6) for node_id in ids if rng.random() < 0.8}
    return SkillGraph(nodes, related, idf, f"random-{seed}-{n_nodes}")


def random_resume(rng: random.Random, skill_ids: list[str], k: int) -> Resume:
    """`k` mentions of random skills with random evidence, verbs, polarity and dates."""
    mentions = []
    for skill_id in rng.sample(skill_ids, k=k):
        evidence = rng.choice(list(Evidence))
        polarity = rng.choices(list(Polarity), weights=(8, 1, 1))[0]
        mentions.append(ref(skill_id, evidence, verb=rng.choice(list(VerbClass)), polarity=polarity))
    third = max(1, k // 3)
    return resume(
        roles=(current_role(*mentions[:third]),
               ended_role(*mentions[third:2 * third], end=date(2020 + rng.randrange(6), 1 + rng.randrange(12), 1))),
        blob=mentions[2 * third:],
    )
