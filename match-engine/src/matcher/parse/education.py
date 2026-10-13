"""Education entries (SPEC §5.6, §9; DECISIONS D-14, D-27, D-41; vocabulary in config/parse/sections.yaml).

`parse_education(section, *, resolver, parse_config)` turns an EDUCATION section into `Education`
records: degree level, `field.*` ids, the field text, in progress. Never the institution and never
a date (SPEC §5.6 forbids prestige and graduation year; D-14).

* Entries. A degree line (a degree word: "BSc", "B.Sc.", "Bachelor of Arts", "Maîtrise", "DEC",
  "High School Diploma"; the longest match wins, periods of an abbreviation ignored) or an
  institution line ("University of Toronto") opens an entry; a second degree line, or a second
  institution line, opens the next. Labelled lines that are not degrees ("Relevant coursework:",
  "GPA", "Awards:", "Thesis:") and bullets are never degree lines (a bullet only when it starts
  with the degree).
* Level: the highest degree word of the line. Cased abbreviations need their casing ("BA", "MS",
  "DEC") and are skipped where they mean something else: before "Office" ("MS Office"), "DEC"
  before a year (a month), "MA"/"MD"/"MS" as a state after a city ("Boston, MA").
* Fields: the `field.*` mentions of the degree line, read with `include_fields=True` so a word
  that is both a skill and a field ("Accounting", "Statistics") is the FIELD here (D-27), outside
  the segment naming the institution. A field that is the degree's own name ("Bachelor of
  Science" -> field.science, "MBA" -> field.business_admin) counts only when the line names no
  specific field; when the degree line names none at all, the entry's other lines may ("Bachelor of
  Applied Science" / "Electrical Engineering"). A field that runs over the degree line's end into
  the next line ("... in Business" / "Administration, ...": a narrow column wrapped it) is read
  over both lines (Task 5.4).
* No degree word: an institution line with a field of study in its entry is that institution's
  usual credential ("University of Toronto, Computer Science": a bachelor's program; a college: a
  diploma), unless the entry says certificate, exchange, course, summer, program, camp, ...
  (`no_inference`), and only when its dates span a school year (240 days) or more, run past
  `as_of`, or the entry says it is in progress. Without this a student who writes no "BSc" would
  score as having no degree, a known-wrong answer (§14.5); with it a summer program is no degree.
* In progress: "expected", "candidate", "in progress", "present", "current", "en cours", ... on
  the entry's lines (coursework lines aside; "Current GPA" is no status), or, when `as_of` is
  given, dates that run past it (an end year still to come). Dates only decide this flag and the
  inference; none reaches the output (D-41).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import TYPE_CHECKING, NamedTuple, Optional

from matcher.errors import Flag
from matcher.parse.dates import DateRange, find_date_ranges

from matcher.models import DegreeLevel, Education
from matcher.parse._text import APOSTROPHES, find_phrase, fold, phrase_words, strip_accents, words
from matcher.parse.config import EducationTable, ParseConfig

if TYPE_CHECKING:  # sections imports this module for its fallback; no runtime cycle
    from matcher.normalize.resolver import Resolver
    from matcher.parse.sections import Section

__all__ = ["DegreeMatch", "degree_matches", "parse_education"]

_FIELD_PREFIX = "field."
_MAX_ENTRIES = 50
_GLYPH = re.compile(r"^\s*(?:[•▪◦●‣∙➢➤►▶✓✔○"
                    r"■◆·–—]\s*|[-*]\s+)")
_APOSTROPHE = str.maketrans({ch: "'" for ch in APOSTROPHES})
_DEGREE_NAME_WORDS = frozenset({"bachelor", "bachelors", "master", "masters", "doctor", "associate",
                                "honours", "honors"})
_OF_NAME = re.compile(r"(?:'s)?\s+of\s+(?:[A-Z][\w\-]*)(?:\s+(?!in\b)[A-Z][\w\-]*){0,2}")
_SEGMENT_BREAK = re.compile(r"\s*(?:[,|;()\[\]•·]|\s[-–—]+\s|[–—]|\s@\s|\sat\s)\s*")


class DegreeMatch(NamedTuple):
    level: DegreeLevel
    start: int  # offsets into the original line
    end: int


# ─── degree words ───────────────────────────────────────────────────────────────────────────


class _Compiled(NamedTuple):
    terms: re.Pattern[str]
    term_levels: dict[str, DegreeLevel]
    cased: Optional[re.Pattern[str]]
    cased_levels: dict[str, DegreeLevel]
    not_before: Optional[re.Pattern[str]]
    month_like: frozenset[str]
    location_like: frozenset[str]
    skip_labels: tuple[tuple[str, ...], ...]
    institutions: tuple[tuple[DegreeLevel, tuple[tuple[str, ...], ...]], ...]
    school_words: tuple[tuple[str, ...], ...]  # every institution phrase, with or without a level
    no_inference: tuple[tuple[str, ...], ...]
    in_progress: tuple[tuple[str, ...], ...]


def _term_key(term: str) -> str:
    return " ".join(fold(term.translate(_APOSTROPHE).replace(".", "")).split())


# One compiled form per table object (a ParseConfig is loaded once; the models hold dicts, so
# they are not hashable and cannot key an lru_cache).
_COMPILED: dict[int, tuple[EducationTable, _Compiled]] = {}
_COMPILED_LIMIT = 16


def _compile(table: EducationTable) -> _Compiled:
    hit = _COMPILED.get(id(table))
    if hit is not None and hit[0] is table:
        return hit[1]
    compiled = _build(table)
    if len(_COMPILED) >= _COMPILED_LIMIT:
        _COMPILED.clear()
    _COMPILED[id(table)] = (table, compiled)
    return compiled


def _build(table: EducationTable) -> _Compiled:
    levels: dict[str, DegreeLevel] = {}
    cased_levels: dict[str, DegreeLevel] = {}
    for level, terms in table.degrees.items():
        for term in terms.terms:
            key = _term_key(term)
            for form in {key, key.replace("'", "")}:
                if form and (form not in levels or levels[form].rank < level.rank):
                    levels[form] = level
        for word in terms.cased:
            if word and (word not in cased_levels or cased_levels[word].rank < level.rank):
                cased_levels[word] = level
    ordered = sorted(levels, key=lambda key: (-len(key), key))
    alternation = "|".join(re.escape(key).replace(r"\ ", r"\s+").replace("'", "'?") for key in ordered)
    terms = re.compile(rf"(?<![^\W_])(?:{alternation})(?![^\W_])")
    cased = None
    if cased_levels:
        words_alt = "|".join(re.escape(w) for w in sorted(cased_levels, key=lambda w: (-len(w), w)))
        cased = re.compile(rf"(?<![^\W_])(?:{words_alt})(?![^\W_])")
    not_before = None
    if table.cased_not_before:
        alt = "|".join(re.escape(fold(w)) for w in table.cased_not_before)
        not_before = re.compile(rf"\s+(?:{alt})(?![^\W_])", re.IGNORECASE)
    institutions = tuple((level, _phrases(names)) for level, names in table.institutions.items())
    school_words = _phrases((*(n for names in table.institutions.values() for n in names), *table.institution_words))
    return _Compiled(terms, levels, cased, cased_levels, not_before, frozenset(table.month_like),
                     frozenset(table.location_like), _phrases(table.skip_labels), institutions, school_words,
                     _phrases(table.no_inference), _phrases(table.in_progress))


def _dotless(text: str) -> tuple[str, list[int]]:
    """`text` without the periods that follow a letter ("B.Sc." -> "BSc"), apostrophes unified,
    with the original offset of every kept character."""
    chars: list[str] = []
    where: list[int] = []
    for i, ch in enumerate(text.translate(_APOSTROPHE)):
        if ch == "." and i > 0 and text[i - 1].isalpha():
            continue
        chars.append(ch)
        where.append(i)
    return "".join(chars), where


def _folded(text: str) -> tuple[str, list[int]]:
    """Casefolded, accent-free `text` with the offset (in `text`) of every character."""
    chars: list[str] = []
    where: list[int] = []
    for i, ch in enumerate(text):
        for out in strip_accents(ch).casefold():
            chars.append(out)
            where.append(i)
    return "".join(chars), where


def degree_matches(text: str, parse_config: ParseConfig) -> list[DegreeMatch]:
    """Every degree word of `text` (longest first, no overlaps), with offsets into `text`."""
    if not isinstance(text, str) or not text.strip():
        return []
    compiled = _compile(parse_config.sections.education)
    dotless, to_text = _dotless(text)
    folded, to_dotless = _folded(dotless)
    found: list[DegreeMatch] = []
    for match in compiled.terms.finditer(folded):
        key = " ".join(match.group().replace("'", "").split())
        level = compiled.term_levels.get(key) or compiled.term_levels.get(" ".join(match.group().split()))
        if level is None:
            continue
        start = to_text[to_dotless[match.start()]]
        end = to_text[to_dotless[match.end() - 1]] + 1
        if any(word in _DEGREE_NAME_WORDS for word in key.split()):
            extension = _OF_NAME.match(text, end)
            if extension is not None:
                end = extension.end()
        found.append(DegreeMatch(level, start, end))
    if compiled.cased is not None:
        for match in compiled.cased.finditer(dotless):
            word = match.group()
            after = dotless[match.end():]
            before = dotless[: match.start()]
            if compiled.not_before is not None and compiled.not_before.match(after):
                continue
            if word in compiled.month_like and re.match(r"\s*[0-9'’]", after):
                continue
            if word in compiled.location_like and re.search(r"[A-Za-z][\w.'\-]*,\s*$", before) and (
                    re.match(r"\s*(?:$|[,|)\-–—]|[0-9])", after)):
                continue
            start = to_text[match.start()]
            end = to_text[match.end() - 1] + 1
            found.append(DegreeMatch(compiled.cased_levels[word], start, end))
    found.sort(key=lambda m: (-(m.end - m.start), m.start))
    kept: list[DegreeMatch] = []
    for candidate in found:
        if all(candidate.end <= other.start or candidate.start >= other.end for other in kept):
            kept.append(candidate)
    return sorted(kept, key=lambda m: m.start)


# ─── lines and entries ──────────────────────────────────────────────────────────────────────


class _Line(NamedTuple):
    text: str
    bullet: bool
    skip: bool
    degrees: tuple[DegreeMatch, ...]
    institution: Optional[DegreeLevel]  # the level an institution word implies
    institution_named: bool  # any institution word (school, institute, ...)


def _phrases(values: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(sorted({phrase_words(v) for v in values if phrase_words(v)}, key=lambda p: (-len(p), p)))


def _has_any(tokens: list[str], phrases: tuple[tuple[str, ...], ...]) -> bool:
    return any(find_phrase(tokens, phrase) >= 0 for phrase in phrases)


def _starts_with_any(tokens: list[str], phrases: tuple[tuple[str, ...], ...]) -> Optional[tuple[str, ...]]:
    for phrase in phrases:
        if tuple(tokens[: len(phrase)]) == phrase:
            return phrase
    return None


def _classify(raw: str, compiled: _Compiled, parse_config: ParseConfig) -> _Line:
    glyph = _GLYPH.match(raw)
    text = (raw[glyph.end():] if glyph else raw).strip()
    tokens = words(text)
    degrees = tuple(degree_matches(text, parse_config))
    if glyph is not None:  # a bullet is a degree line only when it opens with the degree
        degrees = degrees if degrees and degrees[0].start == 0 else ()
    label = _starts_with_any(tokens, compiled.skip_labels)
    skip = False
    if label is not None:
        after_label = re.match(r"[^\w]*" + r"[^\w]+".join(re.escape(w) for w in label) + r"\s*:", fold(text))
        skip = after_label is not None or not degrees
    institution: Optional[DegreeLevel] = None
    for level, names in compiled.institutions:
        if _has_any(tokens, names) and (institution is None or level.rank > institution.rank):
            institution = level
    named = institution is not None or _has_any(tokens, compiled.school_words)
    return _Line(text, glyph is not None, skip, () if skip else degrees, institution, named)


def _institution_spans(text: str, degrees: tuple[DegreeMatch, ...], compiled: _Compiled) -> list[tuple[int, int]]:
    """Spans of the segments that name a school (and hold no degree word)."""
    spans: list[tuple[int, int]] = []
    names = compiled.school_words
    position = 0
    for found in [*_SEGMENT_BREAK.finditer(text), None]:
        end = found.start() if found is not None else len(text)
        segment = text[position:end]
        if segment.strip() and _has_any(words(segment), names) and not any(
                d.start < end and d.end > position for d in degrees):
            spans.append((position, end))
        if found is None:
            break
        position = found.end()
    return spans


def _fields(text: str, degrees: tuple[DegreeMatch, ...], compiled: _Compiled, resolver: "Resolver"
            ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(specific fields, degree-name fields) of a line as (id, surface), in order."""
    if not text:
        return [], []
    mentions = resolver.extract(text, include_fields=True)
    outside = _institution_spans(text, degrees, compiled)
    specific: list[tuple[str, str]] = []
    named: list[tuple[str, str]] = []
    for mention in mentions:
        if not mention.canonical_id.startswith(_FIELD_PREFIX):
            continue
        if any(start <= mention.start and mention.end <= end for start, end in outside):
            continue
        pair = (mention.canonical_id, mention.surface)
        if any(d.start < mention.end and mention.start < d.end for d in degrees):
            named.append(pair)
        else:
            specific.append(pair)
    return specific, named


