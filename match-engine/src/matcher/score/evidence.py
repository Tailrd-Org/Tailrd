"""Evidence strength: verbs, negation, quantified outcomes, E and R.

SPEC §5.1 (E, R) and §15; DECISIONS D-32, D-33, D-34, D-34a..D-34f.

`analyze_bullet` gives every skill mention of a résumé bullet a verb class and a polarity. The
caller (parse/) has already found the mentions and resolved them to canonical ids. The intent of
SPEC §15: credit demonstrated work by its verb, and stop interest or negation from counting as
experience, without ever zeroing a skill the bullet demonstrates.

* Clauses (D-34a). The bullet splits at `verbs.clause_punctuation` and at the coordinating
  conjunctions (whole words, any case). Not boundaries: punctuation inside a word or number
  ("Node.js", "3.5", "1,000"), the period of an abbreviation ("U.S.", "e.g.", "Inc."), a "yet"
  right after a negation ("not yet used") and anything inside a mention span.
* Verb (§15.1, D-34d, D-34e). The nearest `verbs.yaml` entry in attaching position that ends
  before the mention inside its clause gives the verb class. A base or third-person form
  ("design", "support", "engineers") attaches only as the clause's first word or right after
  "to"; a noun entry ("exposure") only before of/to/with/in; past and -ing forms anywhere. Words
  inside a mention are a skill name, never a verb. An instrument participle
  (`verbs.defer_to_earlier`: "using", ...) defers to an earlier verb. A verb chained to an
  earlier one, directly or across only to/in/with/on, takes the head's class: "Helped to build"
  is ASSISTANCE, "Contributed to building" PARTICIPATION.
* Inheritance (D-34a, D-34b, D-34f). A clause with no verb (a deferring participle alone does
  not count) takes the previous clause's verb class when it holds only mentions, list words,
  articles and punctuation, or when it follows and/or/nor inside a sentence. Only such a pure
  list also takes the previous clause's polarity, only from a mention there, and never across a
  sentence end (. ; ! ?) or "but"/"yet".
* Polarity (§15.2, D-34c). A hard-negation term negates a mention only when every token between
  them is a scope filler (`negation.hard_negation.scope_fillers`), another mention, an
  aspiration term or a "yet" right after the term; otherwise it governs the noun it opens
  ("with no downtime using Kubernetes"). A deferring participle right after a mention opens a
  new tool and stops the negation ("without Redis using Nginx"). An exception phrase ("not
  only") and a term joined by a hyphen ("no-code", "not-for-profit") never negate; "n't" counts
  as "not". Else an aspiration term among the `negation.window_tokens` tokens before the
  mention (another mention counts as one token, punctuation as none; a phrase only whole) makes
  it ASPIRATIONAL.

The lexicons are config (verbs.yaml, negation.yaml). The short closed word lists below are
grammar for reading a bullet, not weights to tune.
"""

from __future__ import annotations

import math
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator, Sequence
from datetime import date
from typing import NamedTuple, Optional

from pydantic import Field, model_validator

from matcher.config import EngineConfig
from matcher.models import Evidence, Frozen, Polarity, VerbClass
from matcher.score.numeric import clamp01

__all__ = [
    "ABBREVIATIONS",
    "ARTICLES",
    "CHAIN_LINKS",
    "LIST_CONJUNCTIONS",
    "LIST_WORDS",
    "OUTCOME_VERBS",
    "POLARITY_BARRIERS",
    "SENTENCE_ENDS",
    "BulletMention",
    "MentionEvidence",
    "analyze_bullet",
    "apply_polarity",
    "evidence_multiplier",
    "is_quantified",
    "recency_multiplier",
]

# Words a list-continuation clause may hold besides mentions and punctuation (DECISIONS D-34a).
# "&" is punctuation to the tokenizer and so always allowed; it is listed for completeness.
LIST_WORDS: tuple[str, ...] = ("and", "or", "&", "as well as", "plus", "including", "with")
ARTICLES: tuple[str, ...] = ("a", "an", "the")

# Words that may join a verb chain: "Helped to build", "Participated in designing" (D-34d).
CHAIN_LINKS: frozenset[str] = frozenset({"to", "in", "with", "on"})

# A verbless clause after one of these inherits the verb class whatever it holds (D-34f).
LIST_CONJUNCTIONS: frozenset[str] = frozenset({"and", "or", "nor"})

# Polarity never crosses a sentence end or a contrast (D-34b).
SENTENCE_ENDS: frozenset[str] = frozenset({".", ";", "!", "?"})
POLARITY_BARRIERS: frozenset[str] = SENTENCE_ENDS | {"but", "yet"}

