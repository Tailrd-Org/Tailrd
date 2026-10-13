"""Surface form -> canonical skill id (SPEC §3.3).

`Resolver` indexes every node's label and aliases under `normalize_surface` and each key's hyphen
variants, so "front end", "front-end" and "frontend" all reach a node that lists any of them.
`resolve` maps one surface (exact/alias, then a bounded fuzzy fallback); `extract` finds every
mention in free text by greedy longest match, never fuzzy (bounded cost on long text).

Ambiguous keys ("go", "r", "rest", ...) are common words as well as skills. Each one carries its
required casing ("Go") and resolves only under these rules:

- `resolve` (one standalone surface): the surface must have the required casing.
- `extract`, prose: the required casing, and not the first word of the text or of a sentence
  (after `.`, `!`, `?` or a line break). Inside a slash token an ambiguous part also needs
  another part that is a non-ambiguous skill ("Python/R", never "A/R" or "Spring/Summer"), the
  first part keeping the token's position, unless its casing is an all-capital acronym of two
  letters or more ("AI/ML", "BI/reporting"); inside a hyphen token only a "<key>-<suffix>"
  compound with a known suffix matches ("Go-based", never "Go-Live"; DECISIONS D-25).
- `extract`, list: any casing, any position; the slash and hyphen rules still hold. Only the
  curated ambiguous keys get this leniency: the automatic `cased` keys (one-word product and tool
  names, DECISIONS D-26) keep their casing in lists too.

An ambiguous key is never a fuzzy target: a near miss cannot have the required casing. Nor is a
common word ever a fuzzy query (Task 2.3): a word that many postings use ("patients", "salary",
"analysis", from `common_words.json`) is a word, not a typo of a skill.

`extract` also (DECISIONS D-27): skips fields of study (`field.*` nodes) unless
`include_fields=True`, and then prefers a field to a skill that shares its key ("Statistics" in
an education line); lets a blocked phrase ("American Express", "Sous Chef", "Technical Skills")
consume its words without a mention; never reads a season word followed by a year or a term word,
or next to another season ("Spring 2024", "Fall and Spring"), a person's initial ("John R.
Smith"), or a currency code in a money context ("85,000 CAD plus benefits", "Pay range (CAD)",
"CAD per hour", while "1,200 CAD drawings" is the skill) as a skill; and, like `resolve`, reads past
a French elision ("d'Excel" -> Excel). A formal in-progress designation ("CPA
candidate") is read only as written, never across ":" or a dash; whether a credential is held,
pursued or required is the context's business, not the taxonomy's (D-28).
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Iterable, Literal, Mapping, NamedTuple, Optional

from pydantic import Field, model_validator

from matcher.config import EngineConfig
from matcher.graph.aliases import (
    APOSTROPHES,
    DASHES,
    INVISIBLE,
    normalize_keep_case,
    normalize_surface,
    surface_variants,
)
from matcher.graph.model import SkillGraph
from matcher.models import Frozen
from matcher.normalize.fuzzy import FuzzyHit, FuzzyLog, bounded_levenshtein

_NO_AMBIGUOUS: Mapping[str, str] = MappingProxyType({})

# Tokens: maximal runs of word characters (any script; combining marks too, so NFD text, where an
# accent is a separate mark after its letter, stays one word) and the in-word punctuation of skill
# names (c++, c#, .net, node.js, ci/cd, scikit-learn, o'reilly). "&" keeps R&D / AT&T in one piece;
# invisible characters and Unicode dashes stay inside the word, as normalize_surface treats them.
_COMBINING_MARKS = "\u0300-\u036f"  # a character-class range
_TOKEN_RE = re.compile(rf"[\w{_COMBINING_MARKS}+#./&\-{APOSTROPHES}{DASHES}{INVISIBLE}]+")
_PART_RE = re.compile(rf"[^/\-{DASHES}]+")  # the pieces of "React/Redux" or "React-based"
# Edge characters normalize_surface would strip; trimming them keeps mention offsets tight.
_EDGE_LEAD = frozenset("-/_" + APOSTROPHES + DASHES + INVISIBLE)
_EDGE_TRAIL = _EDGE_LEAD | {"."}
_LINE_BREAKS = frozenset("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")  # what str.splitlines() splits on
_SENTENCE_BREAK = frozenset(".!?\u2026") | _LINE_BREAKS
# A window may continue across these (credential names, DECISIONS D-13): "Microsoft Certified:
# Azure Fundamentals", "Microsoft® Office", and dash runs standing alone between spaces.
_JOIN_MARKS = frozenset(":\u00ae\u2122\u2120\u00a9")  # : ® ™ ℠ ©
_DASH_CHARS = frozenset("-" + DASHES)
_DASH_RUN_RE = re.compile(rf"[\-{DASHES}]+")
# "P.Eng. candidate": the period closing an initialism does not end a window when a lower-case word
# follows (D-28); "ASP.NET. Core" or "P.Eng. Candidate pool" stay apart.
_ABBREVIATION_GAP = re.compile(r"\.[ \t]+")
_INITIALISM_RE = re.compile(r"(?:[A-Za-z]\.){1,3}[A-Za-z]*\.?")
_POSSESSIVE = frozenset("'\u2019")  # "React's", also with a curly apostrophe
# The only hyphen compounds in which an ambiguous key may match, as "<key>-<suffix>" (D-25).
_HYPHEN_SUFFIXES = frozenset(
    {"based", "powered", "driven", "backed", "first", "centric", "enabled", "native", "heavy", "only"}
)
_MAX_WINDOW = 8  # tokens
# Seasons: "Spring 2024", "Spring term", "Fall and Spring" are dates, never the framework (D-27).
_SEASONS = frozenset({"spring", "summer", "fall", "autumn", "winter"})
_TERM_WORDS = frozenset(
    {"term", "terms", "semester", "semesters", "session", "co-op", "coop", "co-ops", "coops"}
)
_SEASON_LINKS = frozenset({"and", "or", "&", "to", "through", "until"})
_NEAR_MARKS = frozenset(",/&" + DASHES + "-" + APOSTROPHES)  # what may stand between a season and its year
_YEAR_RE = re.compile(r"(?:19|20)\d\d|\d\d")
# Currency codes in a money context are money, never the skill (D-27): after "$" or a salary
# word on their line ("$25/hr CAD", "Salary (CAD)"), or a "$" amount, perhaps with its rate, before
# the code's bracket ("$80,000 (CAD)", "$30.00 per hour (CAD)"); after a pay word earlier in the
# clause with only link words, numbers and brackets between ("Pay range (CAD)", "All amounts are in
# CAD", "Currency: CAD"; "a wide range of CAD tools" keeps the skill); before an amount, a range, a
# rate unit or "per <unit>" on their line ("CAD $80k", "CAD 2M", "CAD 25-30", "CAD/hr", "CAD per
# hour", "CAD par mois"); in a currency pair ("USD/CAD"); after an amount (thousands, also grouped
# by a space as in French "70 000", cents, a magnitude, four digits or more) or a number that closes
# a range ("18 - 20 CAD") unless a curated noun of the skill's work follows, alone or after one
# modifier ("85,000 CAD plus benefits" is money, "1,200 CAD drawings" and "1,500 CAD shop drawings"
# the skill; the nouns come from the resolver settings); after a smaller number when a rate or money
# word, a range to another number, an addition or a separator follows ("25 CAD hourly", "25 CAD - 30
# CAD", "40 CAD + HST", "500 CAD (monthly)"). A year is never a number ("the 2024 CAD standards"),
# nor is a number glued to letters ("CATIA V5 CAD") or inside the skill name the code follows
# ("Fusion 360 CAD (cloud)"), and a rate phrase other than "per <unit>" needs a number before the
# code ("Taught CAD annually" keeps CAD). A small count or version before a clause end keeps the
# skill ("Fusion 360 CAD.", "10 CAD models"), and a number before a list comma is no number for the
# code ("Office 365, CAD").
_CURRENCIES = frozenset({"cad", "usd", "eur", "gbp"})
_CURRENCY_CODES = _CURRENCIES | frozenset(
    {"jpy", "chf", "aud", "nzd", "cny", "hkd", "sgd", "mxn", "inr", "sek", "nok", "dkk"}
)
_SPACE = " \t\u00a0\u202f"  # a space within a line: plain, tab, no-break, narrow no-break (French)
_GROUP = " \u00a0\u202f"  # what may group the thousands of an amount besides "," ("70 000")
_RATE_UNIT = r"/\s*(?:hr|h|hour|yr|year|annum)\b"  # written on the code ("CAD/hr"): money by itself
# "per <unit>" right after the code is money by itself too ("CAD per hour"), and so is French "par".
_PER_UNIT = r"(?:per\s+(?:hour|hr|day|week|month|year|annum)|par\s+(?:heure|jour|semaine|mois|année|an))\b"
_RATE_PHRASE = rf"(?:{_PER_UNIT}|(?:annually|hourly|an\s+hour|a\s+year)\b)"  # after a number: money
_MAGNITUDE = r"(?:k|m|b|bn|million|billion|thousand)"
_DOLLAR_BEFORE = re.compile(
    rf"\$\s*[\d,.{_GROUP}]*\s*{_MAGNITUDE}?\s*(?:/\s*[A-Za-z]+|{_RATE_PHRASE})?\s*\(?\s*$", re.IGNORECASE
)
# The number right before the code; one glued to letters ("V5", "S3") is part of a name.
_NUMBER_BEFORE = re.compile(
    rf"(?<![^\W\d])(\d{{1,3}}(?:[{_GROUP}]\d{{3}})+(?:[.,]\d+)?|\d(?:[\d,.\u00a0\u202f]*\d)?)\s*({_MAGNITUDE})?\s*$",
    re.IGNORECASE,
)
_YEAR_ONLY = re.compile(r"(?:19|20)\d\d")
_THOUSANDS = re.compile(rf"\d[,{_GROUP}]\d{{3}}(?!\d)")
_CENTS = re.compile(r"\d+\.\d\d")
# A number that closes a range is an amount, never a version: "18 - 20 CAD", "25 CAD to 30 CAD".
_RANGE_BEFORE = re.compile(
    rf"(?<!\w)\d+(?:[.,]\d+)*[{_SPACE}]*(?:(?:{'|'.join(sorted(_CURRENCY_CODES))})[{_SPACE}]*)?"
    rf"(?:[\-{DASHES}]|to)[{_SPACE}]*\$?[{_SPACE}]*$",
    re.IGNORECASE,
)
_DURATION = r"(?:years?|yrs?|months?|weeks?|days?|hours?|hrs?|times?)\b"  # "CAD 2-3 years": no pay
_AMOUNT_AFTER = re.compile(
    rf"[{_SPACE}]*(?:\$|\d{{1,3}}(?:[,{_GROUP}]\d{{3}})+|\d+(?:\.\d+)?\s*{_MAGNITUDE}\b|\d+\.\d\d\b"
    rf"|(?!(?:19|20)\d\d\b)\d{{4,}}"
    rf"|\d+(?:[.,]\d+)?[{_SPACE}]*(?:[\-{DASHES}]|to\b)[{_SPACE}]*\$?[{_SPACE}]*\d+(?:[.,]\d+)?"
    rf"(?![{_SPACE}]*{_DURATION})|{_RATE_UNIT}|{_PER_UNIT})",
    re.IGNORECASE,
)
# After a small number: a rate phrase, a money or pay word (the award words singular only: "2 CAD
# awards" counts awards), a range to another number, an addition or a separator.
_SMALL_NUMBER_MONEY_AFTER = re.compile(
    rf"[{_SPACE}]*(?:{_RATE_PHRASE}|(?:salary|wages?|budgets?|revenues?|funding|savings|costs?|fees?"
    r"|income|bonus(?:es)?|benefits|equity|depending|DOE|OTE|scholarship|prize|award|grant|bursary"
    rf"|stipend)\b|(?:[\-{DASHES}]|to\b)[{_SPACE}]*\$?[{_SPACE}]*\d|\+|plus\b|[(|])",
    re.IGNORECASE,
)
# The words right after the code, on its line: a curated noun there, or after one modifier ("shop
# drawings", "3D models"), keeps the code a skill. A function or rate word is no modifier: "5,000
# CAD on parts" and "80,000 CAD annual licences" are money.
_WORD = rf"([^\W_][\w\-{APOSTROPHES}]*)"
_NEXT_WORD = re.compile(rf"[{_SPACE}]+{_WORD}")
_NEXT_TWO_WORDS = re.compile(rf"[{_SPACE}]+{_WORD}[{_SPACE}]+{_WORD}")
_NO_MODIFIER = frozenset({
    "a", "an", "the", "of", "on", "in", "for", "to", "at", "by", "from", "with", "into", "per", "and", "or",
    "plus", "as", "each", "every", "worth", "over", "under", "annual", "monthly", "yearly", "weekly", "hourly",
    "total", "de", "des", "en", "pour", "par", "sur", "et", "ou",
})
_SALARY_WORDS = re.compile(r"\b(?:salary|salaire|wages?|compensation|rémunération)\b", re.IGNORECASE)
# Pay words (Task 2.3): money when only link words, numbers and brackets stand between one and the
# code in the same clause ("Hourly rate (CAD)", "All figures cited below are in CAD").
_PAY_WORDS = (
    r"pay|rates?|ranges?|salary|salaries|salaire|wages?|compensation|rémunération|stipends?|bonus(?:es)?"
    r"|amounts?|currency|figures|montants?|devise|taux"
)
_PAY_LINKS = (
    r"in|is|are|be|will|all|the|our|its|to|from|between|per|hour|hours|year|month|week|day|cited|quoted"
    r"|listed|shown|expressed|stated|given|paid|presented|provided|below|above|here|base|hiring|expected"
    r"|starting|annual|hourly|total|target|canadian|dollars?|en|est|sont|les|des|tous|toutes|ci-dessous"
    r"|indiqués|exprimés|versés|par|heure|an|année|mois|semaine"
)
_PAY_BEFORE = re.compile(
    rf"\b(?:{_PAY_WORDS})\b(?:[\s():*/$,.\d\-{DASHES}]|\b(?:{_PAY_WORDS}|{_PAY_LINKS})\b)*$", re.IGNORECASE
)
_CLAUSE_BREAK = re.compile(r"[.!?](?=\s)|[;|•]")  # what ends the clause a pay word must share
# A formal in-progress designation ("CPA candidate", "articling student") is read only as written,
# never across ":" or a dash: "Elementary Teacher - Candidate must hold OCT" names no teacher
# candidate, "Designation: CFA - Level II" no CFA candidacy (D-28).
_FORMAL_BREAKS = frozenset(":-" + DASHES)
# A leading French elision ("d'Excel", "l'apprentissage", "qu'AWS"): the word after it is looked up.
_ELISION_RE = re.compile(rf"(?:qu|[ldjnscmt])[{APOSTROPHES}](?=\w)", re.IGNORECASE)
# Initials (D-27): what may stand around a one-letter capital that is a person's initial.
_NAME_INNER = frozenset("-" + APOSTROPHES)  # "Smith-Jones", "O'Neil"
_TITLES = frozenset({"dr", "mr", "mrs", "ms", "mx", "prof"})
_FIELD_PREFIX = "field."  # fields of study: extract reports them only with include_fields=True
# A skill list after a sentence-initial key (D-141): "Go, Python", "Go/Rust", "Go & SQL", "Go and SQL".
_LIST_SEPARATORS = frozenset(",/&")
_LIST_LINKS = frozenset({"and", "or", "&"})


class Resolution(Frozen):
    surface: str  # the text as given
    canonical_id: Optional[str]
    method: Literal["EXACT", "ALIAS", "FUZZY", "NONE"]
    distance: int = Field(default=0, ge=0)  # edit distance of a FUZZY hit

    @model_validator(mode="after")
    def _consistent(self) -> "Resolution":
        if (self.method == "NONE") != (self.canonical_id is None):
            raise ValueError("canonical_id is None exactly when method is NONE")
        return self


class Mention(Frozen):
    canonical_id: str
    surface: str  # text[start:end] of the original text
    start: int = Field(ge=0)
    end: int
    method: Literal["EXACT", "ALIAS"]

    @model_validator(mode="after")
    def _span(self) -> "Mention":
        if self.end <= self.start:
            raise ValueError("a mention spans at least one character")
        return self


class _Token(NamedTuple):
    start: int
    end: int
    forms: tuple[tuple[int, str], ...]  # (end, normalized): the token, then without a possessive 's
    joined: bool  # a window may span the gap before it (see _joinable)
    sentence_start: bool  # first token of the text, or the gap before it ends a sentence
    # As a window's first word the token reads past a French elision ("d'Excel" -> Excel, D-27);
    # inside a window it keeps it, as labels do ("gestion de l'information").
    lead_start: int
    lead_forms: tuple[tuple[int, str], ...]

    @property
    def norm(self) -> str:
        return self.forms[0][1]


class _Part(NamedTuple):
    """A "/"- or "-"-separated piece of a token, with the separators around it."""

    start: int
    end: int
    before: str  # the separator before it, "" at the token's start
    after: str  # the separator after it, "" at the token's end


class Resolver:
    def __init__(
        self,
        graph: SkillGraph,
        config: EngineConfig,
        *,
        ambiguous: Mapping[str, str] = _NO_AMBIGUOUS,
        cased: Mapping[str, str] = _NO_AMBIGUOUS,
        blocked_phrases: Iterable[str] = (),
        in_progress: Mapping[str, str] = _NO_AMBIGUOUS,
        currency_skill_nouns: Iterable[str] = (),
        common_words: Iterable[str] = (),
        fuzzy_log: Optional[FuzzyLog] = None,
        fuzzy_exclusions: Iterable[str] = (),
        sentence_initial: Optional[Mapping[str, Iterable[str]]] = None,
    ) -> None:
        """`ambiguous` maps a key to its required casing, e.g. {"go": "Go", "r": "R"}; skills
        lists waive it. `cased` keys (automatic, D-26) need their casing in lists too.
        `blocked_phrases` are non-skill phrases ("American Express") whose words never yield a
        mention. `in_progress` maps a credential to the node of its formal in-progress
        designations ("cred.cpa" -> "cred.cpa_in_progress", D-28): those are read only as written.
        `currency_skill_nouns` are the nouns that, right after an amount and a currency code that
        is also a skill, make the code the skill ("1,200 CAD drawings"); otherwise it is money
        ("85,000 CAD plus benefits", D-27). `common_words` are words frequent in the posting corpus
        (`common_words.json`, Task 2.3): `resolve` never reads one as a typo, so it never
        fuzzy-matches ("patients" is no "patents"). Exact matches and `extract` are unaffected.
        `fuzzy_exclusions` (curated, Task 7.3) are ordinary words treated the same way where the
        posting corpus is too small to list them ("patients", "parents", "supervisors").
        `sentence_initial` (curated, Task 7.3, DECISIONS D-141) names the ambiguous or cased keys
        (`keys`) that may open a prose sentence when the sentence is a skill list (another skill
        follows the key after ",", "/", "&", "and" or "or"), and among them the languages
        (`languages`) that may also open one that speaks of languages (a `cues` word or another
        language stands in it): "Go, Python and Rust.", "English and French required." """
        taxonomy = config.scoring.taxonomy
        self._max_distance = taxonomy.fuzzy_max_distance
        self._min_length = taxonomy.fuzzy_min_token_length
        self._fuzzy_log = fuzzy_log
        self._label_key = {
            node_id: normalize_surface(_without_elision(node.label)) for node_id, node in graph.nodes.items()
        }
        # Skills and fields of study are indexed apart: a key both claim ("statistics") is the
        # skill in general text and the field in education lines (D-27).
        self._index = _build_index(graph, fields=False)
        self._field_index = _build_index(graph, fields=True)
        self._formal = frozenset(in_progress.values())
        self._currency_nouns = frozenset(normalize_surface(noun) for noun in currency_skill_nouns) - {""}
        self._common_words = frozenset(normalize_surface(word) for word in common_words) - {""}
        self._never_fuzzy = self._common_words | (frozenset(normalize_surface(w) for w in fuzzy_exclusions) - {""})
        initial = sentence_initial or {}
        self._initial_keys = frozenset(normalize_surface(k) for k in initial.get("keys", ())) - {""}
        self._initial_languages = (
            frozenset(normalize_surface(k) for k in initial.get("languages", ())) & self._initial_keys
        )
        self._language_cues = frozenset(normalize_surface(w) for w in initial.get("cues", ())) - {""}

        self._ambiguous: dict[str, str] = {}
        for key, casing in (*ambiguous.items(), *cased.items()):
            normalized = normalize_surface(key)
            required = normalize_keep_case(casing)
            if normalize_surface(required) != normalized:
                raise ValueError(f"ambiguous key {key!r} and its casing {casing!r} differ")
            if self._ambiguous.get(normalized, required) != required:
                raise ValueError(f"ambiguous key {normalized!r} given two casings")
            self._ambiguous[normalized] = required
        self._lenient = frozenset(normalize_surface(key) for key in ambiguous) - {
            normalize_surface(key) for key in cased
        }
        # Keys whose casing is an all-capital acronym of two letters or more (AI, ML, BI, QA): their
        # casing is evidence enough inside a slash token ("AI/ML"), where the others need a
        # non-ambiguous companion (D-25).
        self._acronyms = frozenset(key for key, casing in self._ambiguous.items() if _is_acronym(casing))
        self._blocked = frozenset(
            variant for phrase in blocked_phrases for variant in surface_variants(normalize_surface(phrase))
        )

        buckets: dict[int, list[str]] = {}
        for key in sorted(set(self._index) | set(self._field_index)):
            if len(key) >= self._min_length and key not in self._ambiguous:
                buckets.setdefault(len(key), []).append(key)
        self._fuzzy_keys = {length: tuple(sorted(keys)) for length, keys in buckets.items()}
        self._fuzzy_masks: dict[int, tuple[int, ...]] = {}  # per length bucket, built on first use

    # ─── one surface ──────────────────────────────────────────────────────────────────────

    def is_common_word(self, word: str) -> bool:
        """Whether `word`, normalised, is a common word of the posting corpus (`common_words.json`):
        the posting parser reads such a word in capitals ("HIRING") as shouting, not an acronym."""
        return normalize_surface(word) in self._common_words

    def resolve(self, surface: str, *, prefer_fields: bool = False) -> Resolution:
        """Exact or alias match over the surface's variants, a skill before a field of study (the
        field first with `prefer_fields`, for an education line's field: "Statistics"); else a
        bounded fuzzy match for a single long token that is no common word (logged); else NONE. A
        leading French elision is read past ("d'Excel"); a blocked phrase is NONE."""
        body = _without_elision(surface)
        normalized = normalize_surface(body)
        variants = surface_variants(normalized)
        if any(key in self._blocked for key in variants):
            return Resolution(surface=surface, canonical_id=None, method="NONE")
        exact_key_seen = False
        indexes = (self._field_index, self._index) if prefer_fields else (self._index, self._field_index)
        for index in indexes:
            for key in variants:
                node_id = index.get(key)
                if node_id is None:
                    continue
                exact_key_seen = True
                if not self._ambiguity_ok(key, body, context="resolve", sentence_start=False):
                    continue
                return Resolution(surface=surface, canonical_id=node_id, method=self._method(key, node_id))
        if not exact_key_seen:  # a known word that failed its casing is not a typo
            fuzzy = self._fuzzy(normalized)
            if fuzzy is not None:
                key, edits = fuzzy
                owner = next(index[key] for index in indexes if key in index)
                hit = FuzzyHit(surface=surface, canonical_id=owner, distance=edits)
                if self._fuzzy_log is not None:
                    self._fuzzy_log.record(hit)
                return Resolution(surface=surface, canonical_id=hit.canonical_id, method="FUZZY", distance=edits)
        return Resolution(surface=surface, canonical_id=None, method="NONE")

    def _fuzzy(self, normalized: str) -> Optional[tuple[str, int]]:
        """Closest key within the edit bound: smaller distance first, then the smaller key. A
        common word of the posting corpus, or a curated fuzzy exclusion, is a word, not a typo: it
        has none."""
        if (
            self._max_distance < 1
            or " " in normalized
            or len(normalized) < self._min_length
            or normalized in self._never_fuzzy
        ):
            return None
        best: Optional[tuple[int, str]] = None  # (edits, key)
        size = len(normalized)
        query_mask = _char_mask(normalized)
        for length in range(size - self._max_distance, size + self._max_distance + 1):
            keys = self._fuzzy_keys.get(length, ())
            for key, mask in zip(keys, self._masks(length)):
                # Only a distance <= the best so far can still win. Two exact lower bounds on the
                # distance skip most keys before the DP: the length gap, and the characters only
                # one side has (each one needs its own edit).
                limit = self._max_distance if best is None else best[0]
                if abs(length - size) > limit:
                    break
                if (mask & ~query_mask).bit_count() > limit or (query_mask & ~mask).bit_count() > limit:
                    continue
                edits = bounded_levenshtein(normalized, key, limit)
                if edits is not None and (best is None or (edits, key) < best):
                    best = (edits, key)
        return None if best is None else (best[1], best[0])

    def _masks(self, length: int) -> tuple[int, ...]:
        masks = self._fuzzy_masks.get(length)
        if masks is None:
            masks = tuple(_char_mask(key) for key in self._fuzzy_keys.get(length, ()))
            self._fuzzy_masks[length] = masks
        return masks

    # ─── free text ────────────────────────────────────────────────────────────────────────

    def extract(
        self, text: str, *, context: Literal["prose", "list"] = "prose", include_fields: bool = False
    ) -> tuple[Mention, ...]:
        """Every skill mention in `text`, by greedy longest match over up to 8 tokens.

        A matched span consumes its tokens. A window continues across whitespace, standalone
        dashes, the (R)/TM/SM/(C) marks, ":" (credential names) and the period closing an
        initialism before a lower-case word ("P.Eng. candidate", D-28), never across other
        punctuation or a line break. A word ending in "'s" also tries its form without the
        suffix, and a window's first word is read past a French elision ("d'Excel"). A blocked
        phrase, and a field of study unless `include_fields`, consumes its span without a mention;
        with `include_fields` a key that a field and a skill both claim is the field (education
        lines). A token holding "/" or "-" that matches nothing is retried as its parts
        ("React/Redux" -> React, Redux). A formal in-progress designation never spans ":" or a
        dash (D-28). Offsets index the original text; sorted by start.
        """
        if context not in ("prose", "list"):
            raise ValueError(f"unknown context {context!r} (expected 'prose' or 'list')")
        tokens = _tokenize(text)
        mentions: list[Mention] = []
        floor = 0  # where the last matched window ends: a number before it belongs to that name
        i = 0
        while i < len(tokens):
            matched = self._match_window(text, tokens, i, context, include_fields, floor)
            if matched is not None:
                width, mention = matched
                if mention is not None:
                    mentions.append(mention)
                floor = tokens[i + width - 1].end
                i += width
                continue
            mentions.extend(self._match_parts(text, tokens[i], context, include_fields, floor))
            i += 1
        return tuple(sorted(mentions, key=lambda mention: (mention.start, mention.end)))

    def _match_window(
        self, text: str, tokens: list[_Token], i: int, context: str, include_fields: bool, floor: int
    ) -> Optional[tuple[int, Optional[Mention]]]:
        """The longest window starting at token i that is a key or a blocked phrase, as (tokens
        consumed, mention). The mention is None when the window is a blocked phrase or a field of
        study the caller left out: the span is consumed all the same. At equal width a blocked
        phrase wins ("R&D"). A one-token match on a season that dates something ("Spring 2024"),
        on a person's initial ("John R. Smith") or on a currency code in a money context ("80,000
        CAD.") is no match. `floor` is where the previous window ended (see `_is_money`)."""
        last = i
        while last + 1 < len(tokens) and last + 1 - i < _MAX_WINDOW and tokens[last + 1].joined:
            last += 1
        first = tokens[i]
        start = first.lead_start
        # A configured key may open a prose sentence that lists skills or speaks of languages (D-141).
        initial_ok = (
            context == "prose"
            and first.sentence_start
            and first.lead_forms[0][1] in self._initial_keys
            and self._initial_context(text, tokens, i)
        )
        for j in range(last, i - 1, -1):
            if j == i:
                head, forms = [], first.lead_forms
            else:
                head = [first.lead_forms[0][1], *(token.norm for token in tokens[i + 1 : j])]
                forms = tokens[j].forms
            for end, form in forms:  # the last word as written, then without a possessive
                variants = surface_variants(" ".join([*head, form]))
                if any(key in self._blocked for key in variants):
                    return j - i + 1, None
                original = text[start:end]
                hit = self._lookup(
                    variants,
                    original,
                    context,
                    sentence_start=first.sentence_start,
                    include_fields=include_fields,
                    initial_ok=initial_ok,
                )
                if hit is None:
                    continue
                node_id, key = hit
                if node_id.startswith(_FIELD_PREFIX) and not include_fields:
                    return j - i + 1, None
                if node_id in self._formal and _crosses(text, tokens, i, j, _FORMAL_BREAKS):
                    continue  # "Teacher - Candidate" is no teacher candidate
                if j == i and (
                    (key in _SEASONS and _dates_a_season(text, tokens, i))
                    or (len(key) == 1 and self._is_initial(text, tokens, i, start, end))
                    or (key in _CURRENCIES and _is_money(text, start, end, floor, self._currency_nouns))
                ):
                    continue
                return j - i + 1, self._mention(text, start, end, node_id, key)
        return None

    def _initial_context(self, text: str, tokens: list[_Token], i: int) -> bool:
        """Whether the sentence that token i opens lets a `sentence_initial` key stand there (D-141):
        a skill list (right after the key comes ",", "/" or "&", or the word "and" or "or", and then a
        skill: "Go, Python and Rust", "Go and SQL"), or, for a language, a sentence about languages
        (a cue word or another configured language in it: "English and French required", "French is
        an asset"). "Go above and beyond" and "English Bay is close" stay words."""
        end = i + 1
        while end < len(tokens) and not tokens[end].sentence_start:
            end += 1
        if self._lists_a_skill(text, tokens, i, end):
            return True
        if tokens[i].lead_forms[0][1] not in self._initial_languages:
            return False
        return any(
            tokens[k].norm in self._language_cues or tokens[k].norm in self._initial_languages
            for k in range(i + 1, end)
        )

    def _lists_a_skill(self, text: str, tokens: list[_Token], i: int, end: int) -> bool:
        """Whether a list separator follows token i and a skill starts right after it, before
        `end` (the end of the sentence)."""
        k = i + 1
        if k >= end:
            return False
        gap = text[tokens[i].end : tokens[k].start].strip()
        if gap:
            if not all(ch in _LIST_SEPARATORS for ch in gap):
                return False
        elif tokens[k].norm in _LIST_LINKS:
            k += 1
            if k >= end or text[tokens[k - 1].end : tokens[k].start].strip():
                return False
        else:
            return False
        last = k
        while last + 1 < end and last + 1 - k < _MAX_WINDOW and tokens[last + 1].joined:
            last += 1
        for j in range(last, k - 1, -1):
            words = [token.norm for token in tokens[k : j + 1]]
            hit = self._lookup(
                surface_variants(" ".join(words)),
                text[tokens[k].start : tokens[j].end],
                "prose",
                sentence_start=False,
                include_fields=False,
            )
            if hit is not None and not hit[0].startswith(_FIELD_PREFIX):
                return True
        return False

    def _is_initial(self, text: str, tokens: list[_Token], i: int, start: int, end: int) -> bool:
        """Whether text[start:end], a one-letter match on token i, is a person's initial (D-27): a
        capital letter between two capitalised words that are not skills ("Mary C Jones"), or
        followed by a period between such words ("John R. Smith", "Dr. Mary C. Jones"). The
        word before may be a title or another initial, then with its period ("Dr. R. Smith",
        "J. R. R. Tolkien"); all on one line. "Wrote firmware in C." keeps its C."""
        if end - start != 1 or not text[start].isupper() or i == 0 or i + 1 == len(tokens):
            return False
        before, after = tokens[i - 1], tokens[i + 1]
        gap_before, gap_after = text[before.end : start], text[end : after.start]
        if any(ch in _LINE_BREAKS for ch in gap_before + gap_after):
            return False
        if gap_after.strip() not in ("", "."):
            return False
        if gap_before.strip() == "." and not _title_or_initial(text, before):
            return False
        if gap_before.strip() not in ("", "."):
            return False
        return self._name_word(text, before) and self._name_word(text, after)

    def _name_word(self, text: str, token: _Token) -> bool:
        """A capitalised word that is not a key ("Smith", "O'Neil", "McDonald"), or an initial."""
        word = text[token.start : token.end]
        if len(word) == 1:
            return word.isupper()
        return (
            word[0].isupper()
            and any(ch.islower() for ch in word)
            and all(ch.isalpha() or ch in _NAME_INNER for ch in word)
            and token.norm not in self._index
            and token.norm not in self._field_index
        )

    def _match_parts(
        self, text: str, token: _Token, context: str, include_fields: bool, floor: int
    ) -> list[Mention]:
        """Each "/"- or "-"-separated part of an unmatched token, looked up on its own (D-25).

        An ambiguous key may match as a slash part only when another part of the token is a
        non-ambiguous skill, and then under the usual rules, the first part having the token's
        position: "Python/R" yields R, "A/R", "Go/No-Go" and "Spring/Summer" yield nothing. An
        all-capital acronym key needs no companion ("AI/ML", "BI/reporting"). As a hyphen part
        an ambiguous key needs a "<key>-<suffix>" compound ("Go-based"). Other keys match freely.
        `floor` is where the previous window ended (see `_is_money`).
        """
        if "/" not in token.norm and "-" not in token.norm:
            return []
        parts = _split_parts(text, token.lead_start, token.end)
        if "/" in token.norm and len(parts) > 1 and all(
            normalize_surface(text[part.start : part.end]) in _CURRENCY_CODES for part in parts
        ):
            return []  # a currency pair, "USD/CAD" (D-27)
        # Which parts are non-ambiguous skills on their own (the companions a slash part needs).
        plain = [
            self._match_part(
                text, part, context, include_fields, floor, sentence_start=False, ambiguous_allowed=False
            )
            is not None
            for part in parts
        ]
        found = []
        for k, part in enumerate(parts):
            hyphen = part.before in _DASH_CHARS or part.after in _DASH_CHARS
            if hyphen:
                ambiguous_allowed = _suffix_compound(text, parts, k)
            else:
                ambiguous_allowed = any(plain[m] for m in range(len(parts)) if m != k)
            mention = self._match_part(
                text,
                part,
                context,
                include_fields,
                floor,
                sentence_start=token.sentence_start and k == 0,
                ambiguous_allowed=ambiguous_allowed,
                acronym_allowed=not hyphen,
                initial_ok=not hyphen and ambiguous_allowed,  # "Go/Python" lists a skill (D-141)
            )
            if mention is not None:
                found.append(mention)
        return found

    def _match_part(
        self,
        text: str,
        part: _Part,
        context: str,
        include_fields: bool,
        floor: int,
        *,
        sentence_start: bool,
        ambiguous_allowed: bool,
        acronym_allowed: bool = False,
        initial_ok: bool = False,
    ) -> Optional[Mention]:
        """The part as written, then without a possessive "'s", through the same lookup. A blocked
        part, a field of study the caller left out, or a currency code in a money context yields
        nothing ("25 CAD/hr")."""
        for end, form in _forms(text, part.start, part.end):
            variants = surface_variants(form)
            if any(key in self._blocked for key in variants):
                return None
            original = text[part.start : end]
            hit = self._lookup(
                variants,
                original,
                context,
                sentence_start=sentence_start,
                include_fields=include_fields,
                ambiguous_allowed=ambiguous_allowed,
                acronym_allowed=acronym_allowed,
                initial_ok=initial_ok,
            )
            if hit is not None:
                node_id, key = hit
                if node_id.startswith(_FIELD_PREFIX) and not include_fields:
                    return None
                if key in _CURRENCIES and _is_money(text, part.start, end, floor, self._currency_nouns):
                    return None
                return self._mention(text, part.start, end, node_id, key)
        return None

    def _lookup(
        self,
        variants: tuple[str, ...],
        original: str,
        context: str,
        *,
        sentence_start: bool,
        include_fields: bool,
        ambiguous_allowed: bool = True,
        acronym_allowed: bool = False,
        initial_ok: bool = False,
    ) -> Optional[tuple[str, str]]:
        """(node id, matched key) for the first of a form's `surface_variants` that is a key and
        may stand at this position, else None: skills first, or fields first when
        `include_fields`. `ambiguous_allowed=False` rules out every ambiguous key except, with
        `acronym_allowed`, the all-capital acronyms (still under their casing rule). `initial_ok`:
        a `sentence_initial` key may stand at this sentence start (D-141)."""
        indexes = (self._field_index, self._index) if include_fields else (self._index, self._field_index)
        for index in indexes:
            for key in variants:
                node_id = index.get(key)
                if node_id is None:
                    continue
                if key in self._ambiguous and not (
                    (ambiguous_allowed or (acronym_allowed and key in self._acronyms))
                    and self._ambiguity_ok(
                        key,
                        original,
                        context=context,
                        sentence_start=sentence_start and not (initial_ok and key in self._initial_keys),
                    )
                ):
                    continue
                return node_id, key
        return None

    def _ambiguity_ok(self, key: str, original: str, *, context: str, sentence_start: bool) -> bool:
        """The casing rule for ambiguous keys, in one place. `resolve`: `original` has the required
        casing. Prose: the required casing, and not opening a sentence. List: a curated key stands
        in any casing at any position; an automatic `cased` key still needs its casing. Text holding
        an apostrophe never stands for one ("Scale's" is no "Scales"). Other keys always stand."""
        required = self._ambiguous.get(key)
        if required is None:
            return True
        if any(ch in APOSTROPHES for ch in original):
            return False  # a name is written as is: "Scale's" is no "Scales" (its bare form is tried next)
        if context == "list" and key in self._lenient:
            return True
        if context == "prose" and sentence_start:
            return False
        return normalize_keep_case(original) == required

    def _mention(self, text: str, start: int, end: int, node_id: str, key: str) -> Mention:
        method = self._method(key, node_id)
        return Mention(canonical_id=node_id, surface=text[start:end], start=start, end=end, method=method)

    def _method(self, key: str, node_id: str) -> Literal["EXACT", "ALIAS"]:
        return "EXACT" if key == self._label_key[node_id] else "ALIAS"


