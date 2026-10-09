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
  first part keeping the token's position; inside a hyphen token only a "<key>-<suffix>"
  compound with a known suffix matches ("Go-based", never "Go-Live"; DECISIONS D-25).
- `extract`, list: any casing, any position; the slash and hyphen rules still hold. Only the
  curated ambiguous keys get this leniency: the automatic `cased` keys (one-word product and tool
  names, DECISIONS D-26) keep their casing in lists too.

An ambiguous key is never a fuzzy target: a near miss cannot have the required casing.

`extract` also (DECISIONS D-27): skips fields of study (`field.*` nodes) unless
`include_fields=True`; lets a blocked phrase ("American Express", "Sous Chef", "Vitamin C") consume
its words without a mention; never reads a season word followed by a year or a term word, or next
to another season ("Spring 2024", "Fall and Spring"), nor a person's initial ("John R. Smith",
"Mary C Jones"), as a skill; and, like `resolve`, reads past a French elision ("d'Excel" -> Excel).
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
_ABBREVIATION_GAP = re.compile(r"\.[ \t]+")  # "P.Eng. candidate": the period closes the abbreviation
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
# A leading French elision ("d'Excel", "l'apprentissage", "qu'AWS"): the word after it is looked up.
_ELISION_RE = re.compile(rf"(?:qu|[ldjnscmt])[{APOSTROPHES}](?=\w)", re.IGNORECASE)
# Initials (D-27): what may stand around a one-letter capital that is a person's initial.
_NAME_INNER = frozenset("-" + APOSTROPHES)  # "Smith-Jones", "O'Neil"
_TITLES = frozenset({"dr", "mr", "mrs", "ms", "mx", "prof"})
_FIELD_PREFIX = "field."  # fields of study: extract reports them only with include_fields=True


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
        fuzzy_log: Optional[FuzzyLog] = None,
    ) -> None:
        """`ambiguous` maps a key to its required casing, e.g. {"go": "Go", "r": "R"}; skills
        lists waive it. `cased` keys (automatic, D-26) need their casing in lists too.
        `blocked_phrases` are non-skill phrases ("American Express") whose words never yield a
        mention."""
        taxonomy = config.scoring.taxonomy
        self._max_distance = taxonomy.fuzzy_max_distance
        self._min_length = taxonomy.fuzzy_min_token_length
        self._fuzzy_log = fuzzy_log
        self._label_key = {
            node_id: normalize_surface(_without_elision(node.label)) for node_id, node in graph.nodes.items()
        }
        self._index = _build_index(graph)

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
        self._blocked = frozenset(
            variant for phrase in blocked_phrases for variant in surface_variants(normalize_surface(phrase))
        )

        buckets: dict[int, list[str]] = {}
        for key in self._index:
            if len(key) >= self._min_length and key not in self._ambiguous:
                buckets.setdefault(len(key), []).append(key)
        self._fuzzy_keys = {length: tuple(sorted(keys)) for length, keys in buckets.items()}
        self._fuzzy_masks: dict[int, tuple[int, ...]] = {}  # per length bucket, built on first use

    # ─── one surface ──────────────────────────────────────────────────────────────────────

    def resolve(self, surface: str) -> Resolution:
        """Exact or alias match over the surface's variants; else a bounded fuzzy match for a
        single long token (logged); else NONE. A leading French elision is read past ("d'Excel");
        a blocked phrase is NONE."""
        body = _without_elision(surface)
        normalized = normalize_surface(body)
        variants = surface_variants(normalized)
        if any(key in self._blocked for key in variants):
            return Resolution(surface=surface, canonical_id=None, method="NONE")
        exact_key_seen = False
        for key in variants:
            node_id = self._index.get(key)
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
                hit = FuzzyHit(surface=surface, canonical_id=self._index[key], distance=edits)
                if self._fuzzy_log is not None:
                    self._fuzzy_log.record(hit)
                return Resolution(surface=surface, canonical_id=hit.canonical_id, method="FUZZY", distance=edits)
        return Resolution(surface=surface, canonical_id=None, method="NONE")

    def _fuzzy(self, normalized: str) -> Optional[tuple[str, int]]:
        """Closest key within the edit bound: smaller distance first, then the smaller key."""
        if self._max_distance < 1 or " " in normalized or len(normalized) < self._min_length:
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
        dashes, the (R)/TM/SM/(C) marks, ":" (credential names) and the period closing a dotted
        abbreviation ("P.Eng. candidate", D-28), never across other punctuation or a line
        break. A word ending in "'s" also tries its form without the suffix, and a
        window's first word is read past a French elision ("d'Excel"). A blocked phrase, and a
        field of study unless `include_fields`, consumes its span without a mention. A token
        holding "/" or "-" that matches nothing is retried as its parts ("React/Redux" -> React,
        Redux). Offsets index the original text; sorted by start.
        """
        if context not in ("prose", "list"):
            raise ValueError(f"unknown context {context!r} (expected 'prose' or 'list')")
        tokens = _tokenize(text)
        mentions: list[Mention] = []
        i = 0
        while i < len(tokens):
            matched = self._match_window(text, tokens, i, context, include_fields)
            if matched is not None:
                width, mention = matched
                if mention is not None:
                    mentions.append(mention)
                i += width
                continue
            mentions.extend(self._match_parts(text, tokens[i], context, include_fields))
            i += 1
        return tuple(sorted(mentions, key=lambda mention: (mention.start, mention.end)))

    def _match_window(
        self, text: str, tokens: list[_Token], i: int, context: str, include_fields: bool
    ) -> Optional[tuple[int, Optional[Mention]]]:
        """The longest window starting at token i that is a key or a blocked phrase, as (tokens
        consumed, mention). The mention is None when the window is a blocked phrase or a field of
        study the caller left out: the span is consumed all the same. At equal width a blocked
        phrase wins ("R&D"). A one-token match on a season that dates something ("Spring 2024")
        or on a person's initial ("John R. Smith") is no match."""
        last = i
        while last + 1 < len(tokens) and last + 1 - i < _MAX_WINDOW and tokens[last + 1].joined:
            last += 1
        first = tokens[i]
        start = first.lead_start
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
                hit = self._lookup(variants, original, context, sentence_start=first.sentence_start)
                if hit is None:
                    continue
                node_id, key = hit
                if node_id.startswith(_FIELD_PREFIX) and not include_fields:
                    return j - i + 1, None
                if j == i and (
                    (key in _SEASONS and _dates_a_season(text, tokens, i))
                    or (len(key) == 1 and self._is_initial(text, tokens, i, start, end))
                ):
                    continue
                return j - i + 1, self._mention(text, start, end, node_id, key)
        return None

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
        )

    def _match_parts(self, text: str, token: _Token, context: str, include_fields: bool) -> list[Mention]:
        """Each "/"- or "-"-separated part of an unmatched token, looked up on its own (D-25).

        An ambiguous key may match as a slash part only when another part of the token is a
        non-ambiguous skill, and then under the usual rules, the first part having the token's
        position: "Python/R" yields R, "A/R", "Go/No-Go" and "Spring/Summer" yield nothing. As a
        hyphen part it needs a "<key>-<suffix>" compound ("Go-based"). Other keys match freely.
        """
        if "/" not in token.norm and "-" not in token.norm:
            return []
        parts = _split_parts(text, token.lead_start, token.end)
        # Which parts are non-ambiguous skills on their own (the companions a slash part needs).
        plain = [
            self._match_part(
                text, part, context, include_fields, sentence_start=False, ambiguous_allowed=False
            )
            is not None
            for part in parts
        ]
        found = []
        for k, part in enumerate(parts):
            if part.before in _DASH_CHARS or part.after in _DASH_CHARS:
                ambiguous_allowed = _suffix_compound(text, parts, k)
            else:
                ambiguous_allowed = any(plain[m] for m in range(len(parts)) if m != k)
            mention = self._match_part(
                text,
                part,
                context,
                include_fields,
                sentence_start=token.sentence_start and k == 0,
                ambiguous_allowed=ambiguous_allowed,
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
        *,
        sentence_start: bool,
        ambiguous_allowed: bool,
    ) -> Optional[Mention]:
        """The part as written, then without a possessive "'s", through the same lookup. A blocked
        part, or a field of study the caller left out, yields nothing."""
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
                ambiguous_allowed=ambiguous_allowed,
            )
            if hit is not None:
                if hit[0].startswith(_FIELD_PREFIX) and not include_fields:
                    return None
                return self._mention(text, part.start, end, *hit)
        return None

    def _lookup(
        self,
        variants: tuple[str, ...],
        original: str,
        context: str,
        *,
        sentence_start: bool,
        ambiguous_allowed: bool = True,
    ) -> Optional[tuple[str, str]]:
        """(node id, matched key) for the first of a form's `surface_variants` that is a key and
        may stand at this position, else None. `ambiguous_allowed=False` rules out every
        ambiguous key."""
        for key in variants:
            node_id = self._index.get(key)
            if node_id is None:
                continue
            if key in self._ambiguous and not (
                ambiguous_allowed
                and self._ambiguity_ok(key, original, context=context, sentence_start=sentence_start)
            ):
                continue
            return node_id, key
        return None

    def _ambiguity_ok(self, key: str, original: str, *, context: str, sentence_start: bool) -> bool:
        """The casing rule for ambiguous keys, in one place. `resolve`: `original` has the required
        casing. Prose: the required casing, and not opening a sentence. List: a curated key stands
        in any casing at any position; an automatic `cased` key still needs its casing. Other keys
        always stand."""
        required = self._ambiguous.get(key)
        if required is None:
            return True
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


