"""The posting parser's tables: `config/parse/posting.yaml` validated (Task 6.5).

`PostingTable` is one table of `ParseConfig` (loaded with the others by `load_parse_config`, so its
content is part of `ParseConfig.version`). Phrases are compared as folded words (`phrase_words`);
`*_patterns` are regular expressions compiled case-insensitively here, so a pattern that does not
compile, or a `{country}` placeholder in a table that has none, is refused at load. Read by
`matcher.parse.posting` only.
"""

from __future__ import annotations

import re
from functools import cached_property
from typing import Optional

from pydantic import Field, field_validator, model_validator

from matcher.models import DegreeLevel, Frozen, SeniorityLevel
from matcher.parse._text import phrase_words

__all__ = [
    "BoilerplateRules",
    "ClearanceLevel",
    "ClearanceTable",
    "ContradictionRules",
    "EducationPhrases",
    "ModalTable",
    "OrGroupTable",
    "PostingSections",
    "PostingTable",
    "TruncationRules",
    "UnresolvedTable",
    "WorkAuthTable",
    "YearsTable",
]

_FLAGS = re.IGNORECASE


def _compile(pattern: str, *, country: Optional[str] = None) -> re.Pattern[str]:
    text = pattern if country is None else pattern.replace("{country}", country)
    try:
        return re.compile(text, _FLAGS)
    except re.error as exc:
        raise ValueError(f"pattern does not compile: {pattern!r}: {exc}") from exc


