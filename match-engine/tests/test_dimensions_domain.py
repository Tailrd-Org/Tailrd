"""§5.5 Domain / industry overlap (Task 3.3).

SPEC §5.5; DECISIONS D-40. NAICS partial credit: one code a prefix of the other 1.00 (the same
industry at two depths; a sector-level code such as 31, 33 or 31-33 stands for its whole sector
group), a shared 3-digit prefix 0.70, the same 2-digit sector 0.40 (31-33, 44-45 and 48-49 each one
sector), else the 0.15 floor ("domain transfer is real and common"). The candidate's credit is the
best pair over every role's codes and every posting code.
"""

from __future__ import annotations

import math
import random
from datetime import date

import pytest

from matcher.config import load_config
from matcher.errors import EngineFault, Flag
from matcher.models import DimensionName, JobPosting, Resume, Role
from matcher.score.dimensions import score_domain
from matcher.score.dimensions.domain import naics_credit
from matcher.score.prepare import prepare_resume
from tests.builders import AS_OF, on, tiny_graph, with_scoring

EXACT, SAME_3DIGIT, SAME_2DIGIT, UNRELATED = 1.00, 0.70, 0.40, 0.15  # SPEC §5.5
W_DOMAIN = 0.10  # SPEC §5.5


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12)


def _role(*codes: str, dated: bool = True) -> Role:
    if not dated:
        return Role(industry_codes=codes)
    return Role(start=on(date(2019, 1, 1)), end=on(date(2021, 1, 1)), industry_codes=codes)


def _score(roles, posting_codes, graph, config):
    prepared = prepare_resume(Resume(roles=tuple(roles)), graph, config, AS_OF)
    return score_domain(prepared, JobPosting(industry_codes=tuple(posting_codes)), graph, config)


@pytest.mark.parametrize("a,b,expected", [
    ("541511", "5415", EXACT),  # custom programming inside computer systems design: a prefix
    ("541511", "541511", EXACT),
    ("54", "541330", EXACT),  # a sector code holds every industry under it
    ("541511", "541330", SAME_3DIGIT),  # common prefix 541
    ("5112", "5191", SAME_2DIGIT),  # common prefix 51
    ("336", "337", SAME_2DIGIT),  # common prefix 33 only
    ("311", "325", SAME_2DIGIT),  # 31 and 32: both manufacturing (31-33), different subsectors
    ("4411", "452", SAME_2DIGIT),  # retail (44-45)
    ("481", "4931", SAME_2DIGIT),  # transportation and warehousing (48-49)
    ("52", "62", UNRELATED),  # finance vs health care: the floor, never 0
    ("44", "48", UNRELATED),
    # A sector-level code stands for its whole sector group, as a prefix does (D-40):
    ("33", "336", EXACT),
    ("31", "336", EXACT),  # 31, 32 and 33 are all manufacturing
    ("31-33", "336", EXACT),  # NAICS writes the sector hyphenated
    ("32", "31", EXACT),
    ("31", "3399", EXACT),
    ("44", "4521", EXACT),
    ("44-45", "4411", EXACT),
    ("48-49", "4931", EXACT),
    ("31-33", "541511", UNRELATED),
    ("31-33", "44-45", UNRELATED),
    ("54", "52", UNRELATED),  # a sector outside every group stands for itself only
])
def test_naics_credit_closed_forms(config, a, b, expected) -> None:
    assert _close(naics_credit(a, b, config), expected)
    assert _close(naics_credit(b, a, config), expected)  # symmetric


def test_naics_credit_follows_the_config(config) -> None:
    no_groups = with_scoring(config, "domain", sector_groups=())
    assert _close(naics_credit("311", "325", no_groups), UNRELATED)
    assert _close(naics_credit("31", "336", no_groups), UNRELATED)  # 31 alone no longer covers 33
    assert _close(naics_credit("31-33", "336", no_groups), EXACT)  # the hyphenated code names 33 itself
    tuned = with_scoring(config, "domain", exact=0.9, same_3digit=0.6, same_2digit=0.3, unrelated=0.0)
    assert [naics_credit(a, b, tuned) for a, b in (("5415", "541511"), ("541511", "541330"), ("5112", "5191"),
                                                   ("52", "62"))] == [0.9, 0.6, 0.3, 0.0]


