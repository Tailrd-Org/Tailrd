"""Every result gets a full, honest explanation (SPEC §8, §13, §17.2, §18; BUILD Milestone 4).

* Every outcome has an explanation with a headline.
* SCORED: the score, the band, the basis, and at least one strength or gap; every REQUIRED group
  is named in a strength (counted in "k of n required skills") or a gap.
* SCORED_LOW_CONFIDENCE: the band, no score, at least one specific note (a reason's sentence).
* GATE_FAIL: the failing gate's sentence, no score. NEEDS_REVIEW: each reason's sentence, no score.
* No percent sign after a number (SPEC §7 naming), no em dash anywhere, and the audit trace still
  accepts the result.

Run over the hand-built explanation pairs (tests/explain_pairs.py), the Task 3.6 fixture pairs
(tests/fixtures/profiles/*.json, when present) and Hypothesis-random valid pairs over graph_small.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from matcher.audit.trace import build_trace
from matcher.calibrate import FixedCalibrator
from matcher.errors import FLAG_TEXT, NEEDS_REVIEW_REASONS, Flag
from matcher.explain.breakdown import NEEDS_REVIEW_HEADLINE
from matcher.explain.labels import skill_label
from matcher.models import (
    DateValue,
    DegreeLevel,
    Education,
    EducationRequirement,
    Evidence,
    JobPosting,
    JobRequirement,
    MatchResult,
    Necessity,
    Outcome,
    ParseTier,
    Polarity,
    Resume,
    Role,
    ScoreOptions,
    SeniorityLevel,
    SkillRef,
    Track,
    VerbClass,
    WorkAuth,
    WorkAuthGate,
)
from matcher.score.engine import score
from tests.builders import AS_OF
from tests.explain_pairs import EXPECTED_OUTCOME, PAIRS

FIXED = FixedCalibrator()
PROFILES = sorted((Path(__file__).resolve().parent / "fixtures" / "profiles").glob("*.json"))
_PERCENT_AFTER_NUMBER = re.compile(r"\d\s*%")


def _texts(result: MatchResult) -> list[str]:
    e = result.explanation
    items = (*e.strengths, *e.gaps)
    return [e.headline, e.basis, *e.notes, *e.to_improve, *(i.label for i in items), *(i.note for i in items)]


def check_complete(result: MatchResult, graph) -> None:
    """The completeness contract (see the module docstring)."""
    e = result.explanation
    assert e is not None and e.headline
    for text in _texts(result):
        assert chr(0x2014) not in text, text  # no em dash (house style)
        assert not _PERCENT_AFTER_NUMBER.search(text), text
    build_trace(result)
    outcome = result.outcome
    if outcome is Outcome.SCORED:
        assert e.score == result.display_score is not None and e.band == result.band is not None
        assert e.basis and (e.strengths or e.gaps)
        assert e.headline.endswith(result.band.value)
        if result.interval is not None and result.interval.display:
            assert f"{result.interval.low}–{result.interval.high}" in e.headline
        else:
            assert f"Fit score {e.score} " in e.headline
        _check_required_named(result, graph)
    elif outcome is Outcome.SCORED_LOW_CONFIDENCE:
        assert e.score is None and e.band == result.band is not None and e.headline == result.band.value
        assert any(FLAG_TEXT[reason] in e.notes for reason in result.reasons)
        _check_required_named(result, graph)
    elif outcome is Outcome.GATE_FAIL:
        assert e.score is None and e.band is None and e.basis == ""
        assert e.headline in {"Not eligible: " + FLAG_TEXT[reason] for reason in result.reasons}
    else:
        assert outcome is Outcome.NEEDS_REVIEW
        assert e.score is None and e.band is None and e.headline == NEEDS_REVIEW_HEADLINE
        assert all(FLAG_TEXT[reason] in e.notes for reason in result.reasons)


def _check_required_named(result: MatchResult, graph) -> None:
    required = result.dimensions.get("required_skills")
    if required is None or required.abstained:
        return
    groups = [fact for fact in required.facts if fact.code in {"REQ_MATCH", "REQ_PARTIAL", "REQ_MISS"}]
    met = sum(1 for fact in groups if fact.code == "REQ_MATCH")
    e = result.explanation
    line = [item for item in e.strengths if item.code == "REQUIRED_SKILLS"]
    gaps = [item for item in e.gaps if item.label.endswith("(required)")]
    if met:
        assert [item.label for item in line] == [f"{met} of {len(groups)} required skills"]
    else:
        assert line == []
    assert met + len(gaps) == len(groups)
    for fact in groups:
        if fact.code != "REQ_MATCH":
            assert any(skill_label(graph, fact.subject) in item.label for item in gaps), fact.subject


def _score(resume, posting, graph, config, as_of=AS_OF, **options) -> MatchResult:
    return score(resume, posting, as_of=as_of, config=config, graph=graph, calibration=FIXED,
                 options=ScoreOptions(strict=True, **options))


# ─── Hand-built pairs ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", sorted(PAIRS))
def test_every_pair_is_explained(name: str, small_graph, config) -> None:
    resume, posting = PAIRS[name]()
    result = _score(resume, posting, small_graph, config, with_counterfactual=True, with_interval=True)
    if name in EXPECTED_OUTCOME:
        assert result.outcome is EXPECTED_OUTCOME[name]
    check_complete(result, small_graph)


def test_the_hand_built_pairs_cover_every_outcome(small_graph, config) -> None:
    seen = {_score(*build(), small_graph, config).outcome for build in PAIRS.values()}
    assert seen == set(Outcome)


@pytest.mark.parametrize("path", PROFILES, ids=[p.stem for p in PROFILES])
def test_every_fixture_pair_is_explained(path: Path, small_graph, config) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = _score(data["resume"], data["posting"], small_graph, config,
                    as_of=date.fromisoformat(data["as_of"]), with_counterfactual=True)
    check_complete(result, small_graph)


def test_unreadable_input_and_broken_options_are_explained(small_graph, config) -> None:
    junk = score(b"\xff", "[]", as_of=AS_OF, config=config, graph=small_graph, calibration=FIXED)
    check_complete(junk, small_graph)
    resume, posting = PAIRS["good_match"]()
    broken = score(resume, posting, as_of=AS_OF, config=config, graph=small_graph, calibration=FIXED,
                   options="not options")
    assert broken.reasons == (Flag.INTERNAL_ERROR,)
    check_complete(broken, small_graph)
    early = score(resume, posting, as_of=date(1990, 1, 1), config=config, graph=small_graph, calibration=FIXED)
    assert early.reasons == (Flag.INVALID_AS_OF,)
    check_complete(early, small_graph)


def test_explanation_can_be_turned_off(small_graph, config) -> None:
    resume, posting = PAIRS["good_match"]()
    result = _score(resume, posting, small_graph, config, with_explanation=False)
    assert result.explanation is None and result.improvements == ()


# ─── Random valid pairs ─────────────────────────────────────────────────────────────────────

SKILLS = ("skill.python", "skill.sql", "skill.react", "skill.vue", "skill.nextjs", "skill.javascript",
          "skill.typescript", "skill.nodejs", "skill.aws", "skill.kubernetes", "skill.docker",
          "skill.terraform", "skill.go", "skill.java", "skill.git", "skill.agile", "skill.scrum",
          "skill.excel", "skill.machine_learning", "skill.pandas", "skill.communication",
          "cred.aws_saa", "skill.not_in_graph")
LEVELS = st.one_of(st.none(), st.builds(SeniorityLevel, track=st.just(Track.IC), level=st.integers(0, 6)))


@st.composite
def mentions(draw, max_size: int = 6) -> tuple[SkillRef, ...]:
    ids = draw(st.lists(st.sampled_from(SKILLS), max_size=max_size, unique=True))
    return tuple(
        SkillRef(canonical_id=skill_id, evidence=draw(st.sampled_from(list(Evidence))),
                 verb_class=draw(st.sampled_from(list(VerbClass))),
                 polarity=draw(st.sampled_from([Polarity.AFFIRMED] * 6 + list(Polarity))))
        for skill_id in ids
    )


@st.composite
def roles(draw) -> Role:
    start = AS_OF - timedelta(days=draw(st.integers(30, 3650)))
    kind = draw(st.sampled_from(("current", "ended", "undated")))
    dates = {}
    if kind == "current":
        dates = dict(start=DateValue.exact(start), is_current=True)
    elif kind == "ended":
        end = start + timedelta(days=draw(st.integers(1, 1000)))
        dates = dict(start=DateValue.exact(start), end=DateValue.exact(min(end, AS_OF)))
    return Role(seniority=draw(LEVELS), industry_codes=draw(st.sampled_from(((), ("541511",), ("6221",)))),
                is_internship=draw(st.booleans()), skills=draw(mentions()), **dates)


@st.composite
def resumes(draw) -> Resume:
    return Resume(
        roles=tuple(draw(st.lists(roles(), max_size=3))),
        skills_blob=draw(mentions(8)),
        certifications=draw(st.sampled_from(((), (SkillRef(canonical_id="cred.aws_saa", evidence=Evidence.CERTIFICATION),)))),
        education=draw(st.sampled_from(((), (Education(level=DegreeLevel.BACHELOR),), (Education(level=DegreeLevel.ASSOCIATE),)))),
        parse_tier=draw(st.sampled_from(list(ParseTier))),
        work_auth=draw(st.sampled_from((None, WorkAuth(needs_sponsorship=True), WorkAuth(authorized_countries=("CA",))))),
        flags=draw(st.sampled_from(((), (), (), (Flag.OCR_USED,), (Flag.NOT_A_RESUME,)))),
    )


@st.composite
def postings(draw) -> JobPosting:
    ids = draw(st.lists(st.sampled_from(SKILLS), min_size=0, max_size=8, unique=True))
    requirements = tuple(
        JobRequirement(skill_id=skill_id, necessity=draw(st.sampled_from(list(Necessity))),
                       or_group=draw(st.sampled_from((None, None, None, "g1"))))
        for skill_id in ids
    )
    return JobPosting(
        seniority=draw(LEVELS),
        min_years_total=draw(st.one_of(st.none(), st.floats(0.0, 8.0))),
        industry_codes=draw(st.sampled_from(((), ("5415",)))),
        education_requirement=draw(st.sampled_from((None, EducationRequirement(min_level=DegreeLevel.BACHELOR)))),
        requirements=requirements,
        requirement_token_count=len(requirements) + draw(st.integers(0, 3)),
        unresolved_requirement_tokens=draw(st.sampled_from(((), ("kafka streams",)))),
        gates=draw(st.sampled_from(((), (WorkAuthGate(countries=("CA",), sponsorship_available=False),)))),
    )


@settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(resumes(), postings(), st.booleans())
def test_random_valid_pairs_are_always_explained(small_graph, config, resume, posting, interval) -> None:
    result = _score(resume, posting, small_graph, config, with_interval=interval)
    check_complete(result, small_graph)
    if result.outcome is Outcome.NEEDS_REVIEW:
        assert set(result.reasons) & NEEDS_REVIEW_REASONS


@settings(max_examples=25, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(resumes(), postings())
def test_random_valid_pairs_with_suggestions(small_graph, config, resume, posting) -> None:
    result = _score(resume, posting, small_graph, config, with_counterfactual=True)
    check_complete(result, small_graph)
    if result.outcome is not Outcome.SCORED:
        assert result.improvements == () and result.explanation.to_improve == ()
    for improvement in result.improvements:
        assert improvement.new_score > result.calibrated_score
