"""§5.7 Tools & secondary keywords (Task 3.2): the §5.1 machinery over IMPLIED requirements — the
skills a posting mentions outside its required/preferred statements, plus the overflow beyond the
top 25 at a reduced weight (SPEC §14.4, DECISIONS D-30).

Expected values are closed-form arithmetic from the spec constants in tests/builders.py with IDF
read from graph_small (EVAL §2.2).
"""

from __future__ import annotations

import math

import pytest

from matcher.errors import Flag
from matcher.models import DimensionName, Necessity, Polarity
from matcher.score.dimensions import score_tools
from matcher.score.prepare import prepare_resume
from tests.builders import (
    AS_OF,
    E_BLOB,
    E_OWNERSHIP,
    W_TOOLS,
    bullet,
    current_role,
    listed,
    posting,
    req,
    resume,
)

REQUIRED, PREFERRED, IMPLIED = Necessity.REQUIRED, Necessity.PREFERRED, Necessity.IMPLIED
OVERFLOW = 0.50  # SPEC §14.4 "reduced weight" (config/scoring.yaml posting.overflow_weight_multiplier)


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12)


def _score(cv, post, graph, config):
    return score_tools(prepare_resume(cv, graph, config, AS_OF), post, graph, config)


def test_closed_form_tools_coverage_with_overflow_at_half_weight(small_graph, config) -> None:
    cv = resume(roles=(current_role(bullet("skill.git")),), blob=listed("skill.docker"))
    post = posting(
        req("skill.git", IMPLIED),
        req("skill.excel", IMPLIED, weight_multiplier=OVERFLOW),  # overflow beyond the top 25
        req("skill.kubernetes", IMPLIED, weight_multiplier=OVERFLOW),
        req("skill.react", REQUIRED),
    )
    i_g, i_e, i_k = (small_graph.idf[s] for s in ("skill.git", "skill.excel", "skill.kubernetes"))
    m_git, m_excel, m_kubernetes = E_OWNERSHIP, 0.0, 0.70 * E_BLOB
    expected = (i_g * 1.0 * m_git + i_e * OVERFLOW * m_excel + i_k * OVERFLOW * m_kubernetes) / (
        i_g * 1.0 + i_e * OVERFLOW + i_k * OVERFLOW)

    dim = _score(cv, post, small_graph, config)
    assert dim.name is DimensionName.TOOLS
    assert _close(dim.value, expected)
    assert _close(dim.weight, W_TOOLS) and _close(dim.effective_weight, W_TOOLS)
    assert _close(dim.contribution, W_TOOLS * expected)
    assert [(f.code, f.subject) for f in dim.facts] == [
        ("REQ_MISS", "skill.excel"), ("REQ_MATCH", "skill.git"), ("REQ_PARTIAL", "skill.kubernetes")]
    assert dim.facts[0].detail["weight_multiplier"] == OVERFLOW and dim.facts[0].detail["necessity"] == "IMPLIED"
    # The overflow weight matters: at full weight the same matches score lower (excel is missed).
    full = posting(req("skill.git", IMPLIED), req("skill.excel", IMPLIED), req("skill.kubernetes", IMPLIED))
    at_full = (i_g * m_git + i_e * m_excel + i_k * m_kubernetes) / (i_g + i_e + i_k)
    assert _close(_score(cv, full, small_graph, config).value, at_full) and at_full < expected


def test_tools_never_raise_a_critical_miss(small_graph, config) -> None:
    dim = _score(resume(), posting(req("skill.terraform", IMPLIED), req("skill.kubernetes", IMPLIED)), small_graph,
                 config)
    assert _close(dim.value, 0.0) and all(f.code == "REQ_MISS" for f in dim.facts) and len(dim.facts) == 2


def test_tools_use_the_same_evidence_rules(small_graph, config) -> None:
    cv = resume(blob=(*listed("skill.git", polarity=Polarity.NEGATED), *listed("skill.excel")))
    dim = _score(cv, posting(req("skill.git", IMPLIED), req("skill.excel", IMPLIED)), small_graph, config)
    i_g, i_e = small_graph.idf["skill.git"], small_graph.idf["skill.excel"]
    assert _close(dim.value, (i_g * 0.0 + i_e * E_BLOB) / (i_g + i_e))


@pytest.mark.parametrize("requirements", [(), (req("skill.python", REQUIRED), req("skill.go", PREFERRED))])
def test_abstains_without_implied_requirements(small_graph, config, requirements) -> None:
    dim = _score(resume(blob=listed("skill.python")), posting(*requirements), small_graph, config)
    assert dim.abstained and dim.value is None and dim.abstain_reason is Flag.NO_IMPLIED_REQUIREMENTS
    assert _close(dim.weight, W_TOOLS) and dim.effective_weight == 0.0 and dim.contribution == 0.0
    assert dim.facts == ()


def test_abstains_when_no_implied_requirement_carries_weight(small_graph, config) -> None:
    post = posting(req("skill.git", IMPLIED, weight_multiplier=0.0), req("skill.excel", IMPLIED, weight_multiplier=0.0))
    dim = _score(resume(blob=listed("skill.git")), post, small_graph, config)
    assert dim.abstained and dim.abstain_reason is Flag.NO_REQUIREMENTS_RESOLVED and dim.facts == ()
