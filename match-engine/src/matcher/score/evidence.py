"""Evidence strength: verbs, negation, quantified outcomes, E and R.

SPEC §5.1 (E, R) and §15; DECISIONS D-32, D-33, D-34, D-34a.

`analyze_bullet` gives every skill mention of a résumé bullet a verb class and a polarity. The
caller (parse/) has already found the mentions and resolved them to canonical ids:

* Clauses. The bullet splits at `verbs.clause_punctuation` and at the coordinating conjunctions
  (whole words, any case). Punctuation inside a word or a number ("Node.js", "3.5", "1,000") and
  anything inside a mention span ("Research and Development") is not a boundary: it belongs to
  one token or to one skill name.
* Verb. The nearest `verbs.yaml` entry (lower-cased whole words, phrases included) that ends
  before the mention inside its clause gives the verb class. Words inside a mention span are a
  skill name, never a verb. An instrument participle (INSTRUMENT_PARTICIPLES: "using", ...)
  defers to an earlier verb of its clause: in "Built dashboards using React" the action is
  "built" and "using" only introduces the tool. This is how DECISIONS D-34a's own examples read
  ("…using React, Redux and D3" all OWNERSHIP; "built dashboards for the sales team using
  Tableau" keeps Tableau on "built"), now that verbs.yaml lists the gerund "using".
* List continuation. A clause with no verb that holds only mentions, list words, articles and
  punctuation ("…, Redux and D3") continues the previous clause's list (DECISIONS D-34a): its
  mentions take the attachment of the previous clause's last mention, transitively. The
  attachment is the verb class *and* the polarity: "Not familiar with Docker or Kubernetes"
  negates both and "Studying Japanese and Korean" is aspirational for both (SPEC §15.2's failure
  mode, applied to lists). Any other clause without a verb before the mention is NO_VERB.
* Polarity. The `negation.window_tokens` tokens before the mention inside its clause are scanned
  (another mention counts as one token, punctuation as none): a hard-negation term makes the
  mention NEGATED, else an aspirational term makes it ASPIRATIONAL, else AFFIRMED. A multi-word
  term matches only as a whole phrase inside the window.

The lexicons are config (verbs.yaml, negation.yaml). The short closed word lists below (list
words, articles, instrument participles, counting nouns, outcome verbs) are grammar for reading
a bullet, not weights to tune.
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
    "ARTICLES",
    "COUNTING_NOUNS",
    "INSTRUMENT_PARTICIPLES",
    "LIST_WORDS",
    "OUTCOME_VERBS",
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

# Participles that introduce the tool of an earlier verb ("Built dashboards using React").
INSTRUMENT_PARTICIPLES: frozenset[str] = frozenset({"using", "utilizing", "utilising"})

# Nouns that make "<number ≥ 2> ... <noun>" a quantified outcome ("40 projects", "3 production
# servers"). Durations of experience ("years", "months") are deliberately absent.
COUNTING_NOUNS: frozenset[str] = frozenset({
    "accounts", "applications", "apps", "attendees", "bugs", "calls", "campaigns", "cases", "classes",
    "clients", "clinics", "clusters", "countries", "courses", "customers", "dashboards", "days",
    "deployments", "developers", "devices", "documents", "downloads", "emails", "employees",
    "endpoints", "engineers", "events", "features", "files", "hospitals", "hours", "incidents",
    "interns", "issues", "jobs", "leads", "locations", "machines", "members", "messages",
    "microservices", "minutes", "models", "orders", "participants", "partners", "patients", "people",
    "pipelines", "products", "projects", "queries", "records", "releases", "reports", "repositories",
    "requests", "rows", "schools", "servers", "services", "sessions", "sites", "staff",
    "stakeholders", "stores", "students", "subscribers", "tables", "teams", "tests", "tickets",
    "transactions", "users", "vendors", "visitors", "volunteers", "websites", "weeks", "workshops",
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
    attached ("" for NO_VERB); `trigger` the negation or aspiration term that fired ("" when
    AFFIRMED). A list continuation carries the verb and trigger it inherited."""

    canonical_id: str = Field(min_length=1, max_length=200)
    start: int = Field(ge=0)
    end: int = Field(ge=1)
    verb_class: VerbClass
    polarity: Polarity
    verb: str = ""
    trigger: str = ""


# ─── Reading a bullet: tokens, lexicons, clauses ─────────────────────────────────────────

# A word: letters and digits joined by an in-word "." or apostrophe ("Node.js", "3.5", "team's")
# or by a thousands comma ("1,000"). Hyphens and slashes separate words ("co-led" -> co, led).
_WORD = re.compile(r"[^\W_]+(?:(?:[.'\u2019]|(?<=[0-9]),(?=[0-9]{3}(?![0-9])))[^\W_]+)*")

_WORD_UNIT, _MENTION_UNIT, _BOUNDARY_UNIT = 0, 1, 2


class _Unit(NamedTuple):
    start: int
    end: int
    kind: int  # _WORD_UNIT, _MENTION_UNIT or _BOUNDARY_UNIT
    word: str = ""  # lower-cased, for a word
    component: int = -1  # index of the merged mention span, for a mention