def _without_elision(surface: str) -> str:
    """`surface` past a leading French elision ("d'Excel" -> "Excel", D-27), else unchanged."""
    for apostrophe in APOSTROPHES:
        if apostrophe in surface:
            break
    else:  # no apostrophe at all: the common case, kept cheap (the index runs this per alias)
        return surface
    stripped = surface.lstrip()
    elision = _ELISION_RE.match(stripped)
    return surface if elision is None else stripped[elision.end() :]


def _is_acronym(casing: str) -> bool:
    """An all-capital casing of two letters or more ("AI", "ML", "REST"): not "R", not "Go"."""
    return casing.isupper() and sum(ch.isalpha() for ch in casing) >= 2


def _is_money(text: str, start: int, end: int, floor: int, nouns: frozenset[str]) -> bool:
    """Whether the currency code text[start:end] stands in a money context (D-27): after "$" or a
    salary word on its line, or a "$" amount before its bracket ("$80,000 (CAD)"); after a pay word
    of its clause with only link words, numbers and brackets between ("Pay range (CAD)", "All
    amounts are in CAD"); before an amount, a range, a rate unit or "per <unit>" on its line ("CAD
    80,000", "CAD 25-30", "CAD/hr", "CAD per hour"); after an amount or a number that closes a
    range unless one of `nouns` follows, alone or after one modifier ("85,000 CAD plus benefits"
    and "18 - 20 CAD" are money, "1,200 CAD drawings" and "1,500 CAD shop drawings" the skill);
    after a smaller number that a rate or money word, a range to another number, an addition or a
    separator follows ("25 CAD hourly", "40 CAD + HST"). A year is no number, nor is one glued to
    letters ("V5") or before `floor`, where the previous window ended ("Fusion 360 CAD (cloud)":
    360 belongs to the name). "Proficient in CAD", "Taught CAD annually", "a wide range of CAD
    tools", "the 2024 CAD standards" and "Fusion 360 CAD." are the skill."""
    line_start = max(text.rfind("\n", 0, start), text.rfind("\r", 0, start)) + 1
    before = text[max(line_start, start - 30) : start]
    if _DOLLAR_BEFORE.search(before) or _SALARY_WORDS.search(before):
        return True
    if _PAY_BEFORE.search(_clause_before(text, line_start, start)):
        return True
    if _AMOUNT_AFTER.match(text, end):
        return True
    low = max(line_start, floor, start - 30)
    number = _NUMBER_BEFORE.search(text, low, start)
    if number is None or _YEAR_ONLY.fullmatch(number.group(1)):
        return False
    if _is_amount(number) or _RANGE_BEFORE.search(text, low, number.start(1)):
        return not _a_noun_of_the_skill_follows(text, end, nouns)
    return _SMALL_NUMBER_MONEY_AFTER.match(text, end) is not None


