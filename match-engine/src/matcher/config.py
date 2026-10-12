"""Engine configuration: typed, validated, hashed (SPEC §6, §12).

`load_config()` is the only place that reads the YAML files. `score/` receives an `EngineConfig`
and never loads anything itself (SPEC §1). The config hash is computed over the validated
values, not the file bytes, so comments and formatting never change it.
"""

from __future__ import annotations

import hashlib
import math
import re
from functools import cached_property
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from matcher.models import Band, DimensionName, Evidence, Frozen, SkillKind, VerbClass, canonical_json

WEIGHT_MIN = 0.03  # EVAL §5.1: no dimension vanishes ...
WEIGHT_MAX = 0.45  # ... or dominates
WEIGHT_SUM_TOLERANCE = 1e-9

_Unit = float  # validated to [0, 1] where it matters


class ConfigError(ValueError):
    """A config file is missing, unreadable or violates an invariant."""


def default_config_dir() -> Path:
    # src/matcher/config.py -> match-engine/config
    return Path(__file__).resolve().parents[2] / "config"


# ─── weights.yaml ─────────────────────────────────────────────────────────────────────────


class Weights(Frozen):
    dimensions: dict[DimensionName, float]

    @model_validator(mode="after")
    def _valid(self) -> "Weights":
        missing = set(DimensionName) - set(self.dimensions)
        if missing:
            raise ValueError(f"weights missing dimensions: {sorted(m.value for m in missing)}")
        for name, w in self.dimensions.items():
            if not (WEIGHT_MIN <= w <= WEIGHT_MAX):
                raise ValueError(f"weight {name.value}={w} outside [{WEIGHT_MIN}, {WEIGHT_MAX}]")
        total = math.fsum(self.dimensions[d] for d in DimensionName)
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=WEIGHT_SUM_TOLERANCE):
            raise ValueError(f"dimension weights sum to {total!r}, not 1.0 (SPEC §6)")
        return self

    def of(self, name: DimensionName) -> float:
        return self.dimensions[name]


# ─── gates.yaml ─────────────────────────────────────────────────────────────────────────────


class WorkAuthGateConfig(Frozen):
    enabled: bool = True
    unknown_status: Literal["PASS", "WARN"] = "WARN"


class LicenseGateConfig(Frozen):
    enabled: bool = True
    professional_credential_ids: tuple[str, ...] = ()
    # Formal designations on the way to a licence (taxonomy DECISIONS D-28): never licence evidence
    # through the graph. An explicit list: an id's spelling decides nothing.
    in_progress_credential_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _disjoint(self) -> "LicenseGateConfig":
        both = sorted(set(self.professional_credential_ids) & set(self.in_progress_credential_ids))
        if both:
            raise ValueError(f"listed both as professional licences and as in-progress designations: {both}")
        return self


_LADDER_TOKEN = re.compile(r"[A-Z]{2}(?:_[A-Z0-9]+)+")  # <CC>_<LEVEL>
_RESERVED_TOKEN = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")


class ClearanceGateConfig(Frozen):
    enabled: bool = True
    obtainable_status: Literal["PASS", "WARN", "FAIL"] = "WARN"
    active_missing_status: Literal["PASS", "WARN", "FAIL"] = "FAIL"
    # Ladders, lowest level first, one country each (DECISIONS D-63): a held token at or above the
    # gate's level on the same ladder meets it; a token on no ladder matches exactly.
    ladders: tuple[tuple[str, ...], ...] = ()
    # The token for an active clearance of unknown country and level. Named here only.
    unknown_level_token: str

    @model_validator(mode="after")
    def _tokens(self) -> "ClearanceGateConfig":
        seen: set[str] = set()
        for ladder in self.ladders:
            if not ladder:
                raise ValueError("a clearance ladder is empty")
            for token in ladder:
                if not _LADDER_TOKEN.fullmatch(token):
                    raise ValueError(f"clearance token is not <CC>_<LEVEL>: {token!r}")
                if token in seen:
                    raise ValueError(f"clearance token listed twice: {token}")
                seen.add(token)
            if len({token[:2] for token in ladder}) > 1:
                raise ValueError(f"a clearance ladder spans more than one country: {list(ladder)}")
        token = self.unknown_level_token
        if not _RESERVED_TOKEN.fullmatch(token):
            raise ValueError(f"the unknown-level token is not an upper-case token: {token!r}")
        if token in seen:
            raise ValueError(f"the unknown-level token is on a ladder: {token}")
        return self


