"""Candidate profiles: the ground truth every synthetic résumé is written from (EVAL.md 2.1).

`generate_profiles` builds each profile for one target posting at a graded fit level, by
construction rather than by guessing:

    level 3  covers 90-100 % of the REQUIRED skills with work-history evidence, and meets the
             posting's years, seniority and education
    level 2  covers about 70 %, a little short on years, up to one rung below on seniority
    level 1  covers about 40 %, clearly short on years, one or two rungs below
    level 0  comes from a different family and shares none of the REQUIRED skills

`fit_level` is the graded relevance label of the pair (profile, target posting). Profile i depends
only on (seed, i, the posting it targets), so generating more profiles never changes earlier ones.

`expected_resume` maps the ground truth onto the engine's `Resume`: what a perfect parser would
recover from any rendering of the profile.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Optional

from pydantic import Field, model_validator

from eval.generate import lexicon
from eval.generate.bullets import compose_bullet, group_of
from eval.generate.careers import (
    IC0,
    Job,
    final_seniority,
    from_index,
    month_index,
    pick_industry,
    role_months,
    target_years,
    timeline,
    union_months,
)
from eval.generate.lexicon import FamilyData, family_data, title_for
from eval.generate.postings import posting_id, viable_families
from eval.generate.vocabulary import (
    VocabIndex,
    VocabSkill,
    Vocabulary,
    find_mentions,
    resolve_form,
    vocabulary_index,
)
from matcher.errors import Flag
from matcher.models import (
    DatePrecision,
    DateValue,
    DegreeLevel,
    Education,
    Evidence,
    Frozen,
    JobPosting,
    Location,
    Necessity,
    Project,
    Resume,
    Role,
    SeniorityLevel,
    SkillRef,
    SkillSource,
    Track,
    VerbClass,
)

# The date every generated career is relative to ("Present" means this day). Fixed, never the clock.
GENERATION_AS_OF = date(2026, 10, 1)

# --------------------------------------------------------------------------------------------
# ground-truth models
# --------------------------------------------------------------------------------------------


class GroundTruthBullet(Frozen):
    text: str
    skill_ids: tuple[str, ...] = ()
    verb_class: VerbClass
    quantified: bool = False


class GroundTruthRole(Frozen):
    title: str
    seniority: SeniorityLevel
    employer: str
    industry: str  # NAICS code
    start: date  # first day of the month
    end: Optional[date] = None  # None = still there
    is_internship: bool = False
    bullets: tuple[GroundTruthBullet, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> "GroundTruthRole":
        if self.end is not None and self.end <= self.start:
            raise ValueError("a role must end after it starts")
        return self


class GroundTruthProject(Frozen):
    name: str
    start: date
    end: date
    bullets: tuple[GroundTruthBullet, ...] = ()

    @model_validator(mode="after")
    def _ordered(self) -> "GroundTruthProject":
        if self.end < self.start:
            raise ValueError("a project must not end before it starts")
        return self


class CandidateProfile(Frozen):
    """One synthetic person. `family` is the profession the profile was written from (for level 0
    that is not the target posting's); name, e-mail and phone are invented and never read by the
    engine (SPEC 9). `skills` is every skill the résumé evidences with its best evidence tier, the
    years it is shown in the work history and the last day of use, all relative to
    `GENERATION_AS_OF` (a skill only listed on the skills line or as a certification has 0.0 years
    and no last-used date: nothing dates it); roles run most recent first."""

    profile_id: str
    family: str
    target_posting_id: str
    fit_level: int = Field(ge=0, le=3)
    name: str = "Alex Example"
    email: str = "alex.example@example.com"
    phone: str = ""
    locations: tuple[Location, ...] = ()
    skills: tuple[tuple[str, float, Evidence, Optional[date]], ...] = ()
    roles: tuple[GroundTruthRole, ...] = ()
    projects: tuple[GroundTruthProject, ...] = ()
    education: tuple[Education, ...] = ()
    certifications: tuple[str, ...] = ()
    skills_section: tuple[str, ...] = ()
    summary: str = ""


# --------------------------------------------------------------------------------------------
# tuning: how a fit level shapes a profile
# --------------------------------------------------------------------------------------------

_LEVEL_FRACTION = {3: (0.9, 1.0), 2: (0.7, 0.7), 1: (0.4, 0.4)}  # share of REQUIRED skills covered
_PREFERRED_COVERAGE = {3: (0.5, 0.8), 2: (0.3, 0.5), 1: (0.1, 0.3), 0: (0.0, 0.0)}
_CLASS_WEIGHTS: dict[int, dict[VerbClass, int]] = {
    3: {
        VerbClass.OWNERSHIP: 38,
        VerbClass.EXECUTION: 40,
        VerbClass.PARTICIPATION: 12,
        VerbClass.ASSISTANCE: 4,
        VerbClass.EXPOSURE: 0,
        VerbClass.NO_VERB: 6,
    },
    2: {
        VerbClass.OWNERSHIP: 24,
        VerbClass.EXECUTION: 38,
        VerbClass.PARTICIPATION: 20,
        VerbClass.ASSISTANCE: 9,
        VerbClass.EXPOSURE: 3,
        VerbClass.NO_VERB: 6,
    },
    1: {
        VerbClass.OWNERSHIP: 10,
        VerbClass.EXECUTION: 28,
        VerbClass.PARTICIPATION: 24,
        VerbClass.ASSISTANCE: 20,
        VerbClass.EXPOSURE: 10,
        VerbClass.NO_VERB: 8,
    },
    0: {
        VerbClass.OWNERSHIP: 20,
        VerbClass.EXECUTION: 38,
        VerbClass.PARTICIPATION: 22,
        VerbClass.ASSISTANCE: 10,
        VerbClass.EXPOSURE: 4,
        VerbClass.NO_VERB: 6,
    },
}
_QUANTIFIED_SHARE = {3: 0.45, 2: 0.35, 1: 0.25, 0: 0.3}
_MAX_SKILLS_PER_ROLE = 12


# --------------------------------------------------------------------------------------------
# skills: what the candidate knows, where it is shown
# --------------------------------------------------------------------------------------------


def _form(rng: random.Random, skill: VocabSkill) -> str:
    return rng.choice(skill.aliases) if skill.aliases and rng.random() < 0.15 else skill.label


def _weighted_class(rng: random.Random, weights: dict[VerbClass, int], *, filler: bool = False) -> VerbClass:
    items = [(c, w) for c, w in weights.items() if not (filler and c is VerbClass.EXPOSURE)]
    roll, acc = rng.random() * sum(w for _c, w in items), 0
    for cls, w in items:
        acc += w
        if roll < acc:
            return cls
    return items[-1][0]


def _role_weights(level: int, seniority: SeniorityLevel) -> dict[VerbClass, int]:
    weights = dict(_CLASS_WEIGHTS[level])
    if seniority.track is Track.IC and seniority.level <= 1:  # juniors own less and are exposed more
        weights[VerbClass.OWNERSHIP] = round(weights[VerbClass.OWNERSHIP] * 0.4)
        weights[VerbClass.ASSISTANCE] += 8
        weights[VerbClass.EXPOSURE] += 4
    return weights


def _write_bullets(
    rng: random.Random,
    vocab: Vocabulary,
    fam: FamilyData,
    pool: Sequence[VocabSkill],
    level: int,
    weights: dict[VerbClass, int],
    minimum: int,
    maximum: int,
) -> tuple[GroundTruthBullet, ...]:
    """One bullet per group of skills (instruments share up to three), plus filler up to `minimum`."""
    instruments = [s for s in pool if group_of(s.kind) == "instrument"]
    singles = [[s] for s in pool if group_of(s.kind) != "instrument"]
    chunks: list[list[VocabSkill]] = []
    size_choices = (1, 2, 2, 3, 3)
    i = 0
    while i < len(instruments):
        size = rng.choice(size_choices)
        chunks.append(instruments[i : i + size])
        i += size
    specs = chunks + singles
    if len(specs) > maximum:  # pack the instruments three to a bullet
        chunks = [instruments[k : k + 3] for k in range(0, len(instruments), 3)]
        specs = chunks + singles
    rng.shuffle(specs)
    while len(specs) < minimum:
        specs.append([])
    bullets: list[GroundTruthBullet] = []
    used: set[str] = set()
    for spec in specs:
        for _attempt in range(40):
            cls = _weighted_class(rng, weights, filler=not spec)
            quantified = cls is not VerbClass.EXPOSURE and rng.random() < _QUANTIFIED_SHARE[level]
            forms = [(s, _form(rng, s)) for s in spec]
            text = compose_bullet(rng, fam, cls, forms, quantified)
            if text in used:
                continue
            if [m.skill_id for m in find_mentions(vocab, text)] != [s.id for s in spec]:
                continue  # a word of the template spelled another skill: draw again
            break
        else:
            raise RuntimeError(f"could not write a clean bullet about {[s.id for s in spec]}")
        used.add(text)
        bullets.append(
            GroundTruthBullet(
                text=text, skill_ids=tuple(s.id for s in spec), verb_class=cls, quantified=quantified
            )
        )
    return tuple(bullets)


def _education(
    rng: random.Random,
    index: VocabIndex,
    family: str,
    level: int,
    posting: JobPosting,
    final: SeniorityLevel,
    required_fields: tuple[str, ...],
) -> tuple[Education, ...]:
    req = posting.education_requirement
    target = req.min_level if req else DegreeLevel.BACHELOR
    ranks = [
        DegreeLevel.HIGH_SCHOOL,
        DegreeLevel.ASSOCIATE,
        DegreeLevel.BACHELOR,
        DegreeLevel.MASTER,
        DegreeLevel.DOCTORATE,
    ]
    at = ranks.index(target) if target in ranks else 2
    if level >= 2:
        picked = ranks[min(at + (1 if rng.random() < 0.15 else 0), len(ranks) - 1)]
    elif level == 1:
        picked = ranks[max(0, at - (1 if rng.random() < 0.6 else 0))]
    else:
        picked = rng.choice(
            (DegreeLevel.ASSOCIATE, DegreeLevel.BACHELOR, DegreeLevel.BACHELOR, DegreeLevel.MASTER)
        )
    fields = list(index.fields[family])
    in_progress = final.track is Track.IC and final.level == 0 and picked.rank >= DegreeLevel.BACHELOR.rank
    if picked is DegreeLevel.HIGH_SCHOOL:
        return (Education(level=picked, field_raw="", in_progress=False),)

    def entry(level_: DegreeLevel, prefer_required: bool, progress: bool) -> Education:
        chosen: VocabSkill | None = None
        if prefer_required and required_fields and level >= 2 and rng.random() < 0.7:
            chosen = index.field_by_id.get(rng.choice(required_fields))
        if chosen is None and fields:
            chosen = rng.choice(fields)
        if chosen is None:
            return Education(level=level_, field_raw="", in_progress=progress)
        return Education(level=level_, field_ids=(chosen.id,), field_raw=chosen.label, in_progress=progress)

    top = entry(picked, True, in_progress)
    if picked.rank >= DegreeLevel.MASTER.rank and rng.random() < 0.6:
        return (top, entry(DegreeLevel.BACHELOR, False, False))
    return (top,)


# --------------------------------------------------------------------------------------------
# one profile, in the order a person is described: who, what they know, where they used it
# --------------------------------------------------------------------------------------------


@dataclass
class _SkillPlan:
    in_bullets: list[VocabSkill]  # skills the work history will show, covered requirements first
    blob_only: list[VocabSkill]  # skills that only the skills line will carry
    softs: list[VocabSkill]  # soft skills (skills line only)
    required_ids: frozenset[str]  # covered requirements: a role may be crowded, they are never dropped


def _plan_skills(
    rng: random.Random,
    index: VocabIndex,
    family: str,
    required: list[VocabSkill],
    preferred: list[VocabSkill],
    level: int,
    junior: bool,
) -> _SkillPlan:
    """Which skills the candidate has: the fit level decides how many REQUIRED ones (EVAL 2.1)."""
    if level == 0:
        covered_required: list[VocabSkill] = []
    elif level == 3:
        low = -(-len(required) * 9 // 10)  # ceil(0.9 n)
        covered_required = rng.sample(required, rng.randint(low, len(required)))
    else:
        covered_required = rng.sample(required, max(1, round(_LEVEL_FRACTION[level][0] * len(required))))
    stuffed: list[VocabSkill] = []  # a required skill claimed on the skills line only (keyword stuffing)
    if level in (1, 2) and rng.random() < 0.3:
        # A one-skill posting leaves nothing to stuff (level 1 and 2 cover at least one skill). The
        # draw above still happens, so every profile that generated before keeps its random stream.
        uncovered = [s for s in required if s not in covered_required]
        if uncovered:
            stuffed = rng.sample(uncovered, 1)

    lo, hi = _PREFERRED_COVERAGE[level]
    pref_take = round(len(preferred) * rng.uniform(lo, hi)) if level else 0
    covered_pref = rng.sample(preferred, pref_take) if pref_take else []
    taken = {s.id for s in covered_required} | {s.id for s in covered_pref} | {s.id for s in stuffed}
    keep_out = {s.id for s in required} | ({s.id for s in preferred} if level else set())
    extra_pool = [s for s in index.hard[family] if s.id not in keep_out and s.id not in taken]
    extras = rng.sample(extra_pool, min(len(extra_pool), rng.randint(3, 5) if junior else rng.randint(3, 8)))
    softs = rng.sample(list(index.soft[family]), min(len(index.soft[family]), rng.randint(2, 4)))

    in_bullets = list(covered_required)
    blob_only = list(stuffed)
    for skill in covered_pref:
        (in_bullets if rng.random() < 0.75 else blob_only).append(skill)
    for skill in extras:
        (in_bullets if rng.random() < 0.65 else blob_only).append(skill)
    return _SkillPlan(in_bullets, blob_only, softs, frozenset(s.id for s in covered_required))


def _place_skills(rng: random.Random, plan: _SkillPlan, n_roles: int, level: int) -> list[list[VocabSkill]]:
    """Spread the skills over the roles (index 0 is the oldest): each skill is used in one to three
    consecutive roles, mostly the recent ones. A skill no role has room for falls back to the skills
    line, unless it is a covered requirement."""
    pools: list[list[VocabSkill]] = [[] for _ in range(n_roles)]
    for skill in plan.in_bullets:
        recent = rng.random() < (0.85 if (level == 3 and skill.id in plan.required_ids) else 0.55)
        end_at = n_roles - 1 if (recent or n_roles == 1) else rng.randrange(n_roles)
        span = rng.choice((1, 1, 2, 2, 3))
        placed = False
        for slot in (end_at - k for k in range(span) if end_at - k >= 0):
            if len(pools[slot]) < _MAX_SKILLS_PER_ROLE:
                pools[slot].append(skill)
                placed = True
        if not placed:
            for slot in range(n_roles - 1, -1, -1):
                if len(pools[slot]) < _MAX_SKILLS_PER_ROLE:
                    pools[slot].append(skill)
                    placed = True
                    break
        if not placed:
            if skill.id in plan.required_ids:  # never lose a covered requirement to a full role
                pools[end_at].append(skill)
            else:
                plan.blob_only.append(skill)
    return pools


def _write_roles(
    rng: random.Random,
    vocab: Vocabulary,
    fam: FamilyData,
    jobs: list[Job],
    pools: list[list[VocabSkill]],
    level: int,
    target_code: str | None,
) -> list[GroundTruthRole]:
    """The role records, most recent first. Only the newest role is placed in the posting's
    industry: level 3 exactly, level 2 nearby, otherwise anywhere in the profession's."""
    mode = {3: "exact", 2: "near"}.get(level, "random")
    employers = rng.sample(lexicon.EMPLOYER_PREFIXES, len(jobs))
    roles: list[GroundTruthRole] = []
    for k, job in enumerate(jobs):
        pool = pools[k]
        weights = _role_weights(level, job.seniority)
        bullets = _write_bullets(rng, vocab, fam, pool, level, weights, 3 if len(pool) >= 2 else 2, 9)
        base = None  # now and then a title names a skill the role's bullets show (ROLE_TITLE)
        if fam.title_pattern and job.seniority.track is Track.IC and rng.random() < 0.2:
            titled = [s for s in pool if s.kind in fam.title_kinds]
            if titled:
                base = fam.title_pattern.format(skill=rng.choice(titled).label)
        roles.append(
            GroundTruthRole(
                title=title_for(fam, job.seniority, rng, base),
                seniority=job.seniority,
                employer=f"{employers[k]} {rng.choice(fam.employer_words)}",
                industry=pick_industry(rng, fam.naics, target_code, mode if k == len(jobs) - 1 else "random"),
                start=from_index(job.start),
                end=None if job.current else from_index(job.end),
                is_internship=job.seniority == IC0,
                bullets=bullets,
            )
        )
    roles.reverse()
    return roles


def _write_projects(
    rng: random.Random,
    vocab: Vocabulary,
    fam: FamilyData,
    plan: _SkillPlan,
    pools: list[list[VocabSkill]],
    level: int,
    junior: bool,
    as_of: date,
) -> list[GroundTruthProject]:
    """Projects are a student's strongest evidence (D-12): most juniors have one or two. They only
    show skills the candidate legitimately has, so a stuffed requirement never reaches a bullet."""
    n = (rng.choice((1, 2, 2)) if rng.random() < 0.85 else 0) if junior else (1 if rng.random() < 0.15 else 0)
    shown = [s for pool in pools for s in pool]
    shown_ids = {s.id for s in shown}
    candidates = list(dict.fromkeys(shown + [s for s in plan.in_bullets if s.id not in shown_ids]))
    projects: list[GroundTruthProject] = []
    for name in rng.sample(lexicon.PROJECT_NAMES, n):
        if len(candidates) < 2:
            break
        picked = rng.sample(candidates, min(len(candidates), rng.randint(2, 5)))
        end_idx = month_index(as_of) - rng.randint(1, 20)
        weights = _role_weights(level, SeniorityLevel(track=Track.IC, level=1))
        projects.append(
            GroundTruthProject(
                name=name,
                start=from_index(end_idx - rng.randint(2, 5)),
                end=from_index(end_idx),
                bullets=_write_bullets(rng, vocab, fam, picked, level, weights, 2, 4),
            )
        )
    return projects


def _pick_certifications(
    rng: random.Random, index: VocabIndex, family: str, level: int, posting: JobPosting
) -> tuple[str, ...]:
    pool = list(index.credentials[family])
    if not pool or rng.random() >= {3: 0.6, 2: 0.35, 1: 0.15, 0: 0.2}[level]:
        return ()
    wanted = [c for c in posting.certifications_preferred if level and c in index.by_id]
    certs = [wanted[0] if wanted and rng.random() < 0.7 else rng.choice(pool).id]
    if rng.random() < 0.2:
        other = rng.choice(pool).id
        if other not in certs:
            certs.append(other)
    return tuple(certs)


def _write_skills_line(
    rng: random.Random,
    index: VocabIndex,
    plan: _SkillPlan,
    pools: list[list[VocabSkill]],
    projects: list[GroundTruthProject],
) -> tuple[str, ...]:
    """The skills line: everything that is only claimed here, the soft skills, and most (not all)
    of what the bullets show, in a shuffled order and now and then under an alias."""
    section = list(plan.blob_only) + list(plan.softs)
    section += [s for pool in pools for s in pool if rng.random() < 0.7]
    section += [
        index.by_id[sid] for p in projects for b in p.bullets for sid in b.skill_ids if rng.random() < 0.5
    ]
    ordered = list({skill.id: skill for skill in section}.values())
    rng.shuffle(ordered)
    return tuple(_form(rng, s) for s in ordered[:30])


def _build_profile(
    i: int,
    rng: random.Random,
    vocab: Vocabulary,
    index: VocabIndex,
    posting: JobPosting,
    target_id: str,
    level: int,
    families: tuple[str, ...],
    as_of: date,
) -> CandidateProfile:
    posting_family = posting.job_family or ""
    family = rng.choice([f for f in families if f != posting_family]) if level == 0 else posting_family
    fam = family_data(family)
    posting_sen = posting.seniority or SeniorityLevel(track=Track.IC, level=2)
    posting_years = posting.min_years_total or 0.0

    def skills_of(necessity: Necessity) -> list[VocabSkill]:
        try:
            return [index.by_id[r.skill_id] for r in posting.requirements if r.necessity is necessity]
        except KeyError as missing:
            raise ValueError(
                f"posting {target_id} asks for {missing}, which is not in the vocabulary"
            ) from None

    # who: seniority, years, the timeline of roles (oldest first)
    if level == 0:
        final = rng.choice(
            [SeniorityLevel(track=Track.IC, level=k) for k in (1, 1, 2, 2, 2, 3, 3, 4)]
            + [SeniorityLevel(track=Track.MGMT, level=2)]
        )
    else:
        final = final_seniority(rng, level, posting_sen)
    years = target_years(rng, level, posting_years, final)
    jobs = timeline(rng, level, final, years, int(round(posting_years * 12)), as_of)
    junior = final.track is Track.IC and final.level <= 1

    # what they know, and where they used it
    plan = _plan_skills(
        rng, index, family, skills_of(Necessity.REQUIRED), skills_of(Necessity.PREFERRED), level, junior
    )
    pools = _place_skills(rng, plan, len(jobs), level)
    target_code = posting.industry_codes[0] if posting.industry_codes else None
    roles = _write_roles(rng, vocab, fam, jobs, pools, level, target_code)
    projects = _write_projects(rng, vocab, fam, plan, pools, level, junior, as_of)

    # the rest of the page
    req = posting.education_requirement
    education = _education(rng, index, family, level, posting, final, req.field_ids if req else ())
    certifications = _pick_certifications(rng, index, family, level, posting)
    skills_section = _write_skills_line(rng, index, plan, pools, projects)
    first, last = rng.choice(lexicon.FIRST_NAMES), rng.choice(lexicon.LAST_NAMES)
    city, region, country = rng.choice(lexicon.LOCATIONS)
    summary = _summary(
        rng, fam, final, union_months([role_months(r.start, r.end, as_of) for r in roles]), education
    )
    profile = CandidateProfile(
        profile_id=f"p{i + 1:04d}",
        family=family,
        target_posting_id=target_id,
        fit_level=level,
        name=f"{first} {last}",
        email=f"{first.lower()}.{last.lower()}@example.com",
        phone=f"({rng.choice(lexicon.AREA_CODES)}) 555-01{rng.randint(0, 99):02d}",
        locations=(Location(country=country, region=region, city=city),),
        roles=tuple(roles),
        projects=tuple(projects),
        education=education,
        certifications=certifications,
        skills_section=skills_section,
        summary=summary,
    )
    return profile.model_copy(update={"skills": _derive_skills(vocab, profile, as_of)})


def _summary(
    rng: random.Random, fam: FamilyData, final: SeniorityLevel, months: int, education: Sequence[Education]
) -> str:
    adjective = rng.choice(lexicon.SUMMARY_ADJECTIVES)
    top = education[0] if education else None
    if final.track is Track.IC and final.level == 0 and top is not None and top.in_progress and top.field_raw:
        return lexicon.STUDENT_SUMMARY.format(adj=adjective, field=top.field_raw.lower())
    noun = fam.ic_title.lower() if final.track is Track.IC else f"{fam.function.lower()} leader"
    years = months // 12
    if years >= 2:
        span = f"{years}+ years"
    elif months >= 12:
        span = "over a year"
    else:
        span = "a year or less"
    return f"{adjective} {noun} with {span} of experience {rng.choice(lexicon.SUMMARY_CLAUSES)}."


# --------------------------------------------------------------------------------------------
# the skills tuple, derived from what the résumé shows
# --------------------------------------------------------------------------------------------

_TIER_RANK = {
    Evidence.SKILLS_BLOB: 0,
    Evidence.ROLE_BULLET: 1,
    Evidence.ROLE_BULLET_QUANTIFIED: 2,
    Evidence.ROLE_TITLE: 3,
    Evidence.CERTIFICATION: 4,
}


def _derive_skills(
    vocab: Vocabulary, profile: CandidateProfile, as_of: date
) -> tuple[tuple[str, float, Evidence, Optional[date]], ...]:
    """(skill, years, best tier, last used) for every skill the résumé evidences. Years count the
    months of the roles whose bullets or title show the skill (merged, never double-counted);
    projects date a skill but never add months (D-12)."""
    tiers: dict[str, Evidence] = {}
    months: dict[str, set[int]] = {}
    last: dict[str, date] = {}

    def note(skill_id: str, tier: Evidence, used: date | None = None, covered: range | None = None) -> None:
        best = tiers.get(skill_id)
        if best is None or _TIER_RANK[tier] > _TIER_RANK[best]:
            tiers[skill_id] = tier
        months.setdefault(skill_id, set())
        if covered is not None:
            months[skill_id].update(covered)
        if used is not None and (skill_id not in last or used > last[skill_id]):
            last[skill_id] = used

    for role in profile.roles:
        covered, used = role_months(role.start, role.end, as_of), role.end or as_of
        for m in find_mentions(vocab, role.title):
            note(m.skill_id, Evidence.ROLE_TITLE, used, covered)
        for bullet in role.bullets:
            tier = Evidence.ROLE_BULLET_QUANTIFIED if bullet.quantified else Evidence.ROLE_BULLET
            for sid in bullet.skill_ids:
                note(sid, tier, used, covered)
    for project in profile.projects:
        for bullet in project.bullets:
            tier = Evidence.ROLE_BULLET_QUANTIFIED if bullet.quantified else Evidence.ROLE_BULLET
            for sid in bullet.skill_ids:
                note(sid, tier, project.end)
    for label in profile.skills_section:
        sid = resolve_form(vocab, label)
        if sid is not None:
            note(sid, Evidence.SKILLS_BLOB)
    for sid in profile.certifications:
        note(sid, Evidence.CERTIFICATION)
    return tuple((sid, round(len(months[sid]) / 12, 2), tiers[sid], last.get(sid)) for sid in tiers)


# --------------------------------------------------------------------------------------------
# generate_profiles
# --------------------------------------------------------------------------------------------


def generate_profiles(
    vocab: Vocabulary,
    postings: Sequence[JobPosting],
    n: int,
    seed: int,
    *,
    as_of: date = GENERATION_AS_OF,
) -> tuple[CandidateProfile, ...]:
    """`n` profiles, spread over the postings round-robin and over the four fit levels per posting.

    Profile i targets posting i mod P; its level is (i mod P + i div P) mod 4, so every posting
    receives every level once it has four candidates, and the levels stay balanced overall.
    """
    if not postings:
        raise ValueError("generate_profiles needs at least one posting")
    index = vocabulary_index(vocab)
    families = viable_families(vocab, index)
    if len(families) < 2:
        raise ValueError("fit level 0 needs a second family to draw the candidate from")
    ids = [posting_id(p) for p in postings]
    profiles: list[CandidateProfile] = []
    for i in range(n):
        j, k = i % len(postings), i // len(postings)
        rng = random.Random(f"profile:{seed}:{i}")
        profiles.append(
            _build_profile(i, rng, vocab, index, postings[j], ids[j], (j + k) % 4, families, as_of)
        )
    return tuple(profiles)


# --------------------------------------------------------------------------------------------
# expected_resume
# --------------------------------------------------------------------------------------------


def _month(d: date) -> DateValue:
    return DateValue.exact(d, DatePrecision.MONTH)


def functional_skills(profile: CandidateProfile, vocab: Vocabulary) -> list[tuple[str, str]]:
    """(skill id, spelling) for the grouped skills of a functional résumé: the skills line as
    written, then every other skill the profile evidences, once each. Credentials are listed
    separately, on the certifications line."""
    index = vocabulary_index(vocab)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(skill_id: str, spelling: str) -> None:
        if skill_id not in seen and skill_id not in profile.certifications:
            seen.add(skill_id)
            out.append((skill_id, spelling))

    for label in profile.skills_section:
        sid = resolve_form(vocab, label)
        if sid is None:
            raise ValueError(f"cannot resolve the skills line entry {label!r} to a skill of the vocabulary")
        add(sid, label)
    for role in profile.roles:
        for bullet in role.bullets:
            for sid in bullet.skill_ids:
                add(sid, index.by_id[sid].label)
    for project in profile.projects:
        for bullet in project.bullets:
            for sid in bullet.skill_ids:
                add(sid, index.by_id[sid].label)
    for role in profile.roles:
        for m in find_mentions(vocab, role.title):
            add(m.skill_id, m.text)
    return out


def _bullet_refs(
    vocab: Vocabulary, bullet: GroundTruthBullet, first: date, last: date, source: SkillSource
) -> list[SkillRef]:
    first_seen: dict[str, str] = {}
    for m in find_mentions(vocab, bullet.text):
        first_seen.setdefault(m.skill_id, m.text)
    evidence = Evidence.ROLE_BULLET_QUANTIFIED if bullet.quantified else Evidence.ROLE_BULLET
    refs = []
    for sid in bullet.skill_ids:
        if sid not in first_seen:
            raise ValueError(f"bullet {bullet.text!r} does not spell its skill {sid}")
        refs.append(
            SkillRef(
                canonical_id=sid,
                surface_form=first_seen[sid],
                evidence=evidence,
                first_used=first,
                last_used=last,
                verb_class=bullet.verb_class,
                source=source,
            )
        )
    return refs


def expected_resume(
    profile: CandidateProfile, vocab: Vocabulary, as_of: date, *, template: str | None = None
) -> Resume:
    """The `Resume` a perfect parser recovers from any rendering of `profile`.

    Evidence tiers follow where the skill is shown (bullet, quantified bullet, title, skills line,
    certification), verb classes are the bullets', dates are month precision, `as_of` closes the
    current role. `template="functional"` describes what survives a résumé without a chronology:
    undated roles with no bullets, every skill a blob entry, no experience total (SPEC 14.3).
    Every other template yields the same Resume.
    """
    index = vocabulary_index(vocab)
    functional = template == "functional"
    roles: list[Role] = []
    for gt in profile.roles:
        last = gt.end or as_of
        refs = [
            SkillRef(
                canonical_id=m.skill_id,
                surface_form=m.text,
                evidence=Evidence.ROLE_TITLE,
                first_used=None if functional else gt.start,
                last_used=None if functional else last,
                verb_class=VerbClass.NO_VERB,
                source=SkillSource.ROLE,
            )
            for m in find_mentions(vocab, gt.title)
        ]
        if not functional:
            for bullet in gt.bullets:
                refs.extend(_bullet_refs(vocab, bullet, gt.start, last, SkillSource.ROLE))
        roles.append(
            Role(
                title_raw=gt.title,
                seniority=gt.seniority,
                employer=gt.employer,
                industry_codes=(gt.industry,),
                start=None if functional else _month(gt.start),
                end=None if functional or gt.end is None else _month(gt.end),
                is_current=False if functional else gt.end is None,
                is_internship=gt.is_internship,
                skills=tuple(refs),
            )
        )

    projects: list[Project] = []
    if not functional:
        for gt in profile.projects:
            refs = [
                r for b in gt.bullets for r in _bullet_refs(vocab, b, gt.start, gt.end, SkillSource.PROJECT)
            ]
            projects.append(
                Project(name=gt.name, skills=tuple(refs), start=_month(gt.start), end=_month(gt.end))
            )

    if functional:
        blob = [
            SkillRef(
                canonical_id=sid,
                surface_form=spelling,
                evidence=Evidence.SKILLS_BLOB,
                source=SkillSource.SKILLS_SECTION,
            )
            for sid, spelling in functional_skills(profile, vocab)
        ]
    else:
        blob = []
        for label in profile.skills_section:
            sid = resolve_form(vocab, label)
            if sid is None:
                raise ValueError(
                    f"cannot resolve the skills line entry {label!r} to a skill of the vocabulary"
                )
            blob.append(
                SkillRef(
                    canonical_id=sid,
                    surface_form=label,
                    evidence=Evidence.SKILLS_BLOB,
                    source=SkillSource.SKILLS_SECTION,
                )
            )
    certifications = tuple(
        SkillRef(
            canonical_id=cid,
            surface_form=index.by_id[cid].label,
            evidence=Evidence.CERTIFICATION,
            source=SkillSource.CERTIFICATION,
        )
        for cid in profile.certifications
    )
    total = (
        None
        if functional
        else float(union_months([role_months(r.start, r.end, as_of) for r in profile.roles]))
    )
    return Resume(
        roles=tuple(roles),
        projects=tuple(projects),
        education=profile.education,
        certifications=certifications,
        skills_blob=tuple(blob),
        total_experience_months=total,
        locations=profile.locations,
        flags=(Flag.FUNCTIONAL_RESUME,) if functional else (),
    )