def _build_index(graph: SkillGraph) -> dict[str, str]:
    """key -> node id. A key claimed by several nodes goes to the curated node, then to a node
    that lists it outright over one that reaches it only through a hyphen variant, then to the
    smallest id. The rank is a total order, so node order never matters. A surface opening with
    a French elision is indexed past it ("s'adapter ..."), as lookups read it."""
    claims: dict[str, tuple[int, int, str]] = {}
    for node_id, node in graph.nodes.items():
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
    """Whether token i, a season word, dates something (D-27): a year or a term word follows it
    ("Spring 2024", "Spring '24", "Spring term", "Spring co-op"), or another season stands next to
    it, perhaps across one link word ("Fall and Spring", "Summer, Fall, Spring")."""
    if i + 1 < len(tokens) and _near(text, tokens[i], tokens[i + 1]):
        after = tokens[i + 1].norm
        if _YEAR_RE.fullmatch(after) or after in _TERM_WORDS:
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
            # The closing period of a dotted abbreviation does not end a window: "P.Eng. candidate",
            # "C.P.A. candidate" (D-28). A plain word's period still does ("Smith. Candidate").
            joined = _joinable(gap) or (
                _ABBREVIATION_GAP.fullmatch(gap) is not None and "." in text[previous_start:previous_end]
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