def _clause_before(text: str, line_start: int, start: int) -> str:
    """The text of the code's clause before it, on its line and at most 60 characters: what
    follows the last sentence end (". ", "! ", "? ") or ";", "|", bullet there."""
    window = text[max(line_start, start - 60) : start]
    cut = 0
    for match in _CLAUSE_BREAK.finditer(window):
        cut = match.end()
    return window[cut:]


def _a_noun_of_the_skill_follows(text: str, end: int, nouns: frozenset[str]) -> bool:
    """Whether one of `nouns` is the next word after text[:end], on its line, or the word after a
    modifier that is no function or rate word ("1,500 CAD shop drawings", "2,000 CAD 3D models";
    not "5,000 CAD on parts")."""
    word = _NEXT_WORD.match(text, end)
    if word is None:
        return False
    if normalize_surface(word.group(1)) in nouns:
        return True
    pair = _NEXT_TWO_WORDS.match(text, end)
    return (
        pair is not None
        and normalize_surface(pair.group(1)) not in _NO_MODIFIER
        and normalize_surface(pair.group(2)) in nouns
    )


def _is_amount(number: re.Match[str]) -> bool:
    """A number before a currency code that is an amount, not a count or a version: thousands
    ("80,000"), cents ("25.50"), a magnitude ("1.2M", "80k") or four digits or more."""
    digits, magnitude = number.group(1), number.group(2)
    return bool(
        magnitude
        or _THOUSANDS.search(digits)
        or _CENTS.fullmatch(digits)
        or sum(ch.isdigit() for ch in digits.split(".")[0]) >= 4
    )


