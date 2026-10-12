"""Word-level normalisation shared by the parsers: headers, titles, company names, degrees.

`fold` is casefold plus accent removal, so "EXPÉRIENCE", "Expérience" and "experience" compare
equal. `words` splits folded text into word tokens (letters and digits); apostrophes either split
words ("d'équipe" -> d, equipe) or vanish ("I've" -> ive). `find_phrase` looks a word sequence
up in a token list. Standard library only, total on any `str`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from typing import NamedTuple

APOSTROPHES = "'’‘`´ʼ"
_WORD = re.compile(r"[^\W_]+")
_APOSTROPHE_TABLE = str.maketrans({ch: None for ch in APOSTROPHES})


def strip_accents(text: str) -> str:
    """NFKD minus combining marks: "Montréal" -> "Montreal", "ﬁ" -> "fi"."""
    if text.isascii():
        return text
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def fold(text: str) -> str:
    """Casefolded and accent-free."""
    return strip_accents(text).casefold()


def words(text: str, *, drop_apostrophes: bool = False) -> list[str]:
    """Folded word tokens. With `drop_apostrophes` an apostrophe joins its word ("I've" -> "ive");
    otherwise it separates words ("d'équipe" -> "d", "equipe")."""
    folded = fold(text)
    if drop_apostrophes:
        folded = folded.translate(_APOSTROPHE_TABLE)
    return _WORD.findall(folded)


class Token(NamedTuple):
    start: int  # offsets into the original text
    end: int
    word: str  # folded
    raw: str  # as written


def tokens(text: str) -> list[Token]:
    """Word tokens with their offsets in `text` (apostrophes and hyphens separate words)."""
    return [Token(m.start(), m.end(), fold(m.group()), m.group()) for m in _WORD.finditer(text)]


def phrase_words(phrase: str) -> tuple[str, ...]:
    """A configured phrase as the folded words it matches ("Co-op" -> ("co", "op"))."""
    return tuple(words(phrase))


def find_phrase(haystack: Sequence[str], phrase: Sequence[str], start: int = 0) -> int:
    """Index of the first occurrence of `phrase` (a word sequence) in `haystack` at or after
    `start`, or -1."""
    n = len(phrase)
    if not n:
        return -1
    first = phrase[0]
    for i in range(start, len(haystack) - n + 1):
        if haystack[i] == first and tuple(haystack[i:i + n]) == tuple(phrase):
            return i
    return -1


def has_phrase(haystack: Sequence[str], phrase: Sequence[str]) -> bool:
    return find_phrase(haystack, phrase) >= 0


_BRACKETED = re.compile(r"\([^()]*\)|\[[^\[\]]*\]")
_DELETED = str.maketrans({ch: None for ch in "." + APOSTROPHES})
_NON_WORD = re.compile(r"[^\w\s]|_")


def company_key(name: str, legal_suffixes: Sequence[str] = ()) -> str:
    """A company name as the industry table compares it: bracketed tags dropped ("Postman
    (Greenhouse)"), folded, "&" read as "and", periods and apostrophes deleted ("U.S." -> "us",
    "McDonald's" -> "mcdonalds"), other punctuation read as a space, a leading "the" and trailing
    legal suffixes ("inc", "llc", ...) removed."""
    text = fold(_BRACKETED.sub(" ", name)).replace("&", " and ").translate(_DELETED)
    parts = _NON_WORD.sub(" ", text).split()
    if parts and parts[0] == "the":
        parts = parts[1:]
    suffixes = frozenset(fold(suffix) for suffix in legal_suffixes)
    while parts and parts[-1] in suffixes:
        parts.pop()
    return " ".join(parts)