class LocationGateConfig(Frozen):
    enabled: bool = True
    # Never FAIL: location is soft by definition (SPEC §4).
    mismatch_status: Literal["PASS", "WARN"] = "WARN"


class GatesConfig(Frozen):
    work_authorization: WorkAuthGateConfig
    license: LicenseGateConfig
    security_clearance: ClearanceGateConfig
    location: LocationGateConfig


# ─── bands.yaml ─────────────────────────────────────────────────────────────────────────────


class BandRule(Frozen):
    band: Band
    min: int = Field(ge=0, le=100)


class BandsConfig(Frozen):
    bands: tuple[BandRule, ...]
    display: dict[str, str]
    basis: dict[str, str]

    @model_validator(mode="after")
    def _valid(self) -> "BandsConfig":
        mins = [b.min for b in self.bands]
        if mins != sorted(mins, reverse=True) or len(set(mins)) != len(mins):
            raise ValueError("bands must be strictly descending by min")
        if not self.bands or self.bands[-1].min != 0:
            raise ValueError("the lowest band must start at 0")
        if {b.band for b in self.bands} != set(Band):
            raise ValueError("every Band appears exactly once")
        for scope in ("posting", "family", "global", "panel", "test"):
            if scope not in self.basis:
                raise ValueError(f"bands.yaml basis missing '{scope}'")
        return self

    def band_for(self, score: int) -> Band:
        for rule in self.bands:
            if score >= rule.min:
                return rule.band
        return self.bands[-1].band


# ─── verbs.yaml / negation.yaml ─────────────────────────────────────────────────────────────


class VerbClassConfig(Frozen):
    multiplier: float = Field(ge=0.0, le=1.0)
    verbs: tuple[str, ...]


class VerbsConfig(Frozen):
    classes: dict[VerbClass, VerbClassConfig]
    no_verb_multiplier: float = Field(ge=0.0, le=1.0)
    quantified_bonus: float = Field(ge=1.0, le=2.0)
    quantified_cap: float = Field(ge=0.0, le=1.0)
    clause_punctuation: tuple[str, ...]
    coordinating_conjunctions: tuple[str, ...]
    list_continuation_inherits_verb: bool = True
    # Instrument participles that defer to an earlier verb of their clause (DECISIONS D-34d).
    defer_to_earlier: tuple[str, ...]
    # Entries that are nouns: they attach only when one of the followers comes next (D-34e).
    noun_entries: tuple[str, ...]
    noun_entry_followers: tuple[str, ...]
    # Lower-case product names whose following number is a version, not a quantity ("react 18").
    version_names: tuple[str, ...]

    @model_validator(mode="after")
    def _valid(self) -> "VerbsConfig":
        if VerbClass.NO_VERB in self.classes:
            raise ValueError("NO_VERB is configured by no_verb_multiplier, not as a class")
        expected = set(VerbClass) - {VerbClass.NO_VERB}
        if set(self.classes) != expected:
            raise ValueError(f"verb classes must be exactly {sorted(c.value for c in expected)}")
        seen: dict[str, VerbClass] = {}
        for cls, cfg in self.classes.items():
            for verb in cfg.verbs:
                key = verb.strip().lower()
                if key in seen and seen[key] is not cls:
                    raise ValueError(f"verb '{verb}' is in both {seen[key].value} and {cls.value}")
                seen[key] = cls
        for noun in self.noun_entries:
            if noun.strip().lower() not in seen:
                raise ValueError(f"noun entry '{noun}' is not a verb-class entry")
        return self

    def multiplier(self, verb_class: VerbClass) -> float:
        if verb_class is VerbClass.NO_VERB:
            return self.no_verb_multiplier
        return self.classes[verb_class].multiplier


