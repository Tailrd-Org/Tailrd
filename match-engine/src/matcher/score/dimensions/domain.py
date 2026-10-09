"""§5.5 Domain / industry overlap (weight `domain`).

SPEC §5.5; DECISIONS D-40.

`naics_credit(a, b)`: one code a prefix of the other (the same industry at two depths) →
`domain.exact`; a common prefix of 3 or more digits → `same_3digit`; of 2, or both codes' sectors
in one of `domain.sector_groups` (31-33 manufacturing, 44-45 retail, 48-49 transportation, each
one sector) → `same_2digit`; else the `unrelated` floor (never 0: "domain transfer is real and
common"). A blank code relates to nothing (it would be a prefix of every code).

`score_domain`: the best credit over every pair of a role's code (any role, dated or not: D-40
takes the max over roles) and a posting code. Abstains with INDUSTRY_UNKNOWN when either side has
no code (blank codes are no codes). One fact, INDUSTRY_MATCH: subject = the posting code, value =
the credit, detail = the candidate's code and the tier; equal credits go to the smaller posting
code, then the smaller candidate code.
"""

from __future__ import annotations

from collections.abc import Iterable

from matcher.config import DomainConfig, EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.models import DimensionName, DimensionScore, Fact, JobPosting
from matcher.score.dimensions._common import abstain, scored
from matcher.score.numeric import clamp01, tie_key
from matcher.score.prepare import PreparedResume, ensure_bound

__all__ = ["INDUSTRY_MATCH", "naics_credit", "score_domain"]

INDUSTRY_MATCH = "INDUSTRY_MATCH"
# Tiers, named as the `domain` config fields that price them.
EXACT, SAME_3DIGIT, SAME_2DIGIT, UNRELATED = "exact", "same_3digit", "same_2digit", "unrelated"


def _shared_prefix(a: str, b: str) -> int:
    length = 0
    for x, y in zip(a, b):
        if x != y:
            break
        length += 1
    return length


def _tier(a: str, b: str, rules: DomainConfig) -> str:
    if not a or not b:
        return UNRELATED
    if a.startswith(b) or b.startswith(a):
        return EXACT
    shared = _shared_prefix(a, b)
    if shared >= 3:
        return SAME_3DIGIT
    if shared >= 2 or any(a[:2] in group and b[:2] in group for group in rules.sector_groups):
        return SAME_2DIGIT
    return UNRELATED


def _credit(tier: str, rules: DomainConfig) -> float:
    prices = {EXACT: rules.exact, SAME_3DIGIT: rules.same_3digit, SAME_2DIGIT: rules.same_2digit,
              UNRELATED: rules.unrelated}
    return clamp01(prices[tier])


def naics_credit(a: str, b: str, config: EngineConfig) -> float:
    """The hierarchical partial credit between two NAICS codes (SPEC §5.5; DECISIONS D-40)."""
    rules = config.scoring.domain
    return _credit(_tier(a.strip(), b.strip(), rules), rules)


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
    pairs = []
    for want in wanted:
        for have in held:
            tier = _tier(have, want, rules)
            pairs.append((_credit(tier, rules), tier, want, have))
    credit, tier, want, have = min(pairs, key=lambda pair: (-tie_key(pair[0]), pair[2], pair[3]))
    fact = Fact(code=INDUSTRY_MATCH, subject=want, value=credit, detail={"candidate_code": have, "relation": tier})
    return scored(name, config, credit, (fact,))
