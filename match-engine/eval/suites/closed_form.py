"""The expected raw score by closed-form arithmetic (EVAL section 2.2, "the strongest test").

`expected_raw` re-implements the SPEC section 5 and 6 arithmetic (with the DECISIONS entries that
pin what the spec leaves open: D-21, D-32, D-34, D-35..D-43) from the spec, independently of the
implementation: nothing here imports `matcher.score`. It reads the models, the config (the numbers)
and the graph (the IDF table), nothing else.

It is exact only on the class of pairs `constructed_pairs` builds, and refuses anything else:

* every résumé skill either IS a requirement (exact match, credit 1.00) or relates to none of
  them (the closed-form graph has no edges and no parents, so no child, sibling or parent credit);
* every mention is AFFIRMED, with known evidence, verb class and dates (no `last_used` override);
* roles are dated at day precision, never overlap and all carry a seniority level;
* NAICS codes are 4-digit (no sector-level codes), degrees carry no field and nothing is a
  credential (no certification bonus); no projects, OR-groups, overflow or gates.

The engine must agree with it to 0.001 on every such pair (`check`).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from matcher.config import EngineConfig
from matcher.graph.model import SkillGraph, SkillNode
from matcher.models import (
    DatePrecision,
    DateValue,
    DegreeLevel,
    DimensionName,
    Education,
    EducationRequirement,
    Evidence,
    JobPosting,
    JobRequirement,
    Necessity,
    Polarity,
    Resume,
    Role,
    SeniorityLevel,
    SkillKind,
    SkillRef,
    SkillSource,
    Track,
    VerbClass,
)

CF_AS_OF = date(2026, 10, 1)
TOLERANCE = 0.001
DAYS_PER_YEAR = 365.25
DAYS_PER_MONTH = 365.25 / 12

# Requirement pool: (id, idf). The four 0.8 skills are the mutation suite's "required skills of equal
# idf 0.8"; ids without an idf entry take `idf.unseen_default` (D-21).
REQUIREMENT_IDF: tuple[tuple[str, Optional[float]], ...] = (
    ("cf.req01", 0.8), ("cf.req02", 0.8), ("cf.req03", 0.8), ("cf.req04", 0.8),
    ("cf.req05", 0.95), ("cf.req06", 0.9), ("cf.req07", 0.72), ("cf.req08", 0.7),
    ("cf.req09", 0.65), ("cf.req10", 0.55), ("cf.req11", 0.4), ("cf.req12", 0.3),
    ("cf.req13", 0.2), ("cf.req14", 0.1), ("cf.req15", None), ("cf.req16", None),
)
NOISE_IDS = tuple(f"cf.noise{i:02d}" for i in range(60))  # related to nothing
NAICS_POOL = ("5112", "5111", "5182", "5415", "6221", "3361", "3254", "5221")
_LEVELS = (
    SeniorityLevel(track=Track.IC, level=0), SeniorityLevel(track=Track.IC, level=1),
    SeniorityLevel(track=Track.IC, level=2), SeniorityLevel(track=Track.IC, level=3),
    SeniorityLevel(track=Track.IC, level=4), SeniorityLevel(track=Track.IC, level=5),
    SeniorityLevel(track=Track.MGMT, level=1), SeniorityLevel(track=Track.MGMT, level=2),
    SeniorityLevel(track=Track.MGMT, level=3),
)
_DEGREES = (DegreeLevel.HIGH_SCHOOL, DegreeLevel.ASSOCIATE, DegreeLevel.BACHELOR, DegreeLevel.MASTER,
            DegreeLevel.DOCTORATE)
_DEGREE_RANK = {DegreeLevel.NONE: 0, DegreeLevel.HIGH_SCHOOL: 1, DegreeLevel.ASSOCIATE: 2,
                DegreeLevel.BACHELOR: 3, DegreeLevel.MASTER: 4, DegreeLevel.DOCTORATE: 5}


def closed_form_graph() -> SkillGraph:
    """Requirement and noise nodes with no edges and no parents: only exact matches exist."""
    ids = [skill_id for skill_id, _ in REQUIREMENT_IDF] + list(NOISE_IDS)
    nodes = {i: SkillNode(id=i, label=i, kind=SkillKind.TECHNOLOGY) for i in ids}
    idf = {skill_id: value for skill_id, value in REQUIREMENT_IDF if value is not None}
    return SkillGraph(nodes, {}, idf, "closed-form-v1")


# ─── The arithmetic ────────────────────────────────────────────────────────────────────────


class OutOfClass(ValueError):
    """The pair is outside the class the closed form is exact on."""


def _require(condition: bool, why: str) -> None:
    if not condition:
        raise OutOfClass(why)


def _evidence(ref: SkillRef, config: EngineConfig) -> float:
    """SPEC 5.1 E; a bullet's E is its verb multiplier, quantified x bonus capped (D-32, 15.1)."""
    evidence, verbs = Evidence(ref.evidence), config.verbs
    if evidence is Evidence.SKILLS_BLOB:
        return config.scoring.evidence.SKILLS_BLOB
    if evidence is Evidence.ROLE_TITLE:
        return config.scoring.evidence.ROLE_TITLE
    if evidence is Evidence.CERTIFICATION:
        return config.scoring.evidence.CERTIFICATION
    verb = VerbClass(ref.verb_class)
    multiplier = verbs.no_verb_multiplier if verb is VerbClass.NO_VERB else verbs.classes[verb].multiplier
    if evidence is Evidence.ROLE_BULLET_QUANTIFIED:
        return min(verbs.quantified_cap, multiplier * verbs.quantified_bonus)
    return multiplier


