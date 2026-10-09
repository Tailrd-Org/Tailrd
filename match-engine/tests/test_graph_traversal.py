"""Traversal over the fixture graph (SPEC §3, DECISIONS D-23): ancestors, relations, distance."""

from __future__ import annotations

import pytest

from matcher.graph.loader import load_graph
from matcher.graph.traversal import Relation, ancestors, distance, related_neighbors, relations
from matcher.models import RelationKind
from tests.fixtures.build_graph_small import FIXTURE_DIR

EXACT = RelationKind.EXACT
CHILD = RelationKind.CHILD
SIBLING = RelationKind.SIBLING
PARENT = RelationKind.PARENT


# ─── ancestors ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "max_depth,expected",
    [
        (1, {"skill.react": 1}),
        (2, {"skill.react": 1, "skill.js_framework": 2}),
        (3, {"skill.react": 1, "skill.js_framework": 2, "skill.javascript": 3}),
        (10, {"skill.react": 1, "skill.js_framework": 2, "skill.javascript": 3}),
    ],
)
def test_ancestors_respect_max_depth(small_graph, max_depth, expected) -> None:
    assert dict(ancestors(small_graph, "skill.nextjs", max_depth)) == expected


def test_ancestors_of_roots_unknown_ids_and_zero_depth_are_empty(small_graph) -> None:
    assert dict(ancestors(small_graph, "skill.javascript", 3)) == {}
    assert dict(ancestors(small_graph, "skill.does_not_exist", 3)) == {}
    assert dict(ancestors(small_graph, "skill.react", 0)) == {}


def test_ancestors_terminate_on_a_cycle_and_exclude_the_start(small_graph) -> None:
    # cycle_a -> cycle_b -> cycle_c -> cycle_a: the walk comes back to the start and stops.
    assert dict(ancestors(small_graph, "skill.cycle_a", 10_000)) == {"skill.cycle_b": 1, "skill.cycle_c": 2}
    assert dict(ancestors(small_graph, "skill.cycle_c", 1)) == {"skill.cycle_a": 1}


def test_ancestors_are_memoised_read_only_in_graph_cache() -> None:
    graph = load_graph(FIXTURE_DIR)
    first = ancestors(graph, "skill.nextjs", 3)
    assert ("anc", "skill.nextjs", 3) in graph.cache
    assert ancestors(graph, "skill.nextjs", 3) is first
    with pytest.raises(TypeError):
        first["skill.python"] = 1  # type: ignore[index]


# ─── relations ─────────────────────────────────────────────────────────────────────────────


def test_relation_exact(small_graph) -> None:
    assert relations(small_graph, "skill.react", "skill.react", 3) == (Relation(kind=EXACT, depth=0),)


def test_relation_child_when_resume_skill_is_a_descendant(small_graph) -> None:
    # react -> js_framework -> javascript
    assert relations(small_graph, "skill.react", "skill.javascript", 3) == (Relation(kind=CHILD, depth=2),)


def test_relation_parent_when_resume_skill_is_an_ancestor(small_graph) -> None:
    assert relations(small_graph, "skill.javascript", "skill.react", 3) == (Relation(kind=PARENT, depth=2),)


def test_relation_sibling_carries_the_edge_weight(small_graph) -> None:
    assert relations(small_graph, "skill.react", "skill.vue", 3) == (Relation(kind=SIBLING, depth=1, weight=0.65),)
    assert relations(small_graph, "skill.vue", "skill.react", 3) == (Relation(kind=SIBLING, depth=1, weight=0.65),)


def test_several_relations_at_once_in_kind_order(small_graph) -> None:
    # nextjs is a child of react AND they share a related edge (0.80).
    assert relations(small_graph, "skill.nextjs", "skill.react", 3) == (
        Relation(kind=CHILD, depth=1),
        Relation(kind=SIBLING, depth=1, weight=0.80),
    )
    assert relations(small_graph, "skill.react", "skill.nextjs", 3) == (
        Relation(kind=SIBLING, depth=1, weight=0.80),
        Relation(kind=PARENT, depth=1),
    )
    # Inside the cycle each node is both an ancestor and a descendant of the other.
    assert relations(small_graph, "skill.cycle_a", "skill.cycle_b", 3) == (
        Relation(kind=CHILD, depth=1),
        Relation(kind=PARENT, depth=2),
    )


def test_no_relation_is_an_empty_tuple(small_graph) -> None:
    assert relations(small_graph, "skill.react", "skill.python", 3) == ()
    # A shared parent without an edge earns nothing (DECISIONS D-23).
    assert relations(small_graph, "skill.react", "skill.angular", 3) == ()
    assert relations(small_graph, "skill.react", "skill.does_not_exist", 3) == ()


def test_relations_respect_max_depth(small_graph) -> None:
    assert relations(small_graph, "skill.nextjs", "skill.javascript", 2) == ()
    assert relations(small_graph, "skill.nextjs", "skill.javascript", 3) == (Relation(kind=CHILD, depth=3),)


# ─── distance ──────────────────────────────────────────────────────────────────────────────


