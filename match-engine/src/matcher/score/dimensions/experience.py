"""§5.3 Relevant experience depth (weight `experience`).

SPEC §5.3, §9, §14.2, §14.3, §14.5; DECISIONS D-12, D-35, D-36, D-37.

* `role_relevance` (D-35): the cosine, in the posting's requirement space, between
  `v_job[r] = idf(r)` and `v_role[r] = idf(r) × the bare relation credit the role's AFFIRMED skill
  mentions earn for r` (`credits.RelationTargets`: exact 1.00, child 0.85, sibling clamp(edge),
  parent 0.40; no evidence, no recency). The space holds every distinct requirement id of the
  posting, each OR-group member on its own and whatever its necessity, with the IDF requirement
  matching uses (D-21). A zero vector on either side gives 0. Skills unrelated to the posting add
  nothing to either vector, so breadth never lowers a role's relevance.
* `relevant_months` (D-36): a sweep over the dated roles, from start to end (`as_of` for a current
  role; a later end is clamped to `as_of`). Each elementary segment counts once, at the highest
  `relevance × (experience.internship_weight if internship else 1)` among the roles covering it, so
  overlapping and concurrent roles are never double-counted and the total never exceeds calendar
  time. A segment no role covers (a gap) is skipped without being measured: gaps are never
  computed, stored or scored (SPEC §9). Months are days / 30.4375 (365.25 / 12). Projects never
  count (D-12).
* The minimum (D-37): `12 × min_years_total`; else the largest `min_years` of a REQUIRED
  requirement; else `experience.default_min_months` for the posting's seniority, with a
  MIN_YEARS_DEFAULTED fact.
* `experience_curve` (§5.3): `value_at_knee × r / knee` below the knee, then linear up to 1.0 at
  r = 1, then 1.0: experience beyond the minimum is never deducted. Months at or above the minimum
  (a stated minimum of zero included) score 1.0 without dividing.

`score_experience`, in this order: abstains with FUNCTIONAL_RESUME (no timeline, §14.3), then
NO_EXPERIENCE_REQUIREMENT (no years and no seniority); a résumé with no roles at all then scores
0.0 with a NO_WORK_EXPERIENCE fact (they have none, whatever the posting asks for); otherwise it
abstains with DATES_MISSING (roles exist but none is dated, §14.2) or NO_REQUIREMENTS_RESOLVED
(the posting's requirement vector is zero, so relevance is undefined: "we don't know", §14.5, the
reason the coverage dimensions give for the same posting).

Facts, in order: RELEVANT_MONTHS (value = months; subject = where the minimum came from:
POSTING_YEARS, REQUIREMENT_YEARS or SENIORITY_DEFAULT; detail = the minimum in months and the
skill or level that set it), MIN_YEARS_DEFAULTED or NO_WORK_EXPERIENCE when they apply, then one
ROLE_RELEVANCE per dated role that has time before `as_of` (a role dated entirely after it adds no
months and gets none; subject = its canonical title; detail = its index on the résumé, whether it
is an internship and its sweep weight). No fact carries a role's own duration.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import NamedTuple, Optional

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, DimensionScore, Fact, JobPosting, Necessity, Polarity, Role
from matcher.score._shared import abstain, requirement_idf, scored
from matcher.score.coverage import NO_WEIGHT
from matcher.score.credits import Credit, RelationTargets
from matcher.score.numeric import clamp01, finite
from matcher.score.prepare import PreparedResume, ensure_bound

__all__ = [
    "DAYS_PER_MONTH",
    "MIN_YEARS_DEFAULTED",
    "NO_WORK_EXPERIENCE",
    "RELEVANT_MONTHS",
    "ROLE_RELEVANCE",
    "experience_curve",
    "relevant_months",
    "role_relevance",
    "score_experience",
]

DAYS_PER_MONTH = 30.4375  # 365.25 / 12, the mean Gregorian month

# Fact codes and the sources of the minimum (DECISIONS D-37).
RELEVANT_MONTHS = "RELEVANT_MONTHS"
ROLE_RELEVANCE = "ROLE_RELEVANCE"
MIN_YEARS_DEFAULTED = Flag.MIN_YEARS_DEFAULTED.value
NO_WORK_EXPERIENCE = Flag.NO_WORK_EXPERIENCE.value
POSTING_YEARS = "POSTING_YEARS"
REQUIREMENT_YEARS = "REQUIREMENT_YEARS"
SENIORITY_DEFAULT = "SENIORITY_DEFAULT"

_Space = tuple[tuple[str, float], ...]  # (requirement id, idf), ids sorted


class _Span(NamedTuple):
    start: date
    end: date  # exclusive
    weight: float


# ─── Relevance (DECISIONS D-35) ──────────────────────────────────────────────────────────


def _requirement_space(posting: JobPosting, graph: SkillGraph, config: EngineConfig) -> _Space:
    ids = sorted({requirement.skill_id for requirement in posting.requirements})
    return tuple((skill_id, requirement_idf(graph, skill_id, config)) for skill_id in ids)


def _norm(components: Iterable[float]) -> float:
    return math.sqrt(finite(math.fsum(component * component for component in components)))


def _cosine(space: _Space, job_norm: float, credits: Mapping[str, Credit]) -> float:
    """cos(v_job, v_role); 0 when either vector is zero (never a division by zero, SPEC §13)."""
    role_vector = [(idf, idf * credits[skill_id][0]) for skill_id, idf in space if skill_id in credits]
    role_norm = _norm(component for _, component in role_vector)
    if job_norm <= NO_WEIGHT or role_norm <= NO_WEIGHT:
        return 0.0
    dot = finite(math.fsum(idf * component for idf, component in role_vector))
    return clamp01(dot / (job_norm * role_norm))


def _relevance(role: Role, space: _Space, job_norm: float, targets: RelationTargets) -> float:
    held = {ref.canonical_id for ref in role.skills if Polarity(ref.polarity) is Polarity.AFFIRMED}
    if not held:
        return 0.0
    return _cosine(space, job_norm, targets.credits(held))


def _targets(space: _Space, graph: SkillGraph, config: EngineConfig) -> RelationTargets:
    return RelationTargets((skill_id for skill_id, _ in space), graph, config)


def role_relevance(role: Role, posting: JobPosting, graph: SkillGraph, config: EngineConfig) -> float:
    """How much of the posting's requirement space this role's skills cover (DECISIONS D-35)."""
    space = _requirement_space(posting, graph, config)
    return _relevance(role, space, _norm(idf for _, idf in space), _targets(space, graph, config))


# ─── Months (DECISIONS D-36) ─────────────────────────────────────────────────────────────


def _bounds(role: Role, as_of: date) -> Optional[tuple[date, date]]:
    """A dated role's [start, end): `as_of` ends a current role and caps a later end; None when
    the role is undated or has no time before `as_of`."""
    if not role.dated:
        return None
    start = role.start.value
    end = as_of if role.is_current else min(role.end.value, as_of)
    return (start, end) if start < end else None


def _weighted_spans(
    prepared: PreparedResume, space: _Space, graph: SkillGraph, config: EngineConfig
) -> tuple[list[_Span], list[Fact]]:
    """Every dated role's span at its sweep weight, with its ROLE_RELEVANCE fact."""
    targets = _targets(space, graph, config)
    job_norm = _norm(idf for _, idf in space)
    internship = config.scoring.experience.internship_weight
    spans: list[_Span] = []
    facts: list[Fact] = []
    for index, role in enumerate(prepared.resume.roles):
        bounds = _bounds(role, prepared.as_of)
        if bounds is None:
            continue
        relevance = _relevance(role, space, job_norm, targets)
        weight = clamp01(relevance * (internship if role.is_internship else 1.0))
        spans.append(_Span(bounds[0], bounds[1], weight))
        facts.append(Fact(code=ROLE_RELEVANCE, subject=role.title_canonical or "", value=relevance,
                          detail={"role": index, "internship": role.is_internship, "weight": weight}))
    return spans, facts