def _crosses(text: str, tokens: list[_Token], i: int, j: int, marks: frozenset[str]) -> bool:
    """Whether the window of tokens i..j spans one of `marks` between its words."""
    return any(ch in marks for k in range(i + 1, j + 1) for ch in text[tokens[k - 1].end : tokens[k].start])


def _build_index(graph: SkillGraph, *, fields: bool) -> dict[str, str]:
    """key -> node id over the fields of study (`fields=True`) or over every other node. A key
    claimed by several nodes goes to the curated node, then to a node that lists it outright
    over one that reaches it only through a hyphen variant, then to the smallest id. The rank is
    a total order, so node order never matters. A surface opening with a French elision is
    indexed past it ("s'adapter ..."), as lookups read it."""
    claims: dict[str, tuple[int, int, str]] = {}
    for node_id, node in graph.nodes.items():
        if node_id.startswith(_FIELD_PREFIX) != fields:
            continue
        curated = 0 if node.source == "curated" else 1
        for surface in (node.label, *node.aliases):
            for position, key in enumerate(surface_variants(normalize_surface(_without_elision(surface)))):
                rank = (curated, 0 if position == 0 else 1, node_id)
                if key not in claims or rank < claims[key]:
                    claims[key] = rank
    return {key: claims[key][2] for key in sorted(claims)}


