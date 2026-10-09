"""§5.6 Education & certifications (weight `education`).

SPEC §5.6, §9, §14.3; DECISIONS D-13, D-14, D-41.

* Degree level: the candidate's level is the highest listed degree (none listed: NONE); a degree in
  progress counts at its level, since graduation timing is never inferred (D-41). The shortfall
  below the required level picks a tier: 0 → `meets`, 1 → `one_below`, 2 or more →
  `two_or_more_below`; "or equivalent" or a PREFERRED requirement makes it one tier more lenient
  (once, whichever applies). No requirement → `no_requirement` (never penalise an unrequested
  credential, §14.3).
* Field of study: `field_multiplier_min + (1 − field_multiplier_min) × c`, c the best bare relation
  credit (`credits.RelationTargets`: exact, child, sibling, parent as for skills; field nodes are
  `field.*` ids) from a field of any listed degree to a required field; 1.0 when no field is
  required, `field_multiplier_min` when one is and none of the candidate's relates.
* Credentials: `cert_bonus_each` for every distinct AFFIRMED résumé credential related (by any
  relation) to a posting requirement skill or to a credential the posting lists as required or
  preferred, at most `cert_bonus_cap` in all.
* Value `min(1, level × field + bonus)`. The dimension never abstains.

School, institution and graduation year are not in the model and are never read (§5.6, §9; D-14).
Facts, in order: DEGREE_LEVEL (subject = the required level; detail = the candidate's level, the
shortfall and whether the tier was softened) or NO_EDUCATION_REQUIREMENT; FIELD_MATCH when a field
is required (subject = the best required field, value = its credit; detail = the relation, the
candidate's field that earns it and the multiplier; the smallest required field, credit 0 and
relation NONE when none relates); one CERT_BONUS per relevant credential in id order (value =
`cert_bonus_each`; detail = the relation and the posting id it relates to).
"""

from __future__ import annotations

import math
from typing import Optional

from matcher.config import EducationConfig, EngineConfig
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
from matcher.score.credits import RelationTargets, best_credit
from matcher.score.dimensions._common import scored
from matcher.score.numeric import clamp01
from matcher.score.prepare import PreparedResume, ensure_bound

__all__ = ["CERT_BONUS", "DEGREE_LEVEL", "FIELD_MATCH", "NO_EDUCATION_REQUIREMENT", "score_education"]

DEGREE_LEVEL = "DEGREE_LEVEL"
NO_EDUCATION_REQUIREMENT = "NO_EDUCATION_REQUIREMENT"
FIELD_MATCH = "FIELD_MATCH"
CERT_BONUS = "CERT_BONUS"


def _level(resume: Resume, requirement: EducationRequirement, rules: EducationConfig) -> tuple[float, Fact]:
    candidate = max((DegreeLevel(education.level) for education in resume.education),
                    key=lambda level: level.rank, default=DegreeLevel.NONE)
    required = DegreeLevel(requirement.min_level)
    shortfall = max(0, required.rank - candidate.rank)
    lenient = bool(requirement.or_equivalent or requirement.necessity == Necessity.PREFERRED)
    tier = max(0, min(shortfall, 2) - (1 if lenient else 0))
    value = clamp01((rules.meets, rules.one_below, rules.two_or_more_below)[tier])
    return value, Fact(code=DEGREE_LEVEL, subject=required.value, value=value,
                       detail={"candidate": candidate.value, "shortfall": shortfall, "lenient": lenient})


def _field(
    resume: Resume, requirement: EducationRequirement, graph: SkillGraph, config: EngineConfig
) -> tuple[float, Optional[Fact]]:
    required = sorted(set(requirement.field_ids))
    if not required:
        return 1.0, None
    held = {field_id for education in resume.education for field_id in education.field_ids}
    found = best_credit(RelationTargets(required, graph, config).credits(held))
    if found is None:
        subject, credit, relation, via = required[0], 0.0, RelationKind.NONE, None
    else:
        subject, (credit, relation, via) = found
    floor = config.scoring.education.field_multiplier_min
    multiplier = clamp01(floor + (1.0 - floor) * credit)
    return multiplier, Fact(code=FIELD_MATCH, subject=subject, value=credit,
                            detail={"relation": relation.value, "via": via, "multiplier": multiplier})


def _credentials(resume: Resume, posting: JobPosting, graph: SkillGraph, config: EngineConfig) -> list[Fact]:
    """One CERT_BONUS fact per relevant credential, in id order."""
    held = sorted({ref.canonical_id for ref in resume.certifications if Polarity(ref.polarity) is Polarity.AFFIRMED})
    if not held:
        return []
    wanted = {requirement.skill_id for requirement in posting.requirements}
    wanted.update(posting.certifications_required, posting.certifications_preferred)
    targets = RelationTargets(wanted, graph, config)
    each = clamp01(config.scoring.education.cert_bonus_each)
    facts = []
    for credential in held:
        found = best_credit(targets.credits((credential,)))
        if found is not None:
            want, (_, relation, _) = found
            facts.append(Fact(code=CERT_BONUS, subject=credential, value=each,
                              detail={"relation": relation.value, "to": want}))
    return facts


def score_education(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig
) -> DimensionScore:
    ensure_bound(prepared, graph, config)
    rules = config.scoring.education
    resume = prepared.resume
    requirement = posting.education_requirement
    if requirement is None:
        level, field = clamp01(rules.no_requirement), 1.0
        facts = [Fact(code=NO_EDUCATION_REQUIREMENT, value=level)]
    else:
        level, level_fact = _level(resume, requirement, rules)
        field, field_fact = _field(resume, requirement, graph, config)
        facts = [level_fact] if field_fact is None else [level_fact, field_fact]
    credentials = _credentials(resume, posting, graph, config)
    bonus = min(clamp01(rules.cert_bonus_cap), clamp01(rules.cert_bonus_each) * len(credentials))
    value = min(1.0, math.fsum((level * field, bonus)))
    return scored(DimensionName.EDUCATION, config, value, (*facts, *credentials))
