"""The prepared résumé: what each canonical skill is worth, computed once per résumé.

SPEC §5.1 (E, R), §15.2, §16; DECISIONS D-23, D-24, D-32, D-33, D-34.

Every skill mention of the résumé (role bullets and titles, projects, the skills section,
certifications) earns `value = apply_polarity(E × R, polarity)`:

* E is the evidence multiplier (D-32). R is the recency multiplier of the date the skill was last
  used (D-34): the mention's own `last_used`, else the end of its role (`as_of` while the role is
  current) or project, else the résumé's latest activity: the latest of `as_of` for a current role
  and every role or project end (a role needs no start for its end to count). A résumé with no
  such date has none, and its mentions get R = 1.0 ("we do not penalise what we cannot date").
* `best[id]` keeps each skill's highest value, so a skill listed 50 times counts as once (D-23),
  and a negated mention never hides an affirmed one (D-33). A negated-only skill stays in `best`
  with value 0.0; matching treats that as no credit.
* `child_cover[a]` is, for every ancestor `a` of a résumé skill (within `relations.max_depth`),
  the best CHILD credit `relations.child × value` any descendant on the résumé gives it, × the
  provisional multiplier when either node is provisional (D-24), with that descendant as `via`.

Equal values are broken by a total order so the result never depends on the order of the input:
the stronger evidence tier (later in the `Evidence` order: a skill listed in the skills section
never hides one demonstrated in a bullet at the same value), then the earlier `SkillSource`, then
the earlier `Polarity`, then the higher E, then the higher R. Child-cover ties go to the smaller
skill id.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Optional

from pydantic import Field

from matcher.config import EngineConfig
from matcher.graph.model import SkillGraph
from matcher.graph.traversal import ancestors
from matcher.models import Evidence, Frozen, Polarity, Resume, Role, SkillRef, SkillSource
from matcher.score.evidence import apply_polarity, evidence_multiplier, recency_multiplier
from matcher.score.numeric import clamp01

__all__ = ["PreparedResume", "SkillValue", "prepare_resume"]


class SkillValue(Frozen):
    """The best mention of one skill: `value = apply_polarity(evidence_mult × recency_mult)`."""

    skill_id: str = Field(min_length=1, max_length=200)
    value: float = Field(ge=0.0, le=1.0)
    evidence_mult: float = Field(ge=0.0, le=1.0)
    recency_mult: float = Field(ge=0.0, le=1.0)
    evidence: Evidence
    polarity: Polarity
    source: SkillSource


class PreparedResume(Frozen):
    """A résumé indexed for matching. Built by `prepare_resume` for one graph, config and as_of;
    it holds no reference to the graph, which is passed to every function that reads it."""

    resume: Resume
    as_of: date
    latest_activity: Optional[date]
    best: dict[str, SkillValue]  # canonical id -> its max-value mention, keys sorted
    child_cover: dict[str, tuple[float, str]]  # requirement id -> (best CHILD credit, via id), keys sorted


_EVIDENCE_ORDER = {evidence: rank for rank, evidence in enumerate(Evidence)}
_SOURCE_ORDER = {source: rank for rank, source in enumerate(SkillSource)}
_POLARITY_ORDER = {polarity: rank for rank, polarity in enumerate(Polarity)}


def _rank(value: SkillValue) -> tuple[float, int, int, int, float, float]:
    """Larger is better. A total order over every field but `skill_id`, so two mentions of one
    skill with equal ranks are equal and the kept one never depends on input order."""
    return (
        value.value,
        _EVIDENCE_ORDER[value.evidence],
        -_SOURCE_ORDER[value.source],
        -_POLARITY_ORDER[value.polarity],
        value.evidence_mult,
        value.recency_mult,
    )


def _role_last_used(role: Role, as_of: date) -> Optional[date]:
    """When a role's skills were last used: `as_of` while it is current, else its end."""
    if role.is_current:
        return as_of
    return role.end.value if role.end is not None else None


def _latest_activity(resume: Resume, as_of: date) -> Optional[date]:
    """DECISIONS D-34: `as_of` if a role is current, else the latest role or project end."""
    dates = [day for day in (_role_last_used(role, as_of) for role in resume.roles) if day is not None]
    dates.extend(project.end.value for project in resume.projects if project.end is not None)
    return max(dates) if dates else None


def _skill_value(ref: SkillRef, last_used: Optional[date], as_of: date, config: EngineConfig) -> SkillValue:
    evidence = Evidence(ref.evidence)
    polarity = Polarity(ref.polarity)
    e = evidence_multiplier(evidence, ref.verb_class, config)
    r = recency_multiplier(last_used, as_of, config)
    return SkillValue(
        skill_id=ref.canonical_id,
        value=apply_polarity(e * r, polarity, config),
        evidence_mult=e,
        recency_mult=r,
        evidence=evidence,
        polarity=polarity,
        source=SkillSource(ref.source),
    )


def _mentions(resume: Resume, as_of: date) -> Iterable[tuple[SkillRef, Optional[date]]]:
    """Every mention with the date its container gives it (None: take the latest activity)."""
    for role in resume.roles:
        dated = _role_last_used(role, as_of)
        for ref in role.skills:
            yield ref, dated
    for project in resume.projects:
        dated = project.end.value if project.end is not None else None
        for ref in project.skills:
            yield ref, dated
    for ref in resume.skills_blob:
        yield ref, None
    for ref in resume.certifications:
        yield ref, None


def _child_cover(best: dict[str, SkillValue], graph: SkillGraph, config: EngineConfig) -> dict[str, tuple[float, str]]:
    relations = config.scoring.relations
    cover: dict[str, tuple[float, str]] = {}
    for skill_id, value in best.items():
        node = graph.node(skill_id)
        if node is None:
            continue  # unknown ids match exactly only
        for ancestor in ancestors(graph, skill_id, relations.max_depth):
            credit = relations.child * value.value
            if node.provisional or graph.nodes[ancestor].provisional:
                credit *= relations.provisional_multiplier
            candidate = (clamp01(credit), skill_id)
            current = cover.get(ancestor)
            # Higher credit first, then the smaller via id: independent of iteration order.
            if current is None or (-candidate[0], candidate[1]) < (-current[0], current[1]):
                cover[ancestor] = candidate
    return cover


def prepare_resume(resume: Resume, graph: SkillGraph, config: EngineConfig, as_of: date) -> PreparedResume:
    """Index `resume` for matching against any posting (SPEC §5.1; DECISIONS D-23, D-33, D-34)."""
    latest = _latest_activity(resume, as_of)
    best: dict[str, SkillValue] = {}
    for ref, dated in _mentions(resume, as_of):
        if ref.last_used is not None:
            last_used: Optional[date] = ref.last_used
        elif dated is not None:
            last_used = dated
        else:
            last_used = latest
        value = _skill_value(ref, last_used, as_of, config)
        kept = best.get(value.skill_id)
        if kept is None or _rank(value) > _rank(kept):
            best[value.skill_id] = value
    ordered = {skill_id: best[skill_id] for skill_id in sorted(best)}
    cover = _child_cover(ordered, graph, config)
    return PreparedResume(
        resume=resume,
        as_of=as_of,
        latest_activity=latest,
        best=ordered,
        child_cover={ancestor: cover[ancestor] for ancestor in sorted(cover)},
    )
