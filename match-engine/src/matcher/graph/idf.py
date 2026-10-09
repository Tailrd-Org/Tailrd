"""Inverse document frequency of skills over postings (SPEC §3.1, DECISIONS D-21).

`raw_idf` is the spec formula. `normalize_idf` maps it to [0, 1] as the mention-weighted CDF:
a skill's value is the share of all skill mentions in the corpus that belong to more common
skills, counting its own tie group at half weight. The result keeps the ordering of `raw_idf`
and gives the critical-miss threshold (0.7) a readable meaning: rarer than 70 % of what postings
ask for.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Mapping

from matcher.config import EngineConfig
from matcher.graph.model import SkillGraph


def _check(n_postings: int, df: int, skill: str = "") -> None:
    if n_postings < 1:
        raise ValueError(f"n_postings must be >= 1, got {n_postings}")
    if not 0 <= df <= n_postings:
        raise ValueError(f"document frequency {df} of {skill or 'skill'} outside [0, {n_postings}]")


def raw_idf(n_postings: int, df: int) -> float:
    """`ln(N / (1 + df))` (SPEC §3.1)."""
    _check(n_postings, df)
    return math.log(n_postings / (1 + df))


def normalize_idf(df: Mapping[str, int], n_postings: int) -> dict[str, float]:
    """Normalised IDF in [0, 1] for every skill with df > 0 (DECISIONS D-21).

    value(s) = (Σ df of skills with a strictly lower raw idf + ½ · Σ df of skills with an equal
    raw idf) / Σ df. Skills with df == 0 are omitted; they take `idf.unseen_default` at lookup.

    `raw_idf` is strictly decreasing in df, so "strictly lower raw idf" is exactly "strictly higher
    df" and "equal raw idf" is "equal df". The comparison therefore runs on the integer counts
    (no float equality), and each value is one division of two integers: identical for any input
    order.
    """
    if n_postings < 1:
        raise ValueError(f"n_postings must be >= 1, got {n_postings}")
    seen: dict[str, int] = {}
    for skill, count in df.items():
        _check(n_postings, count, skill)
        if count > 0:
            seen[skill] = count
    total = sum(seen.values())
    if total == 0:
        return {}
    mass: Counter[int] = Counter()  # df value -> mentions held by all skills with that df
    for count in seen.values():
        mass[count] += count
    value_of: dict[int, float] = {}
    more_common = 0  # mentions held by skills with a higher df (= lower raw idf)
    for count in sorted(mass, reverse=True):
        value_of[count] = (2 * more_common + mass[count]) / (2 * total)
        more_common += mass[count]
    return {skill: value_of[seen[skill]] for skill in sorted(seen)}


def idf(graph: SkillGraph, skill_id: str, config: EngineConfig) -> float:
    """The skill's normalised IDF, or `scoring.idf.unseen_default` when the corpus never saw it."""
    return graph.idf_of(skill_id, config.scoring.idf.unseen_default)