def _blocks(lines: list[_Line], complete: frozenset[int] = frozenset()) -> list[list[int]]:
    """Entries: a second degree line or a second institution line opens the next one, and so does a
    degree line after an institution line that already names its own field (`complete`:
    "Computer Science, University of Toronto, 2027" then "Bachelor of Science ... McGill")."""
    blocks: list[list[int]] = []
    current: list[int] = []
    has_degree = has_institution = closed = False
    for index, line in enumerate(lines):
        if not line.text:
            if current:
                current.append(index)
            continue
        degree_line = bool(line.degrees)
        institution_line = not degree_line and not line.skip and not line.bullet and line.institution_named
        if (degree_line and (has_degree or closed)) or (institution_line and has_institution):
            blocks.append(current)
            current, has_degree, has_institution, closed = [], False, False, False
        current.append(index)
        closed = closed or (institution_line and index in complete)
        has_degree = has_degree or degree_line
        has_institution = has_institution or institution_line or (degree_line and line.institution_named)
    if current:
        blocks.append(current)
    return blocks


def _unique(pairs: list[tuple[str, str]]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    ids: list[str] = []
    surfaces: list[str] = []
    for canonical_id, surface in pairs:
        if canonical_id not in ids:
            ids.append(canonical_id)
        if surface not in surfaces:
            surfaces.append(surface)
    return tuple(ids), tuple(surfaces)


_DETECTION_AS_OF = dt.date(2049, 12, 31)  # reads year-shaped numbers when no as_of is given
_SCHOOL_YEAR_DAYS = 240  # September to April: an inferred program lasts at least this long
_NOT_STATUS = re.compile(r"\b(?:current(?:ly)?|actuel(?:le)?)\s+(?:c?gpa|average|grades?|standing|moyenne)\b",
                         re.IGNORECASE)


def _ranges(texts: list[str], as_of: Optional[dt.date]) -> list[DateRange]:
    reference = as_of if as_of is not None else _DETECTION_AS_OF
    return [found[2] for text in texts for found in find_date_ranges(text, as_of=reference)]


def _open_or_ongoing(ranges: list[DateRange], as_of: Optional[dt.date]) -> bool:
    """A range still running at as_of: current, an end clamped to as_of, a start not yet reached."""
    if as_of is None:
        return any(r.is_current for r in ranges)
    late = {Flag.FUTURE_END_CLAMPED, Flag.FUTURE_START}
    return any(r.is_current or late.intersection(r.flags) for r in ranges)


def _school_year(ranges: list[DateRange], as_of: Optional[dt.date]) -> bool:
    for r in ranges:
        if r.is_current or (r.start is not None and r.end is not None
                            and (r.end.value - r.start.value).days >= _SCHOOL_YEAR_DAYS):
            return True
    return _open_or_ongoing(ranges, as_of)


def _wrapped(degree_at: int, texts: list[int], lines: list[_Line], resolver: "Resolver") -> str:
    """The degree line, joined to the line after it when a field of study runs over the break
    ("Bachelor of Science in Business" / "Administration, Ridgemont": a narrow column wrapped it;
    Task 5.4). Otherwise the degree line alone."""
    line = lines[degree_at]
    position = texts.index(degree_at)
    if position + 1 >= len(texts):
        return line.text
    following = lines[texts[position + 1]]
    if following.bullet or following.skip or following.degrees:
        return line.text
    cut = len(line.text)
    joined = f"{line.text} {following.text}"
    crossing = any(m.canonical_id.startswith(_FIELD_PREFIX) and m.start < cut < m.end
                   for m in resolver.extract(joined, include_fields=True))
    return joined if crossing else line.text


def _entry(block: list[int], lines: list[_Line], compiled: _Compiled, resolver: "Resolver",
           as_of: Optional[dt.date]) -> Optional[Education]:
    texts = [i for i in block if lines[i].text]
    degree_at = next((i for i in texts if lines[i].degrees), None)
    content = [i for i in texts if not lines[i].skip]
    if degree_at is not None:
        line = lines[degree_at]
        level = max((d.level for d in line.degrees), key=lambda lvl: lvl.rank)
        specific, named = _fields(_wrapped(degree_at, texts, lines, resolver), line.degrees, compiled, resolver)
        if not specific:
            for i in content:
                if i == degree_at or lines[i].bullet:
                    continue
                extra, _ = _fields(lines[i].text, (), compiled, resolver)
                if extra:
                    specific = extra
                    break
        chosen = specific or named
    else:
        institution = max((lines[i].institution for i in content if lines[i].institution is not None),
                          key=lambda lvl: lvl.rank, default=None)
        if institution is None:
            return None
        if any(_has_any(words(lines[i].text), compiled.no_inference) for i in content if not lines[i].bullet):
            return None
        specific = []
        for i in content:
            if lines[i].bullet:
                continue
            found, _ = _fields(lines[i].text, (), compiled, resolver)
            if found:
                specific = found
                break
        if not specific and institution is not DegreeLevel.HIGH_SCHOOL:
            return None
        marked = any(_has_any(words(_NOT_STATUS.sub(" ", lines[i].text)), compiled.in_progress) for i in content)
        if not marked and not _school_year(_ranges([lines[i].text for i in content], as_of), as_of):
            return None  # a summer program or a short course is no degree
        level, chosen = institution, specific
    ids, surfaces = _unique(chosen)
    in_progress = any(_has_any(words(_NOT_STATUS.sub(" ", lines[i].text)), compiled.in_progress) for i in content)
    in_progress = in_progress or _open_or_ongoing(_ranges([lines[i].text for i in content], as_of), as_of)
    return Education(level=level, field_ids=ids[:20], field_raw=", ".join(surfaces)[:2000], in_progress=in_progress)


def parse_education(section: "Section", *, resolver: "Resolver", parse_config: ParseConfig,
                    as_of: Optional[dt.date] = None) -> tuple[Education, ...]:
    """The degrees of an EDUCATION section, in order (see the module docstring). With `as_of`, an
    entry whose dates run past it (an end year still to come) is in progress."""
    compiled = _compile(parse_config.sections.education)
    lines = [_classify(str(raw), compiled, parse_config) for raw in section.lines]
    out: list[Education] = []
    complete = frozenset(i for i, line in enumerate(lines) if line.text and not line.degrees and not line.skip
                         and not line.bullet and line.institution_named
                         and _fields(line.text, (), compiled, resolver)[0])
    for block in _blocks(lines, complete):
        entry = _entry(block, lines, compiled, resolver, as_of)
        if entry is not None and entry not in out:
            out.append(entry)
        if len(out) >= _MAX_ENTRIES:
            break
    return tuple(out)
