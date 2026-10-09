"""Surface form -> canonical skill id (SPEC §3.3).

`Resolver` indexes every node's label and aliases under `normalize_surface` and each key's hyphen
variants, so "front end", "front-end" and "frontend" all reach a node that lists any of them.
`resolve` maps one surface (exact/alias, then a bounded fuzzy fallback); `extract` finds every
mention in free text by greedy longest match, never fuzzy (bounded cost on long text).

Ambiguous keys ("go", "r", "rest", ...) are common words as well as skills. Each one carries its
required casing ("Go") and resolves only under these rules:

- `resolve` (one standalone surface): the surface must have the required casing.
- `extract`, prose: the required casing, not the first word of the text or of a sentence (after
  `.`, `!`, `?` or a line break), and never a fragment of a hyphen/slash token ("Go-Live").
- `extract`, list: any casing, any position.

An ambiguous key is never a fuzzy target: a near miss cannot have the required casing.
"""

from __future__ import annotations

import re
from types import MappingProxyType
from typing import Literal, Mapping, NamedTuple, Optional

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

# Tokens: maximal runs of word characters (any script) and the in-word punctuation of skill names
# (c++, c#, .net, node.js, ci/cd, scikit-learn, o'reilly). "&" keeps R&D / AT&T in one piece;
# invisible characters and Unicode dashes stay inside the word, as normalize_surface treats them.
_TOKEN_RE = re.compile(rf"[\w+#./&\-{APOSTROPHES}{DASHES}{INVISIBLE}]+")
_PART_RE = re.compile(rf"[^/\-{DASHES}]+")  # the pieces of "React/Redux" or "React-based"
# Edge characters normalize_surface would strip; trimming them keeps mention offsets tight.
_EDGE_LEAD = frozenset("-/_" + APOSTROPHES + DASHES + INVISIBLE)
_EDGE_TRAIL = _EDGE_LEAD | {"."}
_SENTENCE_BREAK = frozenset(".!?\n\r\u2026\u2028\u2029")
_MAX_WINDOW = 8  # tokens


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
    norm: str
    joined: bool  # only whitespace separates it from the previous token
    sentence_start: bool  # first token of the text, or the previous gap ends a sentence