# Abbreviations whose period ends no clause; dotted letter runs ("U.S.", "e.g.", "i.e.") too.
ABBREVIATIONS: frozenset[str] = frozenset({
    "approx", "co", "corp", "dept", "dr", "est", "etc", "govt", "inc", "intl", "jr", "ltd", "mr", "mrs",
    "ms", "prof", "sr", "st", "univ", "vs",
})

# Verbs after which "by <number>" / "to <number>" states an outcome ("reduced latency by 120ms").
OUTCOME_VERBS: frozenset[str] = frozenset({
    "increase", "increased", "increases", "increasing",
    "decrease", "decreased", "decreases", "decreasing",
    "reduce", "reduced", "reduces", "reducing",
    "improve", "improved", "improves", "improving",
    "grow", "grew", "grown", "grows", "growing",
    "cut", "cuts", "cutting",
    "save", "saved", "saves", "saving",
    "boost", "boosted", "boosts", "boosting",
    "lower", "lowered", "lowers", "lowering",
    "accelerate", "accelerated", "accelerates", "accelerating",
})


# ─── Contract ─────────────────────────────────────────────────────────────────────────────


class BulletMention(Frozen):
    """A skill mention in a bullet: canonical id and character span `[start, end)`."""

    canonical_id: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0)
    end: int = Field(ge=1)

    @model_validator(mode="after")
    def _span(self) -> "BulletMention":
        if self.end <= self.start:
            raise ValueError("a mention span needs end > start")
        return self


class MentionEvidence(Frozen):
    """The verb class and polarity of one mention (SPEC §15). `verb` is the lexicon entry that
    attached, the head of its chain ("" for NO_VERB); `trigger` the negation or aspiration term
    that fired ("" when AFFIRMED). An inheriting clause carries the verb and trigger it took."""

    canonical_id: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    verb_class: VerbClass
    polarity: Polarity
    verb: str = ""
    trigger: str = ""


# ─── Lexicons, built once per config ─────────────────────────────────────────────────────

# A word: letters and digits joined by an in-word "." or apostrophe ("Node.js", "3.5", "didn't")
# or by a thousands comma ("1,000"). Hyphens and slashes separate words ("co-led" -> co, led).
_WORD = re.compile(r"[^\W_]+(?:(?:[.'\u2019]|(?<=[0-9]),(?=[0-9]{3}(?![0-9])))[^\W_]+)*")
_DOTTED_ABBREVIATION = re.compile(r"(?:[^\W\d_]\.)+[^\W\d_]")  # "u.s", "e.g", "i.e"
_HYPHENS = frozenset({"-", "\u2010", "\u2011"})


class _Match(NamedTuple):
    first: int  # index of the first unit matched
    stop: int  # index one past the last unit matched
    text: str  # the normalised lexicon entry, e.g. "coursework in"
    verb_class: Optional[VerbClass]  # for verb entries


_Lexicon = dict[str, list[tuple[tuple[str, ...], str, Optional[VerbClass]]]]


class _Lexicons(NamedTuple):
    verbs: _Lexicon
    position_bound: frozenset[str]  # base and third-person forms (DECISIONS D-34e)
    nouns: frozenset[str]
    noun_followers: frozenset[str]
    defer: frozenset[str]
    conjunctions: _Lexicon
    hard: _Lexicon
    exceptions: _Lexicon
    fillers: _Lexicon
    aspirational: _Lexicon
    listing: _Lexicon
    version_names: frozenset[str]  # lower-case names before a version number (is_quantified)
    counting_nouns: frozenset[str]  # nouns that make "<number> ... <noun>" a quantity (is_quantified)


def _normalised(entry: str) -> str:
    return " ".join(word.lower() for word in _WORD.findall(entry))


def _lexicon(entries: Iterable[tuple[str, Optional[VerbClass]]]) -> _Lexicon:
    """Entries tokenised like the text and indexed by their first word."""
    index: _Lexicon = {}
    for entry, verb_class in entries:
        words = tuple(word.lower() for word in _WORD.findall(entry))
        if words:
            index.setdefault(words[0], []).append((words, " ".join(words), verb_class))
    return index


def _base_or_third_person(form: str, forms: frozenset[str]) -> bool:
    """Is `form` a base form (its -s/-es sibling is listed: "design"/"designs") or that third-
    person sibling? Past and -ing forms and phrases are neither (DECISIONS D-34e)."""
    if " " in form or form.endswith(("ed", "ing")):
        return False
    if f"{form}s" in forms or f"{form}es" in forms:
        return True
    return form.endswith("s") and (form[:-1] in forms or (form.endswith("es") and form[:-2] in forms))


