"""Realistic résumé / posting pairs over tests/fixtures/graph_small, one or more per outcome, for the
explanation tests (Task 4.1). Each builder returns (resume, posting); `PAIRS` names them with the
outcome the engine gives them under `FixedCalibrator` (round(100 × raw)).
"""

from __future__ import annotations

from datetime import date

from matcher.errors import Flag
from matcher.models import (
    DegreeLevel,
    Education,
    EducationRequirement,
    JobPosting,
    JobRequirement,
    Necessity,
    Outcome,
    ParseTier,
    Resume,
    Role,
    SeniorityLevel,
    Track,
    WorkAuth,
    WorkAuthGate,
)
from tests.builders import bullet, listed, on

IC1 = SeniorityLevel(track=Track.IC, level=1)
IC2 = SeniorityLevel(track=Track.IC, level=2)
IC3 = SeniorityLevel(track=Track.IC, level=3)


def reqs(ids, necessity: Necessity = Necessity.REQUIRED) -> tuple[JobRequirement, ...]:
    return tuple(JobRequirement(skill_id=skill_id, necessity=necessity) for skill_id in ids)


def backend_posting(**changes) -> JobPosting:
    """A junior backend role: five required skills, two preferred, two keywords, IC1, one year."""
    fields = dict(
        title_raw="Junior Backend Developer",
        seniority=IC1,
        min_years_total=1.0,
        industry_codes=("5415",),
        requirements=(reqs(("skill.python", "skill.sql", "skill.react", "skill.terraform", "skill.docker"))
                      + reqs(("skill.typescript", "skill.kubernetes"), Necessity.PREFERRED)
                      + reqs(("skill.git", "skill.agile"), Necessity.IMPLIED)),
        requirement_token_count=9,
    )
    fields.update(changes)
    return JobPosting(**fields)


def new_grad_resume(**changes) -> Resume:
    """A new grad in a current IC1 role since 2024-01-15: four skills shown in bullets, AWS and
    Docker listed in the skills section."""
    role = Role(title_raw="Software Developer", seniority=IC1, industry_codes=("541511",),
                start=on(date(2024, 1, 15)), is_current=True,
                skills=tuple(bullet(s) for s in ("skill.python", "skill.sql", "skill.react", "skill.git")))
    fields = dict(roles=(role,), skills_blob=listed("skill.aws", "skill.docker"))
    fields.update(changes)
    return Resume(**fields)


def good_match():
    return new_grad_resume(), backend_posting()


def strong_match():
    """Everything the posting asks for, shown in bullets."""
    ids = ("skill.python", "skill.sql", "skill.react", "skill.terraform", "skill.docker",
           "skill.typescript", "skill.kubernetes", "skill.git", "skill.agile")
    role = Role(seniority=IC1, industry_codes=("541511",), start=on(date(2023, 1, 15)), is_current=True,
                skills=tuple(bullet(s) for s in ids))
    return Resume(roles=(role,)), backend_posting()


def critical_miss_binding():
    """A data role that centres on Terraform and Go: both missing, the rest strong, so the
    two-miss cap (0.55) binds."""
    posting = JobPosting(
        seniority=IC1, min_years_total=1.0, industry_codes=("5415",),
        requirements=reqs(("skill.terraform", "skill.go", "skill.python", "skill.sql", "skill.git")),
        requirement_token_count=5,
    )
    role = Role(seniority=IC1, industry_codes=("541511",), start=on(date(2023, 1, 15)), is_current=True,
                skills=tuple(bullet(s) for s in ("skill.python", "skill.sql", "skill.git")))
    return Resume(roles=(role,)), posting


def low_confidence():
    """A scanned résumé (parse tier C) against a posting with two requirements."""
    posting = JobPosting(seniority=IC1, requirements=reqs(("skill.python", "skill.excel")),
                         requirement_token_count=2)
    role = Role(seniority=IC1, start=on(date(2024, 5, 1)), is_current=True,
                skills=(bullet("skill.python"),))
    return Resume(roles=(role,), parse_tier=ParseTier.C), posting