def _char_mask(text: str) -> int:
    """One bit per character (folded mod 64). Bits set in one mask and not the other count at most
    the characters only one string has, so they never overstate the edit distance."""
    mask = 0
    for ch in text:
        mask |= 1 << (ord(ch) % 64)
    return mask


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start] in _EDGE_LEAD:
        start += 1
    while end > start and text[end - 1] in _EDGE_TRAIL:
        end -= 1
    return start, end


def _forms(text: str, start: int, end: int) -> tuple[tuple[int, str], ...]:
    """(end, normalized form) of a span: the span itself, then the span without a possessive "'s"
    (straight or curly apostrophe; "React's" -> React), so a mention never includes the suffix."""
    normalized = normalize_surface(text[start:end]) if start < end else ""
    if not normalized:
        return ()
    if end - start > 2 and text[end - 2] in _POSSESSIVE and text[end - 1] in "sS":
        bare_start, bare_end = _trim(text, start, end - 2)
        bare = normalize_surface(text[bare_start:bare_end]) if bare_start < bare_end else ""
        if bare:
            return (end, normalized), (bare_end, bare)
    return ((end, normalized),)


def _joinable(gap: str) -> bool:
    """Whether one window may span this gap between two tokens: only whitespace without a line
    break, invisible characters, the (R)/TM/SM/(C) marks, ":" and dash runs standing alone between
    spaces ("Architect - Associate"). Any other punctuation or a line break ends the window."""
    for ch in gap:
        if ch in _LINE_BREAKS:
            return False
        if not (ch.isspace() or ch in INVISIBLE or ch in _JOIN_MARKS or ch in _DASH_CHARS):
            return False
    for run in _DASH_RUN_RE.finditer(gap):
        start, end = run.start(), run.end()
        if start == 0 or end == len(gap) or not (gap[start - 1].isspace() and gap[end].isspace()):
            return False  # attached to a word ("Architect -Associate"), not standing alone
    return True


