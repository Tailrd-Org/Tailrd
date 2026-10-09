"""Hand-built résumés, postings and graphs for the scoring tests (no parsing involved).

The constants are the spec's numbers written out (SPEC §5.1 E and R, §5.1 relation table, §15.1
verbs, §15.2 aspiration, §16 provisional nodes, DECISIONS D-21 unseen IDF, D-43 critical miss,
config/weights.yaml as SPEC §5 states it). Tests compute expected values from these by closed-form
arithmetic, never from the engine's config objects (EVAL §2.2).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Optional

from matcher.config import EngineConfig
from matcher.graph.model import SkillGraph, SkillNode
from matcher.models import (
    DateValue,
    Evidence,
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

AS_OF = date(2026, 10, 8)
FOUR_YEARS_AGO = date(2022, 10, 8)  # 1,461 days (one leap day) = exactly 4.0 years of 365.25 days
TWO_YEARS_AGO = date(2024, 10, 8)  # 730 days


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


# ─── Config and graphs ────────────────────────────────────────────────────────────────────


def with_scoring(config: EngineConfig, section: str, **changes: object) -> EngineConfig:
    """A fresh config with `scoring.<section>` fields replaced (never a model_copy of the whole
    config, which would carry the old cached content hash)."""
    part = getattr(config.scoring, section).model_copy(update=changes)
    scoring = config.scoring.model_copy(update={section: part})
    fields = {name: getattr(config, name) for name in EngineConfig.model_fields}
    fields["scoring"] = scoring
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
