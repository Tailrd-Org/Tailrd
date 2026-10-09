"""Bare relation credits (Task 3.3): what a set of held ids earns for each wanted id, for experience
relevance (DECISIONS D-35) and education fields and credentials (D-41).

The D-23 table with the D-24 provisional discount and no evidence or recency: exact 1.00, child
0.85, sibling clamp(edge, 0.40, 0.80), parent 0.40. The oracle is `match_skill` (Task 3.2) on a
résumé holding every id at value 1.0 (ROLE_TITLE mentions in a current role: E = 1.00, R = 1),
so the two can never drift apart.
"""

from __future__ import annotations

import math
import random

import pytest

from matcher.models import Evidence, RelationKind, SkillSource
from matcher.score.coverage import match_skill
from matcher.score.credits import RelationTargets, best_credit
from matcher.score.prepare import prepare_resume
from tests.builders import (
    AS_OF,
    CHILD_CREDIT,
    EXACT_CREDIT,
    PARENT_CREDIT,
    PROVISIONAL,
    SIBLING_MAX,
    SIBLING_MIN,
    current_role,
    random_graph,
    ref,
    resume,
    tiny_graph,
    with_scoring,
)

EXACT, CHILD, SIBLING, PARENT, NONE = (RelationKind.EXACT, RelationKind.CHILD, RelationKind.SIBLING,
                                       RelationKind.PARENT, RelationKind.NONE)


def _credits(held, wanted, graph, config):
    return RelationTargets(wanted, graph, config).credits(held)


def _assert_credit(got, credit: float, relation: RelationKind, via: str) -> None:
    assert math.isclose(got[0], credit, rel_tol=0.0, abs_tol=1e-12), f"credit {got[0]!r}, expected {credit!r}"
    assert got[1:] == (relation, via)


def _oracle(held, wanted, graph, config) -> dict:
    """`match_skill` on a résumé that holds every `held` id at value 1.0: ROLE_TITLE evidence (E = 1.00,
    SPEC §5.1) in a role current as of today (R = 1)."""
    titles = [ref(skill_id, Evidence.ROLE_TITLE, source=SkillSource.ROLE) for skill_id in sorted(set(held))]
    prepared = prepare_resume(resume(roles=(current_role(*titles),)), graph, config, AS_OF)
    assert all(value.value == 1.0 for value in prepared.best.values())
    found = {want: match_skill(prepared, want, graph, config) for want in sorted(set(wanted))}
    return {want: got for want, got in found.items() if got[1] is not NONE}


# ─── The D-23 table, bare ──────────────────────────────────────────────────────────────


def test_each_relation_earns_its_bare_table_credit(small_graph, config) -> None:
    wanted = ["skill.react"]
    _assert_credit(_credits(["skill.react"], wanted, small_graph, config)["skill.react"], EXACT_CREDIT, EXACT,
                   "skill.react")
    # nextjs sits below react and shares a 0.80 edge with it: the child credit (0.85) is the larger.
    _assert_credit(_credits(["skill.nextjs"], wanted, small_graph, config)["skill.react"], CHILD_CREDIT, CHILD,
                   "skill.nextjs")
    _assert_credit(_credits(["skill.vue"], wanted, small_graph, config)["skill.react"], 0.65, SIBLING, "skill.vue")
    _assert_credit(_credits(["skill.javascript"], wanted, small_graph, config)["skill.react"], PARENT_CREDIT,
                   PARENT, "skill.javascript")
    # A shared parent without an edge is no relation (D-23); nor is an unrelated skill.
    assert _credits(["skill.angular", "skill.python"], wanted, small_graph, config) == {}


def test_relations_reach_max_depth_and_stop_there(small_graph, config) -> None:
    # nextjs → react → js_framework → javascript: three levels either way.
    _assert_credit(_credits(["skill.nextjs"], ["skill.javascript"], small_graph, config)["skill.javascript"],
                   CHILD_CREDIT, CHILD, "skill.nextjs")
    _assert_credit(_credits(["skill.javascript"], ["skill.nextjs"], small_graph, config)["skill.nextjs"],
                   PARENT_CREDIT, PARENT, "skill.javascript")
    shallow = with_scoring(config, "relations", max_depth=2)
    assert _credits(["skill.nextjs"], ["skill.javascript"], small_graph, shallow) == {}
    assert _credits(["skill.javascript"], ["skill.nextjs"], small_graph, shallow) == {}


def test_sibling_weight_is_clamped_and_read_from_the_held_side(config) -> None:
    graph = tiny_graph({}, edges=[("skill.a", "skill.b", 0.95), ("skill.a", "skill.c", 0.30)],
                       one_way=[("skill.d", "skill.a", 0.70),
                                ("skill.e", "skill.f", 0.50), ("skill.f", "skill.e", 0.70)])
    found = _credits(["skill.b", "skill.c", "skill.d"], ["skill.a"], graph, config)
    _assert_credit(found["skill.a"], SIBLING_MAX, SIBLING, "skill.b")  # 0.95 clamps to 0.80
    _assert_credit(_credits(["skill.c"], ["skill.a"], graph, config)["skill.a"], SIBLING_MIN, SIBLING, "skill.c")
    # An edge stored one way only still relates both ways.
    _assert_credit(_credits(["skill.d"], ["skill.a"], graph, config)["skill.a"], 0.70, SIBLING, "skill.d")
    _assert_credit(_credits(["skill.a"], ["skill.d"], graph, config)["skill.d"], 0.70, SIBLING, "skill.a")
    # Stored both ways with different weights: the held id's own edge is read, as match_skill reads it.
    _assert_credit(_credits(["skill.e"], ["skill.f"], graph, config)["skill.f"], 0.50, SIBLING, "skill.e")
    _assert_credit(_credits(["skill.f"], ["skill.e"], graph, config)["skill.e"], 0.70, SIBLING, "skill.f")