def _split_parts(text: str, token_start: int, token_end: int) -> list[_Part]:
    parts = []
    for match in _PART_RE.finditer(text, token_start, token_end):
        before = text[match.start() - 1] if match.start() > token_start else ""
        after = text[match.end()] if match.end() < token_end else ""
        start, end = _trim(text, match.start(), match.end())
        parts.append(_Part(start, end, before, after))
    return parts


def _near(text: str, left: _Token, right: _Token) -> bool:
    """Two tokens on one line with only spaces, commas, slashes, "&", dashes or apostrophes ("'24")
    between them."""
    return all(
        (ch.isspace() and ch not in _LINE_BREAKS) or ch in _NEAR_MARKS for ch in text[left.end : right.start]
    )


def _dates_a_season(text: str, tokens: list[_Token], i: int) -> bool:
    """Whether token i, a season word, dates something (D-27): a year or a term word follows it,
    perhaps after "of" ("Spring 2024", "Spring '24", "Spring of 2024", "Spring term", "Spring
    co-op"), or another season stands next to it, perhaps across one link word ("Fall and
    Spring", "Summer, Fall, Spring")."""
    if i + 1 < len(tokens) and _near(text, tokens[i], tokens[i + 1]):
        after = tokens[i + 1].norm
        if _YEAR_RE.fullmatch(after) or after in _TERM_WORDS:
            return True
        if after == "of" and i + 2 < len(tokens) and _near(text, tokens[i + 1], tokens[i + 2]):
            if _YEAR_RE.fullmatch(tokens[i + 2].norm):
                return True
    for step in (-1, 1):
        near, k = i, i + step
        while 0 <= k < len(tokens) and _near(text, tokens[min(near, k)], tokens[max(near, k)]):
            word = tokens[k].norm
            if word in _SEASONS:
                return True
            if word not in _SEASON_LINKS or near != i:
                break
            near, k = k, k + step
    return False