class _Match(NamedTuple):
    first: int  # index of the first unit matched
    stop: int  # index one past the last unit matched
    text: str  # the normalised lexicon entry, e.g. "coursework in"
    verb_class: Optional[VerbClass]  # for verb entries


class _Attachment(NamedTuple):
    verb_class: VerbClass
    verb: str
    polarity: Polarity
    trigger: str


_Lexicon = dict[str, list[tuple[tuple[str, ...], str, Optional[VerbClass]]]]


def _lexicon(entries: Iterable[tuple[str, Optional[VerbClass]]]) -> _Lexicon:
    """Entries tokenised like the text and indexed by their first word."""
    index: _Lexicon = {}
    for entry, verb_class in entries:
        words = tuple(word.lower() for word in _WORD.findall(entry))
        if words:
            index.setdefault(words[0], []).append((words, " ".join(words), verb_class))
    return index


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


def _clauses(text: str, components: Sequence[tuple[int, int]], config: EngineConfig) -> list[list[_Unit]]:
    """The bullet as clauses of word and mention units, boundaries removed (SPEC §15.1)."""
    words = [(found.start(), found.end(), found.group().lower()) for found in _WORD.finditer(text)]
    word_spans = [(start, end) for start, end, _ in words]
    word_starts = [start for start, _ in word_spans]
    starts = [start for start, _ in components]
    units = [_Unit(s, e, _WORD_UNIT, word) for s, e, word in words if not _overlaps(components, starts, s, e)]
    units += [_Unit(s, e, _MENTION_UNIT, component=k) for k, (s, e) in enumerate(components)]
    units += [
        _Unit(s, e, _BOUNDARY_UNIT)
        for s, e in _occurrences(text, config.verbs.clause_punctuation)
        if not _overlaps(word_spans, word_starts, s, e) and not _overlaps(components, starts, s, e)
    ]
    units.sort()
    cut = [unit.kind == _BOUNDARY_UNIT for unit in units]
    conjunctions = _lexicon((conjunction, None) for conjunction in config.verbs.coordinating_conjunctions)
    for match in _find(units, conjunctions):
        cut[match.first:match.stop] = [True] * (match.stop - match.first)
    clauses: list[list[_Unit]] = []
    current: list[_Unit] = []
    for unit, is_cut in zip(units, cut):
        if not is_cut:
            current.append(unit)
        elif current:
            clauses.append(current)
            current = []
    if current:
        clauses.append(current)
    return clauses


def _only_list_words(clause: Sequence[_Unit], listing: _Lexicon) -> bool:
    """Does the clause hold nothing but mentions, list words and articles (punctuation never
    becomes a unit)?"""
    covered = [unit.kind == _MENTION_UNIT for unit in clause]
    for match in _find(clause, listing):
        covered[match.first:match.stop] = [True] * (match.stop - match.first)
    return all(covered)


class _ClauseReader:
    """What a mention at a given position of one clause receives (SPEC §15.1, §15.2).

    Matches are sorted once by nearness, so each lookup is a bisection plus a scan bounded by
    the window: a long bullet costs O(n log n), never mentions × verbs.
    """

    def __init__(
        self, verbs: Sequence[_Match], negations: Sequence[_Match], aspirations: Sequence[_Match], window: int
    ) -> None:
        self._verbs = sorted(verbs, key=_reach)
        self._verb_stops = [match.stop for match in self._verbs]
        # _last_action[k]: index of the nearest non-instrument verb among the first k, else -1.
        self._last_action = [-1]
        for k, match in enumerate(self._verbs):
            self._last_action.append(k if match.text not in INSTRUMENT_PARTICIPLES else self._last_action[-1])
        self._terms: list[tuple[list[_Match], list[int], Polarity]] = []
        for found, polarity in ((negations, Polarity.NEGATED), (aspirations, Polarity.ASPIRATIONAL)):
            ordered = sorted(found, key=_reach)
            self._terms.append((ordered, [match.stop for match in ordered], polarity))
        self._window = window

    def attach(self, position: int) -> _Attachment:
        """Units `[0, position)` of the clause precede the mention."""
        verb_class, verb = VerbClass.NO_VERB, ""
        k = bisect_right(self._verb_stops, position)  # verbs ending before the mention
        if k:
            action = self._last_action[k]  # an instrument participle defers to an earlier verb
            chosen = self._verbs[action if action >= 0 else k - 1]
            if chosen.verb_class is not None:
                verb_class, verb = chosen.verb_class, chosen.text
        first = max(0, position - self._window)
        for ordered, stops, polarity in self._terms:
            # nearest first; a term ending at or before `first` cannot lie inside the window
            j = bisect_right(stops, position) - 1
            while j >= 0 and ordered[j].stop > first:
                if ordered[j].first >= first:
                    return _Attachment(verb_class, verb, polarity, ordered[j].text)
                j -= 1
        return _Attachment(verb_class, verb, Polarity.AFFIRMED, "")


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
    components = _merge((mention.start, mention.end) for mention in mentions)
    verbs = _lexicon(
        (entry, verb_class) for verb_class, lexicon in config.verbs.classes.items() for entry in lexicon.verbs
    )
    hard = _lexicon((term, None) for term in config.negation.hard_negation.terms)
    aspirational = _lexicon((term, None) for term in config.negation.aspirational.terms)
    listing = _lexicon((word, None) for word in LIST_WORDS + ARTICLES)
    window = config.negation.window_tokens
    inherit = config.verbs.list_continuation_inherits_verb

    attached: dict[int, _Attachment] = {}
    carry: Optional[_Attachment] = None  # what the previous clause hands a list continuation
    for clause in _clauses(text, components, config):
        found = _find(clause, verbs)
        if inherit and carry is not None and not found and _only_list_words(clause, listing):
            for unit in clause:
                if unit.kind == _MENTION_UNIT:
                    attached[unit.component] = carry
            continue
        reader = _ClauseReader(found, _find(clause, hard), _find(clause, aspirational), window)
        last: Optional[_Attachment] = None
        for position, unit in enumerate(clause):
            if unit.kind == _MENTION_UNIT:
                last = attached[unit.component] = reader.attach(position)
        # A clause without mentions still hands on what a mention at its end would receive.
        carry = last if last is not None else reader.attach(len(clause))

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
_MAGNITUDES = frozenset({"k", "K", "M", "B", "x"})  # glued to the number: 10k, 2M, 1.5B, 3x
_MULTIPLY = "\u00d7"  # the multiplication sign, as in "4×"
_SENTENCE_ENDS = frozenset({".", "!", "?", ";"})
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