def test_provisional_ids_are_discounted_once(small_graph, config) -> None:
    # svelte is provisional (SPEC §16): every relation it takes part in earns × 0.8, once.
    _assert_credit(_credits(["skill.svelte"], ["skill.svelte"], small_graph, config)["skill.svelte"],
                   EXACT_CREDIT * PROVISIONAL, EXACT, "skill.svelte")
    _assert_credit(_credits(["skill.svelte"], ["skill.js_framework"], small_graph, config)["skill.js_framework"],
                   CHILD_CREDIT * PROVISIONAL, CHILD, "skill.svelte")
    _assert_credit(_credits(["skill.js_framework"], ["skill.svelte"], small_graph, config)["skill.svelte"],
                   PARENT_CREDIT * PROVISIONAL, PARENT, "skill.js_framework")


def test_ids_outside_the_graph_relate_exactly_only(small_graph, config) -> None:
    found = _credits(["skill.unlisted", "skill.python"], ["skill.unlisted", "skill.other_unknown", "skill.django"],
                     small_graph, config)
    assert set(found) == {"skill.unlisted", "skill.django"}
    _assert_credit(found["skill.unlisted"], EXACT_CREDIT, EXACT, "skill.unlisted")
    _assert_credit(found["skill.django"], PARENT_CREDIT, PARENT, "skill.python")


def test_the_best_relation_wins_and_ties_keep_the_table_order(config) -> None:
    # Wants a. b is a child of a (CHILD 0.85) and shares a 0.85 edge with it (SIBLING, clamped to 0.80).
    graph = tiny_graph({}, edges=[("skill.b", "skill.a", 0.85), ("skill.s", "skill.a", 0.40)],
                       parents={"skill.b": ("skill.a",), "skill.a": ("skill.p",)})
    _assert_credit(_credits(["skill.b"], ["skill.a"], graph, config)["skill.a"], CHILD_CREDIT, CHILD, "skill.b")
    # SIBLING 0.40 (edge at the floor) ties PARENT 0.40: SIBLING comes first in the table.
    _assert_credit(_credits(["skill.p", "skill.s"], ["skill.a"], graph, config)["skill.a"], SIBLING_MIN, SIBLING,
                   "skill.s")
    # Two held ids earning the same credit: the smaller id is the one named.
    graph = tiny_graph({}, parents={"skill.x": ("skill.a",), "skill.y": ("skill.a",)})
    _assert_credit(_credits(["skill.y", "skill.x"], ["skill.a"], graph, config)["skill.a"], CHILD_CREDIT, CHILD,
                   "skill.x")


def test_best_credit_picks_the_strongest_wanted_id_then_the_smaller_id(small_graph, config) -> None:
    found = _credits(["skill.react", "skill.python"], ["skill.react", "skill.django", "skill.vue"], small_graph,
                     config)
    assert best_credit(found) == ("skill.react", found["skill.react"])
    found = _credits(["skill.python"], ["skill.flask", "skill.django"], small_graph, config)  # two PARENT 0.40
    assert best_credit(found) == ("skill.django", found["skill.django"])
    assert best_credit({}) is None


# ─── Held to match_skill on a unit-valued résumé ─────────────────────────────────────────


def test_credits_equal_match_skill_on_a_unit_valued_resume_on_the_fixture(small_graph, config) -> None:
    rng = random.Random(5)
    ids = sorted(small_graph.nodes)
    wanted = [*ids, "skill.unknown"]
    held_sets = [["skill.react"], ["skill.nextjs", "skill.vue", "skill.svelte", "skill.javascript"],
                 ["skill.docker", "skill.terraform", "skill.cycle_a"], *(rng.sample(ids, k) for k in (2, 5, 12, 30))]
    relations = set()
    for held in held_sets:
        expected = _oracle(held, wanted, small_graph, config)
        assert _credits(held, wanted, small_graph, config) == expected
        relations |= {got[1] for got in expected.values()}
    assert relations == {EXACT, CHILD, SIBLING, PARENT}  # every branch was exercised


@pytest.mark.parametrize("seed", [11, 12])
def test_credits_equal_match_skill_on_a_random_graph(config, seed) -> None:
    # One-way edges, edges stored both ways with different weights, provisional nodes, unseen ids.
    graph = random_graph(seed=seed, n_nodes=400)
    rng = random.Random(seed)
    ids = sorted(graph.nodes)
    for k in (1, 8, 40):
        held = rng.sample(ids, k)
        for wanted in (rng.sample(ids, 25), rng.sample(ids, 300)):  # both sides of the smaller-side walk
            assert _credits(held, wanted, graph, config) == _oracle(held, wanted, graph, config)


def test_results_do_not_depend_on_input_order(small_graph, config) -> None:
    held = ["skill.nextjs", "skill.vue", "skill.javascript", "skill.docker", "skill.svelte"]
    wanted = ["skill.react", "skill.kubernetes", "skill.js_framework", "skill.angular", "skill.typescript"]
    reference = _credits(held, wanted, small_graph, config)
    rng = random.Random(3)
    for _ in range(20):
        rng.shuffle(held)
        rng.shuffle(wanted)
        again = _credits(held + held[:2], wanted + wanted[:1], small_graph, config)  # duplicates count once
        assert again == reference and list(again) == sorted(again)
