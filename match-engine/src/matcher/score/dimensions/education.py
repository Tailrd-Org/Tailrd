"""§5.6 Education & certifications (weight `education`).

SPEC §5.6, §9, §14.3; DECISIONS D-13, D-14, D-41.

* Each listed degree is scored as a pair, its level tier × its own field multiplier, and the best
  pair counts (none listed: NONE with no field). One degree's level is never combined with another
  degree's field: a History PhD and a Software Engineering diploma against a Bachelor's in CS score
  max(1.00 × 0.70, 0.55 × 0.955) = 0.70. A degree in progress counts at its level, since graduation
  timing is never inferred (D-41).
* Level tier: the shortfall below the required level picks 0 → `meets`, 1 → `one_below`, 2 or more
  → `two_or_more_below`; "or equivalent", or a requirement that is not REQUIRED (PREFERRED or
  IMPLIED), makes it one tier more lenient, once. No requirement → `no_requirement` for everyone
  (never penalise an unrequested credential, §14.3).
* Field multiplier: `field_multiplier_min + (1 − field_multiplier_min) × c`, c the best bare
  relation credit (`credits.RelationTargets`: exact, child, sibling, parent as for skills; field
  nodes are `field.*` ids) from the degree's fields to a required field; 1.0 when no field is
  required, `field_multiplier_min` when one is and none of the degree's fields relates.
* Credentials: `cert_bonus_each` for each distinct AFFIRMED résumé credential related (by any
  relation) to a posting requirement skill or to a credential the posting lists as required or
  preferred (relation to a listed credential, not only the listed id, is a deliberate widening),
  granted in id order until `cert_bonus_cap` is reached.
* Value `min(1, best pair + bonus)`. The dimension never abstains.

School, institution and graduation year are not in the model and are never read (§5.6, §9; D-14).
Equal pairs go to the higher degree, then the smaller list of fields. Facts, in order: DEGREE_LEVEL
for the best pair's degree (subject = the required level; detail = that degree's level, the
shortfall and whether the tier was softened) or NO_EDUCATION_REQUIREMENT; FIELD_MATCH when a field
is required (subject = the required field the best pair's degree relates to best, value = the
credit; detail = the relation, the field that earns it and the multiplier; the smallest required
field, credit 0 and relation NONE when none relates); one CERT_BONUS per relevant credential in id
order (value = what it actually adds after the cap: `cert_bonus_each`, then what is left of the
cap, then 0; detail = the relation and the posting id it relates to).
"""

from __future__ import annotations

import math
from typing import NamedTuple, Optional

from matcher.config import EngineConfig
from matcher.graph.model import SkillGraph
from matcher.models import (
    DegreeLevel,
    DimensionName,
    DimensionScore,
    EducationRequirement,
    Fact,
    JobPosting,
    Necessity,
    Polarity,
    RelationKind,
    Resume,
)
from matcher.score._shared import scored
from matcher.score.credits import RelationTargets, best_credit
from matcher.score.numeric import clamp01, tie_key
from matcher.score.prepare import PreparedResume, ensure_bound

__all__ = ["CERT_BONUS", "DEGREE_LEVEL", "FIELD_MATCH", "NO_EDUCATION_REQUIREMENT", "score_education"]

DEGREE_LEVEL = "DEGREE_LEVEL"
NO_EDUCATION_REQUIREMENT = "NO_EDUCATION_REQUIREMENT"
FIELD_MATCH = "FIELD_MATCH"
CERT_BONUS = "CERT_BONUS"

_NOISE = 1e-12  # float noise when a running total reaches the cap


class _Pair(NamedTuple):
    value: float  # level tier × field multiplier
    level: DegreeLevel
    fields: tuple[str, ...]
    facts: tuple[Fact, ...]


def _degrees(resume: Resume) -> list[tuple[DegreeLevel, tuple[str, ...]]]:
    """Every distinct listed degree as (level, sorted field ids); none listed: NONE with no field."""
    degrees = {
        (DegreeLevel(education.level), tuple(sorted(set(education.field_ids)))) for education in resume.education
    }
    return sorted(degrees) if degrees else [(DegreeLevel.NONE, ())]


