"""SPEC §2 contract: frozen, validated, JSON round-trippable models (BUILD Milestone 1 gate)."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import BaseModel, ValidationError

from matcher.errors import Flag
from matcher.models import (
    Band,
    CalibrationInfo,
    CapApplied,
    ClearanceGate,
    Confidence,
    DateValue,
    DatePrecision,
    DegreeLevel,
    DimensionName,
    DimensionScore,
    Education,
    EducationRequirement,
    Evidence,
    Explanation,
    ExplanationItem,
    Fact,
    GateKind,
    GateResult,
    GateStatus,
    Improvement,
    JobPosting,
    JobRequirement,
    LicenseGate,
    Location,
    LocationGate,
    MatchResult,
    Necessity,
    Outcome,
    ParseTier,
    Polarity,
    Project,
    RemotePolicy,
    Resume,
    Role,
    ScoreInterval,
    ScoreOptions,
    SeniorityLevel,
    SkillRef,
    SkillSource,
    Track,
    VerbClass,
    WorkAuth,
    WorkAuthGate,
    canonical_json,
)


def month(y: int, m: int) -> DateValue:
    first = dt.date(y, m, 1)
    last = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
    return DateValue(value=first, earliest=first, latest=last, precision=DatePrecision.MONTH)


def full_resume() -> Resume:
    react = SkillRef(
        canonical_id="skill.react",
        surface_form="React.js",
        evidence=Evidence.ROLE_BULLET_QUANTIFIED,
        first_used=dt.date(2021, 1, 1),
        last_used=dt.date(2023, 6, 30),
        verb_class=VerbClass.OWNERSHIP,
        source=SkillSource.ROLE,
    )
    return Resume(
        roles=(
            Role(
                title_raw="Software Engineer II",
                title_canonical="title.software_engineer",
                seniority=SeniorityLevel(track=Track.IC, level=2),
                employer="Acme",
                industry_codes=("5112",),
                start=month(2021, 1),
                end=month(2023, 6),
                skills=(react,),
            ),
            Role(
                title_raw="Engineering Intern",
                seniority=SeniorityLevel(track=Track.IC, level=0),
                start=DateValue(
                    value=dt.date(2020, 7, 15),
                    earliest=dt.date(2020, 6, 1),
                    latest=dt.date(2020, 8, 31),
                    precision=DatePrecision.SEASON,
                ),
                is_current=True,
                is_internship=True,
            ),
        ),
        projects=(
            Project(
                name="Thesis",
                skills=(
                    SkillRef(
                        canonical_id="skill.python",
                        evidence=Evidence.ROLE_BULLET,
                        source=SkillSource.PROJECT,
                        polarity=Polarity.ASPIRATIONAL,
                    ),
                ),
            ),
        ),
        education=(Education(level=DegreeLevel.BACHELOR, field_ids=("field.computer_science",), in_progress=True),),
        certifications=(SkillRef(canonical_id="cred.aws_saa", evidence=Evidence.CERTIFICATION),),
        skills_blob=(SkillRef(canonical_id="skill.sql", evidence=Evidence.SKILLS_BLOB),),
        total_experience_months=30.5,
        locations=(Location(country="ca", region="on", city="Ottawa"),),
        work_auth=WorkAuth(authorized_countries=("ca",), needs_sponsorship=False, clearances=("CA_RELIABILITY",)),
        parse_tier=ParseTier.B,
        flags=(Flag.IMPRECISE_DATE,),
    )


def full_posting() -> JobPosting:
    return JobPosting(
        title_raw="Frontend Developer",
        title_canonical="title.frontend_developer",
        job_family="Software Engineering",
        seniority=SeniorityLevel(track=Track.IC, level=1),
        requirements=(
            JobRequirement(skill_id="skill.react", necessity=Necessity.REQUIRED, min_years=2),
            JobRequirement(skill_id="skill.vue", necessity=Necessity.REQUIRED, or_group="g1"),
            JobRequirement(skill_id="skill.angular", necessity=Necessity.REQUIRED, or_group="g1"),
            JobRequirement(
                skill_id="skill.jira",
                necessity=Necessity.IMPLIED,
                weight_multiplier=0.5,
                flags=(Flag.REQUIREMENTS_TRUNCATED,),
            ),
        ),
        min_years_total=1.5,
        education_requirement=EducationRequirement(
            min_level=DegreeLevel.BACHELOR, field_ids=("field.computer_science",), or_equivalent=True
        ),
        certifications_required=("cred.registered_nurse",),
        industry_codes=("5415",),
        locations=(Location(country="US", region="NY", city="new york"),),
        remote_policy=RemotePolicy.HYBRID,
        gates=(
            WorkAuthGate(countries=("us",), sponsorship_available=False),
            LicenseGate(credential_ids=("cred.registered_nurse",)),
            ClearanceGate(level="US_SECRET", active_required=True),
            LocationGate(locations=(Location(country="US"),), onsite_required=True),
        ),
        unresolved_requirement_tokens=("synergy",),
        requirement_token_count=5,
        is_internship=False,
    )


def full_result() -> MatchResult:
    dim = DimensionScore(
        name=DimensionName.REQUIRED_SKILLS,
        value=0.8,
        weight=0.35,
        effective_weight=0.4,
        contribution=0.32,
        abstained=False,
        facts=(Fact(code="REQ_MATCH", subject="skill.react", value=1.0, detail={"relation": "EXACT", "idf": 0.8}),),
    )
    abstained = DimensionScore(
        name=DimensionName.DOMAIN,
        value=None,
        weight=0.10,
        effective_weight=0.0,
        contribution=0.0,
        abstained=True,
        abstain_reason=Flag.INDUSTRY_UNKNOWN,
    )
    return MatchResult(
        outcome=Outcome.SCORED,
        gate_status=GateStatus.WARN,
        gates=(GateResult(kind=GateKind.LOCATION, status=GateStatus.WARN, reason=Flag.LOCATION_MISMATCH),),
        raw_score=0.61,
        calibrated_score=76,
        display_score=76,
        band=Band.GOOD,
        dimensions={"required_skills": dim, "domain": abstained},
        caps=(CapApplied(reason=Flag.CRITICAL_MISS, cap=0.7, requirement_ids=("skill.terraform",)),),
        confidence=Confidence(value=0.86, components={"parse_quality": 1.0, "evidence_density": None}),
        interval=ScoreInterval(low=71, high=79, display=True),
        calibration=CalibrationInfo(method="percentile", scope="family", n=420, version="ref-1"),
        explanation=Explanation(
            band=Band.GOOD,
            score=76,
            basis="Better fit than 76% of candidates scored for similar roles",
            strengths=(ExplanationItem(label="7 of 8 required skills", impact="+0.31"),),
            gaps=(ExplanationItem(label="Missing: Terraform (required)", impact="-0.08", note="partly covered"),),
            to_improve=("Adding demonstrated Terraform experience would move this to roughly 84",),
        ),
        improvements=(
            Improvement(skill_ids=("skill.terraform",), new_raw_score=0.7, new_score=84, learning_cost=1.5, text="x"),
        ),
        flags=(Flag.MIN_YEARS_DEFAULTED,),
        engine_version="1.0.0",
        config_hash="abc123",
        as_of=dt.date(2026, 10, 8),
        input_digest="deadbeef",
    )


ROUND_TRIP = [
    full_resume,
    full_posting,
    full_result,
    lambda: ScoreOptions(strict=True, with_interval=True),
    lambda: month(2020, 2),
]


@pytest.mark.parametrize("factory", ROUND_TRIP)
def test_models_round_trip_byte_identical(factory) -> None:
    model: BaseModel = factory()
    text = canonical_json(model)
    again = type(model).model_validate_json(text)
    assert again == model
    assert canonical_json(again) == text


@pytest.mark.parametrize("factory", ROUND_TRIP)
def test_models_are_frozen(factory) -> None:
    model = factory()
    field = next(iter(type(model).model_fields))
    with pytest.raises(ValidationError):
        setattr(model, field, getattr(model, field))


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        SkillRef(canonical_id="skill.x", evidence=Evidence.SKILLS_BLOB, shoe_size=11)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, 61.0])
def test_bad_numbers_rejected(bad: float) -> None:
    with pytest.raises(ValidationError):
        JobPosting(min_years_total=bad)


def test_no_prohibited_fields_in_the_model() -> None:
    """SPEC §9: the contract has nowhere to put a name, age, address, photo or school."""
    forbidden = ("name", "gender", "age", "birth", "dob", "photo", "address", "zip", "postal",
                 "school", "institution", "graduation", "gap", "tenure")
    for model in (Resume, Role, Education, WorkAuth, Location, Project):
        for field in model.model_fields:
            tokens = field.lower().split("_")
            for word in forbidden:
                if word == "name" and model is Project:
                    continue  # a project's name is display text, never a person's name
                assert word not in tokens, f"{model.__name__}.{field} looks like a prohibited input"


def test_date_value_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        DateValue(
            value=dt.date(2020, 1, 1), earliest=dt.date(2020, 2, 1), latest=dt.date(2020, 3, 1),
            precision=DatePrecision.MONTH,
        )
    season = DateValue(
        value=dt.date(2020, 7, 15), earliest=dt.date(2020, 6, 1), latest=dt.date(2020, 8, 31),
        precision=DatePrecision.SEASON,
    )
    assert season.imprecise and not month(2020, 7).imprecise


def test_role_dates() -> None:
    with pytest.raises(ValidationError):
        Role(start=month(2022, 5), end=month(2021, 1))
    with pytest.raises(ValidationError):
        Role(start=month(2022, 5), end=month(2023, 1), is_current=True)
    assert Role(start=month(2022, 5), is_current=True).dated
    assert not Role(start=month(2022, 5)).dated
    assert not Role().dated


def test_seniority_ranges() -> None:
    assert SeniorityLevel(track=Track.IC, level=0).code == "IC0"
    assert SeniorityLevel(track=Track.MGMT, level=6).code == "M6"
    with pytest.raises(ValidationError):
        SeniorityLevel(track=Track.MGMT, level=0)
    with pytest.raises(ValidationError):
        SeniorityLevel(track=Track.IC, level=7)


def test_location_is_canonicalised() -> None:
    loc = Location(country="ca", region="on", city="Ottawa")
    assert (loc.country, loc.region, loc.city) == ("CA", "ON", "ottawa")


def test_degree_ranks() -> None:
    ranks = [d.rank for d in (DegreeLevel.NONE, DegreeLevel.HIGH_SCHOOL, DegreeLevel.ASSOCIATE,
                              DegreeLevel.BACHELOR, DegreeLevel.MASTER, DegreeLevel.DOCTORATE)]
    assert ranks == sorted(ranks) and len(set(ranks)) == 6


def test_dimension_abstention_is_structural() -> None:
    with pytest.raises(ValidationError):
        DimensionScore(name=DimensionName.SENIORITY, value=None, weight=0.1, effective_weight=0.1,
                       contribution=0.0, abstained=True)
    with pytest.raises(ValidationError):
        DimensionScore(name=DimensionName.SENIORITY, value=0.5, weight=0.1, effective_weight=0.1,
                       contribution=0.05, abstained=True)


def _result(**overrides) -> MatchResult:
    base = dict(outcome=Outcome.SCORED, gate_status=GateStatus.PASS, raw_score=0.5, calibrated_score=60,
                display_score=60, band=Band.PARTIAL, engine_version="1.0.0", config_hash="h",
                as_of=dt.date(2026, 1, 1))
    base.update(overrides)
    return MatchResult(**base)


def test_gate_fail_never_carries_a_score() -> None:
    with pytest.raises(ValidationError):
        _result(outcome=Outcome.GATE_FAIL, gate_status=GateStatus.FAIL, reasons=(Flag.LICENSE_MISSING,))
    ok = _result(outcome=Outcome.GATE_FAIL, gate_status=GateStatus.FAIL, raw_score=None, calibrated_score=None,
                 display_score=None, band=None, reasons=(Flag.LICENSE_MISSING,))
    assert ok.display_score is None


def test_outcome_shapes() -> None:
    with pytest.raises(ValidationError):
        _result(outcome=Outcome.SCORED_LOW_CONFIDENCE)  # percentage must be suppressed
    low = _result(outcome=Outcome.SCORED_LOW_CONFIDENCE, display_score=None, reasons=(Flag.FEW_REQUIREMENTS,))
    assert low.band is Band.PARTIAL and low.calibrated_score == 60
    with pytest.raises(ValidationError):
        _result(display_score=None)  # SCORED shows a number
    with pytest.raises(ValidationError):
        _result(outcome=Outcome.NEEDS_REVIEW, display_score=None, band=None)  # a reason is required
    review = _result(outcome=Outcome.NEEDS_REVIEW, raw_score=None, calibrated_score=None, display_score=None,
                     band=None, reasons=(Flag.POSTING_UNSCORABLE,))
    assert review.reasons == (Flag.POSTING_UNSCORABLE,)


def test_gate_union_round_trips_by_kind() -> None:
    posting = full_posting()
    kinds = [type(g).__name__ for g in JobPosting.model_validate_json(canonical_json(posting)).gates]
    assert kinds == ["WorkAuthGate", "LicenseGate", "ClearanceGate", "LocationGate"]


def test_interval_spread() -> None:
    assert ScoreInterval(low=71, high=79, display=True).spread == 8
    with pytest.raises(ValidationError):
        ScoreInterval(low=80, high=79, display=False)