def _recency(last: Optional[date], as_of: date, config: EngineConfig) -> float:
    """SPEC 5.1 R = max(floor, exp(-decay x years)); an undatable mention gets 1.0 (D-34)."""
    if last is None:
        return 1.0
    years = max(0, (as_of - last).days) / DAYS_PER_YEAR
    rules = config.scoring.recency
    return max(rules.floor, math.exp(-rules.decay_per_year * years))


def _role_end(role: Role, as_of: date) -> Optional[date]:
    if role.is_current:
        return as_of
    return role.end.value if role.end is not None else None


def skill_values(resume: Resume, config: EngineConfig, as_of: date) -> dict[str, float]:
    """Each skill's best E x R over its mentions."""
    ends = [d for d in (_role_end(role, as_of) for role in resume.roles) if d is not None]
    latest = max(ends) if ends else None
    values: dict[str, float] = {}

    def offer(ref: SkillRef, last: Optional[date]) -> None:
        _require(Polarity(ref.polarity) is Polarity.AFFIRMED, "every mention must be AFFIRMED")
        _require(ref.last_used is None and ref.first_used is None, "no per-mention dates")
        value = _evidence(ref, config) * _recency(last, as_of, config)
        values[ref.canonical_id] = max(values.get(ref.canonical_id, 0.0), value)

    for role in resume.roles:
        end = _role_end(role, as_of)
        for ref in role.skills:
            offer(ref, end if end is not None else latest)
    for ref in (*resume.skills_blob, *resume.certifications):
        offer(ref, latest)
    return values


def _idf(skill_id: str, graph: SkillGraph, config: EngineConfig) -> float:
    unseen = config.scoring.idf.unseen_default
    return graph.idf.get(skill_id, unseen) if skill_id in graph.nodes else unseen


@dataclass(frozen=True)
class Expected:
    """The closed form's dimension values (None = abstained), critical misses and raw score."""

    values: dict[str, Optional[float]]
    critical_misses: int
    raw: Optional[float]


def _coverage(posting: JobPosting, necessity: Necessity, values: dict[str, float], graph: SkillGraph,
              config: EngineConfig) -> tuple[Optional[float], int]:
    ids = list(dict.fromkeys(r.skill_id for r in posting.requirements if Necessity(r.necessity) is necessity))
    if not ids:
        return None, 0
    idfs = {skill_id: _idf(skill_id, graph, config) for skill_id in ids}
    total = math.fsum(idfs.values())
    if total <= 0.0:
        return None, 0
    value = math.fsum(idfs[s] * values.get(s, 0.0) for s in ids) / total
    rules = config.scoring.critical_miss
    critical = 0
    if necessity is Necessity.REQUIRED:
        critical = sum(1 for s in ids if idfs[s] > rules.idf_threshold and values.get(s, 0.0) < rules.m_threshold)
    return value, critical


