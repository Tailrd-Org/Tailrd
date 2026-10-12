"""Title seniority (SPEC §5.4; DECISIONS D-38; the rules table is config/parse/seniority.yaml).

`title_seniority` reads a job title as words (case- and accent-insensitive; "cased" rule words such
as "TA", "VP" or "II" only with their casing) and returns the level of the rule whose matching
phrase is longest ("Crew Chief" M1 beats the generic "chief"), ties going to the rule listed first,
so a student marker beats a one-word title ("Product Manager Intern" is IC0) and a management word
beats an IC modifier ("Senior Manager" is M3). A title written for a student organisation
("Treasurer, Students' Union") is IC0; an executive title there ("President, Robotics Club",
"Director of Sponsorship, Hack the North") abstains (DECISIONS D-38a): a club president is no
director, and a wrong management level is worse than none. A title with no rule takes the posting metadata level (`meta_level`: internship IC0, new_grad
IC1, ...) or none; an unknown level makes the seniority dimension abstain (D-38), never guess.

The same tables answer three smaller questions: `strip_seniority_modifiers` (the title without
the words that qualify it, for the titles index), `is_internship_text` (`Role.is_internship`) and
`is_student_organisation`. Pure functions of their inputs.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Optional

from matcher.models import SeniorityLevel, Track
from matcher.parse._text import Token, find_phrase, fold, tokens, words
from matcher.parse.config import ParseConfig, PhraseList, SeniorityRule, parse_level

__all__ = ["is_executive", "is_internship_text", "is_student_organisation", "strip_seniority_modifiers",
           "title_seniority"]

# What ends a part of a title for `at_end` and `followed_within`: , ( ) [ ] | : / ; or a spaced dash.
_PART_BREAK = re.compile(r"[,()\[\]|:/;]|\s[-–—]+\s")
_IC0 = parse_level("IC0")


def _parts(text: str, toks: Sequence[Token]) -> list[int]:
    """The part index of every token."""
    out: list[int] = []
    part = 0
    for k, tok in enumerate(toks):
        if k and _PART_BREAK.search(text, toks[k - 1].end, tok.start) is not None:
            part += 1
        out.append(part)
    return out


def _occurrences(rule: SeniorityRule, toks: Sequence[Token]) -> list[tuple[int, int]]:
    """(first, last) token index of every place a phrase or cased word of `rule` appears."""
    folded = [tok.word for tok in toks]
    found: list[tuple[int, int]] = []
    for phrase in rule.phrase_tuples:
        start = find_phrase(folded, phrase)
        while start >= 0:
            found.append((start, start + len(phrase) - 1))
            start = find_phrase(folded, phrase, start + 1)
    cased = set(rule.cased)
    if cased:
        found.extend((k, k) for k, tok in enumerate(toks) if tok.raw in cased)
    return sorted(set(found))


def _holds(rule: SeniorityRule, toks: Sequence[Token], parts: Sequence[int], first: int, last: int) -> bool:
    sets = rule.sets
    n = len(toks)
    following = toks[last + 1].word if last + 1 < n else None
    preceding = toks[first - 1].word if first > 0 else None
    if rule.whole and (first != 0 or last != n - 1):
        return False
    if rule.at_end and last + 1 < n and parts[last + 1] == parts[last]:
        return False
    if following is not None and following in sets["unless_followed_by"]:
        return False
    if preceding is not None and preceding in sets["unless_preceded_by"]:
        return False
    if sets["preceded_by"] and (preceding is None or preceding not in sets["preceded_by"]):
        return False
    if sets["followed_within"] and not any(
        toks[k].word in sets["followed_within"] for k in range(last + 1, n) if parts[k] == parts[last]
    ):
        return False
    if sets["with_any"] and not any(tok.word in sets["with_any"] for tok in toks):
        return False
    return True


def _matches(rule: SeniorityRule, toks: Sequence[Token], parts: Sequence[int]) -> list[tuple[int, int]]:
    return [(first, last) for first, last in _occurrences(rule, toks) if _holds(rule, toks, parts, first, last)]


def _listed(phrases: PhraseList, folded: Sequence[str]) -> bool:
    """A phrase of the list appears, not inside one of its exceptions and not before one of its
    `unless_followed_by` words."""
    blocked: set[int] = set()
    for exception in phrases.exception_tuples:
        start = find_phrase(folded, exception)
        while start >= 0:
            blocked.update(range(start, start + len(exception)))
            start = find_phrase(folded, exception, start + 1)
    for phrase in phrases.phrase_tuples:
        start = find_phrase(folded, phrase)
        while start >= 0:
            end = start + len(phrase)
            covered = blocked.intersection(range(start, end))
            following = folded[end] if end < len(folded) else None
            if not covered and (following is None or following not in phrases.not_before):
                return True
            start = find_phrase(folded, phrase, start + 1)
    return False


def is_student_organisation(text: str, parse_config: ParseConfig) -> bool:
    """Whether `text` (an employer or a title) names a student organisation ("Robotics Club",
    "Engineering Society", "Association étudiante"); businesses named "club" are not ("Golf Club")."""
    if not isinstance(text, str) or not text:
        return False
    return _listed(parse_config.seniority.student_organisations, words(text))


def is_internship_text(text: str, parse_config: ParseConfig) -> bool:
    """Whether a title or a date text marks an internship: intern, co-op, stage, stagiaire, work
    term ("Stage Manager" and "Internship Coordinator" are not)."""
    if not isinstance(text, str) or not text:
        return False
    return _listed(parse_config.seniority.internship, words(text))


def _meta(meta_level: Optional[str], parse_config: ParseConfig) -> Optional[SeniorityLevel]:
    if not isinstance(meta_level, str) or not meta_level.strip():
        return None
    index = parse_config.seniority.meta_index
    key = fold(meta_level).strip()
    if key in index:
        return index[key]
    loose = re.sub(r"[\s_\-]+", "_", key)
    for known, level in index.items():
        if re.sub(r"[\s_\-]+", "_", known) == loose:
            return level
    return None


def is_executive(level: Optional[SeniorityLevel]) -> bool:
    """Director and above (M4-M6): founder, CEO, president, VP, director, head of."""
    return level is not None and Track(level.track) is Track.MGMT and level.level >= 4


# Organisation words that, next to an executive title, mark a student body ("President, Women in
# STEM Society"); a company is rarely written into the title itself.
_STUDENT_BODY_WORDS = frozenset({"society", "association", "club", "council", "union", "chapter"})


def _rule_level(toks: Sequence[Token], parts: Sequence[int], parse_config: ParseConfig) -> Optional[SeniorityLevel]:
    """The level of the rule whose matching phrase is longest; ties go to the rule listed first."""
    best: Optional[tuple[int, int, SeniorityLevel]] = None
    for order, rule in enumerate(parse_config.seniority.rules):
        for first, last in _matches(rule, toks, parts):
            key = (last - first + 1, -order)
            if best is None or key > best[:2]:
                best = (key[0], key[1], rule.seniority)
    return None if best is None else best[2]


def title_seniority(title: str, parse_config: ParseConfig, *, meta_level: str | None = None) -> Optional[SeniorityLevel]:
    """The SPEC §5.4 level of a job title (see the module docstring); None when unknown. A title
    naming a student organisation is IC0, except an executive one (club president, VP of a
    students' union, hackathon director), which abstains: a wrong management level is worse than
    none."""
    text = title if isinstance(title, str) else ""
    toks = tokens(text)
    if toks:
        parts = _parts(text, toks)
        level = _rule_level(toks, parts, parse_config)
        executive = is_executive(level) or any(
            is_executive(rule.seniority) and _matches(rule, toks, parts) for rule in parse_config.seniority.rules)
        student_body = is_student_organisation(text, parse_config) or (
            executive and any(tok.word in _STUDENT_BODY_WORDS for tok in toks))
        if student_body:
            return None if executive else _IC0
        if level is not None:
            return level
    return _meta(meta_level, parse_config)


_EMPTY_BRACKETS = re.compile(r"\(\s*[,;|/\-–—]*\s*\)|\[\s*[,;|/\-–—]*\s*\]")
_EDGES = " \t,;:|/.·•–—-"


def strip_seniority_modifiers(title: str, parse_config: ParseConfig) -> str:
    """`title` without the words of the rules marked `modifier` ("Senior Software Engineer" ->
    "Software Engineer", "Software Developer (Co-op)" -> "Software Developer"); the words that
    name an occupation stay ("Teaching Assistant", "Engineering Manager")."""
    text = title if isinstance(title, str) else ""
    toks = tokens(text)
    if not toks:
        return ""
    parts = _parts(text, toks)
    spans: list[tuple[int, int]] = []
    for rule in parse_config.seniority.rules:
        if rule.modifier:
            spans.extend((toks[first].start, toks[last].end) for first, last in _matches(rule, toks, parts))
    if not spans:
        return " ".join(text.split())
    kept: list[str] = []
    position = 0
    for start, end in sorted(spans):
        if start >= position:
            kept.append(text[position:start])
            position = end
        else:
            position = max(position, end)
    kept.append(text[position:])
    out = " ".join(" ".join(kept).split())
    previous = None
    while previous != out:
        previous = out
        out = _EMPTY_BRACKETS.sub(" ", out)
        out = re.sub(r" ([,;)\]])", r"\1", " ".join(out.split()))
        out = out.strip(_EDGES)
    return out