def _title_or_initial(text: str, token: _Token) -> bool:
    """A one-letter capital or a personal title, the words a period may follow before an
    initial ("J. R. R. Tolkien", "Dr. R. Smith")."""
    word = text[token.start : token.end]
    return (len(word) == 1 and word.isupper()) or token.norm in _TITLES


def _suffix_compound(text: str, parts: list[_Part], k: int) -> bool:
    """Part k and the next part form exactly "<part>-<suffix>" with a known suffix ("Go-based")."""
    part = parts[k]
    if part.before in _DASH_CHARS or part.after not in _DASH_CHARS or k + 1 == len(parts):
        return False
    suffix = parts[k + 1]
    if suffix.after in _DASH_CHARS:
        return False
    return normalize_surface(text[suffix.start : suffix.end]) in _HYPHEN_SUFFIXES


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    previous_start = previous_end = 0
    for match in _TOKEN_RE.finditer(text):
        start, end = _trim(text, match.start(), match.end())
        forms = _forms(text, start, end)
        if not forms:
            continue
        if not tokens:
            joined, sentence_start = False, True
        else:
            gap = text[previous_end:start]
            # The period closing an initialism does not end a window before a lower-case word:
            # "P.Eng. candidate", "C.P.A. candidate" (D-28). "ASP.NET. Core features" and
            # "P.Eng. Candidate pool" (a new sentence) stay apart.
            joined = _joinable(gap) or (
                _ABBREVIATION_GAP.fullmatch(gap) is not None
                and _INITIALISM_RE.fullmatch(text, previous_start, previous_end) is not None
                and text[start].islower()
            )
            sentence_start = any(ch in _SENTENCE_BREAK for ch in gap)
        lead_start, lead_forms = start, forms
        elision = _ELISION_RE.match(text, start, end)  # "d'Excel": a window starts at "Excel"
        if elision is not None:
            lead_start = elision.end()
            lead_forms = _forms(text, lead_start, end) or forms
        tokens.append(_Token(start, end, forms, joined, sentence_start, lead_start, lead_forms))
        previous_start, previous_end = start, end
    return tokens