def test_distance_counts_hops_over_parents_children_and_edges(small_graph) -> None:
    assert distance(small_graph, "skill.react", "skill.react", 3) == 0
    assert distance(small_graph, "skill.react", "skill.vue", 3) == 1  # related edge
    assert distance(small_graph, "skill.kubernetes", "skill.docker", 3) == 1  # related edge
    assert distance(small_graph, "skill.vue", "skill.angular", 3) == 2  # siblings under js_framework
    assert distance(small_graph, "skill.nextjs", "skill.javascript", 3) == 3  # parent chain
    assert distance(small_graph, "skill.javascript", "skill.nextjs", 3) == 3  # child chain


def test_distance_is_none_when_farther_unreachable_or_unknown(small_graph) -> None:
    assert distance(small_graph, "skill.nextjs", "skill.javascript", 2) is None
    assert distance(small_graph, "skill.react", "skill.python", 10) is None
    assert distance(small_graph, "skill.kubernetes", "skill.aws", 10) is None
    assert distance(small_graph, "skill.react", "skill.does_not_exist", 3) is None
    assert distance(small_graph, "skill.does_not_exist", "skill.react", 3) is None


def test_distance_is_symmetric(small_graph) -> None:
    ids = sorted(small_graph.nodes)
    for a in ids:
        for b in ids:
            assert distance(small_graph, a, b, 3) == distance(small_graph, b, a, 3), (a, b)


def test_memoised_and_fresh_results_agree() -> None:
    warm = load_graph(FIXTURE_DIR)
    cold = load_graph(FIXTURE_DIR)
    ids = sorted(warm.nodes)
    for _ in range(2):  # the second pass is served from warm.cache
        for a in ids:
            for b in ids:
                cold.cache.clear()
                assert relations(warm, a, b, 3) == relations(cold, a, b, 3), (a, b)
                cold.cache.clear()
                assert distance(warm, a, b, 3) == distance(cold, a, b, 3), (a, b)
            cold.cache.clear()
            assert dict(ancestors(warm, a, 3)) == dict(ancestors(cold, a, 3)), a


# ─── related neighbours (both directions, related_weight precedence) ──────────────────────


def _by_related_weight(graph, node_id: str) -> dict[str, float]:
    """The reference: every other node `related_weight` gives a weight for, read from node_id."""
    found = {}
    for other in graph.nodes:
        weight = graph.related_weight(node_id, other)
        if other != node_id and weight is not None:
            found[other] = weight
    return found


def test_related_neighbors_on_the_fixture(small_graph) -> None:
    assert dict(related_neighbors(small_graph, "skill.react")) == {"skill.nextjs": 0.80, "skill.vue": 0.65}
    assert dict(related_neighbors(small_graph, "skill.kubernetes")) == {"skill.docker": 0.70}
    assert dict(related_neighbors(small_graph, "skill.python")) == {}
    assert dict(related_neighbors(small_graph, "skill.does_not_exist")) == {}
    assert list(related_neighbors(small_graph, "skill.react")) == ["skill.nextjs", "skill.vue"]  # sorted
    for node_id in small_graph.nodes:
        assert dict(related_neighbors(small_graph, node_id)) == _by_related_weight(small_graph, node_id)


def test_related_neighbors_see_one_way_edges_and_prefer_the_edge_out_of_the_node() -> None:
    from tests.builders import tiny_graph

    graph = tiny_graph({}, one_way=[("skill.b", "skill.a", 0.7), ("skill.a", "skill.c", 0.5),
                                    ("skill.c", "skill.a", 0.6), ("skill.d", "skill.d", 0.9)])
    # b -> a stored only one way: both ends see it. a -> c 0.5 and c -> a 0.6: each end reads its own.
    assert dict(related_neighbors(graph, "skill.a")) == {"skill.b": 0.7, "skill.c": 0.5}
    assert dict(related_neighbors(graph, "skill.b")) == {"skill.a": 0.7}
    assert dict(related_neighbors(graph, "skill.c")) == {"skill.a": 0.6}
    assert graph.related_weight("skill.a", "skill.c") == 0.5 and graph.related_weight("skill.c", "skill.a") == 0.6
    assert dict(related_neighbors(graph, "skill.d")) == {}  # a stored self-edge is no neighbour


def test_related_neighbors_agree_with_related_weight_on_a_random_graph() -> None:
    from tests.builders import random_graph

    graph = random_graph(seed=5, n_nodes=250)
    for node_id in graph.nodes:
        assert dict(related_neighbors(graph, node_id)) == _by_related_weight(graph, node_id), node_id


def test_related_neighbors_are_memoised_and_read_only() -> None:
    graph = load_graph(FIXTURE_DIR)
    before = len(graph.cache)
    first = related_neighbors(graph, "skill.react")
    assert related_neighbors(graph, "skill.react") is first
    assert len(graph.cache) == before + 1  # one index for the whole graph; unknown ids add nothing
    related_neighbors(graph, "skill.unknown")
    related_neighbors(graph, "skill.python")
    assert len(graph.cache) == before + 1
    with pytest.raises(TypeError):
        first["skill.python"] = 0.5  # type: ignore[index]