def _sweep(spans: Sequence[_Span]) -> float:
    """Months of the weighted union: Σ over elementary segments of days × the highest weight among
    the spans covering it."""
    points = sorted({day for span in spans for day in (span.start, span.end)})
    parts = []
    for left, right in zip(points, points[1:]):
        covering = [span.weight for span in spans if span.start <= left and right <= span.end]
        if covering:  # no span covers a gap: it is skipped, never measured (SPEC §9)
            parts.append((right - left).days * max(covering))
    return finite(math.fsum(parts)) / DAYS_PER_MONTH


def relevant_months(prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig) -> float:
    """Relevance-weighted months of dated employment, overlaps counted once (DECISIONS D-36)."""
    ensure_bound(prepared, graph, config)
    spans, _ = _weighted_spans(prepared, _requirement_space(posting, graph, config), graph, config)
    return _sweep(spans)


# ─── The minimum, the curve, the dimension ───────────────────────────────────────────────


def _minimum(posting: JobPosting, config: EngineConfig) -> Optional[tuple[float, str, str]]:
    """`(min_months, where it came from, the skill or level that set it)` (DECISIONS D-37); None
    when the posting states neither years nor a level."""
    if posting.min_years_total is not None:
        return 12.0 * finite(posting.min_years_total), POSTING_YEARS, ""
    stated = [
        (requirement.min_years, requirement.skill_id)
        for requirement in posting.requirements
        if requirement.necessity == Necessity.REQUIRED and requirement.min_years is not None
    ]
    if stated:
        years, skill_id = min(stated, key=lambda item: (-item[0], item[1]))  # most years; ties: smaller id
        return 12.0 * finite(years), REQUIREMENT_YEARS, skill_id
    if posting.seniority is not None:
        code = posting.seniority.code
        return finite(config.scoring.experience.default_min_months[code]), SENIORITY_DEFAULT, code
    return None


