"""The engine entry point (SPEC §13, §17, §4, §14.4, §14.5; DECISIONS D-42..D-46, D-50, D-60).

`score(resume, posting, *, as_of, config, graph, calibration, options)` is a total function: every
input ends in one of the four outcomes (SPEC §13). In order:

1. Input that is not a valid Resume / JobPosting (a model, a dict or JSON) or a date → NEEDS_REVIEW
   INVALID_INPUT.
2. A NEEDS_REVIEW flag on the résumé or the posting → NEEDS_REVIEW with those flags.
3. A posting with no requirements → POSTING_UNSCORABLE when flagged, else NO_REQUIREMENTS_RESOLVED.
4. `as_of` before every date on the résumé → INVALID_AS_OF.
5. A failed gate → GATE_FAIL, with no score computed at all (SPEC §4).
6. The seven dimensions and the aggregate; nothing scored → NOTHING_SCORABLE (D-42).
7. Calibration, the band, confidence and the outcome: the percentage only when SCORED (§17.2).

Non-strict mode turns an engine fault into NEEDS_REVIEW (NUMERIC_FAULT or INTERNAL_ERROR); strict
mode raises it (D-46). Every result carries the engine version, the config + graph hash and the
input digest. Expected values are closed-form arithmetic (EVAL §2.2); idf values are the fixture
graph's data.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from pathlib import Path

import pytest

from matcher.audit.trace import build_trace, trace_digest
from matcher.calibrate import FixedCalibrator, PercentileCalibrator, load_panel, percentile
from matcher.config import combined_hash
from matcher.errors import EngineFault, Flag
from matcher.models import (
    Band,
    CalibrationInfo,
    DatePrecision,
    DateValue,
    DimensionName,
    DimensionScore,
    GateKind,
    GateStatus,
    JobPosting,
    JobRequirement,
    MatchResult,
    Necessity,
    Outcome,
    ParseTier,
    Resume,
    Role,
    ScoreInterval,
    ScoreOptions,
    SeniorityLevel,
    Track,
    WorkAuth,
    WorkAuthGate,
    canonical_json,
)
from matcher.score import engine
from matcher.score.engine import UNKNOWN_AS_OF, input_digest, review_result, score
from tests.builders import AS_OF, abstained_dimension, bullet, listed, on

FIXED = FixedCalibrator()
IC1 = SeniorityLevel(track=Track.IC, level=1)
IC3 = SeniorityLevel(track=Track.IC, level=3)
REQUIRED_IDS = ("skill.python", "skill.sql", "skill.react", "skill.aws", "skill.docker")
PREFERRED_IDS = ("skill.typescript", "skill.kubernetes")
IMPLIED_IDS = ("skill.git", "skill.agile", "skill.excel")
ALL_IDS = REQUIRED_IDS + PREFERRED_IDS + IMPLIED_IDS
W_REQUIRED, W_TOOLS = 0.35, 0.10  # SPEC §5.1, §5.7
U_IDF = 0.50  # intervals.unresolved_token_idf
PANEL = Path(__file__).resolve().parent / "fixtures" / "panel_small.json"


def _close(a: float, b: float, tol: float = 1e-12) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


def _requirements(ids, necessity: Necessity) -> tuple[JobRequirement, ...]:
    return tuple(JobRequirement(skill_id=skill_id, necessity=necessity) for skill_id in ids)


def strong_posting(**changes) -> JobPosting:
    """Every dimension can score: years, a level, an industry, all three necessities, ten ids."""
    fields = dict(
        seniority=IC1,
        min_years_total=1.0,
        industry_codes=("5415",),
        requirements=(_requirements(REQUIRED_IDS, Necessity.REQUIRED)
                      + _requirements(PREFERRED_IDS, Necessity.PREFERRED)
                      + _requirements(IMPLIED_IDS, Necessity.IMPLIED)),
        requirement_token_count=10,
    )
    fields.update(changes)
    return JobPosting(**fields)


def strong_resume(skill_ids=ALL_IDS, **changes) -> Resume:
    """A current IC1 role in the posting's industry since 2023-01-15, every skill an ownership bullet:
    each requirement m = 1.0 (E 1.00, R 1.0), 44.7 months against a 12-month minimum."""
    role = Role(seniority=IC1, industry_codes=("541511",), start=on(date(2023, 1, 15)), is_current=True,
                skills=tuple(bullet(skill_id) for skill_id in skill_ids))
    fields = dict(roles=(role,))
    fields.update(changes)
    return Resume(**fields)


def run(resume, posting, graph, config, *, as_of=AS_OF, calibration=FIXED, **options) -> MatchResult:
    """The engine in strict mode unless a test says otherwise: a fault must surface in tests."""
    options.setdefault("strict", True)
    return score(resume, posting, as_of=as_of, config=config, graph=graph, calibration=calibration,
                 options=ScoreOptions(**options))


def _digest(resume: Resume, posting: JobPosting, as_of: date) -> str:
    text = canonical_json(resume) + "|" + canonical_json(posting) + "|" + as_of.isoformat()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ─── SCORED ────────────────────────────────────────────────────────────────────────────────


def test_a_full_match_is_scored_at_100(small_graph, config) -> None:
    resume, posting = strong_resume(), strong_posting()
    result = run(resume, posting, small_graph, config)
    assert result.outcome is Outcome.SCORED
    assert result.gate_status is GateStatus.PASS and result.gates == ()
    assert result.raw_score == 1.0
    assert (result.calibrated_score, result.display_score, result.band) == (100, 100, Band.STRONG)
    assert result.confidence.value == 1.0 and result.confidence.reasons == ()
    assert list(result.dimensions) == [name.value for name in DimensionName]
    assert all(not dimension.abstained for dimension in result.dimensions.values())
    assert result.caps == () and result.reasons == () and result.flags == ()
    assert result.calibration == CalibrationInfo(method="identity", scope="test", n=0, version="test")
    assert result.interval is None  # only on request
    assert result.explanation is None and result.improvements == ()  # Milestone 4
    assert result.engine_version == config.engine_version
    assert result.config_hash == combined_hash(config, small_graph.version)
    assert result.as_of == AS_OF and result.input_digest == _digest(resume, posting, AS_OF)


def test_a_missing_implied_skill_closed_form(small_graph, config) -> None:
    # git missing: §5.7 = (idf agile + idf excel) / (idf git + idf agile + idf excel); every other
    # dimension stays 1.0 (the role still covers well over the 12-month minimum).
    idf = small_graph.idf
    kept = idf["skill.agile"] + idf["skill.excel"]
    tools = kept / (idf["skill.git"] + kept)
    raw = (1.0 - W_TOOLS) + W_TOOLS * tools  # 0.967937
    without_git = strong_resume([s for s in ALL_IDS if s != "skill.git"])
    result = run(without_git, strong_posting(), small_graph, config)
    assert result.outcome is Outcome.SCORED
    assert _close(result.raw_score, raw)
    assert result.calibrated_score == result.display_score == round(100 * raw) == 97
    assert _close(result.dimensions[DimensionName.TOOLS.value].value, tools)
    assert result.confidence.components["evidence_density"] == 1.0  # 9 of 9 matched groups shown


def test_a_gate_warning_does_not_stop_scoring(small_graph, config) -> None:
    posting = strong_posting(gates=(WorkAuthGate(countries=("CA",)),))  # the résumé states nothing
    result = run(strong_resume(), posting, small_graph, config)
    assert result.outcome is Outcome.SCORED and result.gate_status is GateStatus.WARN
    assert [(gate.kind, gate.status, gate.reason) for gate in result.gates] == [
        (GateKind.WORK_AUTHORIZATION, GateStatus.WARN, Flag.WORK_AUTH_UNKNOWN),
    ]


def test_informational_flags_are_carried(small_graph, config) -> None:
    # The inputs' own flags, then the scoring's informational facts (OVERQUALIFIED: IC3 vs IC1).
    role = strong_resume().roles[0].model_copy(update={"seniority": IC3})
    flags = (Flag.IMPRECISE_DATE, Flag.TWO_DIGIT_YEAR, Flag.IMPRECISE_DATE)
    resume = strong_resume(roles=(role,), flags=flags)
    posting = strong_posting(flags=(Flag.REQUIREMENTS_TRUNCATED,))
    result = run(resume, posting, small_graph, config)
    assert result.outcome is Outcome.SCORED and result.raw_score == 1.0  # noted, never deducted (D-39)
    assert result.flags == (Flag.IMPRECISE_DATE, Flag.TWO_DIGIT_YEAR, Flag.REQUIREMENTS_TRUNCATED,
                            Flag.OVERQUALIFIED)


# ─── SCORED_LOW_CONFIDENCE and NEEDS_REVIEW by confidence ──────────────────────────────────


def _one_requirement_posting(**changes) -> JobPosting:
    fields = dict(seniority=IC1, min_years_total=1.0, industry_codes=("5415",), requirement_token_count=1,
                  requirements=_requirements(("skill.python",), Necessity.REQUIRED))
    fields.update(changes)
    return JobPosting(**fields)


def test_low_confidence_shows_the_band_but_not_the_number(small_graph, config) -> None:
    # Parse tier C (0.55), one requirement (0.40), preferred and tools abstain (coverage 0.80); the
    # rest 1.0: (0.55 × 0.40 × 0.80)^(1/6) = 0.7486.
    resume = strong_resume(("skill.python",), parse_tier=ParseTier.C)
    result = run(resume, _one_requirement_posting(), small_graph, config)
    assert _close(result.confidence.value, (0.55 * 0.40 * 0.80) ** (1 / 6))
    assert result.outcome is Outcome.SCORED_LOW_CONFIDENCE
    assert result.display_score is None
    # Every scoring dimension at 1.0, renormalised over 0.80: 1.0 up to float rounding.
    assert result.band is Band.STRONG and result.calibrated_score == 100 and _close(result.raw_score, 1.0)
    assert result.reasons == (Flag.LOW_PARSE_QUALITY, Flag.FEW_REQUIREMENTS)
    assert result.reasons == result.confidence.reasons


def test_very_low_confidence_is_routed_to_review_with_every_reason(small_graph, config) -> None:
    # Tier C (0.55), python only listed (evidence density 0 → 1e-6), no roles (temporal undefined),
    # one requirement (0.40); required, experience (no work: 0.0) and education score: 0.60.
    resume = Resume(skills_blob=listed("skill.python"), parse_tier=ParseTier.C)
    posting = _one_requirement_posting(seniority=None, industry_codes=())
    result = run(resume, posting, small_graph, config)
    logs = math.log(0.55) + math.log(1.0) + math.log(1e-6) + math.log(0.40) + math.log(0.60)
    assert _close(result.confidence.value, math.exp(logs / 5))
    assert result.outcome is Outcome.NEEDS_REVIEW
    assert result.reasons == (Flag.LOW_CONFIDENCE, Flag.LOW_PARSE_QUALITY, Flag.LOW_EVIDENCE_DENSITY,
                              Flag.FEW_REQUIREMENTS, Flag.LOW_DIMENSION_COVERAGE)
    assert result.display_score is None and result.band is None
    # The scores stay on the record for the person who reviews it; nothing is displayed.
    raw = (0.35 * 0.65 + 0.15 * 0.0 + 0.10 * 1.0) / (0.35 + 0.15 + 0.10)
    assert _close(result.raw_score, raw) and result.calibrated_score == round(100 * raw)
    assert result.flags == (Flag.NO_WORK_EXPERIENCE,)


# ─── NEEDS_REVIEW before scoring ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_resume", [
    b"\x89PNG\r\n\x1a\n",
    "not json at all",
    {"roles": "not a list"},
    {"total_experience_months": float("nan")},
    {"name": "Jane Doe"},  # no such field (SPEC §9)
    float("nan"),
    None,
    42,
    [],
    JobPosting(),  # a model of the wrong kind
    Resume.model_construct(roles="junk"),  # a model that skipped validation
    Resume.model_construct(total_experience_months=float("nan")),
])
def test_invalid_resume_input(small_graph, config, bad_resume) -> None:
    result = run(bad_resume, strong_posting(), small_graph, config)
    assert result.outcome is Outcome.NEEDS_REVIEW and result.reasons == (Flag.INVALID_INPUT,)
    assert result.raw_score is None and result.display_score is None and result.band is None
    assert result.as_of == AS_OF and result.input_digest == ""


@pytest.mark.parametrize("bad_posting", [
    b"",
    {"requirements": [{"skill_id": "", "necessity": "REQUIRED"}]},
    {"requirements": [{"skill_id": "skill.python", "necessity": "MANDATORY"}]},
    {"min_years_total": float("inf")},
    Resume(),
    JobPosting.model_construct(requirements=("junk",)),
])
def test_invalid_posting_input(small_graph, config, bad_posting) -> None:
    result = run(strong_resume(), bad_posting, small_graph, config)
    assert result.outcome is Outcome.NEEDS_REVIEW and result.reasons == (Flag.INVALID_INPUT,)


@pytest.mark.parametrize("bad_as_of", [
    "2026-10-08",
    datetime(2026, 10, 8, 12, 0),
    None,
    float("nan"),
    20261008,
])
def test_an_as_of_that_is_not_a_date_is_invalid_input(small_graph, config, bad_as_of) -> None:
    result = run(strong_resume(), strong_posting(), small_graph, config, as_of=bad_as_of)
    assert result.outcome is Outcome.NEEDS_REVIEW and result.reasons == (Flag.INVALID_INPUT,)
    assert result.as_of == UNKNOWN_AS_OF == date.min and result.input_digest == ""


class _BrokenDate(date):
    """A date whose fields cannot be read."""

    @property
    def year(self):
        raise RuntimeError("unreadable")


class _BrokenResume(Resume):
    """A résumé model that refuses to be read back."""

    def model_dump(self, **kwargs):
        raise RuntimeError("unreadable")


@pytest.mark.parametrize("strict", [True, False])
def test_inputs_that_cannot_be_read_are_invalid_not_faults(small_graph, config, strict) -> None:
    # Whatever an input raises while it is read, the input is invalid: nothing escapes (D-46).
    broken_as_of = run(strong_resume(), strong_posting(), small_graph, config, as_of=_BrokenDate(2026, 10, 8),
                       strict=strict)
    assert broken_as_of.reasons == (Flag.INVALID_INPUT,) and broken_as_of.as_of == UNKNOWN_AS_OF
    broken_resume = run(_BrokenResume(), strong_posting(), small_graph, config, strict=strict)
    assert broken_resume.reasons == (Flag.INVALID_INPUT,) and broken_resume.input_digest == ""


def test_valid_input_in_any_form_scores_the_same(small_graph, config) -> None:
    resume, posting = strong_resume([s for s in ALL_IDS if s != "skill.git"]), strong_posting()
    expected = canonical_json(run(resume, posting, small_graph, config))
    forms = [
        (resume.model_dump(), posting.model_dump()),
        (canonical_json(resume), canonical_json(posting)),
        (canonical_json(resume).encode("utf-8"), canonical_json(posting).encode("utf-8")),
        (json.loads(canonical_json(resume)), json.loads(canonical_json(posting))),
    ]
    for resume_form, posting_form in forms:
        assert canonical_json(run(resume_form, posting_form, small_graph, config)) == expected


def test_resume_flags_that_mean_review_route_to_review(small_graph, config) -> None:
    resume = strong_resume(flags=(Flag.OCR_USED, Flag.UNREADABLE))
    result = run(resume, strong_posting(), small_graph, config)
    assert result.outcome is Outcome.NEEDS_REVIEW
    assert result.reasons == (Flag.UNREADABLE,) and result.flags == (Flag.OCR_USED,)
    assert result.input_digest == _digest(resume, strong_posting(), AS_OF)


def test_posting_flags_that_mean_review_route_to_review(small_graph, config) -> None:
    posting = strong_posting(flags=(Flag.UNSUPPORTED_LANGUAGE,))
    resume = strong_resume(flags=(Flag.UNSUPPORTED_CALENDAR,))
    result = run(resume, posting, small_graph, config)
    assert result.outcome is Outcome.NEEDS_REVIEW
    assert result.reasons == (Flag.UNSUPPORTED_CALENDAR, Flag.UNSUPPORTED_LANGUAGE)


def test_a_posting_without_requirements_is_unscorable(small_graph, config) -> None:
    flagged = run(strong_resume(), JobPosting(flags=(Flag.POSTING_UNSCORABLE,)), small_graph, config)
    assert flagged.outcome is Outcome.NEEDS_REVIEW and flagged.reasons == (Flag.POSTING_UNSCORABLE,)
    blank = run(strong_resume(), JobPosting(flags=(Flag.BOILERPLATE_ONLY,)), small_graph, config)
    assert blank.outcome is Outcome.NEEDS_REVIEW and blank.reasons == (Flag.NO_REQUIREMENTS_RESOLVED,)
    assert blank.flags == (Flag.BOILERPLATE_ONLY,)


def test_as_of_before_every_resume_date_is_invalid(small_graph, config) -> None:
    future = Role(start=on(date(2027, 1, 4)), is_current=True, skills=(bullet("skill.python"),))
    result = run(strong_resume(roles=(future,)), strong_posting(), small_graph, config)
    assert result.outcome is Outcome.NEEDS_REVIEW and result.reasons == (Flag.INVALID_AS_OF,)
    # A skill date counts too.
    later = Resume(skills_blob=(bullet("skill.python", last_used=date(2027, 2, 1)),))
    assert run(later, strong_posting(), small_graph, config).reasons == (Flag.INVALID_AS_OF,)


def test_as_of_inside_an_imprecise_date_is_not_before_it(small_graph, config) -> None:
    # Q4 2026 runs from Oct 1; as_of Oct 8 may well be after the start, so the date is not "after
    # as_of" for sure and the pair is scored.
    q4 = DateValue(value=date(2026, 11, 16), earliest=date(2026, 10, 1), latest=date(2026, 12, 31),
                   precision=DatePrecision.QUARTER)
    role = Role(start=q4, is_current=True, skills=(bullet("skill.python"),))
    result = run(strong_resume(roles=(role,)), strong_posting(), small_graph, config)
    assert Flag.INVALID_AS_OF not in result.reasons and result.raw_score is not None


def test_a_resume_with_no_dates_is_never_invalid_as_of(small_graph, config) -> None:
    result = run(Resume(skills_blob=listed(*ALL_IDS)), strong_posting(), small_graph, config,
                 as_of=date(1990, 1, 1))
    assert Flag.INVALID_AS_OF not in result.reasons and result.raw_score is not None


# ─── GATE_FAIL ─────────────────────────────────────────────────────────────────────────────


GATED = WorkAuthGate(countries=("CA",), sponsorship_available=False)


def test_a_failed_gate_carries_no_score(small_graph, config) -> None:
    resume = strong_resume(work_auth=WorkAuth(needs_sponsorship=True))
    result = run(resume, strong_posting(gates=(GATED,)), small_graph, config, with_interval=True)
    assert result.outcome is Outcome.GATE_FAIL and result.gate_status is GateStatus.FAIL
    assert result.reasons == (Flag.SPONSORSHIP_UNAVAILABLE,)
    assert result.raw_score is None and result.calibrated_score is None and result.display_score is None
    assert result.band is None and result.dimensions == {} and result.confidence is None
    assert result.interval is None and result.calibration is None and result.caps == ()
    assert [(gate.kind, gate.status) for gate in result.gates] == [
        (GateKind.WORK_AUTHORIZATION, GateStatus.FAIL),
    ]
    assert result.input_digest == _digest(resume, strong_posting(gates=(GATED,)), AS_OF)


def test_a_failed_gate_computes_nothing(monkeypatch, small_graph, config) -> None:
    def never(*args, **kwargs):
        raise AssertionError("scored a pair that failed a gate")

    monkeypatch.setattr(engine, "prepare_resume", never)
    monkeypatch.setattr(engine, "aggregate", never)
    resume = strong_resume(work_auth=WorkAuth(needs_sponsorship=True))
    for strict in (True, False):
        result = run(resume, strong_posting(gates=(GATED,)), small_graph, config, strict=strict)
        assert result.outcome is Outcome.GATE_FAIL


# ─── NOTHING_SCORABLE ──────────────────────────────────────────────────────────────────────


def test_nothing_scorable(monkeypatch, small_graph, config) -> None:
    # The real dimensions always score education, so this is injected: every dimension abstains.
    reasons = {name: Flag.NO_REQUIREMENTS_RESOLVED for name in DimensionName}
    monkeypatch.setattr(engine, "score_dimensions", lambda prepared, posting, graph, config: tuple(
        abstained_dimension(name, reasons[name]) for name in DimensionName))
    posting = strong_posting(gates=(WorkAuthGate(countries=("CA",)),))  # a warning: gates pass on to scoring
    result = run(strong_resume(), posting, small_graph, config)
    assert result.outcome is Outcome.NEEDS_REVIEW and result.reasons == (Flag.NOTHING_SCORABLE,)
    assert result.raw_score is None and result.calibrated_score is None and result.confidence is None
    assert list(result.dimensions) == [name.value for name in DimensionName]
    assert all(dimension.abstained for dimension in result.dimensions.values())
    assert result.gate_status is GateStatus.WARN and len(result.gates) == 1


# ─── Faults: strict raises, non-strict degrades (D-46) ─────────────────────────────────────


@pytest.mark.parametrize("fault, flag", [
    (EngineFault(Flag.NUMERIC_FAULT, "injected"), Flag.NUMERIC_FAULT),
    (EngineFault(Flag.INTERNAL_ERROR, "injected"), Flag.INTERNAL_ERROR),
    (ValueError("injected"), Flag.INTERNAL_ERROR),
    (KeyError("injected"), Flag.INTERNAL_ERROR),
])
def test_an_injected_fault(monkeypatch, small_graph, config, fault, flag) -> None:
    def broken(dimensions, config):
        raise fault

    monkeypatch.setattr(engine, "aggregate", broken)
    resume, posting = strong_resume(), strong_posting()
    result = run(resume, posting, small_graph, config, strict=False)
    assert result.outcome is Outcome.NEEDS_REVIEW and result.reasons == (flag,)
    assert result.raw_score is None and result.display_score is None
    assert result.as_of == AS_OF and result.input_digest == _digest(resume, posting, AS_OF)
    with pytest.raises(type(fault)):
        run(resume, posting, small_graph, config, strict=True)


def test_the_default_options_degrade(monkeypatch, small_graph, config) -> None:
    monkeypatch.setattr(engine, "aggregate", lambda dimensions, config: 1 / 0)
    result = score(strong_resume(), strong_posting(), as_of=AS_OF, config=config, graph=small_graph,
                   calibration=FIXED)
    assert result.reasons == (Flag.INTERNAL_ERROR,)


def test_a_nan_reaching_the_aggregator(monkeypatch, small_graph, config) -> None:
    # SPEC §13: it raises in tests and becomes NEEDS_REVIEW: NUMERIC_FAULT in production.
    def poisoned(prepared, posting, graph, config):
        nan = DimensionScore.model_construct(name=DimensionName.EXPERIENCE, value=float("nan"), weight=0.15,
                                             effective_weight=0.15, contribution=0.0, abstained=False,
                                             abstain_reason=None, facts=())
        others = [abstained_dimension(name, Flag.NO_REQUIREMENTS_RESOLVED)
                  for name in DimensionName if name is not DimensionName.EXPERIENCE]
        return (nan, *others)

    monkeypatch.setattr(engine, "score_dimensions", poisoned)
    result = run(strong_resume(), strong_posting(), small_graph, config, strict=False)
    assert result.reasons == (Flag.NUMERIC_FAULT,)
    with pytest.raises(EngineFault) as err:
        run(strong_resume(), strong_posting(), small_graph, config, strict=True)
    assert err.value.flag is Flag.NUMERIC_FAULT


class _Answers:
    """A calibrator that answers whatever it is told to (a misbehaving plug-in)."""

    def __init__(self, answer) -> None:
        self.answer = answer

    def calibrate(self, raw):
        return self.answer


INFO = CalibrationInfo(method="identity", scope="test", n=0, version="test")


@pytest.mark.parametrize("answer", [
    (101, INFO), (-1, INFO), (50.0, INFO), (True, INFO), (50, None), (50,), "50",
])
def test_a_calibrator_that_answers_nonsense_is_an_internal_error(small_graph, config, answer) -> None:
    result = run(strong_resume(), strong_posting(), small_graph, config, strict=False,
                 calibration=_Answers(answer))
    assert result.outcome is Outcome.NEEDS_REVIEW and result.reasons == (Flag.INTERNAL_ERROR,)
    with pytest.raises(EngineFault) as err:
        run(strong_resume(), strong_posting(), small_graph, config, calibration=_Answers(answer))
    assert err.value.flag is Flag.INTERNAL_ERROR


@pytest.mark.parametrize("wiring", [
    {"config": None},
    {"graph": "graph"},
    {"calibration": object()},
    {"options": {"strict": True}},
])
def test_wrong_wiring_is_a_type_error(small_graph, config, wiring) -> None:
    # config, graph, calibrator and options are the caller's wiring, not input: there is no config
    # to hash a result with, so this raises in both modes.
    arguments = dict(as_of=AS_OF, config=config, graph=small_graph, calibration=FIXED, options=ScoreOptions())
    arguments.update(wiring)
    with pytest.raises(TypeError):
        score(strong_resume(), strong_posting(), **arguments)


# ─── Calibration ───────────────────────────────────────────────────────────────────────────


def test_a_percentile_calibrator_plugs_in(small_graph, config) -> None:
    references = load_panel(PANEL)
    pooled = next(ref for ref in references if (ref.scope, ref.key) == ("panel", "*"))
    calibrator = PercentileCalibrator(references, min_reference_n=30).bind(posting_key=None, family=None)
    resume = strong_resume(("skill.python", "skill.sql", "skill.git"))
    result = run(resume, strong_posting(), small_graph, config, calibration=calibrator)
    assert result.outcome is Outcome.SCORED
    assert result.calibration == CalibrationInfo(
        method="percentile", scope="panel", n=40, version="panel-test-1"
    )
    assert result.calibrated_score == result.display_score == percentile(result.raw_score, pooled)
    assert result.band is config.bands.band_for(result.calibrated_score)


# ─── Intervals (D-45) ──────────────────────────────────────────────────────────────────────


TOKENS = ("Fly.io", "Kubernetes Operators", "Pulumi")


def test_an_interval_wider_than_6_points_is_displayed(small_graph, config) -> None:
    # Three unresolved tokens at idf 0.5: missed, §5.1 = R / (R + 1.5); matched, 1.0. Everything else
    # is 1.0, so the pessimistic raw is 0.35 × R / (R + 1.5) + 0.65 and the optimistic 1.0.
    posting = strong_posting(unresolved_requirement_tokens=TOKENS, requirement_token_count=13)
    result = run(strong_resume(), posting, small_graph, config, with_interval=True)
    r = math.fsum(small_graph.idf[skill_id] for skill_id in REQUIRED_IDS)
    low = round(100 * (W_REQUIRED * r / (r + len(TOKENS) * U_IDF) + (1.0 - W_REQUIRED)))
    assert result.outcome is Outcome.SCORED and result.display_score == 100
    assert result.interval == ScoreInterval(low=low, high=100, display=True)
    assert result.interval.spread > 6 and low == 87


def test_imprecise_dates_widen_the_interval(small_graph, config) -> None:
    def year(y: int) -> DateValue:
        return DateValue(value=date(y, 7, 2), earliest=date(y, 1, 1), latest=date(y, 12, 31),
                         precision=DatePrecision.YEAR)

    role = Role(seniority=IC1, industry_codes=("541511",), start=year(2022), end=year(2024),
                skills=tuple(bullet(skill_id) for skill_id in ALL_IDS))
    result = run(Resume(roles=(role,)), strong_posting(min_years_total=3.0), small_graph, config,
                 with_interval=True)
    assert result.outcome is Outcome.SCORED
    assert result.interval.low < result.display_score < result.interval.high
    assert result.interval.display


def test_the_base_score_is_part_of_the_interval(small_graph, config) -> None:
    # Both readings can score below the base. B, the IC3 role holding every skill, ends 2024-09-01
    # (precise) and starts in 2022 (a bare year); A, an IC1 role with no skills, runs from 2024-01-01
    # to sometime in 2024. Base: B ends last (IC3 meets IC3) and 792 days ≥ 24 months. Optimistic:
    # A could end 2024-12-31, after B, so the candidate reads as IC1 (seniority 1 − 0.28 × 2).
    # Pessimistic: B starts 2022-12-31, 610 days, under 24 months. All skills were last used
    # 2024-09-01, 767 days before as_of, in every reading.
    def year(y: int) -> DateValue:
        return DateValue(value=date(y, 7, 2), earliest=date(y, 1, 1), latest=date(y, 12, 31),
                         precision=DatePrecision.YEAR)

    senior = Role(seniority=IC3, industry_codes=("541511",), start=year(2022), end=on(date(2024, 9, 1)),
                  skills=tuple(bullet(skill_id) for skill_id in ALL_IDS))
    junior = Role(seniority=IC1, industry_codes=("541511",), start=on(date(2024, 1, 1)), end=year(2024))
    posting = strong_posting(seniority=IC3, min_years_total=2.0)
    result = run(Resume(roles=(junior, senior)), posting, small_graph, config, with_interval=True)
    r = max(0.55, math.exp(-0.15 * 767 / 365.25))  # SPEC §5.1 recency, the same in every reading
    base = 0.55 * r + 0.45  # §5.1, §5.2, §5.7 at r; the other four dimensions at 1.0
    optimistic = base - 0.10 * (0.28 * 2)
    months = 610 / 30.4375
    pessimistic = 0.55 * r + 0.15 * (0.6 + 0.4 * (months / 24 - 0.5) / 0.5) + 0.30
    assert result.outcome is Outcome.SCORED and _close(result.raw_score, base)
    assert round(100 * optimistic) < round(100 * pessimistic) < round(100 * base)  # 80 < 83 < 85
    expected = ScoreInterval(low=round(100 * optimistic), high=round(100 * base), display=False)
    assert result.interval == expected  # without the base, high would be the pessimistic 83


def test_a_pair_with_nothing_ambiguous_has_a_point_interval(small_graph, config) -> None:
    result = run(strong_resume(), strong_posting(), small_graph, config, with_interval=True)
    assert result.interval == ScoreInterval(low=100, high=100, display=False)


def test_only_a_scored_result_gets_an_interval(small_graph, config) -> None:
    resume = strong_resume(("skill.python",), parse_tier=ParseTier.C)
    posting = _one_requirement_posting(unresolved_requirement_tokens=TOKENS, requirement_token_count=4)
    result = run(resume, posting, small_graph, config, with_interval=True)
    assert result.outcome is not Outcome.SCORED and result.interval is None


# ─── Digest, hashes, determinism ───────────────────────────────────────────────────────────


def test_the_input_digest_is_stable(small_graph, config) -> None:
    resume, posting = strong_resume(), strong_posting()
    expected = _digest(resume, posting, AS_OF)
    assert len(expected) == 16 and int(expected, 16) >= 0
    assert input_digest(resume, posting, AS_OF) == expected
    assert run(resume, posting, small_graph, config).input_digest == expected
    assert run(resume.model_dump(), canonical_json(posting), small_graph, config).input_digest == expected
    later = run(resume, posting, small_graph, config, as_of=date(2026, 10, 9))
    assert later.input_digest == _digest(resume, posting, date(2026, 10, 9)) != expected


def test_identical_inputs_give_identical_bytes(small_graph, config) -> None:
    from matcher.graph.loader import load_graph
    from tests.fixtures.build_graph_small import FIXTURE_DIR

    resume = strong_resume([s for s in ALL_IDS if s != "skill.git"], flags=(Flag.IMPRECISE_DATE,))
    posting = strong_posting(unresolved_requirement_tokens=TOKENS, requirement_token_count=13)
    first = canonical_json(run(resume, posting, small_graph, config, with_interval=True))
    assert canonical_json(run(resume, posting, small_graph, config, with_interval=True)) == first
    fresh = load_graph(FIXTURE_DIR)  # a graph loaded again: same artifacts, same bytes
    assert canonical_json(run(resume, posting, fresh, config, with_interval=True)) == first


# ─── review_result ─────────────────────────────────────────────────────────────────────────


def test_review_result(small_graph, config) -> None:
    result = review_result([Flag.ENCRYPTED, "CORRUPT", Flag.ENCRYPTED], config=config, graph=small_graph,
                           as_of=AS_OF, input_digest="0123456789abcdef", flags=(Flag.OCR_USED, Flag.CORRUPT))
    assert result.outcome is Outcome.NEEDS_REVIEW and result.gate_status is GateStatus.PASS
    assert result.reasons == (Flag.ENCRYPTED, Flag.CORRUPT)
    assert result.flags == (Flag.OCR_USED,)  # a reason is not repeated as a flag
    assert result.gates == () and result.raw_score is None and result.dimensions == {}
    assert result.engine_version == config.engine_version
    assert result.config_hash == combined_hash(config, small_graph.version)
    assert (result.as_of, result.input_digest) == (AS_OF, "0123456789abcdef")


def test_review_result_needs_a_reason(small_graph, config) -> None:
    with pytest.raises(ValueError):
        review_result([], config=config, graph=small_graph, as_of=AS_OF)


# ─── The audit trace takes every outcome (Task 7.2A) ───────────────────────────────────────


def test_the_audit_trace_accepts_every_outcome(small_graph, config) -> None:
    results = {
        Outcome.SCORED: run(strong_resume(), strong_posting(unresolved_requirement_tokens=TOKENS,
                                                            requirement_token_count=13),
                            small_graph, config, with_interval=True),
        Outcome.SCORED_LOW_CONFIDENCE: run(strong_resume(("skill.python",), parse_tier=ParseTier.C),
                                           _one_requirement_posting(), small_graph, config),
        Outcome.NEEDS_REVIEW: run(Resume(skills_blob=listed("skill.python"), parse_tier=ParseTier.C),
                                  _one_requirement_posting(seniority=None, industry_codes=()),
                                  small_graph, config),
        Outcome.GATE_FAIL: run(strong_resume(work_auth=WorkAuth(needs_sponsorship=True)),
                               strong_posting(gates=(GATED,)), small_graph, config),
    }
    for outcome, result in results.items():
        assert result.outcome is outcome
        trace = build_trace(result)
        assert trace["outcome"] == outcome.value
        assert trace["engine_version"] == config.engine_version and trace["as_of"] == AS_OF.isoformat()
        assert trace["input_digest"] == result.input_digest
        assert len(trace_digest(trace)) == 16  # JSON-native, finite
    scored = build_trace(results[Outcome.SCORED])
    assert scored["calibration"] == {"method": "identity", "scope": "test", "n": 0, "version": "test"}
    assert len(scored["requirements"]) == len(ALL_IDS)
    assert build_trace(results[Outcome.GATE_FAIL])["raw"] is None
    reviewed = build_trace(run(b"junk", strong_posting(), small_graph, config))
    assert reviewed["reasons"] == ["INVALID_INPUT"] and reviewed["input_digest"] == ""