def _currency_before(tokens: Sequence[_QToken], i: int, text: str) -> bool:
    if i == 0:
        return False
    sign = tokens[i - 1]
    return sign.kind == "sym" and _is_currency(sign.text) and text[sign.end:tokens[i].start] in ("", " ")


def _after_name(tokens: Sequence[_QToken], i: int, text: str) -> bool:
    """Is number `i` a version right after a tech name ("React 18", "Java 8+")? It is when the
    word just before it holds a capital and does not open the sentence: the opening word of a
    bullet is its verb ("Managed 3 servers"), not a product name."""
    if i < 2:
        return False
    name, opener = tokens[i - 1], tokens[i - 2]
    if name.kind != "word" or not text[name.end:tokens[i].start].isspace():
        return False
    if not any(char.isupper() for char in name.text):
        return False
    return not (opener.kind == "sym" and opener.text in _SENTENCE_OPENERS)


def _next_words(tokens: Sequence[_QToken], i: int, limit: int = 2) -> Iterator[str]:
    """The next `limit` word or number tokens after token `i`, lower-cased. Symbols are skipped;
    a clause mark ends the scan."""
    seen = 0
    for j in range(i + 1, len(tokens)):  # by index: slicing the rest would be quadratic
        token = tokens[j]
        if token.kind == "sym":
            if token.text in _NOUN_SCAN_STOPS:
                return
            continue
        yield token.text.lower()
        seen += 1
        if seen == limit:
            return


def _is_quantity(tokens: Sequence[_QToken], i: int, text: str) -> bool:
    token = tokens[i]
    if _currency_before(tokens, i, text):  # $120K, €50k
        return True
    if token.tail:  # 10k, 2M, 1.5B, 3x; not 120ms, 3D, 2nd
        return token.tail in _MAGNITUDES
    following = tokens[i + 1] if i + 1 < len(tokens) else None
    if following is not None:
        gap = text[token.end:following.start]
        if following.kind == "sym":
            if following.text == "%" and gap in ("", " "):
                return True
            if following.text == _MULTIPLY and not gap:
                return True
            if following.text == "+" and not gap:  # 500+, but not "Java 8+"
                return not _after_name(tokens, i, text)
        elif following.kind == "word" and following.text.lower() in ("percent", "times"):
            return True
    if _after_name(tokens, i, text) or not _at_least_two(token.text):
        return False
    for distance, word in enumerate(_next_words(tokens, i), start=1):
        if word in COUNTING_NOUNS:
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


def is_quantified(text: str) -> bool:
    """Does the bullet state a quantified outcome (ROLE_BULLET_QUANTIFIED)?

    True for a number followed by "%" / "percent", preceded by a currency sign, followed by
    k/K/M/B/x/×/+ or "times"; a number ≥ 2 followed within two tokens by a counting noun; or a
    number after "by"/"to" following an outcome verb in the same sentence. Never for a year on
    its own or a version right after a tech name ("React 18", "Python 3", "ES6").
    """
    tokens = _qtokens(text)
    after_outcome_verb = False
    for i, token in enumerate(tokens):
        if token.kind == "sym":
            if token.text in _SENTENCE_ENDS:
                after_outcome_verb = False
        elif token.kind == "word":
            word = token.text.lower()
            if word in OUTCOME_VERBS:
                after_outcome_verb = True
            elif after_outcome_verb and word in ("by", "to") and _amount_follows(tokens, i):
                return True
        elif _is_quantity(tokens, i, text):
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