def experience_curve(ratio: float, config: EngineConfig) -> float:
    """SPEC §5.3: `value_at_knee × r / knee` below the knee, `value_at_knee + (1 − value_at_knee) ×
    (r − knee) / (1 − knee)` up to r = 1, then 1.0 (the knee lies strictly inside (0, 1))."""
    rules = config.scoring.experience
    ratio = finite(ratio)
    if ratio <= 0.0:
        return 0.0
    if ratio < rules.knee_ratio:
        return clamp01(rules.value_at_knee * ratio / rules.knee_ratio)
    if ratio < 1.0:
        rise = (1.0 - rules.value_at_knee) * (ratio - rules.knee_ratio) / (1.0 - rules.knee_ratio)
        return clamp01(rules.value_at_knee + rise)
    return 1.0


def score_experience(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig
) -> DimensionScore:
    ensure_bound(prepared, graph, config)
    name = DimensionName.EXPERIENCE
    resume = prepared.resume
    if Flag.FUNCTIONAL_RESUME in resume.flags:
        return abstain(name, config, Flag.FUNCTIONAL_RESUME)
    minimum = _minimum(posting, config)
    if minimum is None:
        return abstain(name, config, Flag.NO_EXPERIENCE_REQUIREMENT)
    min_months, basis, basis_id = minimum
    facts: list[Fact] = []
    if basis == SENIORITY_DEFAULT:
        facts.append(Fact(code=MIN_YEARS_DEFAULTED, subject=basis_id, value=min_months))
    if not resume.roles:
        months = 0.0
        facts.append(Fact(code=NO_WORK_EXPERIENCE, value=0.0))
    elif not any(role.dated for role in resume.roles):
        return abstain(name, config, Flag.DATES_MISSING)
    else:
        space = _requirement_space(posting, graph, config)
        if _norm(idf for _, idf in space) <= NO_WEIGHT:
            return abstain(name, config, Flag.NO_REQUIREMENTS_RESOLVED)
        spans, role_facts = _weighted_spans(prepared, space, graph, config)
        months = _sweep(spans)
        facts.extend(role_facts)
    # Below the minimum the ratio is under 1 (and the minimum positive), so the division can
    # neither fail nor overflow; at or above it the requirement is met.
    value = 1.0 if months >= min_months else experience_curve(months / min_months, config)
    summary = Fact(code=RELEVANT_MONTHS, subject=basis, value=months,
                   detail={"min_months": min_months, "basis_id": basis_id})
    return scored(name, config, value, (summary, *facts))
