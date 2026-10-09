"""SkillGraph holds validated, immutable data (SPEC §3)."""

from __future__ import annotations

import pytest

from matcher.graph.model import SkillGraph, SkillNode
from matcher.models import SkillKind


def _nodes() -> dict[str, SkillNode]:
    return {
        "skill.js": SkillNode(id="skill.js", label="JavaScript", kind=SkillKind.TECHNOLOGY),
        "skill.react": SkillNode(id="skill.react", label="React", kind=SkillKind.TECHNOLOGY, parents=("skill.js",)),
        "skill.vue": SkillNode(id="skill.vue", label="Vue", kind=SkillKind.TECHNOLOGY, parents=("skill.js",)),
    }


def test_valid_graph() -> None:
    g = SkillGraph(_nodes(), {"skill.react": {"skill.vue": 0.65}}, {"skill.react": 0.6}, "v1")
    assert "skill.react" in g and len(g) == 3
    assert g.related_weight("skill.react", "skill.vue") == 0.65
    assert g.related_weight("skill.vue", "skill.react") == 0.65  # symmetric lookup
    assert g.related_weight("skill.js", "skill.vue") is None
    assert g.idf_of("skill.react", 0.75) == 0.6
    assert g.idf_of("skill.vue", 0.75) == 0.75  # unseen default (DECISIONS D-21)


def test_graph_is_immutable() -> None:
    g = SkillGraph(_nodes(), {}, {}, "v1")
    with pytest.raises(TypeError):
        g.nodes["skill.x"] = g.nodes["skill.js"]  # type: ignore[index]


@pytest.mark.parametrize(
    "related,idf",
    [({"skill.react": {"skill.nope": 0.5}}, {}), ({"skill.react": {"skill.vue": 1.5}}, {}), ({}, {"skill.js": -0.1})],
)
def test_invalid_graph_rejected(related, idf) -> None:
    with pytest.raises(ValueError):
        SkillGraph(_nodes(), related, idf, "v1")


def test_unknown_parent_rejected() -> None:
    nodes = _nodes()
    nodes["skill.svelte"] = SkillNode(id="skill.svelte", label="Svelte", kind=SkillKind.TECHNOLOGY,
                                      parents=("skill.missing",))
    with pytest.raises(ValueError, match="unknown parent"):
        SkillGraph(nodes, {}, {}, "v1")
