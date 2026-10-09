"""§5.5 Domain / industry overlap (weight `domain`).

SPEC §5.5; DECISIONS D-40.

`naics_credit(a, b)`:
* `domain.exact` when one code is a prefix of the other (the same industry at two depths), or one is
  a sector-level code standing for the other's sector: two digits, or a hyphenated NAICS sector, a
  sector in one of `domain.sector_groups` standing for its whole group (31-33 manufacturing, 44-45
  retail, 48-49 transportation). So 31, 32, 33 and 31-33 each match 336 exactly, as 33 does.
* `same_3digit` for a common prefix of 3 or more digits.
* `same_2digit` for a common prefix of 2, or two codes whose sectors share a group (311 and 325).
* Else the `unrelated` floor (never 0: "domain transfer is real and common").
A blank code relates to nothing (it would be a prefix of every code).

`score_domain`: the best credit over every pair of a role's code (any role, dated or not: D-40
takes the max over roles) and a posting code. Abstains with INDUSTRY_UNKNOWN when either side has
no code (blank codes are no codes). One fact, INDUSTRY_MATCH: subject = the posting code, value =
the credit, detail = the candidate's code and the tier; equal credits go to the smaller posting
code, then the smaller candidate code.
"""

from __future__ import annotations

from collections.abc import Iterable
from operator import attrgetter

from matcher.config import DomainConfig, EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, DimensionScore, Fact, JobPosting
from matcher.score._shared import abstain, scored
from matcher.score.numeric import clamp01, tie_key
from matcher.score.prepare import PreparedResume, ensure_bound

__all__ = ["INDUSTRY_MATCH", "naics_credit", "score_domain"]

INDUSTRY_MATCH = "INDUSTRY_MATCH"
# Tiers, named as the `domain` config fields that price them; each tier's price is read through
# a getter built once here.
EXACT, SAME_3DIGIT, SAME_2DIGIT, UNRELATED = "exact", "same_3digit", "same_2digit", "unrelated"
_PRICE = {tier: attrgetter(tier) for tier in (EXACT, SAME_3DIGIT, SAME_2DIGIT, UNRELATED)}

_Groups = tuple[tuple[str, ...], ...]


def _sectors(code: str, groups: _Groups) -> frozenset[str]:
    """The two-digit sectors a sector-level code stands for: "33" → 31, 32, 33 (its group), "31-33"
    → 31, 32, 33, "52" → 52; none for a code below sector level ("336")."""
    parts = code.split("-")
    if not all(len(part) == 2 for part in parts):
        return frozenset()
    covered = set(parts)
    for group in groups:
        if covered.intersection(group):
            covered.update(group)
    return frozenset(covered)


def _shared_prefix(a: str, b: str) -> int:
    length = 0
    for x, y in zip(a, b):
        if x != y:
            break
        length += 1
    return length


def _tier(a: str, b: str, a_sectors: frozenset[str], b_sectors: frozenset[str], groups: _Groups) -> str:
    if not a or not b:
        return UNRELATED
    if a.startswith(b) or b.startswith(a) or b[:2] in a_sectors or a[:2] in b_sectors:
        return EXACT
    shared = _shared_prefix(a, b)
    if shared >= 3:
        return SAME_3DIGIT
    if shared >= 2 or any(a[:2] in group and b[:2] in group for group in groups):
        return SAME_2DIGIT
    return UNRELATED


def _credit(tier: str, rules: DomainConfig) -> float:
    return clamp01(_PRICE[tier](rules))


def naics_credit(a: str, b: str, config: EngineConfig) -> float:
    """The hierarchical partial credit between two NAICS codes (SPEC §5.5; DECISIONS D-40)."""
    rules = config.scoring.domain
    a, b = a.strip(), b.strip()
    groups = rules.sector_groups
    return _credit(_tier(a, b, _sectors(a, groups), _sectors(b, groups), groups), rules)


def _codes(codes: Iterable[str]) -> list[str]:
    return sorted({code.strip() for code in codes if code.strip()})


def score_domain(prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig) -> DimensionScore:
    ensure_bound(prepared, graph, config)
    name = DimensionName.DOMAIN
    held = _codes(code for role in prepared.resume.roles for code in role.industry_codes)
    wanted = _codes(posting.industry_codes)
    if not held or not wanted:
        return abstain(name, config, Flag.INDUSTRY_UNKNOWN)
    rules = config.scoring.domain
    groups = rules.sector_groups
    sectors = {code: _sectors(code, groups) for code in (*held, *wanted)}
    pairs = []
    for want in wanted:
        for have in held:
            tier = _tier(have, want, sectors[have], sectors[want], groups)
            pairs.append((_credit(tier, rules), tier, want, have))
    credit, tier, want, have = min(pairs, key=lambda pair: (-tie_key(pair[0]), pair[2], pair[3]))
    fact = Fact(code=INDUSTRY_MATCH, subject=want, value=credit, detail={"candidate_code": have, "relation": tier})
    return scored(name, config, credit, (fact,))
