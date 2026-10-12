"""Section segmentation (SPEC §14.3; the header table is config/parse/sections.yaml).

`segment(lines, *, parse_config, layout=None)` splits a résumé's lines into `Section`s.

* A header is a short line (at most `max_header_words` words, no final period; decoration such as
  "##", "==", a trailing ":" or dashes on both sides ignored) that the English/French synonym table
  lists, compared case-, accent- and punctuation-insensitively, creative headers included ("Where
  I've Been", "My Toolkit"). A header the table does not list whole may still be read through its
  filler words ("Relevant Technical Experience") or as a composite whose parts are all listed
  ("Education & Certifications": the first part decides).
* "Label: text" with a listed label ("Languages: English, French") opens a section with the text as
  its first line, except inside an entry section (experience, volunteering, projects, education:
  "Technologies: React" belongs to its role), for the kind already open, and for "Languages:"
  inside a skills section (a skill category).
* A line styled like the document's listed headers is a header too, of kind OTHER ("SIDE QUESTS"),
  when it is header-shaped (no digits, no comma or bar), not the first line of the document (the
  name), followed by content, and has every style the listed headers share: ALL CAPS, bold (when
  bold is rare in the document) or a larger font (`layout` only). Inside an entry section only a
  larger font counts: an employer in capitals ("SHOPIFY") is not a header. Styles are learned from
  the listed headers only, so a document with no listed header gets no styled header.
* Lines before the first header are the CONTACT section. No header anywhere: flag NO_SECTIONS and
  fall back to date-anchored blocks (SPEC §14.3): a line whose header holds a date range opens a
  block with its head lines and bullets (EXPERIENCE; EDUCATION when the block names a degree or a
  school but no job), and every other line is OTHER.

Sections keep their lines as given (blank lines included, trailing whitespace dropped) and every
non-blank input line belongs to exactly one section. `Section.language` is the majority language
of the section's lines (`matcher.ingest.language`), else the language of its listed header. The
fallback reads dates only to find dated lines; it uses a fixed reference date (`_DETECTION_AS_OF`)
because no `as_of` is passed here, and `split_roles` reads the same lines again with the real one.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
from collections.abc import Sequence
from typing import NamedTuple, Optional

from pydantic import Field

from matcher.errors import Flag
from matcher.ingest import TextLine
from matcher.ingest.language import detect_language
from matcher.models import Frozen
from matcher.parse import _entries
from matcher.parse._text import has_phrase, phrase_words, words
from matcher.parse.config import ParseConfig, SectionKind, SectionsTable, header_key
from matcher.parse.education import degree_matches

__all__ = ["Section", "SectionKind", "classify_header", "segment"]

# Recognises year-shaped numbers 1969..2059 (the window of `find_date_ranges` is as_of - 80 .. + 10).
_DETECTION_AS_OF = dt.date(2049, 12, 31)


class Section(Frozen):
    kind: SectionKind
    header: str
    lines: tuple[str, ...]
    start_line: int = Field(ge=0)
    language: Optional[str]


# ─── header lines ───────────────────────────────────────────────────────────────────────────

_DECORATION = " \t#=*_~:|<>[]•·–—-"
_BULLET = re.compile(r"^\s*(?:[•▪◦●‣∙➢➤►▶✓✔○"
                     r"■◆·–—]\s*|[-*]\s+)")
_INLINE = re.compile(r"^\s*([^\W\d_][^:]{0,60}?)\s*:\s*(\S.*)$")
_STYLED_FORBIDDEN = re.compile(r"[0-9@|,;•·]|https?:|www\.", re.IGNORECASE)


def _shape(line: str, table: SectionsTable) -> Optional[str]:
    """The header text of a header-shaped line, without decoration; None otherwise."""
    text = line.strip()
    if not text:
        return None
    if _BULLET.match(text) and text[-1] not in _DECORATION:
        return None  # "- Skills" is a bullet; a header with dashes on both sides is decorated
    core = text.strip(_DECORATION)
    if not core or core.endswith(".") or len(core.split()) > table.max_header_words:
        return None
    return core


def _simple(text: str, table: SectionsTable) -> Optional[tuple[SectionKind, Optional[str]]]:
    index = table.header_index
    key = header_key(text)
    if not key:
        return None
    if key in index:
        return index[key]
    core = " ".join(word for word in key.split() if word not in table.filler_words)
    if core and core != key and core in index:
        return index[core]
    if " " not in key and len(key) >= _MIN_FUSED:
        # OCR often drops the space of a styled header ("ProfessionalExperience"): one token equal to
        # a multi-word header with its spaces removed is that header.
        for listed, found in index.items():
            if " " in listed and listed.replace(" ", "") == key:
                return found
    return None


_MIN_FUSED = 8  # shortest fused header considered ("workhistory" is 11)


def _composite_parts(text: str, table: SectionsTable) -> list[str]:
    pieces = [text]
    for symbol in table.joiner_symbols:
        pieces = [part for piece in pieces for part in piece.split(symbol)]
    if table.joiner_words:
        pattern = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in sorted(table.joiner_words)) + r")\b",
                             re.IGNORECASE)
        pieces = [part for piece in pieces for part in pattern.split(piece)]
    return [piece for piece in pieces if piece.strip()]


def _lookup(text: str, table: SectionsTable) -> Optional[tuple[SectionKind, Optional[str]]]:
    found = _simple(text, table)
    if found is not None:
        return found
    parts = _composite_parts(text, table)
    if len(parts) >= 2:
        kinds = [_simple(part, table) for part in parts]
        if all(kinds):
            return kinds[0]
    return None


def classify_header(line: str, parse_config: ParseConfig) -> Optional[SectionKind]:
    """The kind of section a line opens when the synonym table lists it (see the module
    docstring); None for any other line."""
    if not isinstance(line, str):
        return None
    table = parse_config.sections
    core = _shape(line, table)
    if core is None:
        return None
    found = _lookup(core, table)
    return None if found is None else found[0]


# ─── styles ─────────────────────────────────────────────────────────────────────────────────


def _caps(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return len(letters) >= 2 and any(ch != ch.lower() for ch in letters) and text == text.upper()


def _styles(raw: Sequence[str], layout: Optional[Sequence[TextLine]]) -> list[frozenset[str]]:
    """Per line: "caps", plus "bold" / "larger" when the layout matches the lines."""
    nonblank = [i for i, text in enumerate(raw) if text.strip()]
    bold = [False] * len(raw)
    larger = [False] * len(raw)
    placed = list(layout) if layout is not None else []
    aligned = bool(placed) and len(placed) == len(nonblank) and all(
        isinstance(item, TextLine) and item.text.strip() == raw[i].strip() for item, i in zip(placed, nonblank))
    if aligned:
        sizes = [item.size for item in placed if item.size > 0]
        median = statistics.median(sizes) if sizes else 0.0
        bold_share = sum(1 for item in placed if item.bold) / len(placed)
        for item, i in zip(placed, nonblank):
            bold[i] = item.bold and bold_share < 0.5
            larger[i] = median > 0 and item.size >= median + max(1.0, 0.1 * median)
    out = []
    for i, text in enumerate(raw):
        features = set()
        if _caps(text):
            features.add("caps")
        if bold[i]:
            features.add("bold")
        if larger[i]:
            features.add("larger")
        out.append(frozenset(features))
    return out


# ─── segmentation ───────────────────────────────────────────────────────────────────────────


class _Header(NamedTuple):
    index: int
    kind: SectionKind
    header: str
    inline: Optional[str]  # the text after "Label:" for an inline header
    lang: Optional[str]


def _clean(line: object) -> str:
    return str(line).replace("\r", "").replace("\n", " ").rstrip()


def _language(header: str, lines: Sequence[str], fallback: Optional[str]) -> Optional[str]:
    code, _ = detect_language("\n".join([header, *lines]))
    return code or fallback


def _inline(text: str, current: Optional[SectionKind], table: SectionsTable) -> Optional[tuple[str, str, SectionKind, Optional[str]]]:
    match = _INLINE.match(text)
    if match is None:
        return None
    label, content = match.group(1).strip(), match.group(2).strip()
    core = _shape(label, table)
    found = _lookup(core, table) if core is not None else None
    if found is None or not content:
        return None
    kind, lang = found
    if current in table.entry_sections or kind is current:
        return None
    if current is SectionKind.SKILLS and kind is SectionKind.LANGUAGES:
        return None
    return label, content, kind, lang


def _styled(text: str, table: SectionsTable) -> bool:
    core = _shape(text, table)
    if core is None or _STYLED_FORBIDDEN.search(core) is not None or ":" in core:
        return False
    return sum(1 for ch in core if ch.isalpha()) >= 2


def segment(lines: Sequence[str], *, parse_config: ParseConfig,
            layout: Sequence[TextLine] | None = None) -> tuple[tuple[Section, ...], tuple[Flag, ...]]:
    """The sections of a résumé and the flags of the segmentation (NO_SECTIONS); see the module
    docstring. Total: any sequence of strings gives a valid result."""
    raw = [_clean(line) for line in lines]
    nonblank = [i for i, text in enumerate(raw) if text.strip()]
    if not nonblank:
        return (), ()
    table = parse_config.sections
    found = {i: _lookup(core, table) for i in nonblank if (core := _shape(raw[i], table)) is not None}
    listed = [i for i, hit in found.items() if hit is not None]
    styles = _styles(raw, layout)
    signature = frozenset.intersection(*(styles[i] for i in listed)) if listed else frozenset()

    headers: list[_Header] = []
    current: Optional[SectionKind] = None
    for position, i in enumerate(nonblank):
        text = raw[i]
        hit = found.get(i)
        if hit is not None:
            headers.append(_Header(i, hit[0], text.strip(), None, hit[1]))
            current = hit[0]
            continue
        inline = _inline(text, current, table)
        if inline is not None:
            label, content, kind, lang = inline
            headers.append(_Header(i, kind, label, content, lang))
            current = kind
            continue
        if not signature or position == 0 or position + 1 >= len(nonblank):
            continue
        if not signature <= styles[i] or not _styled(text, table):
            continue
        if "larger" not in signature and current in table.entry_sections:
            continue
        if found.get(nonblank[position + 1]) is not None:
            continue  # followed by another header, not by content
        headers.append(_Header(i, SectionKind.OTHER, text.strip(), None, None))
        current = SectionKind.OTHER

    if not headers:
        return _fallback(raw, parse_config), (Flag.NO_SECTIONS,)
    return _build(raw, headers), ()


def _build(raw: list[str], headers: list[_Header]) -> tuple[Section, ...]:
    sections: list[Section] = []
    first = headers[0].index
    before = [i for i in range(first) if raw[i].strip()]
    if before:
        block = tuple(raw[before[0]:first])
        sections.append(Section(kind=SectionKind.CONTACT, header="", lines=block, start_line=before[0],
                                language=_language("", block, None)))
    for k, header in enumerate(headers):
        end = headers[k + 1].index if k + 1 < len(headers) else len(raw)
        body = tuple(raw[header.index + 1:end])
        content = ((header.inline,) + body) if header.inline is not None else body
        sections.append(Section(kind=header.kind, header=header.header, lines=content, start_line=header.index,
                                language=_language(header.header, content, header.lang)))
    return tuple(sections)


def _educational(entry: _entries.Entry, lines: list[_entries.Line], parse_config: ParseConfig) -> bool:
    """A date-anchored block about a degree, or about a school and no job."""
    heads = [*entry.before, *([entry.anchor] if entry.anchor is not None else []), *entry.after]
    text = " | ".join(lines[i].text for i in heads)
    if degree_matches(text, parse_config):
        return True
    return _names_school(text, parse_config) and _entries.title_score(text) == 0


def _names_school(text: str, parse_config: ParseConfig) -> bool:
    education = parse_config.sections.education
    tokens = words(text)
    names = [*(n for names in education.institutions.values() for n in names), *education.institution_words]
    return any(has_phrase(tokens, phrase_words(name)) for name in names if phrase_words(name))


def _fallback(raw: list[str], parse_config: ParseConfig) -> tuple[Section, ...]:
    lines = _entries.classify(raw, as_of=_DETECTION_AS_OF, lang="en")
    owner: list[Optional[SectionKind]] = [None] * len(raw)
    for entry in _entries.entries(lines, strict=True):
        kind = SectionKind.EDUCATION if _educational(entry, lines, parse_config) else SectionKind.EXPERIENCE
        for i in range(entry.start, entry.end + 1):
            owner[i] = kind
    sections: list[Section] = []
    i = 0
    while i < len(raw):
        if not raw[i].strip():
            i += 1
            continue
        kind = owner[i] or SectionKind.OTHER
        j = i
        while j + 1 < len(raw) and (not raw[j + 1].strip() or (owner[j + 1] or SectionKind.OTHER) is kind):
            j += 1
        block = tuple(raw[i:j + 1])
        sections.append(Section(kind=kind, header="", lines=block, start_line=i, language=_language("", block, None)))
        i = j + 1
    return tuple(sections)