def _experience(resume: Resume, posting: JobPosting, graph: SkillGraph, config: EngineConfig,
                as_of: date) -> Optional[float]:
    rules = config.scoring.experience
    if posting.min_years_total is not None:
        minimum = 12.0 * posting.min_years_total
    elif any(r.min_years is not None for r in posting.requirements if Necessity(r.necessity) is Necessity.REQUIRED):
        minimum = 12.0 * max(r.min_years for r in posting.requirements
                             if Necessity(r.necessity) is Necessity.REQUIRED and r.min_years is not None)
    elif posting.seniority is not None:
        minimum = float(rules.default_min_months[posting.seniority.code])
    else:
        return None
    if not resume.roles:
        months = 0.0
    elif not any(role.dated for role in resume.roles):
        return None  # roles but no dates: we don't know (D-37 DATES_MISSING)
    else:
        space = {s: _idf(s, graph, config) for s in dict.fromkeys(r.skill_id for r in posting.requirements)}
        job_norm = math.sqrt(math.fsum(v * v for v in space.values()))
        if job_norm <= 0.0:
            return None
        parts = []
        for role in resume.roles:
            _require(role.start is not None and (role.is_current or role.end is not None), "every role is dated")
            held = {r.canonical_id for r in role.skills}
            # cos(v_job, v_role) with v_role[r] = idf(r) for a held requirement (exact credit 1.00):
            # dot = sum idf^2 over held, |v_role| = sqrt(that), so cos = sqrt(sum idf^2 held) / |v_job|.
            relevance = math.sqrt(math.fsum(v * v for s, v in space.items() if s in held)) / job_norm
            weight = relevance * (rules.internship_weight if role.is_internship else 1.0)
            end = as_of if role.is_current else min(role.end.value, as_of)
            parts.append(max(0, (end - role.start.value).days) * weight)
        months = math.fsum(parts) / DAYS_PER_MONTH
    if months >= minimum:
        return 1.0
    ratio = months / minimum
    if ratio <= 0.0:
        return 0.0
    if ratio < rules.knee_ratio:
        return rules.value_at_knee * ratio / rules.knee_ratio
    return rules.value_at_knee + (1.0 - rules.value_at_knee) * (ratio - rules.knee_ratio) / (1.0 - rules.knee_ratio)


def _seniority(resume: Resume, posting: JobPosting, config: EngineConfig) -> Optional[float]:
    current = [r for r in resume.roles if r.is_current]
    if current:
        recent = current
    else:
        ended = [r for r in resume.roles if r.end is not None]
        if not ended:
            return None
        latest = max(r.end.value for r in ended)
        recent = [r for r in ended if r.end.value == latest]
    levels = [r.seniority for r in recent if r.seniority is not None]
    job = posting.seniority
    if job is None or not levels:
        return None
    rules = config.scoring.seniority
    best = 0.0
    for level in levels:
        cross = Track(level.track) is not Track(job.track)
        placed = level.level
        if cross:
            placed += rules.cross_track_offset if Track(job.track) is Track.IC else -rules.cross_track_offset
        gap = job.level - placed
        counted = gap if gap >= 0 else (-gap if rules.penalize_overqualification else 0)
        best = max(best, max(0.0, 1.0 - rules.step * counted - (rules.cross_track_penalty if cross else 0.0)))
    return best


def naics_credit(a: str, b: str, config: EngineConfig) -> float:
    """SPEC 5.5 for codes below sector level (D-40): prefix 1.00, same 3-digit 0.70, same sector
    (31-33, 44-45, 48-49 each one sector) 0.40, else 0.15."""
    _require(len(a) >= 3 and len(b) >= 3 and "-" not in a + b, "4-digit NAICS codes only")
    rules = config.scoring.domain
    if a.startswith(b) or b.startswith(a):
        return rules.exact
    if a[:3] == b[:3]:
        return rules.same_3digit

    def sector(code: str) -> str:
        for group in rules.sector_groups:
            if code[:2] in group:
                return group[0]
        return code[:2]

    return rules.same_2digit if sector(a) == sector(b) else rules.unrelated


def _domain(resume: Resume, posting: JobPosting, config: EngineConfig) -> Optional[float]:
    held = [c for role in resume.roles for c in role.industry_codes if c.strip()]
    wanted = [c for c in posting.industry_codes if c.strip()]
    if not held or not wanted:
        return None
    return max(naics_credit(h, w, config) for h in held for w in wanted)


def _education(resume: Resume, posting: JobPosting, config: EngineConfig) -> float:
    rules = config.scoring.education
    requirement = posting.education_requirement
    _require(not posting.certifications_required and not posting.certifications_preferred, "no credentials")
    _require(not resume.certifications, "no credentials")
    if requirement is None:
        return rules.no_requirement
    _require(not requirement.field_ids, "no required field")
    tiers = (rules.meets, rules.one_below, rules.two_or_more_below)
    degrees = [DegreeLevel(e.level) for e in resume.education] or [DegreeLevel.NONE]
    best = 0.0
    for level in degrees:
        shortfall = _DEGREE_RANK[DegreeLevel(requirement.min_level)] - _DEGREE_RANK[level]
        index = 0 if shortfall <= 0 else (1 if shortfall == 1 else 2)
        if requirement.or_equivalent or Necessity(requirement.necessity) is not Necessity.REQUIRED:
            index = max(0, index - 1)
        best = max(best, tiers[index])
    return min(1.0, best)


