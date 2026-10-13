"""Mutation suite (EVAL section 2.3): change exactly one thing, assert the predicted direction and size.

One case per row of the EVAL table, each on a constructed pair (the closed-form graph of
`eval.suites.closed_form`: every requirement an exact id, IDF fixed) whose arithmetic makes the
stated range exact, and every case also checks the engine against the closed form before and after
the change, so a pass means "the right amount for the right reason":

* remove one required skill: four REQUIRED skills of equal idf 0.8, so one fewer drops required
  coverage by 0.25 and raw by 0.35 x 0.25 = 0.0875 in [0.06, 0.12]. The base is a moderate
  candidate (raw 0.772): above 0.7875 the miss would also trip the critical-miss cap (idf 0.8 >
  0.7, D-43), and the drop would be the cap's, not the coverage's.
* remove two high-idf required skills: the two-miss cap engages, raw <= 0.55.
* shift all dates back 4 years: the last role ended 3 years ago (R = exp(-0.45) = 0.638) and moves
  to 7 years (R floor 0.55); the three coverage dimensions (weight 0.55) lose 0.55 x 0.088 = 0.048
  in [0.02, 0.06]. Months and levels do not change.
* "architected" -> "assisted": the bullet is classified by `score.evidence.analyze_bullet`; the
  requirement's match quality goes from 1.00 to 0.45 (the 0.45 / 1.00 multiplier ratio, §15.1).
* add 40 irrelevant skills (20 in bullets, 20 in the skills list): |delta| <= 0.01 (stuffing
  defense; the closed form says exactly 0: skills unrelated to the posting enter no vector).
* swap an IC3 title for M2 against an IC3 posting: the cross-track penalty (0.20) is visible as the
  seniority value and the SENIORITY_GAP fact's cross_track flag.
* blank all dates: experience abstains with DATES_MISSING (and seniority with SENIORITY_UNKNOWN),
  their weight redistributes (effective weights still sum to 1) and the raw score is the closed
  form's, not a silent zero.

Add a case for every bug fixed: this module is the regression suite and only ever grows.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from eval.suites import BULK
from eval.suites.closed_form import CF_AS_OF, NOISE_IDS, day, expected
from matcher.errors import Flag
from matcher.models import (
    DegreeLevel,
    DimensionName,
    Education,
    EducationRequirement,
    Evidence,
    JobPosting,
    JobRequirement,
    MatchResult,
    Necessity,
    Resume,
    Role,
    SeniorityLevel,
    SkillRef,
    SkillSource,
    Track,
    VerbClass,
)

REQUIRED_IDS = ("cf.req01", "cf.req02", "cf.req03", "cf.req04")  # idf 0.8 each
PREFERRED_ID = "cf.req11"
IMPLIED_ID = "cf.req12"
IC3 = SeniorityLevel(track=Track.IC, level=3)
IC2 = SeniorityLevel(track=Track.IC, level=2)
M2 = SeniorityLevel(track=Track.MGMT, level=2)
TOLERANCE = 0.001


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


def bullet(skill_id: str, verb: VerbClass = VerbClass.OWNERSHIP) -> SkillRef:
    return SkillRef(canonical_id=skill_id, surface_form=skill_id, evidence=Evidence.ROLE_BULLET,
                    verb_class=verb, source=SkillSource.ROLE)


def listed(skill_id: str) -> SkillRef:
    return SkillRef(canonical_id=skill_id, surface_form=skill_id, evidence=Evidence.SKILLS_BLOB,
                    source=SkillSource.SKILLS_SECTION)


def base_posting() -> JobPosting:
    requirements = [JobRequirement(skill_id=s, necessity=Necessity.REQUIRED, surface_form=s) for s in REQUIRED_IDS]
    requirements.append(JobRequirement(skill_id=PREFERRED_ID, necessity=Necessity.PREFERRED, surface_form="p"))
    requirements.append(JobRequirement(skill_id=IMPLIED_ID, necessity=Necessity.IMPLIED, surface_form="i"))
    return JobPosting(
        title_raw="Senior Software Engineer",
        seniority=IC3,
        requirements=tuple(requirements),
        min_years_total=2.0,
        education_requirement=EducationRequirement(min_level=DegreeLevel.BACHELOR),
        industry_codes=("5112",),
        requirement_token_count=len(requirements),
    )


def strong_resume(*, end: Optional[date] = None, years: int = 10, seniority: SeniorityLevel = IC3,
                  title: str = "Senior Software Engineer") -> Resume:
    """Every requirement in OWNERSHIP bullets of one role; current unless `end` is given."""
    last = end or CF_AS_OF
    role = Role(
        title_raw=title, seniority=seniority, employer="Example Co", industry_codes=("5112",),
        start=day(_years_before(last, years)), end=None if end is None else day(end), is_current=end is None,
        skills=tuple(bullet(s) for s in (*REQUIRED_IDS, PREFERRED_ID, IMPLIED_ID)),
    )
    return Resume(roles=(role,), education=(Education(level=DegreeLevel.BACHELOR),))


def moderate_resume() -> Resume:
    """raw 0.772: unrelated industry (0.15), two degrees short (0.20), one level below (0.72), the
    implied skill only listed (0.65); every REQUIRED and the PREFERRED skill in OWNERSHIP bullets."""
    role = Role(
        title_raw="Software Engineer", seniority=IC2, employer="Example Health", industry_codes=("6221",),
        start=day(_years_before(CF_AS_OF, 10)), is_current=True,
        skills=tuple(bullet(s) for s in (*REQUIRED_IDS, PREFERRED_ID)),
    )
    return Resume(roles=(role,), skills_blob=(listed(IMPLIED_ID),),
                  education=(Education(level=DegreeLevel.HIGH_SCHOOL),))


def without(resume: Resume, *skill_ids: str) -> Resume:
    """Every mention of `skill_ids` removed, wherever it is."""
    drop = set(skill_ids)

    def keep(refs: tuple[SkillRef, ...]) -> tuple[SkillRef, ...]:
        return tuple(r for r in refs if r.canonical_id not in drop)

    return resume.model_copy(update={
        "roles": tuple(r.model_copy(update={"skills": keep(r.skills)}) for r in resume.roles),
        "projects": tuple(p.model_copy(update={"skills": keep(p.skills)}) for p in resume.projects),
        "skills_blob": keep(resume.skills_blob),
        "certifications": keep(resume.certifications),
    })


def shift_dates(resume: Resume, years: int, as_of: date) -> Resume:
    """Every date moved `years` back; a current role becomes one that ended `years` before as_of."""
    def moved(value):
        return None if value is None else day(_years_before(value.value, years))

    roles = []
    for role in resume.roles:
        end = day(_years_before(as_of, years)) if role.is_current else moved(role.end)
        roles.append(role.model_copy(update={"start": moved(role.start), "end": end, "is_current": False}))
    return resume.model_copy(update={"roles": tuple(roles)})


def blank_dates(resume: Resume) -> Resume:
    return resume.model_copy(update={"roles": tuple(
        r.model_copy(update={"start": None, "end": None, "is_current": False}) for r in resume.roles)})


@dataclass
class MutationResult:
    name: str
    passed: bool
    before: Optional[float]
    after: Optional[float]
    expectation: str
    problems: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def delta(self) -> Optional[float]:
        if self.before is None or self.after is None:
            return None
        return self.after - self.before


@dataclass(frozen=True)
class MutationCase:
    name: str
    row: str  # the EVAL section 2.3 row
    build: Callable[[], tuple[Resume, Resume, JobPosting]]  # (before, after, posting)
    expect: Callable[[MatchResult, MatchResult, list[str]], None]  # appends problems
    expectation: str
    as_of: date = CF_AS_OF

    def run(self, engine: Any) -> MutationResult:
        before_resume, after_resume, posting = self.build()
        before = engine.score(before_resume, posting, as_of=self.as_of, options=BULK)
        after = engine.score(after_resume, posting, as_of=self.as_of, options=BULK)
        problems: list[str] = []
        for label, resume, result in (("before", before_resume, before), ("after", after_resume, after)):
            want = expected(resume, posting, engine.graph, engine.config, self.as_of).raw
            got = result.raw_score
            agree = (want is None and got is None) or (
                want is not None and got is not None and math.isclose(got, want, rel_tol=0.0, abs_tol=TOLERANCE))
            if not agree:
                problems.append(f"{label}: engine raw {got} differs from the closed form {want}")
        self.expect(before, after, problems)
        return MutationResult(
            name=self.name, passed=not problems, before=before.raw_score, after=after.raw_score,
            expectation=self.expectation, problems=problems,
            detail={
                "row": self.row,
                "posting": posting.model_dump(mode="json"),
                "resume_before": before_resume.model_dump(mode="json"),
                "resume_after": after_resume.model_dump(mode="json"),
                "as_of": self.as_of.isoformat(),
                "dimensions_before": {k: d.value for k, d in before.dimensions.items()},
                "dimensions_after": {k: d.value for k, d in after.dimensions.items()},
            },
        )


def _delta(before: MatchResult, after: MatchResult, problems: list[str]) -> Optional[float]:
    if before.raw_score is None or after.raw_score is None:
        problems.append(f"no raw score (before {before.outcome.value}, after {after.outcome.value})")
        return None
    return after.raw_score - before.raw_score


def _drop_between(low: float, high: float):
    def expect(before: MatchResult, after: MatchResult, problems: list[str]) -> None:
        delta = _delta(before, after, problems)
        if delta is not None and not (low <= -delta <= high):
            problems.append(f"raw dropped by {-delta:.4f}, expected {low}..{high}")
    return expect


def _remove_one() -> tuple[Resume, Resume, JobPosting]:
    base = moderate_resume()
    return base, without(base, REQUIRED_IDS[-1]), base_posting()


def _remove_two() -> tuple[Resume, Resume, JobPosting]:
    base = strong_resume()
    return base, without(base, *REQUIRED_IDS[:2]), base_posting()


def _expect_cap(before: MatchResult, after: MatchResult, problems: list[str]) -> None:
    if after.raw_score is None or after.raw_score > 0.55 + 1e-12:
        problems.append(f"raw {after.raw_score} is above the two-miss cap 0.55")
    if not any(cap.reason == Flag.CRITICAL_MISS and math.isclose(cap.cap, 0.55) for cap in after.caps):
        problems.append(f"no CRITICAL_MISS cap at 0.55 recorded (caps: {after.caps})")


def _shift() -> tuple[Resume, Resume, JobPosting]:
    base = strong_resume(end=_years_before(CF_AS_OF, 3))
    return base, shift_dates(base, 4, CF_AS_OF), base_posting()


ARCHITECTED = "Architected a billing service in Kafka"
ASSISTED = "Assisted a billing service in Kafka"


def _verb(text: str) -> VerbClass:
    from matcher.config import load_config
    from matcher.score.evidence import BulletMention, analyze_bullet

    start = text.index("Kafka")
    (found,) = analyze_bullet(text, [BulletMention(canonical_id=REQUIRED_IDS[0], start=start, end=start + 5)],
                              load_config())
    return VerbClass(found.verb_class)


def _verb_swap() -> tuple[Resume, Resume, JobPosting]:
    def with_verb(verb: VerbClass) -> Resume:
        base = strong_resume()
        role = base.roles[0]
        skills = tuple(bullet(r.canonical_id, verb) if r.canonical_id == REQUIRED_IDS[0] else r for r in role.skills)
        return base.model_copy(update={"roles": (role.model_copy(update={"skills": skills}),)})

    return with_verb(_verb(ARCHITECTED)), with_verb(_verb(ASSISTED)), base_posting()


def _requirement_m(result: MatchResult, skill_id: str) -> Optional[float]:
    dimension = result.dimensions.get(DimensionName.REQUIRED_SKILLS.value)
    for fact in dimension.facts if dimension else ():
        if fact.subject == skill_id and fact.code != Flag.CRITICAL_MISS.value:
            return fact.value
    return None


def _expect_ratio(before: MatchResult, after: MatchResult, problems: list[str]) -> None:
    if _verb(ARCHITECTED) is not VerbClass.OWNERSHIP or _verb(ASSISTED) is not VerbClass.ASSISTANCE:
        problems.append(f"classifier: architected -> {_verb(ARCHITECTED)}, assisted -> {_verb(ASSISTED)}")
    m_before, m_after = _requirement_m(before, REQUIRED_IDS[0]), _requirement_m(after, REQUIRED_IDS[0])
    if not m_before or m_after is None or not math.isclose(m_after / m_before, 0.45 / 1.00, abs_tol=1e-9):
        problems.append(f"match quality {m_before} -> {m_after}, expected the ratio 0.45")
    delta = _delta(before, after, problems)
    if delta is not None and delta >= 0.0:
        problems.append(f"raw did not drop ({delta:+.4f})")


def _stuff() -> tuple[Resume, Resume, JobPosting]:
    base = moderate_resume()
    role = base.roles[0]
    noise_bullets = tuple(bullet(s) for s in NOISE_IDS[:20])
    noise_listed = tuple(listed(s) for s in NOISE_IDS[20:40])
    stuffed = base.model_copy(update={
        "roles": (role.model_copy(update={"skills": role.skills + noise_bullets}),),
        "skills_blob": base.skills_blob + noise_listed,
    })
    return base, stuffed, base_posting()


def _expect_small(before: MatchResult, after: MatchResult, problems: list[str]) -> None:
    delta = _delta(before, after, problems)
    if delta is not None and abs(delta) > 0.01:
        problems.append(f"|delta| = {abs(delta):.4f} > 0.01")


def _title_swap() -> tuple[Resume, Resume, JobPosting]:
    return (strong_resume(), strong_resume(seniority=M2, title="Engineering Manager"), base_posting())


def _expect_cross_track(before: MatchResult, after: MatchResult, problems: list[str]) -> None:
    name = DimensionName.SENIORITY.value
    b, a = before.dimensions[name], after.dimensions[name]
    if b.value != 1.0 or a.value is None or not math.isclose(a.value, 1.0 - 0.20, abs_tol=1e-9):
        problems.append(f"seniority {b.value} -> {a.value}, expected 1.0 -> 0.80 (cross-track penalty 0.20)")
    gap = [f for f in a.facts if f.code == "SENIORITY_GAP"]
    if not gap or gap[0].detail.get("cross_track") is not True:
        problems.append("SENIORITY_GAP fact does not show cross_track")
    delta = _delta(before, after, problems)
    if delta is not None and delta >= 0.0:
        problems.append(f"raw did not drop ({delta:+.4f})")


def _blank() -> tuple[Resume, Resume, JobPosting]:
    base = strong_resume()
    return base, blank_dates(base), base_posting()


def _expect_abstain(before: MatchResult, after: MatchResult, problems: list[str]) -> None:
    experience = after.dimensions[DimensionName.EXPERIENCE.value]
    if not experience.abstained or experience.abstain_reason != Flag.DATES_MISSING:
        problems.append(f"experience did not abstain with DATES_MISSING ({experience.abstain_reason})")
    weights = math.fsum(d.effective_weight for d in after.dimensions.values())
    if not math.isclose(weights, 1.0, abs_tol=1e-9):
        problems.append(f"effective weights sum to {weights}, the abstained weight did not redistribute")
    if after.raw_score is None or after.raw_score <= 0.0:
        problems.append(f"raw {after.raw_score}: a silent zero (or nothing) instead of a redistributed score")


MUTATIONS: tuple[MutationCase, ...] = (
    MutationCase("remove_one_required", "remove one required skill (idf 0.8) -> score drops 0.06-0.12",
                 _remove_one, _drop_between(0.06, 0.12), "drop in [0.06, 0.12]"),
    MutationCase("remove_two_high_idf_required",
                 "remove two high-idf required skills -> critical-miss cap engages, score <= 0.55",
                 _remove_two, _expect_cap, "raw <= 0.55 with the cap recorded"),
    MutationCase("shift_dates_back_4_years", "shift all dates back 4 years -> drops via recency decay, 0.02-0.06",
                 _shift, _drop_between(0.02, 0.06), "drop in [0.02, 0.06]"),
    MutationCase("architected_to_assisted", "change \"architected\" -> \"assisted\" -> multiplier ratio 0.45/1.00",
                 _verb_swap, _expect_ratio, "m ratio 0.45, raw drops"),
    MutationCase("add_40_irrelevant_skills", "add 40 irrelevant skills -> |delta score| <= 0.01",
                 _stuff, _expect_small, "|delta| <= 0.01"),
    MutationCase("swap_ic3_title_for_m2", "swap IC3 title for M2 title -> cross-track penalty visible",
                 _title_swap, _expect_cross_track, "seniority 1.00 -> 0.80, cross_track shown"),
    MutationCase("blank_all_dates", "blank all dates -> experience abstains, weight redistributes, no silent zero",
                 _blank, _expect_abstain, "DATES_MISSING, weights sum to 1, raw > 0"),
)


def run_mutations(engine: Any) -> list[MutationResult]:
    return [case.run(engine) for case in MUTATIONS]