def _pair(
    level: DegreeLevel,
    fields: tuple[str, ...],
    requirement: EducationRequirement,
    required_fields: list[str],
    targets: Optional[RelationTargets],
    config: EngineConfig,
) -> _Pair:
    rules = config.scoring.education
    required = DegreeLevel(requirement.min_level)
    shortfall = max(0, required.rank - level.rank)
    lenient = bool(requirement.or_equivalent or requirement.necessity != Necessity.REQUIRED)
    tier = max(0, min(shortfall, 2) - (1 if lenient else 0))
    level_score = clamp01((rules.meets, rules.one_below, rules.two_or_more_below)[tier])
    facts = [Fact(code=DEGREE_LEVEL, subject=required.value, value=level_score,
                  detail={"candidate": level.value, "shortfall": shortfall, "lenient": lenient})]
    multiplier = 1.0
    if targets is not None:
        found = best_credit(targets.credits(fields))
        if found is None:
            subject, credit, relation, via = required_fields[0], 0.0, RelationKind.NONE, None
        else:
            subject, (credit, relation, via) = found
        floor = rules.field_multiplier_min
        multiplier = clamp01(floor + (1.0 - floor) * credit)
        facts.append(Fact(code=FIELD_MATCH, subject=subject, value=credit,
                          detail={"relation": relation.value, "via": via, "multiplier": multiplier}))
    return _Pair(clamp01(level_score * multiplier), level, fields, tuple(facts))


def _best_pair(resume: Resume, requirement: EducationRequirement, graph: SkillGraph, config: EngineConfig) -> _Pair:
    required_fields = sorted(set(requirement.field_ids))
    targets = RelationTargets(required_fields, graph, config) if required_fields else None
    pairs = [
        _pair(level, fields, requirement, required_fields, targets, config) for level, fields in _degrees(resume)
    ]
    return min(pairs, key=lambda pair: (-tie_key(pair.value), -pair.level.rank, pair.fields))


def _granted(count: int, each: float, cap: float) -> list[float]:
    """What each of `count` relevant credentials adds, in order: `each` while the running total stays
    within the cap (equal to it within float noise counts as within), then what is left, then 0."""
    amounts = []
    for index in range(count):
        reached = (index + 1) * each
        if reached < cap or math.isclose(reached, cap, rel_tol=1e-9, abs_tol=_NOISE):
            amounts.append(each)
        else:
            rest = cap - index * each
            amounts.append(rest if rest > _NOISE else 0.0)
    return amounts


def _credentials(
    resume: Resume, posting: JobPosting, graph: SkillGraph, config: EngineConfig
) -> tuple[list[Fact], float]:
    """One CERT_BONUS fact per relevant credential in id order, and the bonus they add in all."""
    held = sorted({ref.canonical_id for ref in resume.certifications if Polarity(ref.polarity) is Polarity.AFFIRMED})
    if not held:
        return [], 0.0
    wanted = {requirement.skill_id for requirement in posting.requirements}
    wanted.update(posting.certifications_required, posting.certifications_preferred)
    targets = RelationTargets(wanted, graph, config)
    relevant = []
    for credential in held:
        found = best_credit(targets.credits((credential,)))
        if found is not None:
            want, (_, relation, _) = found
            relevant.append((credential, relation, want))
    rules = config.scoring.education
    cap = clamp01(rules.cert_bonus_cap)
    amounts = _granted(len(relevant), clamp01(rules.cert_bonus_each), cap)
    facts = [
        Fact(code=CERT_BONUS, subject=credential, value=amount, detail={"relation": relation.value, "to": want})
        for (credential, relation, want), amount in zip(relevant, amounts)
    ]
    return facts, min(cap, math.fsum(amounts))


def score_education(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig
) -> DimensionScore:
    ensure_bound(prepared, graph, config)
    resume = prepared.resume
    requirement = posting.education_requirement
    if requirement is None:
        best = clamp01(config.scoring.education.no_requirement)
        facts: list[Fact] = [Fact(code=NO_EDUCATION_REQUIREMENT, value=best)]
    else:
        pair = _best_pair(resume, requirement, graph, config)
        best, facts = pair.value, list(pair.facts)
    credential_facts, bonus = _credentials(resume, posting, graph, config)
    value = min(1.0, math.fsum((best, bonus)))
    return scored(DimensionName.EDUCATION, config, value, (*facts, *credential_facts))
