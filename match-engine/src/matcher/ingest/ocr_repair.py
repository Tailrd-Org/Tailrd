"""OCR text repair: what OCR misreads in a scanned résumé, put back (Task 5.4; config/parse/ocr.yaml).

A scanned résumé must parse like its PDF (the M6 format-invariance gate). These misreads change what
the parser reads, and `repair_ocr` repairs them in OCR-sourced text only (flag OCR_USED); typed text
is returned as it is. Every rule is a table entry, and a rule only puts back what the page shows in
another spelling, never adds a word:

* a roman level misread after a title word ("Data Analyst Ill," -> "Data Analyst III,"): a
  `level_tokens` key standing as its own word right after a title word (`title_words`, the role
  parser's job nouns), followed by the end of the line, a space or punctuation;
* a bullet glyph misread at a line start (": Deployed ...", ". Automated ...", ":. Cut ...") is the
  bullet "- ": up to three `bullet_glyphs` (with spaces between them), or one `letter_bullets` letter
  and a space, then an upper-case letter or a digit (a stray quote before it is the same speck);
* a space misread as a `space_glyphs` glyph between a lower-case letter and a capitalised word
  ("07/2023 to.Present" -> "to Present"), unless the word it is part of is an exact resolver key;
  the same glyph touching a range dash is a speck ("May 2024. - Present", "2025 -.Present");
* a region code read in lower case at a line end ("Denver, co" -> "Denver, CO"): a final word of
  `place_code_letters` letters after a comma is put in capitals when that makes the line a place
  (`place`, the role parser's place reading) and it was not one;
* a speck read as a final period after a section header ("Education." -> "Education"), when
  `header_period` is set and the rest of the line is exactly a listed header (`header`);
* an acronym misread ("Bl" -> "BI", "Cl/CD" -> "CI/CD", "laC" -> "IaC"): a word holding an upper-case
  letter and a character of `acronym_swaps` is repaired with those swaps (then in upper case) when
  the repaired form is an exact key of the resolver (`known`) and the word as read is not; so is
  a word that lost one of a doubled `doubled_letters` letter ("Ilustrator" -> "Illustrator"). The
  document's first line (the person's name, "Al") is never repaired.

`repair_lines` is the rule set on plain lines; `repair_ocr` applies it to an `IngestResult`'s lines
and text. Standard library only; the parse layer passes the title words, the resolver check and
the place reading and the header check.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Collection, Sequence
from functools import cached_property

from pydantic import Field, field_validator

from matcher.errors import Flag
from matcher.models import Frozen

__all__ = ["OcrTable", "exact_key", "repair_lines", "repair_ocr"]

_logger = logging.getLogger(__name__)
_ROMAN_LEVELS = frozenset({"I", "II", "III", "IV", "V"})


class OcrTable(Frozen):
    level_tokens: dict[str, str] = Field(default_factory=dict)  # misread -> roman level
    bullet_glyphs: tuple[str, ...] = ()  # one-character misreads of a bullet glyph
    letter_bullets: tuple[str, ...] = ()  # letters a bullet glyph is misread as (need a space after)
    acronym_swaps: dict[str, str] = Field(default_factory=dict)  # character -> what it was misread for
    acronym_max_length: int = Field(default=12, ge=2, le=40)
    doubled_letters: tuple[str, ...] = ()  # letters OCR reads once where the page shows two
    space_glyphs: tuple[str, ...] = ()  # glyphs a space is misread as before a capitalised word
    place_code_letters: int = Field(default=0, ge=0, le=3)  # 0: no region-code case repair
    header_period: bool = False  # drop a final period after an exact listed header

    @field_validator("level_tokens")
    @classmethod
    def _levels(cls, value: dict[str, str]) -> dict[str, str]:
        for read, level in value.items():
            if level not in _ROMAN_LEVELS or read == level or not read.isalpha():
                raise ValueError(f"ocr: level token {read!r} -> {level!r} is no misread roman level")
        return value

    @field_validator("bullet_glyphs", "letter_bullets", "space_glyphs", "doubled_letters")
    @classmethod
    def _one_character(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(len(glyph) != 1 or glyph.isspace() for glyph in value):
            raise ValueError("ocr: a bullet glyph is one visible character")
        return value

    @field_validator("acronym_swaps")
    @classmethod
    def _swaps(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(a) != 1 or len(b) != 1 or not a.isalpha() or not b.isalpha() for a, b in value.items()):
            raise ValueError("ocr: an acronym swap maps one letter to one letter")
        return value

    @cached_property
    def level_pattern(self) -> re.Pattern[str] | None:
        if not self.level_tokens:
            return None
        tokens = "|".join(re.escape(t) for t in sorted(self.level_tokens, key=lambda t: (-len(t), t)))
        return re.compile(rf"(?<=\S)(?P<gap> +)(?P<token>{tokens})(?=\s*$|\s*[,;:|/()\[\]]|\s+[-\u2013\u2014]\s|\s+\d)")

    @cached_property
    def bullet_pattern(self) -> re.Pattern[str] | None:
        alternatives = []
        if self.bullet_glyphs:
            glyphs = "".join(re.escape(g) for g in self.bullet_glyphs)
            alternatives.append(rf"(?:[{glyphs}] ?){{0,2}}[{glyphs}] ")
        if self.letter_bullets:
            letters = "".join(re.escape(g) for g in self.letter_bullets)
            alternatives.append(rf"[{letters}] ")
        if not alternatives:
            return None
        return re.compile(rf"^(?P<indent>\s*)(?:{'|'.join(alternatives)})\s*['‘’`]?(?=[A-Z0-9])")

    @cached_property
    def space_pattern(self) -> re.Pattern[str] | None:
        if not self.space_glyphs:
            return None
        glyphs = "".join(re.escape(g) for g in self.space_glyphs)
        return re.compile(rf"(?<=[a-z])[{glyphs}](?=[A-Z][a-z])")

    @cached_property
    def dash_pattern(self) -> re.Pattern[str] | None:
        if not self.space_glyphs:
            return None
        glyphs = "".join(re.escape(g) for g in self.space_glyphs)
        return re.compile(rf"(?<=\d)[{glyphs}](?=\s*[-\u2013\u2014]\s)|(?<=\s[-\u2013\u2014])[{glyphs}](?=[A-Z0-9])")

    @cached_property
    def place_pattern(self) -> re.Pattern[str] | None:
        if not self.place_code_letters:
            return None
        return re.compile(rf"(?<=,)(?P<gap>\s*)(?P<code>[A-Za-z]{{{self.place_code_letters}}})\.?\s*$")


_WORD = re.compile(r"[A-Za-z][A-Za-z/&+.\-]*[A-Za-z]")


def _repair_levels(line: str, table: OcrTable, title_words: Collection[str]) -> str:
    pattern = table.level_pattern
    if pattern is None:
        return line

    def fix(found: re.Match[str]) -> str:
        before = line[: found.start()].split()
        word = before[-1] if before else ""
        if not word[-1:].isalpha() or word.casefold() not in title_words:  # "Server, Il Fornaio": no
            return found.group(0)
        return found.group("gap") + table.level_tokens[found.group("token")]

    return pattern.sub(fix, line)


def _repair_bullet(line: str, table: OcrTable) -> str:
    pattern = table.bullet_pattern
    if pattern is None:
        return line
    found = pattern.match(line)
    if found is None:
        return line
    return f"{found.group('indent')}- {line[found.end():]}"


_TOKEN = re.compile(r"\S+")


def _repair_spaces(line: str, table: OcrTable, known: Callable[[str], bool]) -> str:
    dash = table.dash_pattern
    if dash is not None:
        text = line  # a speck before " - " goes; one glued to the word after the dash becomes a space
        line = dash.sub(lambda found: "" if text[found.end():found.end() + 1].isspace() else " ", text)
    pattern = table.space_pattern
    if pattern is None or pattern.search(line) is None:
        return line

    def fix(found: re.Match[str]) -> str:
        token = found.group(0)
        if "@" in token or "://" in token or known(token.strip(",;:()[]")):
            return token
        return pattern.sub(" ", token)

    return _TOKEN.sub(fix, line)


def _repair_place(line: str, table: OcrTable, place: Callable[[str], bool] | None) -> str:
    pattern = table.place_pattern
    if pattern is None or place is None:
        return line
    found = pattern.search(line)
    if found is None or found.group("code").isupper() or place(line):
        return line
    repaired = line[: found.start()] + found.group("gap") + found.group("code").upper()
    return repaired if place(repaired) else line


def _repair_header(line: str, table: OcrTable, header: Callable[[str], bool] | None) -> str:
    if not table.header_period or header is None:
        return line
    text = line.rstrip()
    if not text.endswith(".") or text.endswith("..") or not header(text[:-1].strip()):
        return line
    return text[:-1].rstrip()


def _acronym_forms(word: str, table: OcrTable) -> list[str]:
    swapped = "".join(table.acronym_swaps.get(ch, ch) for ch in word)
    forms = [swapped, word.upper(), swapped.upper()]
    forms += [word[: i + 1] + ch + word[i + 1:] for i, ch in enumerate(word) if ch in table.doubled_letters]
    return [form for form in dict.fromkeys(forms) if form != word]


def _repair_acronyms(line: str, table: OcrTable, known: Callable[[str], bool]) -> str:
    if not table.acronym_swaps:
        return line

    def fix(found: re.Match[str]) -> str:
        word = found.group(0)
        if len(word) > table.acronym_max_length or word.islower() or word.isupper():
            return word
        if not any(ch in table.acronym_swaps for ch in word) and not any(
                ch.isupper() for ch in word[1:]):
            return word  # "Excel": no misread character and no inner capital
        if known(word):
            return word
        following = line[found.end():].lstrip()
        if following[:1].isupper():
            return word  # a name before another capitalised word ("Al Smith", "Al Ain", "Mel Studio")
        for form in _acronym_forms(word, table):
            if known(form):
                return form
        return word

    return _WORD.sub(fix, line)


def exact_key(resolver) -> Callable[[str], bool]:
    """Whether a word is, whole, an exact key of the resolver (a label or an alias; never a fuzzy
    match, so the check has no side effect): one list-context mention spanning all of it."""

    def known(word: str) -> bool:
        return any(m.start == 0 and m.end == len(word) for m in resolver.extract(word, context="list"))

    return known


def _repair_line(line: str, *, first: bool, table: OcrTable, title_words: Collection[str],
                 known: Callable[[str], bool], place: Callable[[str], bool] | None,
                 header: Callable[[str], bool] | None) -> str:
    text = _repair_bullet(line, table)
    text = _repair_levels(text, table, title_words)
    if not first:
        text = _repair_spaces(text, table, known)
        text = _repair_acronyms(text, table, known)
        text = _repair_place(text, table, place)
        text = _repair_header(text, table, header)
    return text


def repair_lines(lines: Sequence[str], *, table: OcrTable, title_words: Collection[str],
                 known: Callable[[str], bool], place: Callable[[str], bool] | None = None,
                 header: Callable[[str], bool] | None = None, only: Sequence[bool] | None = None) -> list[str]:
    """The lines of OCR text, repaired (see the module docstring). Same number of lines. With
    `only`, a line is repaired only where it is True (the lines OCR read). A repair that fails on
    a line leaves that line as read (fix round 1, M4)."""
    out: list[str] = []
    first = next((i for i, line in enumerate(lines) if line.strip()), -1)
    for i, line in enumerate(lines):
        if only is not None and not only[i]:
            out.append(line)
            continue
        try:
            out.append(_repair_line(line, first=i == first, table=table, title_words=title_words, known=known,
                                    place=place, header=header))
        except Exception:  # an optional repair: the line as OCR read it is still a line
            _logger.warning("repair_lines: OCR repair failed on a line", exc_info=True)
            out.append(line)
    return out


def repair_ocr(result, *, table: OcrTable, title_words: Collection[str], known: Callable[[str], bool],
               place: Callable[[str], bool] | None = None, header: Callable[[str], bool] | None = None):
    """An `IngestResult` with its OCR text repaired; unchanged unless it is ok and flagged OCR_USED.
    Only the lines OCR read (`TextLine.ocr`, set per page at ingest) are repaired (fix round 1)."""
    if not result.ok or Flag.OCR_USED not in result.flags or not result.lines:
        return result
    texts = repair_lines([line.text for line in result.lines], table=table, title_words=title_words, known=known,
                         place=place, header=header, only=[bool(getattr(line, "ocr", False)) for line in result.lines])
    if all(new == line.text for new, line in zip(texts, result.lines)):
        return result
    lines = tuple(line if new == line.text else line.model_copy(update={"text": new})
                  for line, new in zip(result.lines, texts))
    return result.model_copy(update={"lines": lines, "text": "\n".join(line.text for line in lines)})