def expected(resume: Resume, posting: JobPosting, graph: SkillGraph, config: EngineConfig,
             as_of: date) -> Expected:
    """The dimension values and the raw score the spec's arithmetic gives for this pair."""
    _require(not resume.projects, "no projects")
    _require(not any(r.or_group for r in posting.requirements), "no OR-groups")
    _require(all(r.weight_multiplier == 1.0 for r in posting.requirements), "no overflow")
    _require(not posting.gates, "no gates")
    values = skill_values(resume, config, as_of)
    required, critical = _coverage(posting, Necessity.REQUIRED, values, graph, config)
    preferred, _ = _coverage(posting, Necessity.PREFERRED, values, graph, config)
    tools, _ = _coverage(posting, Necessity.IMPLIED, values, graph, config)
    dims: dict[str, Optional[float]] = {
        DimensionName.REQUIRED_SKILLS.value: required,
        DimensionName.PREFERRED_SKILLS.value: preferred,
        DimensionName.EXPERIENCE.value: _experience(resume, posting, graph, config, as_of),
        DimensionName.SENIORITY.value: _seniority(resume, posting, config),
        DimensionName.DOMAIN.value: _domain(resume, posting, config),
        DimensionName.EDUCATION.value: _education(resume, posting, config),
        DimensionName.TOOLS.value: tools,
    }
    weights = {name.value: config.weights.of(name) for name in DimensionName}
    no_required = not any(Necessity(r.necessity) is Necessity.REQUIRED for r in posting.requirements)
    req, pref = DimensionName.REQUIRED_SKILLS.value, DimensionName.PREFERRED_SKILLS.value
    carried = {name: w for name, w in weights.items() if dims[name] is not None}
    if (dims[req] is None and no_required and dims[pref] is not None
            and config.scoring.redistribution.required_to_preferred_when_no_required):
        carried[pref] += weights[req]  # D-42 special case: the whole 5.1 weight goes to 5.2
    total = math.fsum(carried.values())
    if not carried or total <= 0.0:
        return Expected(values=dims, critical_misses=critical, raw=None)
    raw = math.fsum(w / total * dims[name] for name, w in carried.items())
    rules = config.scoring.critical_miss
    if critical >= 2:
        raw = min(raw, rules.cap_two_or_more)
    elif critical == 1:
        raw = min(raw, rules.cap_one)
    return Expected(values=dims, critical_misses=critical, raw=min(1.0, max(0.0, raw)))


def expected_raw(resume: Resume, posting: JobPosting, graph: SkillGraph, config: EngineConfig,
                 as_of: date) -> Optional[float]:
    return expected(resume, posting, graph, config, as_of).raw


# ─── Constructed pairs ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConstructedPair:
    case: str
    resume: Resume
    posting: JobPosting
    as_of: date = CF_AS_OF


def day(d: date) -> DateValue:
    return DateValue.exact(d, DatePrecision.DAY)


def month_start(d: date) -> date:
    return d.replace(day=1)