class NegationRule(Frozen):
    m: float = Field(ge=0.0, le=1.0)
    terms: tuple[str, ...]


class HardNegationRule(NegationRule):
    """Hard negation reaches a skill only across `scope_fillers`; an `exceptions` phrase ("not
    only") never negates (DECISIONS D-34c)."""

    scope_fillers: tuple[str, ...]
    exceptions: tuple[str, ...] = ()


class NegationConfig(Frozen):
    window_tokens: int = Field(ge=1, le=20)  # the aspiration window (DECISIONS D-34c)
    hard_negation: HardNegationRule
    aspirational: NegationRule


# ─── scoring.yaml ─────────────────────────────────────────────────────────────────────────


class EvidenceConfig(Frozen):
    SKILLS_BLOB: float = Field(ge=0.0, le=1.0)
    ROLE_BULLET: float = Field(ge=0.0, le=1.0)
    ROLE_BULLET_QUANTIFIED: float = Field(ge=0.0, le=1.0)
    ROLE_TITLE: float = Field(ge=0.0, le=1.0)
    CERTIFICATION: float = Field(ge=0.0, le=1.0)


class RecencyConfig(Frozen):
    decay_per_year: float = Field(ge=0.0, le=5.0)
    floor: float = Field(ge=0.0, le=1.0)


class RelationsConfig(Frozen):
    exact: float = Field(ge=0.0, le=1.0)
    child: float = Field(ge=0.0, le=1.0)
    parent: float = Field(ge=0.0, le=1.0)
    sibling_min: float = Field(ge=0.0, le=1.0)
    sibling_max: float = Field(ge=0.0, le=1.0)
    max_depth: int = Field(ge=1, le=10)
    provisional_multiplier: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> "RelationsConfig":
        if self.sibling_min > self.sibling_max:
            raise ValueError("sibling_min must not exceed sibling_max")
        return self


class CriticalMissConfig(Frozen):
    idf_threshold: float = Field(ge=0.0, le=1.0)
    m_threshold: float = Field(ge=0.0, le=1.0)
    cap_one: float = Field(ge=0.0, le=1.0)
    cap_two_or_more: float = Field(ge=0.0, le=1.0)


class ExperienceConfig(Frozen):
    knee_ratio: float = Field(gt=0.0, lt=1.0)
    value_at_knee: float = Field(gt=0.0, lt=1.0)
    internship_weight: float = Field(ge=0.0, le=1.0)
    default_min_months: dict[str, float]

    @field_validator("default_min_months")
    @classmethod
    def _levels(cls, v: dict[str, float]) -> dict[str, float]:
        expected = {f"IC{i}" for i in range(7)} | {f"M{i}" for i in range(1, 7)}
        if set(v) != expected:
            raise ValueError(f"default_min_months needs exactly {sorted(expected)}")
        if any(m <= 0 for m in v.values()):
            raise ValueError("default_min_months must be positive")
        return v


class SeniorityConfig(Frozen):
    step: float = Field(ge=0.0, le=1.0)
    cross_track_penalty: float = Field(ge=0.0, le=1.0)
    cross_track_offset: int = Field(ge=0, le=6)
    penalize_overqualification: bool = False


class DomainConfig(Frozen):
    exact: float = Field(ge=0.0, le=1.0)
    same_3digit: float = Field(ge=0.0, le=1.0)
    same_2digit: float = Field(ge=0.0, le=1.0)
    unrelated: float = Field(ge=0.0, le=1.0)
    sector_groups: tuple[tuple[str, ...], ...] = ()


