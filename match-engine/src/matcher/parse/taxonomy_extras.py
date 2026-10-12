"""The extra artifacts of Task 2.2 as the parsers read them: job titles and military codes.

Both are read through the taxonomy MANIFEST (`matcher.graph.loader.load_json_artifact` verifies
the sha256 of the document it returns), never by a pinned path or hash.

`TitleIndex`: normalised job title -> O*NET occupation (`titles.json`: SOC code, SOC major group,
job zone). A title is looked up whole, then without its seniority modifiers ("Senior Software
Engineer"), then in O*NET's inverted form ("Engineer, Software"), then by the head before a team or
specialty qualifier ("Software Developer, Payments"); each form also with its hyphen variants
("Front-End Developer"). Generic titles ("Intern", "Engineer") were left out of the artifact at
build time (DECISIONS D-26) and stay unknown.

`MosIndex`: military occupation codes (`mos_crosswalk.json`; SPEC §14.3, DECISIONS D-29). SPEC
§14.3 calls veterans a legally sensitive population to get right, and many codes are ordinary
tokens ("IT", "HR", "AWS", "100", "0311"), so a code counts only when
* it is labelled: "MOS 25B", "AFSC 3D0X2", "MOC: 0311" (and "NEC", "Rating" in a military role),
* or, in a military role, it mixes letters and digits ("68W", "3D0X2", "Combat Medic (68W)"),
* or, in a military role, its line shares a word with the crosswalk's title for it ("Rifleman
  (0311)", "Hospital Corpsman (HM)", "Infantry Rifleman 0311"; never "Provided IT support to 300
  Marines" or "Human Resources (HR)", whose words are not those of "Information Systems
  Technician" or "Hospitalman Recruit").
A military role names a service, a unit or a rank in the text or the `context` (the employer). When
the role names a branch, a code another service uses differently resolves to that branch's entry
("IT" in the Coast Guard). A code is reported once, in order of appearance, with the crosswalk's
skill ids (possibly none: D-29 keeps codes without O*NET skill data, with a note).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional, Union

from pydantic import ValidationError

from matcher.graph.aliases import normalize_surface, surface_variants
from matcher.graph.loader import ArtifactError, load_json_artifact
from matcher.models import Frozen
from matcher.parse._text import words
from matcher.parse.config import ParseConfig, load_parse_config
from matcher.parse.seniority import strip_seniority_modifiers

__all__ = ["MosIndex", "TitleIndex", "TitleInfo", "load_mos_index", "load_naics_codes", "load_title_index"]

TITLES_JSON = "titles.json"
MOS_JSON = "mos_crosswalk.json"
NAICS_JSON = "naics.json"


class TitleInfo(Frozen):
    soc: str
    family: str
    job_zone: Optional[int]


# ─── titles ─────────────────────────────────────────────────────────────────────────────────

_BRACKETED = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_HEAD = re.compile(r"\s*(?:,|\||;|:|/|\s[-\u2013\u2014]+\s|\s@\s|\s(?:at|chez)\s)")


class TitleIndex:
    """normalised title -> TitleInfo; see the module docstring for the lookup order."""

    def __init__(self, titles: Mapping[str, Mapping[str, Any]], parse_config: ParseConfig) -> None:
        self._raw = titles
        self._parse_config = parse_config
        self._cache: dict[str, Optional[TitleInfo]] = {}

    def __len__(self) -> int:
        return len(self._raw)

    def _info(self, key: str) -> Optional[TitleInfo]:
        if key in self._cache:
            return self._cache[key]
        record = self._raw.get(key)
        info: Optional[TitleInfo] = None
        if isinstance(record, Mapping):
            try:
                info = TitleInfo(soc=record["soc"], family=record.get("family") or "", job_zone=record.get("job_zone"))
            except (KeyError, TypeError, ValidationError):
                info = None
        self._cache[key] = info
        return info

    def _forms(self, title: str) -> list[str]:
        strip = self._parse_config
        unbracketed = " ".join(_BRACKETED.sub(" ", title).split())
        forms = [title, strip_seniority_modifiers(title, strip), unbracketed,
                 strip_seniority_modifiers(unbracketed, strip)]
        parts = [part.strip() for part in unbracketed.split(",")]
        if len(parts) == 2 and all(parts):  # O*NET writes "Engineer, Software"
            inverted = f"{parts[1]} {parts[0]}"
            forms += [inverted, strip_seniority_modifiers(inverted, strip)]
        head = _HEAD.split(unbracketed, maxsplit=1)[0]
        forms += [head, strip_seniority_modifiers(head, strip)]
        return forms

    def lookup(self, title: str) -> Optional[TitleInfo]:
        if not isinstance(title, str) or not title.strip():
            return None
        seen: set[str] = set()
        for form in self._forms(title):
            normalized = normalize_surface(form)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            for key in surface_variants(normalized):
                info = self._info(key)
                if info is not None:
                    return info
        return None

    def canonical_id(self, title: str) -> Optional[str]:
        """"title.<soc>" (e.g. "title.15-1252.00"), or None when the title is unknown."""
        info = self.lookup(title)
        return None if info is None else f"title.{info.soc}"


def load_title_index(taxonomy_dir: Union[str, Path], *, parse_config: Optional[ParseConfig] = None) -> TitleIndex:
    """`titles.json` from `taxonomy_dir`, verified against its MANIFEST. The seniority modifiers come
    from `parse_config` (default: the committed config/parse tables)."""
    document = load_json_artifact(taxonomy_dir, TITLES_JSON)
    titles = document.get("titles")
    if not isinstance(titles, dict):
        raise ArtifactError(f"{TITLES_JSON}: 'titles' must map titles to occupations")
    return TitleIndex(titles, parse_config if parse_config is not None else load_parse_config())


# ─── military codes ─────────────────────────────────────────────────────────────────────────

_CODE = re.compile(r"(?<![A-Za-z0-9&])([A-Z0-9][A-Z0-9&]{1,6})(?![A-Za-z0-9&])")
_LABEL = re.compile(
    r"(?i:\b(?P<label>MOS(?:ID)?|MOC|AFSC|NEC|rating|rate|military\s+occupational\s+specialty|"
    r"military\s+occupation(?:al)?\s+code))\s*[:#\-]?\s*\(?\s*$"
)
_AMBIGUOUS_LABELS = frozenset({"nec", "rating", "rate"})  # a military role must say so
_CHAIN = re.compile(r"\s*(?:/|,|&|\band\b|\bor\b)\s*$", re.IGNORECASE)
_MILITARY_WORDS = re.compile(
    r"\b(?:army|navy|naval|marine\s+corps|marines|air\s+force|space\s+force|coast\s+guard|national\s+guard|"
    r"armed\s+forces|military|canadian\s+forces|forces\s+arm[ée]es|enlisted|soldiers?|sailors?|airm[ae]n|"
    r"corpsman|infantry|battalion|regiment|brigade|platoon|squadron)\b",
    re.IGNORECASE,
)
_MILITARY_ACRONYMS = re.compile(r"\b(?:MOS|MOC|AFSC|USMC|USAF|USSF|USCG|USN|CAF|RCAF|RCN|NCO|U\.S\.M\.C\.?)\b")
_BRANCHES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("marine_corps", re.compile(r"\b(?:marine\s+corps|marines|usmc|u\.s\.m\.c)\b", re.IGNORECASE)),
    ("coast_guard", re.compile(r"\b(?:coast\s+guard|uscg)\b", re.IGNORECASE)),
    ("space_force", re.compile(r"\b(?:space\s+force|ussf)\b", re.IGNORECASE)),
    ("air_force", re.compile(r"\b(?:air\s+force|usaf)\b", re.IGNORECASE)),
    ("navy", re.compile(r"\b(?:navy|naval|usn)\b", re.IGNORECASE)),
    ("army", re.compile(r"\barmy\b", re.IGNORECASE)),
)
# Words too common in crosswalk titles to tie a line to one code.
_TITLE_STOPWORDS = frozenset({
    "specialist", "technician", "operator", "other", "general", "basic", "senior", "apprentice", "helper",
    "manager", "officer", "assistant", "journeyman", "craftsman", "supervisor", "chief", "master", "leader",
    "member", "personnel", "advanced", "with", "from", "into", "under", "over", "staff", "conversion",
    "course", "school", "level", "class", "first", "second", "third", "entry", "unit", "systems",
})


def _military(text: str) -> bool:
    return _MILITARY_WORDS.search(text) is not None or _MILITARY_ACRONYMS.search(text) is not None


def _branches(text: str) -> frozenset[str]:
    return frozenset(name for name, pattern in _BRANCHES if pattern.search(text))


def _content_words(text: str) -> frozenset[str]:
    return frozenset(word for word in words(text) if len(word) >= 4 and word not in _TITLE_STOPWORDS)


class MosIndex:
    """Military occupation code -> crosswalk skill ids; see the module docstring for the rules."""

    def __init__(self, codes: Mapping[str, Mapping[str, Any]]) -> None:
        self._codes = codes

    def __len__(self) -> int:
        return len(self._codes)

    def _entries(self, code: str) -> list[Mapping[str, Any]]:
        primary = self._codes.get(code)
        if not isinstance(primary, Mapping):
            return []
        alternatives = primary.get("alternatives") or []
        return [primary, *(alt for alt in alternatives if isinstance(alt, Mapping))]

    def _title_words(self, entries: list[Mapping[str, Any]]) -> frozenset[str]:
        out: set[str] = set()
        for entry in entries:
            title = entry.get("title")
            if isinstance(title, str):
                out |= _content_words(title)
        return frozenset(out)

    def find(self, text: str, *, context: str = "") -> tuple[tuple[str, tuple[str, ...]], ...]:
        """(code, skill ids) for every code found in `text`, in order, each once. `context` (the
        role's employer) only says whether the role is military and which branch."""
        if not isinstance(text, str) or not text:
            return ()
        context = context if isinstance(context, str) else ""
        both = f"{text}\n{context}"
        military = _military(both)
        branches = _branches(both)
        found: dict[str, tuple[str, ...]] = {}
        labelled_end = -1  # where the last labelled code ended: "MOS 25B/25U" labels both
        for match in _CODE.finditer(text):
            code = match.group(1)
            entries = self._entries(code)
            if not entries:
                continue
            start, end = match.span(1)
            line_start = text.rfind("\n", 0, start) + 1
            line_end = text.find("\n", end)
            line = text[line_start: line_end if line_end >= 0 else len(text)]
            before = text[line_start:start]
            label = _LABEL.search(before)
            labelled = label is not None and (
                label.group("label").lower() not in _AMBIGUOUS_LABELS or military)
            if not labelled and labelled_end >= line_start and _CHAIN.fullmatch(text[labelled_end:start]):
                labelled = True
            mixed = any(ch.isdigit() for ch in code) and any(ch.isalpha() for ch in code)
            shared = bool(_content_words(line.replace(code, " ")) & self._title_words(entries))
            accepted = labelled or (military and (mixed or shared))
            if labelled:
                labelled_end = end
            if accepted and code not in found:
                found[code] = self._skills(entries, branches)
        return tuple(found.items())

    @staticmethod
    def _skills(entries: list[Mapping[str, Any]], branches: frozenset[str]) -> tuple[str, ...]:
        chosen = entries[0]
        if branches:
            chosen = next((entry for entry in entries if entry.get("branch") in branches), entries[0])
        ids = chosen.get("skill_ids") or []
        return tuple(sid for sid in ids if isinstance(sid, str) and sid)


def load_mos_index(taxonomy_dir: Union[str, Path]) -> MosIndex:
    """`mos_crosswalk.json` from `taxonomy_dir`, verified against its MANIFEST."""
    document = load_json_artifact(taxonomy_dir, MOS_JSON)
    codes = document.get("codes")
    if not isinstance(codes, dict):
        raise ArtifactError(f"{MOS_JSON}: 'codes' must map codes to entries")
    return MosIndex(codes)


def load_naics_codes(taxonomy_dir: Union[str, Path]) -> Mapping[str, str]:
    """`naics.json` (NAICS 2022 code -> title) from `taxonomy_dir`, verified against its MANIFEST."""
    document = load_json_artifact(taxonomy_dir, NAICS_JSON)
    codes = document.get("codes")
    if not isinstance(codes, dict):
        raise ArtifactError(f"{NAICS_JSON}: 'codes' must map codes to titles")
    return MappingProxyType(dict(codes))
