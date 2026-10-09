"""Text normalisation at ingest, before any matching (SPEC §14.1; brief row 9).

Every extractor's text goes through the same steps, so a résumé reads the same whether it came
from a PDF, a DOCX, a scan or a .txt file (EVAL §2.2 format invariance):

1. Line breaks are unified (CR LF, CR, VT, FF, NEL, U+2028, U+2029 become LF). Lines are kept.
2. Unicode format characters (category Cf) are removed: zero-width space and joiners, word
   joiner, BOM, bidi marks and isolates, soft hyphens. A soft hyphen that ends a line is kept
   until step 5 needs it.
3. NFKC: ligatures (U+FB00..FB06 become ff, fi, fl, ffi, ffl, ...), full-width forms,
   superscripts, and compatibility spaces (NBSP and friends) become their plain forms.
4. Per line: every control character and every Unicode space becomes " "; smart quotes become
   straight; a bullet glyph at the start of the line becomes "- "; runs of spaces collapse; the
   line is stripped.
5. Across lines: a word broken at a line end by a soft hyphen (next line starts with a cased
   letter) or by a hyphen after a letter (next line starts with a lowercase letter) is joined:
   the first word of the next line moves up. Remaining soft hyphens are dropped.

`normalize_text` is idempotent (property-tested). `normalize_line` is steps 2-4 for one line
(soft hyphens kept), for the layout code that must normalise fragments before it knows their
reading order; `normalize_lines` adds step 5 and reports which lines a join used up.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, Sequence

SOFT_HYPHEN = "\xad"
_HYPHENS = ("-", "\u2010")  # hyphen-minus, HYPHEN (NFKC turns U+2011 into U+2010)

_NEWLINES = re.compile("\r\n|[\r\n\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_SPACE_RUNS = re.compile(" {2,}")

# Bullet glyphs that start a line. The brief's list first, then other common glyphs, then the
# private-use code points Symbol/Wingdings bullets extract as from Word-made PDFs.
BULLET_GLYPHS = (
    "\u2022\u25aa\u25e6\u25cf\u2023\u2219\u27a2\u2713\u2013"
    "\u25a0\u25cb\u25c6\u25ba\u25b6\u27a4\u2714\u2043\xb7\u2014"
    "\uf0b7\uf0a7\uf076\uf0d8\uf0fc\uf0a8"
)
# A lone "*" is a bullet; "**" opens Markdown bold and is left alone.
_BULLET_START = re.compile("^(?:[" + re.escape(BULLET_GLYPHS) + r"]|\*(?!\*))\s*")

_QUOTES = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'", 0x2032: "'", 0x2035: "'", 0x02BC: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"', 0x2033: '"', 0x2036: '"',
}
# Control characters (Cc) and space separators (Zs) all become a plain space.
_SPACE_LIKE = (
    list(range(0x00, 0x20)) + list(range(0x7F, 0xA1)) + [0x1680] + list(range(0x2000, 0x200B))
    + [0x202F, 0x205F, 0x3000]
)
_TRANSLATE = {cp: " " for cp in _SPACE_LIKE} | _QUOTES


def _is_format(char: str) -> bool:
    return unicodedata.category(char) == "Cf"


def _strip_format_chars(line: str) -> str:
    """Drop every format character. A soft hyphen that ends the line (ignoring spaces and other
    format characters) survives: it marks a word break for the joiner. Removing the others BEFORE
    NFKC lets NFKC compose what they separated, which keeps normalisation idempotent."""
    if line.isascii():
        return line
    kept = "".join(c for c in line if not _is_format(c))
    for char in reversed(line):
        if char.isspace() or (char != SOFT_HYPHEN and _is_format(char)):
            continue
        return kept.rstrip() + SOFT_HYPHEN if char == SOFT_HYPHEN else kept
    return kept


def normalize_line(line: str) -> str:
    """Steps 2-4 for one line (no line breaks inside). A line-final soft hyphen is kept for the
    joiner; every other format character is gone."""
    text = unicodedata.normalize("NFKC", _strip_format_chars(line)).translate(_TRANSLATE)
    text = _SPACE_RUNS.sub(" ", text).strip()
    if text:
        text = _BULLET_START.sub("- ", text, count=1).rstrip()
    return text


def _joinable(current: str, following: str) -> bool:
    if not current or not following:
        return False
    first = following[0]
    if current.endswith(SOFT_HYPHEN):  # an explicit break opportunity inside a word
        return first.isalpha() and (first.islower() or first.isupper())
    return (len(current) >= 2 and current[-1] in _HYPHENS and current[-2].isalpha()
            and first.islower())


def _tail(pieces: Sequence[str]) -> str:
    """The last two characters of "".join(pieces), without building the whole string."""
    tail = ""
    for piece in reversed(pieces):
        tail = piece + tail
        if len(tail) >= 2:
            break
    return tail[-2:]


def join_hyphenated(lines: Sequence[str]) -> list[Optional[str]]:
    """Step 5 over already-normalised lines. One entry per input line: its text, or None when a
    join moved all of it onto the previous line. Linear time: a line that absorbs thousands of
    continuations collects them as pieces and is joined once."""
    out: list[Optional[str]] = list(lines)
    for i in range(len(out)):
        current = out[i]
        if current is None:
            continue
        pieces = [current]
        j = i + 1
        while j < len(out):
            following = out[j]
            if following is None or not _joinable(_tail(pieces), following):
                break
            word, _, rest = following.partition(" ")
            pieces[-1] = pieces[-1][:-1]  # drop the hyphen or soft hyphen
            pieces.append(word)
            rest = normalize_line(rest)  # what is left now starts a line (it may start with a bullet)
            if rest:
                out[j] = rest
            else:
                out[j] = None
                j += 1
        joined = pieces[0] if len(pieces) == 1 else "".join(pieces)
        out[i] = joined.replace(SOFT_HYPHEN, "").rstrip(" ")  # a dropped final soft hyphen may expose a space
    return out


def normalize_lines(lines: Sequence[str]) -> list[Optional[str]]:
    """Steps 2-5 for a sequence of lines; see `join_hyphenated` for the return shape."""
    return join_hyphenated([normalize_line(line) for line in lines])


def split_lines(text: str) -> list[str]:
    return _NEWLINES.split(text)


def normalize_text(text: str) -> str:
    """Normalised text with line breaks kept (blank lines included)."""
    return "\n".join(line for line in normalize_lines(split_lines(text)) if line is not None)
