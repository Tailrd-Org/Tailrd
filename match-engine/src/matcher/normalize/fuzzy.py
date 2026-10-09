"""Bounded fuzzy matching and its review log (SPEC §3.3).

Fuzzy hits are taxonomy gaps: every one is logged so a person can promote it to a real alias.
"""

from __future__ import annotations

from typing import Optional

from pydantic import Field

from matcher.models import Frozen


def bounded_levenshtein(a: str, b: str, max_distance: int) -> Optional[int]:
    """Levenshtein distance between `a` and `b`, or None when it exceeds `max_distance`.

    Classic dynamic programming, one row at a time, abandoned as soon as every cell of a row is
    above the bound (the distance can only grow from there).
    """
    if max_distance < 0:
        raise ValueError(f"max_distance must be >= 0, got {max_distance}")
    if abs(len(a) - len(b)) > max_distance:
        return None
    if a == b:
        return 0
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b)))
        if min(current) > max_distance:
            return None
        previous = current
    return previous[-1] if previous[-1] <= max_distance else None


class FuzzyHit(Frozen):
    surface: str  # the text as it was given to the resolver
    canonical_id: str
    distance: int = Field(ge=1)


class FuzzyLog:
    """Collects fuzzy hits, in the order they happened, for review."""

    def __init__(self) -> None:
        self.hits: list[FuzzyHit] = []

    def record(self, hit: FuzzyHit) -> None:
        self.hits.append(hit)

    def lines(self) -> list[str]:
        """One tab-separated `surface<TAB>canonical_id<TAB>distance` line per hit; whitespace in the
        surface is collapsed so each hit stays on one line."""
        return [f"{' '.join(hit.surface.split())}\t{hit.canonical_id}\t{hit.distance}" for hit in self.hits]