class Resolver:
    def __init__(
        self,
        graph: SkillGraph,
        config: EngineConfig,
        *,
        ambiguous: Mapping[str, str] = _NO_AMBIGUOUS,
        fuzzy_log: Optional[FuzzyLog] = None,
    ) -> None:
        """`ambiguous` maps a key to its required casing, e.g. {"go": "Go", "r": "R"}."""
        taxonomy = config.scoring.taxonomy
        self._max_distance = taxonomy.fuzzy_max_distance
        self._min_length = taxonomy.fuzzy_min_token_length
        self._fuzzy_log = fuzzy_log
        self._label_key = {node_id: normalize_surface(node.label) for node_id, node in graph.nodes.items()}
        self._index = _build_index(graph)

        self._ambiguous: dict[str, str] = {}
        for key, casing in ambiguous.items():
            normalized = normalize_surface(key)
            required = normalize_keep_case(casing)
            if normalize_surface(required) != normalized:
                raise ValueError(f"ambiguous key {key!r} and its casing {casing!r} differ")
            if self._ambiguous.get(normalized, required) != required:
                raise ValueError(f"ambiguous key {normalized!r} given two casings")
            self._ambiguous[normalized] = required

        buckets: dict[int, list[str]] = {}
        for key in self._index:
            if len(key) >= self._min_length and key not in self._ambiguous:
                buckets.setdefault(len(key), []).append(key)
        self._fuzzy_keys = {length: tuple(sorted(keys)) for length, keys in buckets.items()}
        self._fuzzy_masks: dict[int, tuple[int, ...]] = {}  # per length bucket, built on first use

    # ─── one surface ──────────────────────────────────────────────────────────────────────

    def resolve(self, surface: str) -> Resolution:
        """Exact or alias match over the surface's variants; else a bounded fuzzy match for a
        single long token (logged); else NONE."""
        normalized = normalize_surface(surface)
        exact_key_seen = False
        for key in surface_variants(normalized):
            node_id = self._index.get(key)
            if node_id is None:
                continue
            exact_key_seen = True
            required = self._ambiguous.get(key)
            if required is not None and normalize_keep_case(surface) != required:
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

    def extract(self, text: str, *, context: Literal["prose", "list"] = "prose") -> tuple[Mention, ...]:
        """Every skill mention in `text`, by greedy longest match over up to 8 tokens.

        A matched span consumes its tokens. Windows never cross punctuation (only whitespace may
        separate their tokens). A token holding "/" or "-" that matches nothing is retried as its
        parts ("React/Redux" -> React, Redux). Offsets index the original text; sorted by start.
        """
        if context not in ("prose", "list"):
            raise ValueError(f"unknown context {context!r} (expected 'prose' or 'list')")
        tokens = _tokenize(text)
        mentions: list[Mention] = []
        i = 0
        while i < len(tokens):
            matched = self._match_window(text, tokens, i, context)
            if matched is not None:
                width, mention = matched
                mentions.append(mention)
                i += width
                continue
            mentions.extend(self._match_parts(text, tokens[i], context))
            i += 1
        return tuple(sorted(mentions, key=lambda mention: (mention.start, mention.end)))

    def _match_window(
        self, text: str, tokens: list[_Token], i: int, context: str
    ) -> Optional[tuple[int, Mention]]:
        """The longest window starting at token i that is a key, as (tokens consumed, mention)."""
        last = i
        while last + 1 < len(tokens) and last + 1 - i < _MAX_WINDOW and tokens[last + 1].joined:
            last += 1
        first = tokens[i]
        for j in range(last, i - 1, -1):
            phrase = " ".join(token.norm for token in tokens[i : j + 1])
            original = text[first.start : tokens[j].end]
            hit = self._lookup(phrase, original, context, sentence_start=first.sentence_start, fragment=False)
            if hit is not None:
                node_id, key = hit
                return j - i + 1, self._mention(text, first.start, tokens[j].end, node_id, key)
        return None

    def _match_parts(self, text: str, token: _Token, context: str) -> list[Mention]:
        """Each "/"- or "-"-separated part of an unmatched token, looked up on its own."""
        if "/" not in token.norm and "-" not in token.norm:
            return []
        found = []
        for part in _PART_RE.finditer(text, token.start, token.end):
            start, end = _trim(text, part.start(), part.end())
            normalized = normalize_surface(text[start:end]) if start < end else ""
            if not normalized:
                continue
            hit = self._lookup(normalized, text[start:end], context, sentence_start=False, fragment=True)
            if hit is not None:
                found.append(self._mention(text, start, end, *hit))
        return found

    def _lookup(
        self, normalized: str, original: str, context: str, *, sentence_start: bool, fragment: bool
    ) -> Optional[tuple[str, str]]:
        """(node id, matched key) for the first variant that is a key and passes the ambiguity
        rules for this position, else None."""
        for key in surface_variants(normalized):
            node_id = self._index.get(key)
            if node_id is None:
                continue
            required = self._ambiguous.get(key)
            if required is not None and context == "prose":
                if fragment or sentence_start or normalize_keep_case(original) != required:
                    continue
            return node_id, key
        return None

    def _mention(self, text: str, start: int, end: int, node_id: str, key: str) -> Mention:
        method = self._method(key, node_id)
        return Mention(canonical_id=node_id, surface=text[start:end], start=start, end=end, method=method)

    def _method(self, key: str, node_id: str) -> Literal["EXACT", "ALIAS"]:
        return "EXACT" if key == self._label_key[node_id] else "ALIAS"


def _build_index(graph: SkillGraph) -> dict[str, str]:
    """key -> node id. A key claimed by several nodes goes to the curated node, then to a node
    that lists it outright over one that reaches it only through a hyphen variant, then to the
    smallest id. The rank is a total order, so node order never matters."""
    claims: dict[str, tuple[int, int, str]] = {}
    for node_id, node in graph.nodes.items():
        curated = 0 if node.source == "curated" else 1
        for surface in (node.label, *node.aliases):
            for position, key in enumerate(surface_variants(normalize_surface(surface))):
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


def _tokenize(text: str) -> list[_Token]:
    tokens: list[_Token] = []
    previous_end: Optional[int] = None
    for match in _TOKEN_RE.finditer(text):
        start, end = _trim(text, match.start(), match.end())
        if start >= end:
            continue
        normalized = normalize_surface(text[start:end])
        if not normalized:
            continue
        if previous_end is None:
            joined, sentence_start = False, True
        else:
            gap = text[previous_end:start]
            joined = all(ch.isspace() or ch in INVISIBLE for ch in gap)
            sentence_start = any(ch in _SENTENCE_BREAK for ch in gap)
        tokens.append(_Token(start, end, normalized, joined, sentence_start))
        previous_end = end
    return tokens