def _phrases(values: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """Folded word tuples, longest first, empty phrases dropped."""
    return tuple(sorted({phrase_words(v) for v in values if phrase_words(v)}, key=lambda p: (-len(p), p)))


def _check_patterns(values: tuple[str, ...], *, placeholder_ok: bool) -> tuple[str, ...]:
    for value in values:
        if "{country}" in value and not placeholder_ok:
            raise ValueError(f"{{country}} is not allowed here: {value!r}")
        _compile(value, country="(?:x)")
    return values


class PostingSections(Frozen):
    max_header_words: int = Field(ge=1, le=20)
    filler: tuple[str, ...] = ()
    preferred: tuple[str, ...]
    required: tuple[str, ...]
    responsibilities: tuple[str, ...]
    about: tuple[str, ...]
    ignored: tuple[str, ...]

    @model_validator(mode="after")
    def _unique(self) -> "PostingSections":
        seen: dict[tuple[str, ...], str] = {}
        for kind in ("preferred", "required", "responsibilities", "about", "ignored"):
            for phrase in getattr(self, kind):
                key = phrase_words(phrase)
                if not key:
                    raise ValueError(f"posting header {phrase!r} has no words")
                if seen.get(key, kind) != kind:
                    raise ValueError(f"posting header {phrase!r} listed under two section kinds")
                seen[key] = kind
        return self

    @cached_property
    def index(self) -> dict[tuple[str, ...], str]:
        """header words -> kind."""
        out: dict[tuple[str, ...], str] = {}
        for kind in ("preferred", "required", "responsibilities", "about", "ignored"):
            for phrase in getattr(self, kind):
                out.setdefault(phrase_words(phrase), kind)
        return out

    @cached_property
    def stripped_index(self) -> dict[tuple[str, ...], tuple[int, str]]:
        """header words without filler -> (length of the phrase, kind); the longest phrase, then the
        kind listed first, wins."""
        order = {"preferred": 0, "required": 1, "responsibilities": 2, "about": 3, "ignored": 4}
        filler = self.filler_words
        out: dict[tuple[str, ...], tuple[int, str]] = {}
        for words_, kind in self.index.items():
            key = tuple(w for w in words_ if w not in filler)
            if not key:
                continue
            best = out.get(key)
            if best is None or (len(words_), -order[kind]) > (best[0], -order[best[1]]):
                out[key] = (len(words_), kind)
        return out

    @cached_property
    def filler_words(self) -> frozenset[str]:
        return frozenset(w for f in self.filler for w in phrase_words(f)) | frozenset(
            f for f in self.filler if not phrase_words(f))


class ModalTable(Frozen):
    required: tuple[str, ...]
    preferred: tuple[str, ...]
    implied: tuple[str, ...] = ()
    negated: tuple[str, ...] = ()

    @cached_property
    def tuples(self) -> dict[str, tuple[tuple[str, ...], ...]]:
        return {kind: _phrases(getattr(self, kind)) for kind in ("required", "preferred", "implied", "negated")}


class OrGroupTable(Frozen):
    lead_ins: tuple[str, ...] = ()
    equivalent_words: tuple[str, ...] = ()

    @cached_property
    def lead_in_tuples(self) -> tuple[tuple[str, ...], ...]:
        return _phrases(self.lead_ins)

    @cached_property
    def equivalent_set(self) -> frozenset[str]:
        return frozenset(w for v in self.equivalent_words for w in phrase_words(v))


class YearsTable(Frozen):
    number_words: dict[str, int] = Field(default_factory=dict)
    not_experience: tuple[str, ...] = ()
    experience_words: tuple[str, ...] = ()

    @field_validator("number_words")
    @classmethod
    def _numbers(cls, v: dict[str, int]) -> dict[str, int]:
        if any(n < 0 or n > 60 for n in v.values()):
            raise ValueError("years.number_words must be 0..60")
        return v

    @cached_property
    def not_experience_set(self) -> frozenset[str]:
        return frozenset(w for v in self.not_experience for w in phrase_words(v))

    @cached_property
    def experience_set(self) -> frozenset[str]:
        return frozenset(w for v in self.experience_words for w in phrase_words(v))


class EducationPhrases(Frozen):
    phrases: dict[DegreeLevel, tuple[str, ...]] = Field(default_factory=dict)
    cues: tuple[str, ...] = ()
    degree_in_field: DegreeLevel = DegreeLevel.BACHELOR
    equivalent: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _valid(self) -> "EducationPhrases":
        if DegreeLevel.NONE in self.phrases or self.degree_in_field is DegreeLevel.NONE:
            raise ValueError("education: NONE is the absence of a degree, never a degree phrase")
        return self

    @cached_property
    def phrase_tuples(self) -> tuple[tuple[tuple[str, ...], DegreeLevel], ...]:
        pairs = {(phrase_words(p), level) for level, values in self.phrases.items() for p in values if phrase_words(p)}
        return tuple(sorted(pairs, key=lambda pair: (-len(pair[0]), pair[0], pair[1].rank)))

    @cached_property
    def cue_set(self) -> frozenset[str]:
        return frozenset(w for v in self.cues for w in phrase_words(v))

    @cached_property
    def equivalent_tuples(self) -> tuple[tuple[str, ...], ...]:
        return _phrases(self.equivalent)


class WorkAuthTable(Frozen):
    authorized_patterns: tuple[str, ...] = ()
    requirement_words: tuple[str, ...] = ()
    sponsorship_refused_patterns: tuple[str, ...] = ()
    sponsorship_offered_patterns: tuple[str, ...] = ()
    citizenship_patterns: tuple[str, ...] = ()
    citizenship_words: tuple[str, ...] = ()
    resident_patterns: tuple[str, ...] = ()

    @field_validator("authorized_patterns", "citizenship_patterns")
    @classmethod
    def _with_country(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_patterns(v, placeholder_ok=True)

    @field_validator("sponsorship_refused_patterns", "sponsorship_offered_patterns", "resident_patterns")
    @classmethod
    def _plain(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_patterns(v, placeholder_ok=False)

    @cached_property
    def requirement_set(self) -> frozenset[str]:
        return frozenset(w for v in self.requirement_words for w in phrase_words(v))

    @cached_property
    def citizenship_set(self) -> frozenset[str]:
        return frozenset(w for v in self.citizenship_words for w in phrase_words(v))


class ClearanceLevel(Frozen):
    suffix: str = Field(pattern=r"^[A-Z0-9]+(?:_[A-Z0-9]+)*$")
    countries: tuple[str, ...] = Field(min_length=1)
    patterns: tuple[str, ...] = Field(min_length=1)

    @field_validator("countries")
    @classmethod
    def _countries(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if any(not re.fullmatch(r"[A-Z]{2}", c) for c in v):
            raise ValueError(f"clearance countries are ISO codes: {v}")
        return v

    @field_validator("patterns")
    @classmethod
    def _patterns(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_patterns(v, placeholder_ok=False)

    @cached_property
    def compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(_compile(p) for p in self.patterns)


class ClearanceTable(Frozen):
    context_patterns: tuple[str, ...] = Field(min_length=1)
    levels: tuple[ClearanceLevel, ...] = ()
    country_patterns: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    active_patterns: tuple[str, ...] = ()
    obtainable_patterns: tuple[str, ...] = ()
    negation_patterns: tuple[str, ...] = ()

    @field_validator("context_patterns", "active_patterns", "obtainable_patterns", "negation_patterns")
    @classmethod
    def _plain(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return _check_patterns(v, placeholder_ok=False)

    @field_validator("country_patterns")
    @classmethod
    def _countries(cls, v: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for code, patterns in v.items():
            if not re.fullmatch(r"[A-Z]{2}", code):
                raise ValueError(f"clearance country {code!r} is not an ISO code")
            _check_patterns(patterns, placeholder_ok=False)
        return v

    @cached_property
    def compiled(self) -> dict[str, tuple[re.Pattern[str], ...]]:
        out = {name: tuple(_compile(p) for p in getattr(self, f"{name}_patterns"))
               for name in ("context", "active", "obtainable", "negation")}
        return out

    @cached_property
    def country_compiled(self) -> tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]:
        return tuple((code, tuple(_compile(p) for p in patterns)) for code, patterns in sorted(self.country_patterns.items()))


class UnresolvedTable(Frozen):
    triggers: tuple[str, ...] = ()
    lead_words: tuple[str, ...] = ()
    generic_words: tuple[str, ...] = ()
    acronym_stoplist: tuple[str, ...] = ()

    @cached_property
    def trigger_tuples(self) -> tuple[tuple[str, ...], ...]:
        return _phrases(self.triggers)

    @cached_property
    def lead_set(self) -> frozenset[str]:
        return frozenset(w for v in self.lead_words for w in phrase_words(v))

    @cached_property
    def generic_set(self) -> frozenset[str]:
        return frozenset(w for v in self.generic_words for w in phrase_words(v))

    @cached_property
    def acronym_set(self) -> frozenset[str]:
        return frozenset(v.upper() for v in self.acronym_stoplist)


class TruncationRules(Frozen):
    caps: tuple[int, ...] = ()
    tolerance: int = Field(default=0, ge=0, le=1000)
    markers: tuple[str, ...] = ()


class BoilerplateRules(Frozen):
    max_requirements: int = Field(ge=0, le=1000)
    min_share: float = Field(ge=0.0, le=1.0)


class ContradictionRules(Frozen):
    max_level: str
    min_years: int = Field(ge=0, le=60)

    @field_validator("max_level")
    @classmethod
    def _level(cls, v: str) -> str:
        from matcher.parse.config import parse_level  # the one level parser (no import cycle at load)

        parse_level(v)
        return v

    @cached_property
    def level(self) -> SeniorityLevel:
        from matcher.parse.config import parse_level

        return parse_level(self.max_level)


class PostingTable(Frozen):
    sections: PostingSections
    modals: ModalTable
    credential_holding: tuple[str, ...] = ()
    or_groups: OrGroupTable = OrGroupTable()
    years: YearsTable = YearsTable()
    education: EducationPhrases = EducationPhrases()
    countries: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    work_authorization: WorkAuthTable = WorkAuthTable()
    security_clearance: ClearanceTable
    unresolved: UnresolvedTable = UnresolvedTable()
    truncation: TruncationRules = TruncationRules()
    boilerplate: BoilerplateRules
    contradiction: ContradictionRules
    max_chars: int = Field(ge=1000, le=1_000_000)

    @field_validator("countries")
    @classmethod
    def _countries(cls, v: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
        for code, patterns in v.items():
            if not re.fullmatch(r"[A-Z]{2}", code):
                raise ValueError(f"country {code!r} is not an ISO code")
            _check_patterns(patterns, placeholder_ok=False)
        return v

    @cached_property
    def holding_tuples(self) -> tuple[tuple[str, ...], ...]:
        return _phrases(self.credential_holding)

    @cached_property
    def country_alternation(self) -> str:
        """One non-capturing alternation of every country pattern, each in a named group."""
        parts = [f"(?P<c_{code}_{i}>{pattern})" for code, patterns in sorted(self.countries.items())
                 for i, pattern in enumerate(patterns)]
        return "(?:" + "|".join(parts) + ")" if parts else "(?!x)x"

    def _with_countries(self, patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
        return tuple(_compile(p, country=self.country_alternation) for p in patterns)

    @cached_property
    def authorized_compiled(self) -> tuple[re.Pattern[str], ...]:
        return self._with_countries(self.work_authorization.authorized_patterns)

    @cached_property
    def citizenship_compiled(self) -> tuple[re.Pattern[str], ...]:
        return self._with_countries(self.work_authorization.citizenship_patterns)

    @cached_property
    def refused_compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(_compile(p) for p in self.work_authorization.sponsorship_refused_patterns)

    @cached_property
    def offered_compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(_compile(p) for p in self.work_authorization.sponsorship_offered_patterns)

    @cached_property
    def resident_compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(_compile(p) for p in self.work_authorization.resident_patterns)

    @cached_property
    def country_compiled(self) -> tuple[tuple[str, re.Pattern[str]], ...]:
        return tuple((code, _compile(p)) for code, patterns in sorted(self.countries.items()) for p in patterns)
