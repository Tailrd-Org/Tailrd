"""Parse configuration: the rules tables the résumé and posting parsers read (Task 6.3).

Three YAML files under `config/parse/` (one directory, read once by `load_parse_config`):

* `sections.yaml`: section-header synonyms in English and French, the filler words a header may
  carry, composite joiners, and the education vocabulary (degree words per level, institution
  words, in-progress markers, labelled lines that are not degrees).
* `seniority.yaml`: the SPEC §5.4 title rules (first match wins), posting metadata levels,
  internship markers and student organisations.
* `industries.yaml`: the curated company -> NAICS table and the keyword rules.

Like `matcher.config`, the files are validated into frozen models and the `version` is a hash of
the validated content (16 hex characters), never of the bytes: comments and formatting change
nothing, any table change changes it. Top-level keys starting with "_" are YAML anchors for reuse
inside the file and are not part of a table. Parse functions receive a `ParseConfig` and never
read a file themselves.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from functools import cached_property
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from matcher.config import ConfigError, default_config_dir
from matcher.models import DegreeLevel, Frozen, SeniorityLevel, Track
from matcher.parse._text import APOSTROPHES, company_key, fold, phrase_words, words

__all__ = [
    "CompanyEntry",
    "DegreeTerms",
    "EducationTable",
    "HeaderTerms",
    "IndustriesTable",
    "IndustryRule",
    "ParseConfig",
    "PhraseList",
    "SectionKind",
    "SectionsTable",
    "SeniorityRule",
    "SeniorityTable",
    "default_parse_config_dir",
    "header_key",
    "load_parse_config",
    "parse_level",
]

FORMAT = 1
_FILES = ("sections", "seniority", "industries")


class SectionKind(str, Enum):
    """What a résumé section holds (re-exported by `matcher.parse.sections`)."""

    CONTACT = "CONTACT"
    SUMMARY = "SUMMARY"
    EXPERIENCE = "EXPERIENCE"
    EDUCATION = "EDUCATION"
    SKILLS = "SKILLS"
    PROJECTS = "PROJECTS"
    CERTIFICATIONS = "CERTIFICATIONS"
    COURSEWORK = "COURSEWORK"
    PUBLICATIONS = "PUBLICATIONS"
    AWARDS = "AWARDS"
    VOLUNTEER = "VOLUNTEER"
    LANGUAGES = "LANGUAGES"
    INTERESTS = "INTERESTS"
    REFERENCES = "REFERENCES"
    OTHER = "OTHER"


# ─── header keys ────────────────────────────────────────────────────────────────────────────

_APOSTROPHE_DROP = str.maketrans({ch: None for ch in APOSTROPHES})


def header_key(text: str) -> str:
    """A header as the synonym table compares it: folded, apostrophes dropped ("Where I've Been"
    -> "where ive been"), "&" read as "and", punctuation ignored, words joined by one space."""
    return " ".join(words(text.replace("&", " and ").translate(_APOSTROPHE_DROP)))


# ─── sections.yaml ──────────────────────────────────────────────────────────────────────────


class HeaderTerms(Frozen):
    en: tuple[str, ...] = ()
    fr: tuple[str, ...] = ()


class DegreeTerms(Frozen):
    terms: tuple[str, ...] = ()
    cased: tuple[str, ...] = ()


class EducationTable(Frozen):
    degrees: dict[DegreeLevel, DegreeTerms]
    cased_not_before: tuple[str, ...] = ()
    location_like: tuple[str, ...] = ()
    month_like: tuple[str, ...] = ()
    institutions: dict[DegreeLevel, tuple[str, ...]] = Field(default_factory=dict)
    institution_words: tuple[str, ...] = ()
    no_inference: tuple[str, ...] = ()
    in_progress: tuple[str, ...] = ()
    skip_labels: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid(self) -> "EducationTable":
        if DegreeLevel.NONE in self.degrees or DegreeLevel.NONE in self.institutions:
            raise ValueError("education: NONE is the absence of a degree, never a degree word")
        if not any(terms.terms or terms.cased for terms in self.degrees.values()):
            raise ValueError("education: no degree words")
        return self


class SectionsTable(Frozen):
    max_header_words: int = Field(ge=1, le=20)
    headers: dict[SectionKind, HeaderTerms]
    fillers: HeaderTerms = HeaderTerms()
    joiners: tuple[str, ...] = ()
    entry_sections: tuple[SectionKind, ...] = ()
    education: EducationTable

    @model_validator(mode="after")
    def _unique(self) -> "SectionsTable":
        _ = self.header_index  # builds the index; raises on a key listed under two kinds
        return self

    @cached_property
    def header_index(self) -> dict[str, tuple[SectionKind, Optional[str]]]:
        """header key -> (kind, language or None when both languages list it)."""
        index: dict[str, tuple[SectionKind, Optional[str]]] = {}
        for kind, terms in self.headers.items():
            for lang, listed in (("en", terms.en), ("fr", terms.fr)):
                for term in listed:
                    key = header_key(term)
                    if not key:
                        raise ValueError(f"sections: empty header {term!r} under {kind.value}")
                    known = index.get(key)
                    if known is not None and known[0] is not kind:
                        raise ValueError(
                            f"sections: header {term!r} is listed under two section kinds "
                            f"({known[0].value}, {kind.value})"
                        )
                    index[key] = (kind, lang if known is None or known[1] == lang else None)
        return index

    @cached_property
    def filler_words(self) -> frozenset[str]:
        return frozenset(word for term in (*self.fillers.en, *self.fillers.fr) for word in words(term))

    @cached_property
    def joiner_words(self) -> frozenset[str]:
        return frozenset(fold(j) for j in self.joiners if words(j))

    @cached_property
    def joiner_symbols(self) -> tuple[str, ...]:
        return tuple(j for j in self.joiners if not words(j))


# ─── seniority.yaml ─────────────────────────────────────────────────────────────────────────

_LEVEL = re.compile(r"(IC)([0-6])|(M)([1-6])")


def parse_level(code: str) -> SeniorityLevel:
    """"IC3" -> SeniorityLevel(IC, 3); ValueError for anything else."""
    found = _LEVEL.fullmatch(code.strip()) if isinstance(code, str) else None
    if found is None:
        raise ValueError(f"unknown seniority level {code!r} (expected IC0..IC6 or M1..M6)")
    if found.group(1):
        return SeniorityLevel(track=Track.IC, level=int(found.group(2)))
    return SeniorityLevel(track=Track.MGMT, level=int(found.group(4)))


class SeniorityRule(Frozen):
    level: str
    phrases: tuple[str, ...] = ()
    cased: tuple[str, ...] = ()
    at_end: bool = False
    whole: bool = False
    unless_followed_by: tuple[str, ...] = ()
    unless_preceded_by: tuple[str, ...] = ()
    preceded_by: tuple[str, ...] = ()
    followed_within: tuple[str, ...] = ()
    with_any: tuple[str, ...] = ()
    modifier: bool = False

    @field_validator("level")
    @classmethod
    def _level(cls, v: str) -> str:
        parse_level(v)
        return v

    @model_validator(mode="after")
    def _has_words(self) -> "SeniorityRule":
        if not self.phrases and not self.cased:
            raise ValueError(f"seniority rule {self.level}: no phrases and no cased words")
        if any(not phrase_words(p) for p in self.phrases):
            raise ValueError(f"seniority rule {self.level}: an empty phrase")
        return self

    @cached_property
    def seniority(self) -> SeniorityLevel:
        return parse_level(self.level)

    @cached_property
    def phrase_tuples(self) -> tuple[tuple[str, ...], ...]:
        return tuple(sorted({phrase_words(p) for p in self.phrases}, key=lambda p: (-len(p), p)))

    @cached_property
    def sets(self) -> dict[str, frozenset[str]]:
        def folded(values: tuple[str, ...]) -> frozenset[str]:
            return frozenset(fold(v) for v in values)

        return {
            "unless_followed_by": folded(self.unless_followed_by),
            "unless_preceded_by": folded(self.unless_preceded_by),
            "preceded_by": folded(self.preceded_by),
            "followed_within": folded(self.followed_within),
            "with_any": folded(self.with_any),
        }


class PhraseList(Frozen):
    phrases: tuple[str, ...]
    unless_followed_by: tuple[str, ...] = ()
    exceptions: tuple[str, ...] = ()

    @cached_property
    def phrase_tuples(self) -> tuple[tuple[str, ...], ...]:
        return tuple(sorted({phrase_words(p) for p in self.phrases if phrase_words(p)}, key=lambda p: (-len(p), p)))

    @cached_property
    def exception_tuples(self) -> tuple[tuple[str, ...], ...]:
        return tuple(sorted({phrase_words(p) for p in self.exceptions if phrase_words(p)}))

    @cached_property
    def not_before(self) -> frozenset[str]:
        return frozenset(fold(v) for v in self.unless_followed_by)


class SeniorityTable(Frozen):
    rules: tuple[SeniorityRule, ...] = Field(min_length=1)
    meta_levels: dict[str, str] = Field(default_factory=dict)
    internship: PhraseList
    student_organisations: PhraseList

    @field_validator("meta_levels")
    @classmethod
    def _meta(cls, v: dict[str, str]) -> dict[str, str]:
        for level in v.values():
            parse_level(level)
        return v

    @cached_property
    def meta_index(self) -> dict[str, SeniorityLevel]:
        return {fold(key).strip(): parse_level(level) for key, level in self.meta_levels.items()}


# ─── industries.yaml ────────────────────────────────────────────────────────────────────────

_NAICS = re.compile(r"[0-9]{2,6}|[0-9]{2}-[0-9]{2}")


def _naics_codes(codes: tuple[str, ...]) -> tuple[str, ...]:
    for code in codes:
        if not isinstance(code, str) or not _NAICS.fullmatch(code):
            raise ValueError(f"{code!r} is not a NAICS code (2 to 6 digits, or a sector such as 44-45)")
    return codes


class CompanyEntry(Frozen):
    name: str = Field(min_length=1, max_length=200)
    aliases: tuple[str, ...] = ()
    naics: tuple[str, ...] = Field(min_length=1, max_length=5)

    @field_validator("naics")
    @classmethod
    def _codes(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _naics_codes(v)


class IndustryRule(Frozen):
    naics: tuple[str, ...] = Field(min_length=1, max_length=5)
    phrases: tuple[str, ...] = ()
    name_only: tuple[str, ...] = ()

    @field_validator("naics")
    @classmethod
    def _codes(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _naics_codes(v)

    @model_validator(mode="after")
    def _has_phrases(self) -> "IndustryRule":
        if not self.phrases and not self.name_only:
            raise ValueError(f"industry rule {self.naics}: no phrases")
        return self

    @cached_property
    def context_tuples(self) -> tuple[tuple[str, ...], ...]:
        return tuple(phrase_words(p) for p in self.phrases if phrase_words(p))

    @cached_property
    def name_tuples(self) -> tuple[tuple[str, ...], ...]:
        return self.context_tuples + tuple(phrase_words(p) for p in self.name_only if phrase_words(p))


class IndustriesTable(Frozen):
    legal_suffixes: tuple[str, ...] = ()
    name_qualifiers: tuple[str, ...] = ()
    companies: tuple[CompanyEntry, ...] = ()
    rules: tuple[IndustryRule, ...] = ()

    @model_validator(mode="after")
    def _unique(self) -> "IndustriesTable":
        _ = self.company_index
        return self

    @cached_property
    def company_index(self) -> dict[str, tuple[str, ...]]:
        """normalised company name or alias -> NAICS codes."""
        index: dict[str, tuple[str, ...]] = {}
        for company in self.companies:
            for name in (company.name, *company.aliases):
                key = company_key(name, self.legal_suffixes)
                if not key:
                    raise ValueError(f"industries: company name {name!r} normalises to nothing")
                if index.get(key, company.naics) != company.naics:
                    raise ValueError(f"industries: {name!r} maps to two different code lists")
                index[key] = company.naics
        return index

    @cached_property
    def qualifier_tuples(self) -> tuple[tuple[str, ...], ...]:
        return tuple(sorted({tuple(company_key(q).split()) for q in self.name_qualifiers if company_key(q)},
                            key=lambda q: (-len(q), q)))


# ─── the whole parse config ─────────────────────────────────────────────────────────────────


class ParseConfig(Frozen):
    version: str = Field(min_length=1, max_length=64)
    sections: SectionsTable
    seniority: SeniorityTable
    industries: IndustriesTable


def default_parse_config_dir() -> Path:
    """match-engine/config/parse"""
    return default_config_dir() / "parse"


def _read(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"missing parse config file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"unreadable YAML in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path} must hold a mapping at the top level")
    if data.get("format") != FORMAT:
        raise ConfigError(f"{path}: unsupported format {data.get('format')!r} (expected {FORMAT})")
    return {key: value for key, value in data.items() if key != "format" and not str(key).startswith("_")}


def _version(tables: dict[str, Frozen]) -> str:
    payload = {name: table.model_dump(mode="json") for name, table in tables.items()}
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def load_parse_config(config_dir: Optional[Union[str, Path]] = None) -> ParseConfig:
    """Read and validate `sections.yaml`, `seniority.yaml` and `industries.yaml`.

    `config_dir` is the directory holding them (default: match-engine/config/parse) or the engine
    config directory above it (match-engine/config). Raises `matcher.config.ConfigError` on a
    missing file, unreadable YAML or a table that breaks an invariant.
    """
    root = Path(config_dir) if config_dir is not None else default_parse_config_dir()
    if not (root / "sections.yaml").exists() and (root / "parse" / "sections.yaml").exists():
        root = root / "parse"
    raw = {name: _read(root / f"{name}.yaml") for name in _FILES}
    try:
        tables: dict[str, Frozen] = {
            "sections": SectionsTable(**raw["sections"]),
            "seniority": SeniorityTable(**raw["seniority"]),
            "industries": IndustriesTable(**raw["industries"]),
        }
    except (ValidationError, ValueError, TypeError) as exc:
        raise ConfigError(f"invalid parse config in {root}: {exc}") from exc
    return ParseConfig(version=_version(tables), **tables)
