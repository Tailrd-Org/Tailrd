"""Global properties of the score (SPEC §10 property tests, §4, §13), over random valid models.

* Monotonicity: adding a mention of a skill the posting asks for never lowers the raw score,
  wherever on the résumé it is added (skills list, a role's bullets, a project, the certifications).
* Monotonicity: removing every mention of a résumé skill a requirement names never raises it.
* Boundedness: the raw score always lies in [0, 1].
* Gate precedence: a GATE_FAIL never carries a score (raw, calibrated or displayed).

The pairs come from `tests/strategies.py` (graph_small ids plus ids the graph does not hold).
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from matcher.calibrate import FixedCalibrator
from matcher.config import load_config
from matcher.graph.loader import load_graph
from matcher.models import (
    Evidence,
    JobPosting,
    MatchResult,
    Outcome,
    Polarity,
    Resume,
    ScoreOptions,
    SkillRef,
    SkillSource,
    VerbClass,
)
from matcher.score.engine import score
from tests.fixtures.build_graph_small import FIXTURE_DIR
from tests.strategies import AS_OF, maybe_days, postings, resumes

CONFIG = load_config()
GRAPH = load_graph(FIXTURE_DIR)
FIXED = FixedCalibrator()
EPS = 1e-12  # float noise only: a property that holds in exact arithmetic
EXAMPLES = int(os.environ.get("PROPERTY_EXAMPLES", "500"))
PROPERTY = settings(max_examples=EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
PLACES = ("blob", "role", "project", "certification")


def run(resume: Resume, posting: JobPosting) -> MatchResult:
    return score(resume, posting, as_of=AS_OF, config=CONFIG, graph=GRAPH, calibration=FIXED,
                 options=ScoreOptions(strict=True))


def _with_mention(resume: Resume, ref: SkillRef, place: str, index: int) -> Resume:
    """`resume` with `ref` added at `place` (a role or project picked by `index`; the skills list
    when there is none)."""
    if place == "role" and resume.roles:
        roles = list(resume.roles)
        at = index % len(roles)
        roles[at] = roles[at].model_copy(update={"skills": (*roles[at].skills, ref)})
        return resume.model_copy(update={"roles": tuple(roles)})
    if place == "project" and resume.projects:
        projects = list(resume.projects)
        at = index % len(projects)
        projects[at] = projects[at].model_copy(update={"skills": (*projects[at].skills, ref)})
        return resume.model_copy(update={"projects": tuple(projects)})
    if place == "certification":
        return resume.model_copy(update={"certifications": (*resume.certifications, ref)})
    return resume.model_copy(update={"skills_blob": (*resume.skills_blob, ref)})


def _without_skill(resume: Resume, skill_id: str) -> Resume:
    def keep(refs: tuple[SkillRef, ...]) -> tuple[SkillRef, ...]:
        return tuple(ref for ref in refs if ref.canonical_id != skill_id)

    return resume.model_copy(update={
        "roles": tuple(role.model_copy(update={"skills": keep(role.skills)}) for role in resume.roles),
        "projects": tuple(project.model_copy(update={"skills": keep(project.skills)}) for project in resume.projects),
        "skills_blob": keep(resume.skills_blob),
        "certifications": keep(resume.certifications),
    })


@st.composite
def added_mentions(draw: Any, posting: JobPosting) -> tuple[SkillRef, str, int]:
    skill_id = draw(st.sampled_from(sorted({requirement.skill_id for requirement in posting.requirements})))
    first, last = draw(maybe_days), draw(maybe_days)
    if first is not None and last is not None and first > last:
        first, last = last, first
    ref = SkillRef(
        canonical_id=skill_id,
        evidence=draw(st.sampled_from(list(Evidence))),
        verb_class=draw(st.sampled_from(list(VerbClass))),
        polarity=Polarity.AFFIRMED,
        source=draw(st.sampled_from(list(SkillSource))),
        first_used=first,
        last_used=last,
    )
    return ref, draw(st.sampled_from(PLACES)), draw(st.integers(0, 10))


def _raw(result: MatchResult) -> float | None:
    return result.raw_score


@PROPERTY
@given(data=st.data(), resume=resumes(review_flags=False),
       posting=postings(review_flags=False, min_requirements=1))
def test_adding_a_matching_skill_never_lowers_the_score(data, resume, posting) -> None:
    ref, place, index = data.draw(added_mentions(posting))
    before = run(resume, posting)
    after = run(_with_mention(resume, ref, place, index), posting)
    if before.raw_score is not None and after.raw_score is not None:
        assert after.raw_score >= before.raw_score - EPS, (place, ref.canonical_id, before.raw_score, after.raw_score)


@PROPERTY
@given(data=st.data(), resume=resumes(review_flags=False),
       posting=postings(review_flags=False, min_requirements=1))
def test_removing_a_required_skill_never_raises_the_score(data, resume, posting) -> None:
    wanted = {requirement.skill_id for requirement in posting.requirements}
    held = sorted(wanted & {ref.canonical_id for ref in (
        *(ref for role in resume.roles for ref in role.skills),
        *(ref for project in resume.projects for ref in project.skills),
        *resume.skills_blob, *resume.certifications)})
    if not held:
        return
    skill_id = data.draw(st.sampled_from(held))
    before = run(resume, posting)
    after = run(_without_skill(resume, skill_id), posting)
    if before.raw_score is not None and after.raw_score is not None:
        assert after.raw_score <= before.raw_score + EPS, (skill_id, before.raw_score, after.raw_score)


def _cosine_case(*, python_in_role: bool) -> tuple[Resume, JobPosting]:
    """A role that touches seven requirements only through their ancestor javascript (parent credit
    0.40 each), on a 38-requirement posting; python is already shown by a project (0.85)."""
    from datetime import timedelta

    from matcher.models import DateValue, JobRequirement, Necessity, Project, Role

    js = ("skill.react", "skill.vue", "skill.angular", "skill.nextjs", "skill.typescript", "skill.nodejs",
          "skill.redux")
    ids = (*js, "skill.python", *(f"skill.filler_{i:02d}" for i in range(30)))
    posting = JobPosting(requirements=tuple(JobRequirement(skill_id=i, necessity=Necessity.REQUIRED) for i in ids),
                         min_years_total=5.0)
    skills = [SkillRef(canonical_id="skill.javascript", evidence=Evidence.ROLE_BULLET, verb_class=VerbClass.OWNERSHIP)]
    if python_in_role:
        skills.append(SkillRef(canonical_id="skill.python", evidence=Evidence.ROLE_BULLET,
                               verb_class=VerbClass.OWNERSHIP))
    role = Role(start=DateValue.exact(AS_OF - timedelta(days=1826)), is_current=True, skills=tuple(skills))
    project = Project(skills=(SkillRef(canonical_id="skill.python", evidence=Evidence.ROLE_BULLET,
                                       verb_class=VerbClass.EXECUTION),), end=DateValue.exact(AS_OF))
    return Resume(roles=(role,), projects=(project,)), posting


@pytest.mark.xfail(strict=True, reason=(
    "SPEC §10 monotonicity vs §5.3 / DECISIONS D-35: a cosine is not monotone in one component. "
    "Adding a low-IDF exact match to a role whose other matches are weak (parent 0.40) turns the role "
    "vector away from the posting's, so relevance, relevant months and §5.3 drop by more than §5.1 "
    "gains (raw 0.356657 -> 0.356106). Needs a decision on the relevance formula; see the Task 3.6 report."))
def test_adding_a_matching_skill_to_a_role_never_lowers_the_score_cosine_case() -> None:
    before = run(*_cosine_case(python_in_role=False))
    after = run(*_cosine_case(python_in_role=True))
    assert after.dimensions["required_skills"].value > before.dimensions["required_skills"].value  # §5.1 gains
    assert after.raw_score >= before.raw_score - EPS


@PROPERTY
@given(resume=resumes(), posting=postings())
def test_the_raw_score_is_bounded_and_gate_fail_has_none(resume, posting) -> None:
    result = run(resume, posting)
    if result.raw_score is not None:
        assert 0.0 <= result.raw_score <= 1.0
    if result.outcome is Outcome.GATE_FAIL:
        assert result.raw_score is None and result.calibrated_score is None and result.display_score is None
        assert result.band is None and result.dimensions == {}