def test_the_best_pair_over_every_role_and_posting_code(small_graph, config) -> None:
    roles = [_role("52"), _role("541330", "62"), _role("9211", dated=False)]
    dim = _score(roles, ["541511", "4411"], small_graph, config)
    # Pairs: 52/541511 0.15, 52/4411 0.15, 541330/541511 0.70, 541330/4411 0.15, 62/… 0.15, 9211/… 0.15.
    assert dim.name is DimensionName.DOMAIN and dim.abstained is False
    assert _close(dim.value, SAME_3DIGIT)
    assert _close(dim.weight, W_DOMAIN) and _close(dim.effective_weight, W_DOMAIN)
    assert _close(dim.contribution, W_DOMAIN * SAME_3DIGIT)
    (fact,) = dim.facts
    assert (fact.code, fact.subject) == ("INDUSTRY_MATCH", "541511") and _close(fact.value, SAME_3DIGIT)
    assert fact.detail == {"candidate_code": "541330", "relation": "same_3digit"}


def test_every_role_counts_whatever_its_dates(small_graph, config) -> None:
    # D-40: the max over roles; an old or undated role in the posting's industry still counts.
    dim = _score([_role("52"), _role("5415", dated=False)], ["541511"], small_graph, config)
    assert _close(dim.value, EXACT)
    assert dim.facts[0].detail == {"candidate_code": "5415", "relation": "exact"}


def test_an_unrelated_industry_scores_the_floor(small_graph, config) -> None:
    dim = _score([_role("52")], ["62"], small_graph, config)
    assert _close(dim.value, UNRELATED) and dim.facts[0].detail["relation"] == "unrelated"


def test_ties_and_order_never_change_the_result(small_graph, config) -> None:
    roles = [_role("5415", "541511"), _role("52"), _role("541330")]
    codes = ["4411", "541511", "62"]
    reference = _score(roles, codes, small_graph, config)
    assert reference.facts[0].subject == "541511" and reference.facts[0].detail["candidate_code"] == "5415"
    rng = random.Random(4)
    for _ in range(10):
        rng.shuffle(roles)
        rng.shuffle(codes)
        assert _score(roles, codes, small_graph, config) == reference


def test_equal_credits_name_the_smaller_posting_code_first(small_graph, config) -> None:
    # With same-sector pairs priced at 0, the two best pairs (0.15 each) are 311/4599 and 441/3299:
    # the smaller posting code (311) and the smaller candidate code (3299) sit in different pairs.
    tuned = with_scoring(config, "domain", same_2digit=0.0)
    dim = _score([_role("3299"), _role("4599")], ["441", "311"], small_graph, tuned)
    assert _close(dim.value, UNRELATED)
    assert dim.facts[0].subject == "311" and dim.facts[0].detail["candidate_code"] == "4599"


def test_a_posting_tagged_with_a_sector_matches_any_industry_in_it(small_graph, config) -> None:
    # Before: 0.40 (only the same-sector group rule reached 336 from 31 or 31-33).
    for sector in ("31", "31-33", "33"):
        dim = _score([_role("336111")], [sector], small_graph, config)
        assert _close(dim.value, EXACT) and dim.facts[0].detail == {"candidate_code": "336111", "relation": "exact"}


@pytest.mark.parametrize("roles,codes", [
    ([_role()], ["541511"]),  # roles without codes
    ([], ["541511"]),  # no roles at all
    ([_role("541511")], []),  # the posting has none
    ([_role("", "  ")], ["541511"]),  # blank codes are no codes (a blank would prefix everything)
    ([_role("541511")], [" "]),
])
def test_abstains_when_either_side_has_no_codes(small_graph, config, roles, codes) -> None:
    dim = _score(roles, codes, small_graph, config)
    assert dim.abstained and dim.value is None and dim.abstain_reason is Flag.INDUSTRY_UNKNOWN
    assert _close(dim.weight, W_DOMAIN) and dim.effective_weight == 0.0 and dim.contribution == 0.0
    assert dim.facts == ()


def test_blank_codes_beside_real_ones_are_ignored(small_graph, config) -> None:
    dim = _score([_role("", "62")], ["541511", ""], small_graph, config)
    assert _close(dim.value, UNRELATED)


def test_a_prepared_resume_is_bound_to_its_graph_and_config(small_graph, config) -> None:
    prepared = prepare_resume(Resume(roles=(_role("541511"),)), small_graph, config, AS_OF)
    other_graph = tiny_graph({"skill.python": 0.5})
    other_config = with_scoring(config, "domain", unrelated=0.10)
    for post in (JobPosting(industry_codes=("5415",)), JobPosting()):  # even where it would abstain
        score_domain(prepared, post, small_graph, config)
        score_domain(prepared, post, small_graph, load_config())
        for graph, cfg in ((other_graph, config), (small_graph, other_config)):
            with pytest.raises(EngineFault) as err:
                score_domain(prepared, post, graph, cfg)
            assert err.value.flag is Flag.INTERNAL_ERROR