def _build_lexicons(config: EngineConfig) -> _Lexicons:
    verbs, negation = config.verbs, config.negation
    position_bound: set[str] = set()
    for lexicon in verbs.classes.values():
        forms = frozenset(_normalised(entry) for entry in lexicon.verbs)
        position_bound.update(form for form in forms if _base_or_third_person(form, forms))
    return _Lexicons(
        verbs=_lexicon(
            (entry, verb_class) for verb_class, lexicon in verbs.classes.items() for entry in lexicon.verbs
        ),
        position_bound=frozenset(position_bound),
        nouns=frozenset(_normalised(entry) for entry in verbs.noun_entries),
        noun_followers=frozenset(_normalised(entry) for entry in verbs.noun_entry_followers),
        defer=frozenset(_normalised(entry) for entry in verbs.defer_to_earlier),
        conjunctions=_lexicon((entry, None) for entry in verbs.coordinating_conjunctions),
        hard=_lexicon((term, None) for term in negation.hard_negation.terms),
        exceptions=_lexicon((term, None) for term in negation.hard_negation.exceptions),
        fillers=_lexicon((term, None) for term in negation.hard_negation.scope_fillers),
        aspirational=_lexicon((term, None) for term in negation.aspirational.terms),
        listing=_lexicon((word, None) for word in LIST_WORDS + ARTICLES),
        version_names=frozenset(_normalised(name) for name in verbs.version_names),
        counting_nouns=frozenset(_normalised(noun) for noun in verbs.counting_nouns),
    )


_LEXICON_CACHE: dict[str, _Lexicons] = {}
_LEXICON_CACHE_SIZE = 16


def _lexicons(config: EngineConfig) -> _Lexicons:
    """The lexicon structures of `config`, built once per config content hash. Deterministic
    memoisation: nothing cached ever reaches an output."""
    key = config.content_hash
    found = _LEXICON_CACHE.get(key)
    if found is None:
        found = _build_lexicons(config)
        if len(_LEXICON_CACHE) >= _LEXICON_CACHE_SIZE:
            _LEXICON_CACHE.clear()
        _LEXICON_CACHE[key] = found
    return found


# ─── Reading a bullet: units and clauses ─────────────────────────────────────────────────

_WORD_UNIT, _MENTION_UNIT, _BOUNDARY_UNIT = 0, 1, 2


class _Unit(NamedTuple):
    start: int
    end: int
    kind: int  # _WORD_UNIT, _MENTION_UNIT or _BOUNDARY_UNIT
    word: str = ""  # lower-cased word, or the boundary itself
    component: int = -1  # index of the merged mention span, for a mention
    compound: bool = False  # joined by a hyphen to a neighbouring word ("no" of "no-code")


class _Clause(NamedTuple):
    units: list[_Unit]
    after: frozenset[str]  # the boundaries between this clause and the previous one


def _find(units: Sequence[_Unit], lexicon: _Lexicon) -> list[_Match]:
    """Every occurrence of an entry as consecutive words of `units`. A mention or a boundary
    breaks a phrase, so an entry never matches inside a skill name."""
    found: list[_Match] = []
    for i, unit in enumerate(units):
        if unit.kind != _WORD_UNIT:
            continue
        for words, text, verb_class in lexicon.get(unit.word, ()):
            stop = i + len(words)
            if stop <= len(units) and all(
                units[i + k].kind == _WORD_UNIT and units[i + k].word == words[k] for k in range(1, len(words))
            ):
                found.append(_Match(i, stop, text, verb_class))
    return found


def _mark(flags: list[bool], matches: Iterable[_Match]) -> None:
    for match in matches:
        flags[match.first:match.stop] = [True] * (match.stop - match.first)


def _reach(match: _Match) -> tuple[int, int]:
    """Nearness order: where a match ends, then its length (of two phrases ending together, the
    longer one is the nearer match)."""
    return match.stop, match.stop - match.first


