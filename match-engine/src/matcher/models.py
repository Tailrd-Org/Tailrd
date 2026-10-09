"""The contract between stages (SPEC §2).

Every model is frozen, rejects unknown fields and rejects NaN/inf at validation, so a value that
reaches the scorer has already been checked (SPEC §13). Collections are tuples so a frozen
model cannot be mutated through a list it holds. Free-text fields (`title_raw`, `employer`,
`surface_form`, `field_raw`) exist for display and explanation only; `score/` reads canonical ids,
enums, dates and numbers (DECISIONS D-14). There is deliberately no field for name, age, address,
photo, school or graduation date (SPEC §9).
"""

from __future__ import annotations

import datetime as dt
import json
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from matcher.errors import Flag

# Generous structural limits: they keep absurd inputs from turning into absurd runtimes while
# never rejecting a real résumé or posting (a 200-skill résumé is legitimate, SPEC §14.5).
_ID = Annotated[str, Field(min_length=1, max_length=200)]
_TEXT = Annotated[str, Field(max_length=2000)]
_SHORT = Annotated[str, Field(max_length=200)]
_UNIT = Annotated[float, Field(ge=0.0, le=1.0)]


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


def canonical_json(model: BaseModel) -> str:
    """The one serialisation used for hashing and determinism checks (DECISIONS D-10)."""
    return json.dumps(
        model.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


# ─── Enums ──────────────────────────────────────────────────────────────────────────────


class Evidence(str, Enum):
    """How a skill was shown on the résumé (SPEC §5.1 evidence tiers)."""

    SKILLS_BLOB = "SKILLS_BLOB"
    ROLE_BULLET = "ROLE_BULLET"
    ROLE_BULLET_QUANTIFIED = "ROLE_BULLET_QUANTIFIED"
    ROLE_TITLE = "ROLE_TITLE"
    CERTIFICATION = "CERTIFICATION"


class VerbClass(str, Enum):
    """Strength of the action verb attached to a bullet mention (SPEC §15.1)."""

    OWNERSHIP = "OWNERSHIP"
    EXECUTION = "EXECUTION"
    PARTICIPATION = "PARTICIPATION"
    ASSISTANCE = "ASSISTANCE"
    EXPOSURE = "EXPOSURE"
    NO_VERB = "NO_VERB"


class Polarity(str, Enum):
    """Negation / aspiration of a mention (SPEC §15.2)."""

    AFFIRMED = "AFFIRMED"
    NEGATED = "NEGATED"
    ASPIRATIONAL = "ASPIRATIONAL"


class SkillSource(str, Enum):
    """Where on the résumé a mention was found (explanation only; DECISIONS D-12)."""

    SKILLS_SECTION = "SKILLS_SECTION"
    ROLE = "ROLE"
    PROJECT = "PROJECT"
    EDUCATION = "EDUCATION"
    SUMMARY = "SUMMARY"
    CERTIFICATION = "CERTIFICATION"
    OTHER = "OTHER"


class Necessity(str, Enum):
    REQUIRED = "REQUIRED"
    PREFERRED = "PREFERRED"
    IMPLIED = "IMPLIED"


class Track(str, Enum):
    IC = "IC"
    MGMT = "M"


class DegreeLevel(str, Enum):
    NONE = "NONE"
    HIGH_SCHOOL = "HIGH_SCHOOL"
    ASSOCIATE = "ASSOCIATE"  # also college diplomas
    BACHELOR = "BACHELOR"
    MASTER = "MASTER"
    DOCTORATE = "DOCTORATE"

    @property
    def rank(self) -> int:
        return _DEGREE_RANK[self]


_DEGREE_RANK = {
    DegreeLevel.NONE: 0,
    DegreeLevel.HIGH_SCHOOL: 1,
    DegreeLevel.ASSOCIATE: 2,
    DegreeLevel.BACHELOR: 3,
    DegreeLevel.MASTER: 4,
    DegreeLevel.DOCTORATE: 5,
}


class DatePrecision(str, Enum):
    DAY = "DAY"
    MONTH = "MONTH"
    SEASON = "SEASON"
    QUARTER = "QUARTER"
    YEAR = "YEAR"


class ParseTier(str, Enum):
    """Parse quality (SPEC §17.1): A clean, B degraded (e.g. column order uncertain), C OCR."""

    A = "A"
    B = "B"
    C = "C"


class RemotePolicy(str, Enum):
    ONSITE = "ONSITE"
    HYBRID = "HYBRID"
    REMOTE = "REMOTE"
    UNKNOWN = "UNKNOWN"


class SkillKind(str, Enum):
    """Node kinds (SPEC §3) plus CREDENTIAL for certifications and licences (DECISIONS D-13)."""

    TECHNOLOGY = "TECHNOLOGY"
    METHODOLOGY = "METHODOLOGY"
    DOMAIN = "DOMAIN"
    TOOL = "TOOL"
    SOFT = "SOFT"
    CREDENTIAL = "CREDENTIAL"


class RelationKind(str, Enum):
    """Résumé skill → requirement relationship (SPEC §5.1 table)."""

    EXACT = "EXACT"
    CHILD = "CHILD"  # résumé skill is a descendant of the requirement
    SIBLING = "SIBLING"  # related edge
    PARENT = "PARENT"  # résumé skill is an ancestor of the requirement
    NONE = "NONE"


class Outcome(str, Enum):
    """The four outcomes of the totality contract (SPEC §13)."""

    SCORED = "SCORED"
    SCORED_LOW_CONFIDENCE = "SCORED_LOW_CONFIDENCE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    GATE_FAIL = "GATE_FAIL"


class GateKind(str, Enum):
    WORK_AUTHORIZATION = "WORK_AUTHORIZATION"
    LICENSE = "LICENSE"
    SECURITY_CLEARANCE = "SECURITY_CLEARANCE"
    LOCATION = "LOCATION"


class GateStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


class Band(str, Enum):
    """Display bands on the calibrated integer (SPEC §7)."""

    STRONG = "Strong match"
    GOOD = "Good match"
    PARTIAL = "Partial match"
    LIMITED = "Limited match"


class DimensionName(str, Enum):
    """Keys of config/weights.yaml `dimensions` (SPEC §5)."""

    REQUIRED_SKILLS = "required_skills"
    PREFERRED_SKILLS = "preferred_skills"
    EXPERIENCE = "experience"
    SENIORITY = "seniority"
    DOMAIN = "domain"
    EDUCATION = "education"
    TOOLS = "tools"


# ─── Shared value objects ─────────────────────────────────────────────────────────────────


class DateValue(Frozen):
    """A parsed date with its plausible range (DECISIONS D-11).

    `value` is the point estimate (quarter midpoint for seasons/quarters, SPEC §14.2);
    `earliest`/`latest` bound it. Score intervals (§17.3) vary imprecise dates across the range.
    """

    value: dt.date
    earliest: dt.date
    latest: dt.date
    precision: DatePrecision

    @model_validator(mode="after")
    def _ordered(self) -> "DateValue":
        if not (self.earliest <= self.value <= self.latest):
            raise ValueError("DateValue requires earliest <= value <= latest")
        return self

    @property
    def imprecise(self) -> bool:
        return self.precision in (DatePrecision.SEASON, DatePrecision.QUARTER, DatePrecision.YEAR)

    @classmethod
    def exact(cls, value: dt.date, precision: DatePrecision = DatePrecision.DAY) -> "DateValue":
        return cls(value=value, earliest=value, latest=value, precision=precision)


class SeniorityLevel(Frozen):
    """Two-track ladder (SPEC §5.4). IC0 = intern / co-op / student (DECISIONS D-38)."""

    track: Track
    level: int = Field(ge=0, le=6)

    @model_validator(mode="after")
    def _range(self) -> "SeniorityLevel":
        if self.track is Track.MGMT and self.level < 1:
            raise ValueError("management levels run M1..M6")
        return self

    @property
    def code(self) -> str:
        return f"{self.track.value}{self.level}"


class Location(Frozen):
    """Canonical place: country (ISO 3166-1 alpha-2), region code, folded city. Never an
    address or postal code (SPEC §9). Used by the location gate only."""

    country: Optional[Annotated[str, Field(min_length=2, max_length=2)]] = None
    region: Optional[_SHORT] = None
    city: Optional[_SHORT] = None

    @field_validator("country", "region")
    @classmethod
    def _upper(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v

    @field_validator("city")
    @classmethod
    def _fold(cls, v: Optional[str]) -> Optional[str]:
        return v.casefold() if v else v


# ─── Résumé side ─────────────────────────────────────────────────────────────────────────


class SkillRef(Frozen):
    canonical_id: _ID
    surface_form: _TEXT = ""
    evidence: Evidence
    first_used: Optional[dt.date] = None
    last_used: Optional[dt.date] = None
    verb_class: VerbClass = VerbClass.NO_VERB
    polarity: Polarity = Polarity.AFFIRMED
    source: SkillSource = SkillSource.OTHER

    @model_validator(mode="after")
    def _ordered(self) -> "SkillRef":
        if self.first_used and self.last_used and self.first_used > self.last_used:
            raise ValueError("first_used must not be after last_used")
        return self


class Role(Frozen):
    title_raw: _TEXT = ""
    title_canonical: Optional[_ID] = None
    seniority: Optional[SeniorityLevel] = None
    employer: _TEXT = ""
    industry_codes: tuple[_SHORT, ...] = Field(default=(), max_length=20)
    start: Optional[DateValue] = None
    end: Optional[DateValue] = None
    is_current: bool = False
    is_internship: bool = False
    skills: tuple[SkillRef, ...] = Field(default=(), max_length=1000)

    @model_validator(mode="after")
    def _dates(self) -> "Role":
        if self.is_current and self.end is not None:
            raise ValueError("a current role has no end date (as_of is used instead)")
        if self.start is not None and self.end is not None and self.end.value < self.start.value:
            raise ValueError("role ends before it starts; the parser must swap or drop it")
        return self

    @property
    def dated(self) -> bool:
        """Has a parseable start and an end (or is current). SPEC §17.1 temporal completeness."""
        return self.start is not None and (self.end is not None or self.is_current)


class Project(Frozen):
    """Skill evidence outside employment (DECISIONS D-12). Never counts toward months."""

    name: _TEXT = ""
    skills: tuple[SkillRef, ...] = Field(default=(), max_length=1000)
    start: Optional[DateValue] = None
    end: Optional[DateValue] = None


class Education(Frozen):
    level: DegreeLevel
    field_ids: tuple[_ID, ...] = Field(default=(), max_length=20)
    field_raw: _TEXT = ""
    in_progress: bool = False


class WorkAuth(Frozen):
    authorized_countries: tuple[Annotated[str, Field(min_length=2, max_length=2)], ...] = ()
    needs_sponsorship: Optional[bool] = None
    citizenships: tuple[Annotated[str, Field(min_length=2, max_length=2)], ...] = ()
    clearances: tuple[_SHORT, ...] = ()  # e.g. "US_SECRET", "CA_RELIABILITY"

    @field_validator("authorized_countries", "citizenships")
    @classmethod
    def _upper(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(c.upper() for c in v)


class Resume(Frozen):
    roles: tuple[Role, ...] = Field(default=(), max_length=300)
    projects: tuple[Project, ...] = Field(default=(), max_length=300)
    education: tuple[Education, ...] = Field(default=(), max_length=50)
    certifications: tuple[SkillRef, ...] = Field(default=(), max_length=300)
    skills_blob: tuple[SkillRef, ...] = Field(default=(), max_length=5000)
    total_experience_months: Optional[float] = Field(default=None, ge=0.0, le=1200.0)
    locations: tuple[Location, ...] = Field(default=(), max_length=20)
    work_auth: Optional[WorkAuth] = None
    parse_tier: ParseTier = ParseTier.A
    language: _SHORT = "en"
    flags: tuple[Flag, ...] = ()


# ─── Posting side ──────────────────────────────────────────────────────────────────────────


class JobRequirement(Frozen):
    skill_id: _ID
    necessity: Necessity
    min_years: Optional[int] = Field(default=None, ge=0, le=60)
    or_group: Optional[_SHORT] = None
    or_equivalent: bool = False
    surface_form: _TEXT = ""
    weight_multiplier: _UNIT = 1.0  # overflow requirements beyond the top 25 (SPEC §14.4)
    flags: tuple[Flag, ...] = ()


class EducationRequirement(Frozen):
    min_level: DegreeLevel
    field_ids: tuple[_ID, ...] = Field(default=(), max_length=20)
    or_equivalent: bool = False
    necessity: Necessity = Necessity.REQUIRED


class WorkAuthGate(Frozen):
    kind: Literal["WORK_AUTHORIZATION"] = "WORK_AUTHORIZATION"
    countries: tuple[Annotated[str, Field(min_length=2, max_length=2)], ...] = ()
    sponsorship_available: Optional[bool] = None
    citizenship_required: bool = False

    @field_validator("countries")
    @classmethod
    def _upper(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(c.upper() for c in v)


class LicenseGate(Frozen):
    kind: Literal["LICENSE"] = "LICENSE"
    credential_ids: tuple[_ID, ...] = Field(min_length=1)  # any one satisfies the gate
    surface_form: _TEXT = ""


class ClearanceGate(Frozen):
    kind: Literal["SECURITY_CLEARANCE"] = "SECURITY_CLEARANCE"
    level: _SHORT
    active_required: bool = False


class LocationGate(Frozen):
    kind: Literal["LOCATION"] = "LOCATION"
    locations: tuple[Location, ...] = ()
    onsite_required: bool = True


Gate = Annotated[
    Union[WorkAuthGate, LicenseGate, ClearanceGate, LocationGate], Field(discriminator="kind")
]


class JobPosting(Frozen):
    title_raw: _TEXT = ""
    title_canonical: Optional[_ID] = None
    job_family: Optional[_SHORT] = None
    seniority: Optional[SeniorityLevel] = None
    requirements: tuple[JobRequirement, ...] = Field(default=(), max_length=1000)
    min_years_total: Optional[float] = Field(default=None, ge=0.0, le=60.0)
    education_requirement: Optional[EducationRequirement] = None
    certifications_required: tuple[_ID, ...] = ()
    certifications_preferred: tuple[_ID, ...] = ()
    industry_codes: tuple[_SHORT, ...] = Field(default=(), max_length=20)
    locations: tuple[Location, ...] = Field(default=(), max_length=50)
    remote_policy: RemotePolicy = RemotePolicy.UNKNOWN
    gates: tuple[Gate, ...] = Field(default=(), max_length=20)
    # Requirement phrases that did not resolve to a canonical id (SPEC §16, §17.1, §17.3).
    unresolved_requirement_tokens: tuple[_TEXT, ...] = Field(default=(), max_length=1000)
    requirement_token_count: int = Field(default=0, ge=0, le=100000)
    is_internship: bool = False
    language: _SHORT = "en"
    flags: tuple[Flag, ...] = ()


# ─── Scoring inputs ────────────────────────────────────────────────────────────────────────


class ScoreOptions(Frozen):
    strict: bool = False  # True in tests: engine faults raise instead of degrading (SPEC §13)
    with_explanation: bool = True
    with_interval: bool = False  # two extra runs (SPEC §17.3, DECISIONS D-45)
    with_counterfactual: bool = False  # `to_improve` (SPEC §8, ADVANCED §7)
    max_counterfactual_steps: int = Field(default=4, ge=1, le=4)


# ─── Results ────────────────────────────────────────────────────────────────────────────────

_Scalar = Union[bool, int, float, str, None]


class Fact(Frozen):
    """One specific fact behind a dimension score (SPEC §5: 'the specific facts that produced it').

    `subject` is a canonical id or a dimension-specific key; labels are resolved by explain/.
    """

    code: _SHORT
    subject: _TEXT = ""
    value: Optional[float] = None
    detail: dict[str, _Scalar] = Field(default_factory=dict)


class DimensionScore(Frozen):
    name: DimensionName
    value: Optional[_UNIT]  # None = abstained (insufficient data), never "zero"
    weight: _UNIT  # as configured
    effective_weight: _UNIT  # after redistribution (SPEC §14.5); 0 when abstained
    contribution: _UNIT  # effective_weight × value
    abstained: bool
    abstain_reason: Optional[Flag] = None
    facts: tuple[Fact, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> "DimensionScore":
        if self.abstained != (self.value is None):
            raise ValueError("abstained must be True exactly when value is None")
        if self.abstained and (self.effective_weight > 0.0 or self.contribution > 0.0):
            raise ValueError("an abstaining dimension carries no weight")
        return self


class GateResult(Frozen):
    kind: GateKind
    status: GateStatus
    reason: Optional[Flag] = None
    detail: _TEXT = ""


class CapApplied(Frozen):
    reason: Flag
    cap: _UNIT
    requirement_ids: tuple[_ID, ...] = ()


class Confidence(Frozen):
    value: _UNIT
    # parse_quality, taxonomy_coverage, evidence_density, temporal_completeness,
    # requirement_count, dimension_coverage; None = undefined for this pair (D-44)
    components: dict[str, Optional[_UNIT]]
    reasons: tuple[Flag, ...] = ()


class ScoreInterval(Frozen):
    low: int = Field(ge=0, le=100)
    high: int = Field(ge=0, le=100)
    display: bool

    @property
    def spread(self) -> int:
        return self.high - self.low

    @model_validator(mode="after")
    def _ordered(self) -> "ScoreInterval":
        if self.low > self.high:
            raise ValueError("interval low must not exceed high")
        return self


class CalibrationInfo(Frozen):
    method: Literal["percentile", "isotonic", "identity"]
    scope: Literal["posting", "family", "global", "panel", "test"]
    n: int = Field(ge=0)
    version: _SHORT


class ExplanationItem(Frozen):
    label: _TEXT
    impact: _SHORT = ""  # signed, raw scale, e.g. "+0.31" (SPEC §8)
    note: _TEXT = ""
    code: _SHORT = ""


class Explanation(Frozen):
    """SPEC §8 output, plus `notes` for the §17.2 limited-information sentence(s)."""

    band: Optional[Band] = None
    score: Optional[int] = Field(default=None, ge=0, le=100)
    basis: _TEXT = ""
    headline: _TEXT = ""
    strengths: tuple[ExplanationItem, ...] = ()
    gaps: tuple[ExplanationItem, ...] = ()
    to_improve: tuple[_TEXT, ...] = ()
    notes: tuple[_TEXT, ...] = ()


class Improvement(Frozen):
    """One counterfactual suggestion (SPEC §8 `to_improve`, ADVANCED §7)."""

    skill_ids: tuple[_ID, ...] = Field(min_length=1, max_length=4)
    new_raw_score: _UNIT
    new_score: Optional[int] = Field(default=None, ge=0, le=100)
    new_band: Optional[Band] = None
    learning_cost: float = Field(ge=0.0)
    text: _TEXT


class MatchResult(Frozen):
    outcome: Outcome
    gate_status: GateStatus
    gates: tuple[GateResult, ...] = ()
    raw_score: Optional[_UNIT] = None
    calibrated_score: Optional[int] = Field(default=None, ge=0, le=100)  # internal, for sorting
    display_score: Optional[int] = Field(default=None, ge=0, le=100)  # shown to people
    band: Optional[Band] = None
    dimensions: dict[str, DimensionScore] = Field(default_factory=dict)
    caps: tuple[CapApplied, ...] = ()
    confidence: Optional[Confidence] = None
    interval: Optional[ScoreInterval] = None
    calibration: Optional[CalibrationInfo] = None
    explanation: Optional[Explanation] = None
    improvements: tuple[Improvement, ...] = ()
    reasons: tuple[Flag, ...] = ()  # why NEEDS_REVIEW / GATE_FAIL / low confidence
    flags: tuple[Flag, ...] = ()  # informational
    engine_version: _SHORT
    config_hash: _SHORT
    as_of: dt.date
    input_digest: _SHORT = ""

    @model_validator(mode="after")
    def _outcome_shape(self) -> "MatchResult":
        # Gate precedence (SPEC §4, §10): an ineligible pair never carries a percentage.
        if self.outcome is Outcome.GATE_FAIL:
            if self.raw_score is not None or self.calibrated_score is not None or self.display_score is not None:
                raise ValueError("GATE_FAIL never carries a score")
            if self.gate_status is not GateStatus.FAIL:
                raise ValueError("GATE_FAIL requires gate_status FAIL")
        if self.outcome is Outcome.NEEDS_REVIEW and self.display_score is not None:
            raise ValueError("NEEDS_REVIEW never displays a score")
        if self.outcome is Outcome.SCORED_LOW_CONFIDENCE:
            if self.display_score is not None:
                raise ValueError("low-confidence results suppress the percentage (SPEC §17.2)")
            if self.band is None:
                raise ValueError("low-confidence results still show a band")
        if self.outcome is Outcome.SCORED and (self.display_score is None or self.band is None):
            raise ValueError("SCORED results show a score and a band")
        if self.outcome in (Outcome.NEEDS_REVIEW, Outcome.GATE_FAIL) and not self.reasons:
            raise ValueError("NEEDS_REVIEW and GATE_FAIL always carry a reason")
        return self


def model_from_json(model_type: type[BaseModel], data: Any) -> BaseModel:
    """Validate a model from a JSON string/bytes or an already-decoded object."""
    if isinstance(data, (str, bytes, bytearray)):
        return model_type.model_validate_json(data)
    return model_type.model_validate(data)
