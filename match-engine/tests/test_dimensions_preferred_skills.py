"""§5.2 Preferred skills coverage (Task 3.2): the §5.1 machinery over PREFERRED requirements, with
no critical-miss cap ("Missing these is normal").

SPEC §5.2, §14.5; DECISIONS D-30, D-31. Expected values are closed-form arithmetic from the spec
constants in tests/builders.py with IDF read from graph_small (EVAL §2.2).
"""

from __future__ import annotations

import math

import pytest

from matcher.errors import Flag
from matcher.models import DimensionName, Necessity, VerbClass
from matcher.score.coverage import match_requirements
from matcher.score.dimensions import score_preferred_skills
from matcher.score.prepare import prepare_resume
from tests.builders import (
    AS_OF,
    CHILD_CREDIT,
    E_BLOB,
    E_EXECUTION,
    W_PREFERRED,
    bullet,
    current_role,
    listed,
    posting,
    req,
    resume,
)

REQUIRED, PREFERRED, IMPLIED = Necessity.REQUIRED, Necessity.PREFERRED, Necessity.IMPLIED


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12)


def _score(cv, post, graph, config):
    return score_preferred_skills(prepare_resume(cv, graph, config, AS_OF), post, graph, config)


def test_closed_form_preferred_coverage(small_graph, config) -> None:
    # Has nextjs (EXECUTION, current) and Docker listed; prefers react, kubernetes and terraform.
    cv = resume(roles=(current_role(bullet("skill.nextjs", VerbClass.EXECUTION)),), blob=listed("skill.docker"))
    post = posting(req("skill.react", PREFERRED), req("skill.kubernetes", PREFERRED),
                   req("skill.terraform", PREFERRED), req("skill.python", REQUIRED))
    i_r, i_k, i_t = (small_graph.idf[s] for s in ("skill.react", "skill.kubernetes", "skill.terraform"))
    m_react = CHILD_CREDIT * E_EXECUTION  # nextjs is a child of react: 0.85 × 0.85
    m_kubernetes = 0.70 * E_BLOB  # SIBLING via docker
    expected = (i_r * m_react + i_k * m_kubernetes + i_t * 0.0) / (i_r + i_k + i_t)

    dim = _score(cv, post, small_graph, config)
    assert dim.name is DimensionName.PREFERRED_SKILLS
    assert _close(dim.value, expected)
    assert _close(dim.weight, W_PREFERRED) and _close(dim.effective_weight, W_PREFERRED)
    assert _close(dim.contribution, W_PREFERRED * expected)
    assert not dim.abstained and dim.abstain_reason is None
    assert [(f.code, f.subject) for f in dim.facts] == [
        ("REQ_PARTIAL", "skill.kubernetes"), ("REQ_MATCH", "skill.react"), ("REQ_MISS", "skill.terraform")]
    assert dim.facts[1].detail["relation"] == "CHILD" and dim.facts[1].detail["via"] == "skill.nextjs"
    assert all(f.detail["necessity"] == "PREFERRED" for f in dim.facts)


def test_no_critical_miss_for_preferred_skills(small_graph, config) -> None:
    # terraform is rare (idf > 0.7) and missed: normal for a preferred skill, never a cap.
    assert small_graph.idf["skill.terraform"] > 0.70
    post = posting(req("skill.terraform", PREFERRED), req("skill.go", PREFERRED))
    prepared = prepare_resume(resume(blob=listed("skill.excel")), small_graph, config, AS_OF)
    assert not any(m.critical for m in match_requirements(prepared, post.requirements, small_graph, config))
    dim = score_preferred_skills(prepared, post, small_graph, config)
    assert _close(dim.value, 0.0) and not dim.abstained
    assert [f.code for f in dim.facts] == ["REQ_MISS", "REQ_MISS"]


def test_preferred_or_group(small_graph, config) -> None:
    cv = resume(blob=listed("skill.mysql"))
    post = posting(req("skill.postgresql", PREFERRED, or_group="db"), req("skill.mysql", PREFERRED, or_group="db"))
    dim = _score(cv, post, small_graph, config)
    assert _close(dim.value, E_BLOB)  # one group: the max member, whatever its idf
    assert dim.facts[0].subject == "skill.mysql" and dim.facts[0].detail["members"] == "skill.mysql,skill.postgresql"
    assert _close(dim.facts[0].detail["idf"], max(small_graph.idf["skill.postgresql"], small_graph.idf["skill.mysql"]))


@pytest.mark.parametrize("requirements", [(), (req("skill.python", REQUIRED), req("skill.git", IMPLIED))])
def test_abstains_without_preferred_requirements(small_graph, config, requirements) -> None:
    dim = _score(resume(blob=listed("skill.python")), posting(*requirements), small_graph, config)
    assert dim.abstained and dim.value is None and dim.abstain_reason is Flag.NO_PREFERRED_REQUIREMENTS
    assert _close(dim.weight, W_PREFERRED) and dim.effective_weight == 0.0 and dim.contribution == 0.0
    assert dim.facts == ()


def test_abstains_when_no_preferred_requirement_carries_weight(small_graph, config) -> None:
    post = posting(req("skill.python", PREFERRED, weight_multiplier=0.0), req("skill.react", REQUIRED))
    dim = _score(resume(blob=listed("skill.python")), post, small_graph, config)
    assert dim.abstained and dim.abstain_reason is Flag.NO_REQUIREMENTS_RESOLVED and dim.facts == ()