class EducationConfig(Frozen):
    meets: float = Field(ge=0.0, le=1.0)
    one_below: float = Field(ge=0.0, le=1.0)
    two_or_more_below: float = Field(ge=0.0, le=1.0)
    no_requirement: float = Field(ge=0.0, le=1.0)
    field_multiplier_min: float = Field(ge=0.0, le=1.0)
    cert_bonus_each: float = Field(ge=0.0, le=1.0)
    cert_bonus_cap: float = Field(ge=0.0, le=1.0)


class IdfConfig(Frozen):
    unseen_default: float = Field(ge=0.0, le=1.0)


class PostingConfig(Frozen):
    max_weighted_requirements: int = Field(ge=1, le=1000)
    overflow_weight_multiplier: float = Field(ge=0.0, le=1.0)


class RedistributionConfig(Frozen):
    required_to_preferred_when_no_required: bool = True


class ConfidenceConfig(Frozen):
    weights: dict[str, float]
    parse_quality: dict[str, float]
    requirement_count_curve: tuple[tuple[float, float], ...]
    scored_threshold: float = Field(ge=0.0, le=1.0)
    low_confidence_threshold: float = Field(ge=0.0, le=1.0)
    component_floor: float = Field(gt=0.0, lt=1.0)
    weak_component: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _valid(self) -> "ConfidenceConfig":
        expected = {
            "parse_quality",
            "taxonomy_coverage",
            "evidence_density",
            "temporal_completeness",
            "requirement_count",
            "dimension_coverage",
        }
        if set(self.weights) != expected:
            raise ValueError(f"confidence weights need exactly {sorted(expected)}")
        if any(w <= 0 for w in self.weights.values()):
            raise ValueError("confidence weights must be positive")
        if set(self.parse_quality) != {"A", "B", "C"}:
            raise ValueError("parse_quality needs tiers A, B and C")
        xs = [x for x, _ in self.requirement_count_curve]
        if len(xs) < 2 or xs != sorted(xs) or len(set(xs)) != len(xs):
            raise ValueError("requirement_count_curve needs >= 2 strictly increasing points")
        if self.low_confidence_threshold > self.scored_threshold:
            raise ValueError("low_confidence_threshold must not exceed scored_threshold")
        return self


class IntervalsConfig(Frozen):
    display_spread: int = Field(ge=0, le=100)
    unresolved_token_idf: float = Field(ge=0.0, le=1.0)


class CounterfactualConfig(Frozen):
    """SPEC §8 `to_improve` (Task 4.1): what learning a missing skill costs, and how it is added."""

    weeks_by_kind: dict[SkillKind, float]
    weeks_per_hop: float = Field(ge=0.0, le=10.0)
    unreachable_hops: int = Field(ge=0, le=10)
    added_evidence: Evidence
    added_verb: VerbClass
    max_candidates: int = Field(ge=1, le=100)

    @model_validator(mode="after")
    def _valid(self) -> "CounterfactualConfig":
        missing = set(SkillKind) - set(self.weeks_by_kind)
        if missing:
            raise ValueError(f"weeks_by_kind missing kinds: {sorted(kind.value for kind in missing)}")
        if any(not (0.0 < weeks <= 520.0) for weeks in self.weeks_by_kind.values()):
            raise ValueError("weeks_by_kind values must lie in (0, 520]")
        return self


class CalibrationConfig(Frozen):
    min_reference_n: int = Field(ge=1)
    isotonic_min_labels: int = Field(ge=1)


class TaxonomyConfig(Frozen):
    fuzzy_max_distance: int = Field(ge=0, le=2)  # SPEC §3.3 bound
    fuzzy_min_token_length: int = Field(ge=6)  # SPEC §3.3 bound
    pmi_threshold: float = Field(ge=0.0, le=1.0)
    pmi_min_cooccurrence: int = Field(ge=1)
    max_out_degree: int = Field(ge=1)
    coverage_alert_threshold: float = Field(ge=0.0, le=1.0)
    auto_promote_min_postings: int = Field(ge=1)
    auto_promote_window_days: int = Field(ge=1)
    review_queue_size: int = Field(ge=1)