def _months_before(d: date, months: int) -> date:
    index = d.year * 12 + d.month - 1 - months
    return date(index // 12, index % 12 + 1, 1)


def _mention(rng: random.Random, skill_id: str) -> SkillRef:
    evidence = rng.choice((Evidence.ROLE_BULLET, Evidence.ROLE_BULLET, Evidence.ROLE_BULLET_QUANTIFIED,
                           Evidence.ROLE_TITLE))
    verb = VerbClass.NO_VERB if evidence is Evidence.ROLE_TITLE else rng.choice(tuple(VerbClass))
    return SkillRef(canonical_id=skill_id, surface_form=skill_id, evidence=evidence, verb_class=verb,
                    source=SkillSource.ROLE)


def _constructed(rng: random.Random, case: str, as_of: date) -> ConstructedPair:
    pool = [skill_id for skill_id, _ in REQUIREMENT_IDF]
    rng.shuffle(pool)
    n_req, n_pref, n_impl = rng.randint(0, 6), rng.randint(0, 4), rng.randint(0, 4)
    if n_req + n_pref + n_impl == 0:
        n_req = 1
    chosen = pool[: n_req + n_pref + n_impl]
    necessities = [Necessity.REQUIRED] * n_req + [Necessity.PREFERRED] * n_pref + [Necessity.IMPLIED] * n_impl
    requirements = tuple(JobRequirement(skill_id=s, necessity=n, surface_form=s)
                         for s, n in zip(chosen, necessities))
    education = None
    if rng.random() < 0.6:
        education = EducationRequirement(min_level=rng.choice(_DEGREES[1:4]), or_equivalent=rng.random() < 0.3,
                                         necessity=rng.choice((Necessity.REQUIRED, Necessity.PREFERRED)))
    posting = JobPosting(
        title_raw="Closed-form posting",
        seniority=rng.choice((*_LEVELS[1:], None)),
        requirements=requirements,
        min_years_total=rng.choice((None, None, 0.5, 1.0, 2.0, 3.0, 5.0)),
        education_requirement=education,
        industry_codes=tuple(rng.sample(NAICS_POOL, k=rng.choice((0, 1, 1, 2)))),
        requirement_token_count=len(requirements),
    )
    # Roles, most recent first, laid end to end backwards from as_of with gaps: never overlapping.
    roles: list[Role] = []
    cursor = as_of if rng.random() < 0.5 else _months_before(month_start(as_of), rng.randint(1, 30))
    current = cursor == as_of
    for k in range(rng.randint(1, 3)):
        start = _months_before(month_start(cursor), rng.randint(3, 60))
        held = rng.sample(chosen, k=rng.randint(0, len(chosen))) + rng.sample(NOISE_IDS, k=rng.randint(0, 3))
        mentions = [_mention(rng, s) for s in held for _ in range(rng.choice((1, 1, 2)))]
        is_current = current and k == 0
        roles.append(Role(
            title_raw=f"Role {k}",
            seniority=rng.choice(_LEVELS),
            employer=f"Employer {k}",
            industry_codes=tuple(rng.sample(NAICS_POOL, k=rng.choice((0, 1, 1, 2)))),
            start=day(start),
            end=None if is_current else day(cursor),
            is_current=is_current,
            is_internship=rng.random() < 0.2,
            skills=tuple(mentions),
        ))
        cursor = _months_before(start, rng.randint(0, 12))
    blob = tuple(SkillRef(canonical_id=s, surface_form=s, evidence=Evidence.SKILLS_BLOB,
                          source=SkillSource.SKILLS_SECTION)
                 for s in rng.sample(chosen + list(NOISE_IDS[:10]), k=rng.randint(0, 5)))
    degrees = tuple(Education(level=lvl, in_progress=rng.random() < 0.2)
                    for lvl in rng.sample(_DEGREES, k=rng.choice((0, 1, 1, 2))))
    resume = Resume(roles=tuple(roles), skills_blob=blob, education=degrees)
    return ConstructedPair(case=case, resume=resume, posting=posting, as_of=as_of)


def constructed_pairs(n: int, seed: int = 0, as_of: date = CF_AS_OF) -> tuple[ConstructedPair, ...]:
    """`n` random pairs inside the closed form's class (seeded, so a failing case is reproducible
    from its name alone: `closed_form:<seed>:<i>`)."""
    return tuple(_constructed(random.Random(f"closed-form:{seed}:{i}"), f"closed_form:{seed}:{i}", as_of)
                 for i in range(n))


@dataclass(frozen=True)
class Mismatch:
    case: str
    engine_raw: Optional[float]
    expected_raw: Optional[float]
    engine_dimensions: dict[str, Optional[float]]
    expected_dimensions: dict[str, Optional[float]]
    pair: ConstructedPair


def check(pairs: tuple[ConstructedPair, ...], engine: object, tolerance: float = TOLERANCE) -> tuple[
        list[Mismatch], float]:
    """Engine raw vs closed form on every pair: (mismatches, largest absolute difference)."""
    from eval.suites import BULK

    mismatches: list[Mismatch] = []
    worst = 0.0
    for pair in pairs:
        want = expected(pair.resume, pair.posting, engine.graph, engine.config, pair.as_of)
        result = engine.score(pair.resume, pair.posting, as_of=pair.as_of, options=BULK)
        got = result.raw_score
        if got is None or want.raw is None:
            ok = got is None and want.raw is None
        else:
            worst = max(worst, abs(got - want.raw))
            ok = math.isclose(got, want.raw, rel_tol=0.0, abs_tol=tolerance)
        if not ok:
            mismatches.append(Mismatch(
                case=pair.case, engine_raw=got, expected_raw=want.raw,
                engine_dimensions={k: d.value for k, d in result.dimensions.items()},
                expected_dimensions=want.values, pair=pair))
    return mismatches, worst


def shifted(d: date, days: int) -> date:
    return d - timedelta(days=days)