def needs_review_flag():
    return Resume(flags=(Flag.NOT_A_RESUME,)), backend_posting()


def gate_fail_sponsorship():
    """The candidate needs sponsorship; the posting offers none in Canada."""
    resume = new_grad_resume(work_auth=WorkAuth(needs_sponsorship=True))
    posting = backend_posting(gates=(WorkAuthGate(countries=("CA",), sponsorship_available=False),))
    return resume, posting


def undated_student():
    """Roles without dates: experience abstains (DATES_MISSING); a degree below the requirement."""
    role = Role(title_raw="Research Assistant", skills=(bullet("skill.python"), bullet("skill.pandas")))
    resume = Resume(roles=(role,), education=(Education(level=DegreeLevel.ASSOCIATE),),
                    skills_blob=listed("skill.sql", "skill.machine_learning"))
    posting = JobPosting(
        seniority=IC1, min_years_total=2.0,
        education_requirement=EducationRequirement(min_level=DegreeLevel.BACHELOR),
        requirements=(reqs(("skill.python", "skill.sql", "skill.machine_learning", "skill.scikit_learn"))
                      + reqs(("skill.r",), Necessity.PREFERRED)),
        requirement_token_count=5,
    )
    return resume, posting


def overqualified():
    """A senior candidate (IC3) for an IC1 role: noted, never a gap."""
    role = Role(seniority=IC3, industry_codes=("541511",), start=on(date(2018, 1, 15)), is_current=True,
                skills=tuple(bullet(s) for s in ("skill.python", "skill.sql", "skill.react", "skill.docker")))
    return Resume(roles=(role,)), backend_posting()


def student_projects_only():
    """No roles, skills in a project and the skills section: NO_WORK_EXPERIENCE, typical years."""
    from matcher.models import Project

    resume = Resume(projects=(Project(name="Capstone", skills=(bullet("skill.react"), bullet("skill.nodejs"))),),
                    skills_blob=listed("skill.javascript", "skill.git"))
    posting = JobPosting(seniority=IC1, requirements=reqs(("skill.react", "skill.nodejs", "skill.typescript")),
                         requirement_token_count=3)
    return resume, posting


def stretch_role():
    """A junior (IC1, bachelor's) applying up to an IC3 role asking five years and a master's."""
    role = Role(seniority=IC1, industry_codes=("541511",), start=on(date(2023, 6, 1)), is_current=True,
                skills=tuple(bullet(s) for s in ("skill.python", "skill.sql", "skill.docker", "skill.aws")))
    resume = Resume(roles=(role,), education=(Education(level=DegreeLevel.BACHELOR),))
    posting = backend_posting(
        seniority=IC3, min_years_total=5.0,
        education_requirement=EducationRequirement(min_level=DegreeLevel.MASTER),
        requirements=(reqs(("skill.python", "skill.sql", "skill.docker", "skill.aws", "skill.kubernetes"))
                      + reqs(("skill.terraform", "skill.go"), Necessity.PREFERRED)),
        requirement_token_count=7,
    )
    return resume, posting


PAIRS = {
    "stretch_role": stretch_role,
    "good_match": good_match,
    "strong_match": strong_match,
    "critical_miss_binding": critical_miss_binding,
    "low_confidence": low_confidence,
    "needs_review_flag": needs_review_flag,
    "gate_fail_sponsorship": gate_fail_sponsorship,
    "undated_student": undated_student,
    "overqualified": overqualified,
    "student_projects_only": student_projects_only,
}

EXPECTED_OUTCOME = {
    "good_match": Outcome.SCORED,
    "strong_match": Outcome.SCORED,
    "needs_review_flag": Outcome.NEEDS_REVIEW,
    "gate_fail_sponsorship": Outcome.GATE_FAIL,
}