def _merge(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Mention spans merged where they overlap: each merged span is one token of the bullet."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlaps(spans: Sequence[tuple[int, int]], starts: Sequence[int], start: int, end: int) -> bool:
    """Does `[start, end)` overlap one of `spans` (sorted and disjoint; `starts` their starts)?"""
    k = bisect_left(starts, end) - 1
    return k >= 0 and spans[k][1] > start


def _occurrences(text: str, marks: Iterable[str]) -> Iterator[tuple[int, int]]:
    for mark in marks:
        if not mark:
            continue
        at = text.find(mark)
        while at >= 0:
            yield at, at + len(mark)
            at = text.find(mark, at + len(mark))


def _in_compound(text: str, start: int, end: int) -> bool:
    """Is the word at `[start, end)` joined by a hyphen to a neighbouring word?"""
    joined_before = start >= 2 and text[start - 1] in _HYPHENS and text[start - 2].isalnum()
    joined_after = end + 1 < len(text) and text[end] in _HYPHENS and text[end + 1].isalnum()
    return joined_before or joined_after


def _is_abbreviation(word: Optional[str]) -> bool:
    return word is not None and (word in ABBREVIATIONS or _DOTTED_ABBREVIATION.fullmatch(word) is not None)


def _negation_view(units: Sequence[_Unit]) -> list[_Unit]:
    """The units as the negation lexicon reads them: a contraction in "n't" is "not"."""
    return [
        unit._replace(word="not")
        if unit.kind == _WORD_UNIT and len(unit.word) > 3 and unit.word.endswith(("n't", "n\u2019t"))
        else unit
        for unit in units
    ]


def _clauses(text: str, components: Sequence[tuple[int, int]], lex: _Lexicons,
             config: EngineConfig) -> list[_Clause]:
    """The bullet as clauses of word and mention units, boundaries removed (SPEC §15.1)."""
    words = [(found.start(), found.end(), found.group().lower()) for found in _WORD.finditer(text)]
    word_spans = [(start, end) for start, end, _ in words]
    word_starts = [start for start, _ in word_spans]
    word_ending_at = {end: word for _, end, word in words}
    starts = [start for start, _ in components]
    units = [
        _Unit(s, e, _WORD_UNIT, word, compound=_in_compound(text, s, e))
        for s, e, word in words
        if not _overlaps(components, starts, s, e)
    ]
    units += [_Unit(s, e, _MENTION_UNIT, component=k) for k, (s, e) in enumerate(components)]
    for s, e in _occurrences(text, config.verbs.clause_punctuation):
        if _overlaps(word_spans, word_starts, s, e) or _overlaps(components, starts, s, e):
            continue  # inside a word, a number or a skill name
        if text[s:e] == "." and _is_abbreviation(word_ending_at.get(s)):
            continue  # "U.S.", "e.g.", "Inc."
        units.append(_Unit(s, e, _BOUNDARY_UNIT, text[s:e]))
    units.sort()
    cut = [unit.kind == _BOUNDARY_UNIT for unit in units]
    after_negation = {match.stop for match in _find(_negation_view(units), lex.hard)}
    for match in _find(units, lex.conjunctions):
        if match.text == "yet" and match.first in after_negation:
            continue  # "not yet used": part of the negation
        cut[match.first:match.stop] = [True] * (match.stop - match.first)
    clauses: list[_Clause] = []
    pending: list[str] = []
    for unit, is_cut in zip(units, cut):
        if is_cut:
            pending.append(unit.word)
        elif clauses and not pending:
            clauses[-1].units.append(unit)
        else:
            clauses.append(_Clause([unit], frozenset(pending)))
            pending = []
    return clauses


def _only_list_words(clause: Sequence[_Unit], listing: _Lexicon) -> bool:
    """Does the clause hold nothing but mentions, list words and articles (punctuation never
    becomes a unit)?"""
    covered = [unit.kind == _MENTION_UNIT for unit in clause]
    _mark(covered, _find(clause, listing))
    return all(covered)


def _attaches(units: Sequence[_Unit], match: _Match, lex: _Lexicons) -> bool:
    """Is a verb entry in attaching position (DECISIONS D-34e)?"""
    if match.text in lex.nouns:  # "exposure to Kafka", not "brand awareness campaigns"
        return (match.stop < len(units) and units[match.stop].kind == _WORD_UNIT
                and units[match.stop].word in lex.noun_followers)
    if match.text in lex.position_bound:  # "Design APIs", "to design", not "design docs"
        k = match.first
        return k == 0 or (units[k - 1].kind == _WORD_UNIT and units[k - 1].word == "to")
    return True


_NO_VERB = (VerbClass.NO_VERB, "")
_AFFIRMED = (Polarity.AFFIRMED, "")


class _ClauseReader:
    """Verb and polarity lookups for the mentions of one clause (SPEC §15).

    Matches are sorted once, so each lookup is a bisection plus a scan bounded by the window: a
    long bullet costs O(n log n), never mentions × verbs.
    """

    def __init__(self, units: Sequence[_Unit], lex: _Lexicons, window: int) -> None:
        verbs = [match for match in _find(units, lex.verbs) if _attaches(units, match, lex)]
        # A clause whose only verbs defer ("reports using Tableau") is verbless for D-34f.
        self.has_verbs = any(match.text not in lex.defer for match in verbs)
        # Chain heads (D-34d): a verb right after another, or after only link words, takes the
        # earlier verb's head.
        heads: dict[tuple[int, int], tuple[VerbClass, str]] = {}
        head_ending_at: dict[int, tuple[VerbClass, str]] = {}
        for match in sorted(verbs, key=lambda m: (m.first, m.stop)):
            j = match.first
            head = head_ending_at.get(j)
            while head is None and j > 0 and units[j - 1].kind == _WORD_UNIT and units[j - 1].word in CHAIN_LINKS:
                j -= 1
                head = head_ending_at.get(j)
            own = (match.verb_class, match.text) if match.verb_class is not None else _NO_VERB
            heads[(match.first, match.stop)] = head if head is not None else own
            head_ending_at.setdefault(match.stop, heads[(match.first, match.stop)])
        ordered = sorted(verbs, key=_reach)
        self._verb_stops = [match.stop for match in ordered]
        self._verb_heads = [heads[(match.first, match.stop)] for match in ordered]
        # _last_action[k]: index of the nearest verb among the first k that does not defer.
        self._last_action = [-1]
        for k, match in enumerate(ordered):
            self._last_action.append(k if match.text not in lex.defer else self._last_action[-1])

        negation = _negation_view(units)
        excepted = [False] * len(units)
        _mark(excepted, _find(negation, lex.exceptions))
        hard = [
            match for match in _find(negation, lex.hard)
            if not any(excepted[k] or units[k].compound for k in range(match.first, match.stop))
        ]
        aspirations = _find(units, lex.aspirational)
        # What a hard negation reaches across (D-34c): scope fillers, other mentions, aspiration
        # terms, and a "yet" right after the term.
        passable = [unit.kind == _MENTION_UNIT for unit in units]
        _mark(passable, _find(units, lex.fillers))
        _mark(passable, aspirations)
        for k in range(1, len(units)):  # "without Redis using Nginx": the participle opens a new tool
            if units[k].kind == _WORD_UNIT and units[k].word in lex.defer and units[k - 1].kind == _MENTION_UNIT:
                passable[k] = False
        term_ends = {match.stop for match in hard}
        for k, unit in enumerate(units):
            if unit.kind == _WORD_UNIT and unit.word == "yet" and k in term_ends:
                passable[k] = True
        # _run_start[p]: where the run of passable units that ends at p begins.
        self._run_start = [0]
        for k, ok in enumerate(passable):
            self._run_start.append(self._run_start[-1] if ok else k + 1)
        self._hard = sorted(hard, key=_reach)
        self._hard_stops = [match.stop for match in self._hard]
        self._aspirations = sorted(aspirations, key=_reach)
        self._aspiration_stops = [match.stop for match in self._aspirations]
        self._window = window

    def verb_at(self, position: int) -> Optional[tuple[VerbClass, str]]:
        """The verb (head class, head text) of a mention at `position`; None when none attaches."""
        k = bisect_right(self._verb_stops, position)  # verbs ending before the mention
        if not k:
            return None
        action = self._last_action[k]  # an instrument participle defers to an earlier verb
        return self._verb_heads[action if action >= 0 else k - 1]

    def polarity_at(self, position: int) -> Optional[tuple[Polarity, str]]:
        """The polarity a mention at `position` takes from its own clause; None when no term
        reaches it."""
        k = bisect_right(self._hard_stops, position) - 1  # the nearest hard term before it
        if k >= 0 and self._hard_stops[k] >= self._run_start[position]:
            return Polarity.NEGATED, self._hard[k].text
        first = max(0, position - self._window)
        j = bisect_right(self._aspiration_stops, position) - 1
        while j >= 0 and self._aspirations[j].stop > first:  # nearest first, inside the window
            if self._aspirations[j].first >= first:
                return Polarity.ASPIRATIONAL, self._aspirations[j].text
            j -= 1
        return None


class _Attachment(NamedTuple):
    verb_class: VerbClass
    verb: str
    polarity: Polarity
    trigger: str


def _verb_for(
    inherited: Optional[tuple[VerbClass, str]], reader: _ClauseReader, position: int
) -> tuple[VerbClass, str]:
    """The inherited verb, or the clause's own: an inherited NO_VERB never hides the clause's
    deferring participle ("Shipped features and reports using Tableau" attaches "using")."""
    if inherited is not None and inherited[0] is not VerbClass.NO_VERB:
        return inherited
    return reader.verb_at(position) or _NO_VERB


class _Handoff(NamedTuple):
    """What a clause hands the next one: its verb, and its polarity when it holds a mention."""

    verb: tuple[VerbClass, str]
    polarity: Optional[tuple[Polarity, str]]


def analyze_bullet(
    text: str, mentions: Sequence[BulletMention], config: EngineConfig
) -> tuple[MentionEvidence, ...]:
    """Verb class and polarity of every mention, in the order given (SPEC §15).

    Raises ValueError when a mention span lies outside `text`.
    """
    mentions = tuple(mentions)
    if not mentions:
        return ()
    for mention in mentions:
        if mention.end > len(text):
            raise ValueError(
                f"mention {mention.canonical_id!r} [{mention.start}, {mention.end}) lies outside the "
                f"bullet ({len(text)} characters)"
            )
    lex = _lexicons(config)
    window = config.negation.window_tokens
    inherit = config.verbs.list_continuation_inherits_verb
    components = _merge((mention.start, mention.end) for mention in mentions)

    attached: dict[int, _Attachment] = {}
    previous: Optional[_Handoff] = None
    for clause in _clauses(text, components, lex, config):
        reader = _ClauseReader(clause.units, lex, window)
        pure_list = _only_list_words(clause.units, lex.listing)
        inherited: Optional[tuple[VerbClass, str]] = None  # D-34a pure list, D-34f after and/or/nor
        if inherit and previous is not None and not reader.has_verbs and (
            pure_list or (bool(clause.after & LIST_CONJUNCTIONS) and not clause.after & SENTENCE_ENDS)
        ):
            inherited = previous.verb
        handed = None  # polarity from a mention of the previous clause, into a pure list only (D-34b)
        if inherited is not None and pure_list and previous is not None and not clause.after & POLARITY_BARRIERS:
            handed = previous.polarity
        last: Optional[_Attachment] = None
        for position, unit in enumerate(clause.units):
            if unit.kind != _MENTION_UNIT:
                continue
            verb = _verb_for(inherited, reader, position)
            polarity = reader.polarity_at(position) or handed or _AFFIRMED
            last = attached[unit.component] = _Attachment(verb[0], verb[1], polarity[0], polarity[1])
        if last is not None:
            previous = _Handoff((last.verb_class, last.verb), (last.polarity, last.trigger))
        else:  # a clause without a mention hands on its verb class only
            previous = _Handoff(_verb_for(inherited, reader, len(clause.units)), None)

    starts = [start for start, _ in components]
    result = []
    for mention in mentions:
        attachment = attached[bisect_right(starts, mention.start) - 1]
        result.append(
            MentionEvidence(
                canonical_id=mention.canonical_id,
                start=mention.start,
                end=mention.end,
                verb_class=attachment.verb_class,
                polarity=attachment.polarity,
                verb=attachment.verb,
                trigger=attachment.trigger,
            )
        )
    return tuple(result)


# ─── Quantified outcomes (ROLE_BULLET_QUANTIFIED) ────────────────────────────────────────

# A number (thousands commas, decimals) with any letters glued to it ("10k", "120ms"); a word,
# which may hold digits ("ES6", "S3", "v2.0", "COVID-19"); or one symbol.
_QTOKEN = re.compile(
    r"(?P<num>(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)(?P<tail>[^\W_]*)"
    r"|(?P<word>[^\W\d_][^\W_]*(?:['\u2019.\-][^\W_]+)*)"
    r"|(?P<sym>[^\w\s])"
)
_MAGNITUDES = frozenset({"k", "K", "M", "B", "x", "X"})  # glued to the number: 10k, 2M, 1.5B, 3x
_MULTIPLY = "\u00d7"  # the multiplication sign, as in "4×"
_SCALE_WORDS = frozenset({"percent", "times", "thousand", "million", "billion", "trillion"})
_NOUN_SCAN_STOPS = frozenset({".", "!", "?", ";", ",", ":"})
_SENTENCE_OPENERS = frozenset({".", "!", "?", ";", ":", "*", "-", "\u2022", "\u2013", "\u2014"})


class _QToken(NamedTuple):
    kind: str  # "num", "word" or "sym"
    text: str  # a number without its tail, a word, or one symbol
    tail: str  # letters glued to a number ("k" of "10k", "ms" of "120ms"); "" otherwise
    start: int
    end: int  # end of the whole token, tail included


def _qtokens(text: str) -> list[_QToken]:
    tokens: list[_QToken] = []
    for found in _QTOKEN.finditer(text):
        if found.group("num") is not None:
            tokens.append(_QToken("num", found.group("num"), found.group("tail"), found.start(), found.end()))
        elif found.group("word") is not None:
            tokens.append(_QToken("word", found.group("word"), "", found.start(), found.end()))
        else:
            tokens.append(_QToken("sym", found.group("sym"), "", found.start(), found.end()))
    return tokens


def _is_currency(symbol: str) -> bool:
    return len(symbol) == 1 and unicodedata.category(symbol) == "Sc"


def _is_year(token: _QToken) -> bool:
    """A bare four-digit year 1900–2100 (no separator, decimal or unit)."""
    return token.kind == "num" and not token.tail and len(token.text) == 4 and "1900" <= token.text <= "2100"


def _at_least_two(number: str) -> bool:
    """`number` ≥ 2, decided on the digits (an absurdly long number never becomes a float)."""
    whole = number.split(".", 1)[0].replace(",", "").lstrip("0")
    return len(whole) > 1 or (whole != "" and whole >= "2")


def _word(tokens: Sequence[_QToken], i: int) -> str:
    """Token `i` lower-cased when it is a word; "" otherwise."""
    return tokens[i].text.lower() if 0 <= i < len(tokens) and tokens[i].kind == "word" else ""


def _currency_before(tokens: Sequence[_QToken], i: int, text: str) -> bool:
    if i == 0:
        return False
    sign = tokens[i - 1]
    return sign.kind == "sym" and _is_currency(sign.text) and text[sign.end:tokens[i].start] in ("", " ")


def _after_name(tokens: Sequence[_QToken], i: int, text: str, names: frozenset[str]) -> bool:
    """Is number `i` a version right after a tech name ("React 18", "react 18", "Java 8+")?

    The word just before it, separated by spaces only, must not open the sentence (the first
    word of a bullet is its verb: "Managed 3 servers"). A word holding a capital is a name; a
    lower-case word is one only when it is in `verbs.version_names` ("react", "java"): any
    other lower-case word opens a count ("maintain 12 microservices", "all 40 services").
    """
    if i < 2:
        return False
    name, opener = tokens[i - 1], tokens[i - 2]
    if name.kind != "word" or not text[name.end:tokens[i].start].isspace():
        return False
    if opener.kind == "sym" and opener.text in _SENTENCE_OPENERS:
        return False
    if any(char.isupper() for char in name.text):
        return True
    return name.text.lower() in names


def _next_words(tokens: Sequence[_QToken], i: int, text: str, limit: int = 2) -> Iterator[str]:
    """The next `limit` word or number tokens after token `i`, lower-cased. Symbols are skipped,
    a clause mark ends the scan, and a word with its number ("Grade 9", "Fortune 500") is one
    token."""
    seen, j = 0, i + 1
    while j < len(tokens):
        token = tokens[j]
        if token.kind == "sym":
            if token.text in _NOUN_SCAN_STOPS:
                return
            j += 1
            continue
        yield token.text.lower()
        seen += 1
        if seen == limit:
            return
        follower = tokens[j + 1] if j + 1 < len(tokens) else None
        paired = (token.kind == "word" and follower is not None and follower.kind == "num"
                  and not follower.tail and text[token.end:follower.start].isspace())
        j += 2 if paired else 1


def _is_quantity(
    tokens: Sequence[_QToken], i: int, text: str, names: frozenset[str], nouns: frozenset[str]
) -> bool:
    token = tokens[i]
    if _currency_before(tokens, i, text):  # $120K, €50k
        return True
    if token.tail:  # 10k, 2M, 1.5B, 3x, 10X, "1000s of"; not 120ms, 3D, 2nd, 1990s
        return token.tail in _MAGNITUDES or (token.tail.lower() == "s" and _word(tokens, i + 1) == "of")
    following = tokens[i + 1] if i + 1 < len(tokens) else None
    if following is not None:
        gap = text[token.end:following.start]
        if following.kind == "sym":
            if following.text == "%" and gap in ("", " "):
                return True
            if following.text == _MULTIPLY and not gap:
                return True
            if following.text == "+" and not gap:  # 500+, but not "Java 8+"
                return not _after_name(tokens, i, text, names)
        elif following.kind == "word" and following.text.lower() in _SCALE_WORDS:
            return True  # 30 percent, 3 times, 2 million
    if not _at_least_two(token.text):
        return False
    if _word(tokens, i - 1) == "of" and _word(tokens, i - 2) in ("team", "teams"):
        return True  # "a team of 4"
    if _after_name(tokens, i, text, names):
        return False
    for distance, word in enumerate(_next_words(tokens, i, text), start=1):
        if word in nouns:
            # a year is "on its own" unless the noun follows it directly ("2000 employees")
            return distance == 1 or not _is_year(token)
    return False


def _amount_follows(tokens: Sequence[_QToken], i: int) -> bool:
    """Does an amount follow "by"/"to" at token `i` ("by 40", "to $3", "to 0.94")? A year does
    not ("by 2024")."""
    j = i + 1
    currency = j < len(tokens) and tokens[j].kind == "sym" and _is_currency(tokens[j].text)
    if currency:
        j += 1
    if j >= len(tokens) or tokens[j].kind != "num":
        return False
    return currency or not _is_year(tokens[j])


def is_quantified(text: str, config: EngineConfig) -> bool:
    """Does the bullet state a quantified outcome (ROLE_BULLET_QUANTIFIED)?

    True for a number followed by "%" / "percent", preceded by a currency sign, followed by
    k/K/M/B/x/X/×/+ or by "times" / "thousand" / "million" / "billion"; "1000s of"; "team of N"
    (N ≥ 2); a number ≥ 2 followed within two tokens by a counting noun (`verbs.counting_nouns`) ("15 Grade 9 students":
    a name with its number is one token); or a number after "by"/"to" following an outcome verb
    in the same sentence. Never for a year on its own or a version right after a tech name
    ("React 18", "Python 3", "ES6"; in lower case only after `verbs.version_names`: "react 18").
    """
    lexicons = _lexicons(config)
    names, nouns = lexicons.version_names, lexicons.counting_nouns
    tokens = _qtokens(text)
    after_outcome_verb = False
    for i, token in enumerate(tokens):
        if token.kind == "sym":
            if token.text in SENTENCE_ENDS:
                after_outcome_verb = False
        elif token.kind == "word":
            word = token.text.lower()
            if word in OUTCOME_VERBS:
                after_outcome_verb = True
            elif after_outcome_verb and word in ("by", "to") and _amount_follows(tokens, i):
                return True
        elif _is_quantity(tokens, i, text, names, nouns):
            return True
    return False


# ─── E, R and the polarity override ──────────────────────────────────────────────────────


def evidence_multiplier(evidence: Evidence, verb_class: VerbClass, config: EngineConfig) -> float:
    """E (DECISIONS D-32): the evidence tier, with the §15.1 verb multiplier replacing the coarse
    tier for bullets; a quantified bullet multiplies it by `quantified_bonus`, capped."""
    evidence, verb_class = Evidence(evidence), VerbClass(verb_class)
    verbs = config.verbs
    if evidence is Evidence.ROLE_BULLET:
        value = verbs.multiplier(verb_class)
    elif evidence is Evidence.ROLE_BULLET_QUANTIFIED:
        value = min(verbs.quantified_cap, verbs.multiplier(verb_class) * verbs.quantified_bonus)
    else:
        tiers = config.scoring.evidence
        value = {
            Evidence.SKILLS_BLOB: tiers.SKILLS_BLOB,
            Evidence.ROLE_TITLE: tiers.ROLE_TITLE,
            Evidence.CERTIFICATION: tiers.CERTIFICATION,
        }[evidence]
    return clamp01(value)


def recency_multiplier(last_used: Optional[date], as_of: date, config: EngineConfig) -> float:
    """R (SPEC §5.1, DECISIONS D-34): `max(floor, exp(-decay_per_year × years))` with
    `years = (as_of - last_used).days / 365.25`, never negative; undated (None) is 1.0."""
    if last_used is None:
        return 1.0
    years = max(0.0, (as_of - last_used).days / 365.25)
    recency = config.scoring.recency
    return clamp01(max(recency.floor, math.exp(-recency.decay_per_year * years)))


def apply_polarity(value: float, polarity: Polarity, config: EngineConfig) -> float:
    """The §15.2 override (DECISIONS D-33): NEGATED contributes `hard_negation.m` (0.0),
    ASPIRATIONAL at most `aspirational.m` (0.15), AFFIRMED the value itself."""
    value = clamp01(value)
    polarity = Polarity(polarity)
    if polarity is Polarity.NEGATED:
        return clamp01(config.negation.hard_negation.m)
    if polarity is Polarity.ASPIRATIONAL:
        return clamp01(min(value, config.negation.aspirational.m))
    return value
