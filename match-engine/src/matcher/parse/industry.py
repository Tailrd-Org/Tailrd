"""Employer -> industry codes (SPEC §5.5; DECISIONS D-40; the table is config/parse/industries.yaml).

`classify_industry(name, context, parse_config)` returns NAICS 2022 codes, or `()` when nothing
says what the employer does (never a guess):

1. the curated company table, by normalised name (`normalize_company`: bracketed ATS tags, a
   leading "the" and trailing legal suffixes dropped), each side of a spaced slash ("City of Ottawa
   / Ville d'Ottawa"), then without trailing qualifiers ("Google Canada", "TD Bank Group"); never
   by a shorter prefix ("Apple Tree Daycare" is not Apple);
2. the keyword rules over the name (`phrases` and `name_only`);
3. the same rules over the context (a role's first bullet, a posting's company description), with
   `phrases` only: bullets talk about the work ("a data warehouse", "a CI pipeline").
The first rule listed that matches wins. Pure function of its inputs; the phrase lookups use a
word-position index, so a long company description costs one pass.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence

from matcher.parse._text import APOSTROPHES, company_key, words
from matcher.parse.config import IndustriesTable, ParseConfig

__all__ = ["classify_industry", "normalize_company"]

_SLASH = re.compile(r"\s+/\s+")
_APOSTROPHE_SPACE = str.maketrans({ch: " " for ch in APOSTROPHES})


def normalize_company(name: str, parse_config: ParseConfig) -> str:
    """The key the company table compares (see the module docstring)."""
    if not isinstance(name, str):
        return ""
    return company_key(name, parse_config.industries.legal_suffixes)


def _from_table(name: str, table: IndustriesTable) -> tuple[str, ...]:
    index = table.company_index
    key = company_key(name, table.legal_suffixes)
    if not key:
        return ()
    if key in index:
        return index[key]
    tokens = key.split()
    changed = True
    while changed and tokens:
        changed = False
        for qualifier in table.qualifier_tuples:
            n = len(qualifier)
            if len(tokens) > n and tuple(tokens[-n:]) == qualifier:
                tokens = tokens[:-n]
                stripped = company_key(" ".join(tokens), table.legal_suffixes)
                if stripped in index:
                    return index[stripped]
                tokens = stripped.split()
                changed = True
                break
    return ()


class _Positions:
    """Where each word occurs in a text, for phrase lookups in one pass."""

    def __init__(self, text: str) -> None:
        self.words = words(text.replace("&", " and ").translate(_APOSTROPHE_SPACE))
        self.at: dict[str, list[int]] = defaultdict(list)
        for i, word in enumerate(self.words):
            self.at[word].append(i)

    def has(self, phrase: Sequence[str]) -> bool:
        n = len(phrase)
        for i in self.at.get(phrase[0], ()):
            if tuple(self.words[i:i + n]) == tuple(phrase):
                return True
        return False


def _from_rules(text: str, table: IndustriesTable, *, name: bool) -> tuple[str, ...]:
    if not text:
        return ()
    positions = _Positions(text)
    if not positions.words:
        return ()
    for rule in table.rules:
        phrases = rule.name_tuples if name else rule.context_tuples
        if any(phrase and positions.has(phrase) for phrase in phrases):
            return rule.naics
    return ()


def classify_industry(name: str, context: str, parse_config: ParseConfig) -> tuple[str, ...]:
    """NAICS codes for an employer (see the module docstring); `()` when unknown."""
    table = parse_config.industries
    name = name if isinstance(name, str) else ""
    context = context if isinstance(context, str) else ""
    for part in (name, *(_SLASH.split(name) if _SLASH.search(name) else ())):
        codes = _from_table(part, table)
        if codes:
            return codes
    return _from_rules(name, table, name=True) or _from_rules(context, table, name=False)