class IngestConfig(Frozen):
    max_experience_tokens: int = Field(ge=1000)
    ocr_min_confidence: float = Field(ge=0.0, le=1.0)
    max_pages: int = Field(ge=1, le=10_000)  # pages read from a PDF; the rest flag TRUNCATED
    resume_likelihood_threshold: float = Field(ge=0.0, le=1.0)  # below it: NOT_A_RESUME


class ScoringConfig(Frozen):
    evidence: EvidenceConfig
    recency: RecencyConfig
    relations: RelationsConfig
    critical_miss: CriticalMissConfig
    experience: ExperienceConfig
    seniority: SeniorityConfig
    domain: DomainConfig
    education: EducationConfig
    idf: IdfConfig
    posting: PostingConfig
    redistribution: RedistributionConfig
    confidence: ConfidenceConfig
    intervals: IntervalsConfig
    counterfactual: CounterfactualConfig
    calibration: CalibrationConfig
    taxonomy: TaxonomyConfig
    ingest: IngestConfig


# ─── The whole config ─────────────────────────────────────────────────────────────────────


class EngineConfig(Frozen):
    engine_version: str = Field(min_length=1, max_length=50)
    weights: Weights
    gates: GatesConfig
    bands: BandsConfig
    verbs: VerbsConfig
    negation: NegationConfig
    scoring: ScoringConfig

    @cached_property
    def content_hash(self) -> str:
        """sha256 of the validated config (16 hex chars), persisted with every score."""
        return hashlib.sha256(canonical_json(self).encode("utf-8")).hexdigest()[:16]


def config_hash(config: EngineConfig) -> str:
    return config.content_hash


def combined_hash(config: EngineConfig, graph_version: str) -> str:
    """The hash a score is persisted with: config + skill-graph artifacts (SPEC §0.4)."""
    digest = hashlib.sha256(f"{config.content_hash}|{graph_version}".encode("utf-8"))
    return digest.hexdigest()[:16]


def replace_weights(config: EngineConfig, dimensions: dict[Union[DimensionName, str], float]) -> EngineConfig:
    """A copy of `config` with new dimension weights, validated (tuning, sensitivity runs)."""
    weights = Weights(dimensions={DimensionName(k): float(v) for k, v in dimensions.items()})
    # A fresh model, never model_copy: a copy would carry the cached content_hash of the old
    # weights into the new config.
    fields = {name: getattr(config, name) for name in EngineConfig.model_fields}
    fields["weights"] = weights
    return EngineConfig(**fields)


_FILES = ("weights", "gates", "bands", "verbs", "negation", "scoring")


def _read_yaml(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"unreadable YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a mapping at the top level")
    return data


def load_config(config_dir: Optional[Union[str, Path]] = None) -> EngineConfig:
    """Read and validate every config file. Fails loudly (SPEC §6: an assertion at load)."""
    root = Path(config_dir) if config_dir is not None else default_config_dir()
    raw = {name: _read_yaml(root / f"{name}.yaml") for name in _FILES}
    version_file = root / "engine_version.txt"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ConfigError(f"missing {version_file}") from exc
    try:
        return EngineConfig(
            engine_version=version,
            weights=Weights(**raw["weights"]),
            gates=GatesConfig(**raw["gates"]),
            bands=BandsConfig(**raw["bands"]),
            verbs=VerbsConfig(**raw["verbs"]),
            negation=NegationConfig(**raw["negation"]),
            scoring=ScoringConfig(**raw["scoring"]),
        )
    except ValidationError as exc:
        raise ConfigError(f"invalid config in {root}: {exc}") from exc
