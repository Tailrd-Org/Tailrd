"""§5.1 Required skills coverage and the matching machinery shared by §5.2 and §5.7 (Task 3.2).

SPEC §5.1, §13, §14.4, §14.5, §16; DECISIONS D-21, D-23, D-24, D-30, D-31, D-43. IDF values are
read from the graph_small fixture; every expected value is closed-form arithmetic from the spec
constants in tests/builders.py (EVAL §2.2).
"""

from __future__ import annotations

import math
import random
import time
from datetime import date, timedelta

import pytest

from matcher.config import load_config
from matcher.errors import EngineFault, Flag
from matcher.graph.traversal import ancestors, related_neighbors
from matcher.models import (
    DimensionName,
    Evidence,
    Necessity,
    Polarity,
    RelationKind,
    SkillSource,
    VerbClass,
    canonical_json,
)
from matcher.score.coverage import RequirementMatch, coverage_value, match_requirements, match_skill
from matcher.score.dimensions import score_preferred_skills, score_required_skills, score_tools
from matcher.score.prepare import PreparedResume, SkillValue, prepare_resume
from tests.builders import (
    AS_OF,
    ASPIRATIONAL_M,
    CHILD_CREDIT,
    E_BLOB,
    E_EXECUTION,
    E_OWNERSHIP,
    FOUR_YEARS_AGO,
    IDF_CRITICAL,
    M_CRITICAL,
    PARENT_CREDIT,
    PROVISIONAL,
    QUANTIFIED_BONUS,
    R_FLOOR,
    SIBLING_MAX,
    SIBLING_MIN,
    UNSEEN_IDF,
    W_REQUIRED,
    bullet,
    current_role,
    ended_role,
    listed,
    posting,
    random_graph,
    random_resume,
    recency,
    req,
    resume,
    tiny_graph,
    with_scoring,
)

EXACT, CHILD, SIBLING, PARENT, NONE = (RelationKind.EXACT, RelationKind.CHILD, RelationKind.SIBLING,
                                       RelationKind.PARENT, RelationKind.NONE)
REQUIRED, PREFERRED, IMPLIED = Necessity.REQUIRED, Necessity.PREFERRED, Necessity.IMPLIED


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12)


def _match(got: tuple, m: float, relation: RelationKind, via) -> None:
    assert _close(got[0], m), f"m = {got[0]!r}, expected {m!r}"
    assert got[1:] == (relation, via)


def _prepare(cv, graph, config):
    return prepare_resume(cv, graph, config, AS_OF)


def _score(cv, post, graph, config):
    return score_required_skills(_prepare(cv, graph, config), post, graph, config)


# ─── The headline closed form (plan Task 3.2) ───────────────────────────────────────────


def test_closed_form_required_coverage(small_graph, config) -> None:
    # Current role bullet "Built React apps" (OWNERSHIP), skills section Python and Docker.
    cv = resume(roles=(current_role(bullet("skill.react", VerbClass.OWNERSHIP)),),
                blob=listed("skill.python", "skill.docker"))
    post = posting(req("skill.react"), req("skill.python"), req("skill.kubernetes"))
    i_r, i_p, i_k = (small_graph.idf[s] for s in ("skill.react", "skill.python", "skill.kubernetes"))
    v_react = E_OWNERSHIP * 1.0  # used as of today: R = 1
    v_python = E_BLOB * 1.0  # the skills section takes the current role's activity: R = 1
    m_kubernetes = 0.70 * E_BLOB  # SIBLING via docker: edge 0.70 is inside [0.40, 0.80] → 0.455
    expected = (i_r * v_react + i_p * v_python + i_k * m_kubernetes) / (i_r + i_p + i_k)

    dim = _score(cv, post, small_graph, config)
    assert dim.name is DimensionName.REQUIRED_SKILLS
    assert _close(dim.value, expected)
    assert _close(dim.weight, W_REQUIRED) and _close(dim.effective_weight, W_REQUIRED)
    assert _close(dim.contribution, W_REQUIRED * expected)
    assert dim.abstained is False and dim.abstain_reason is None

    # One fact per group, sorted by group key; kubernetes is rare (idf > 0.7) but covered above 0.2.
    assert [(f.code, f.subject) for f in dim.facts] == [
        ("REQ_PARTIAL", "skill.kubernetes"), ("REQ_MATCH", "skill.python"), ("REQ_MATCH", "skill.react")]
    kubernetes = dim.facts[0]
    assert _close(kubernetes.value, m_kubernetes)
    assert kubernetes.detail == {
        "relation": "SIBLING", "via": "skill.docker", "idf": i_k, "weight_multiplier": 1.0,
        "necessity": "REQUIRED", "members": "skill.kubernetes",
    }
    assert dim.facts[2].detail["relation"] == "EXACT" and dim.facts[2].detail["via"] == "skill.react"
    assert _close(dim.facts[2].value, v_react)


# ─── Relations (SPEC §5.1 table, DECISIONS D-23) ─────────────────────────────────────────


def test_exact_match_is_the_mention_value(small_graph, config) -> None:
    prepared = _prepare(resume(blob=listed("skill.python")), small_graph, config)
    _match(match_skill(prepared, "skill.python", small_graph, config), 1.00 * E_BLOB, EXACT, "skill.python")


def test_child_has_react_wants_a_framework_above_it(small_graph, config) -> None:
    # nextjs is a child of react AND shares a 0.80 edge with it: CHILD (0.85) is the better credit.
    prepared = _prepare(resume(blob=listed("skill.nextjs")), small_graph, config)
    _match(match_skill(prepared, "skill.react", small_graph, config), CHILD_CREDIT * E_BLOB, CHILD, "skill.nextjs")
    _match(match_skill(prepared, "skill.javascript", small_graph, config),  # three levels up
           CHILD_CREDIT * E_BLOB, CHILD, "skill.nextjs")


def test_parent_has_javascript_wants_react(small_graph, config) -> None:
    prepared = _prepare(resume(blob=listed("skill.javascript")), small_graph, config)
    _match(match_skill(prepared, "skill.react", small_graph, config), PARENT_CREDIT * E_BLOB, PARENT,
           "skill.javascript")
    _match(match_skill(prepared, "skill.nextjs", small_graph, config),  # three levels down
           PARENT_CREDIT * E_BLOB, PARENT, "skill.javascript")


def test_relations_stop_at_max_depth(small_graph, config) -> None:
    shallow = with_scoring(config, "relations", max_depth=2)
    has_js = _prepare(resume(blob=listed("skill.javascript")), small_graph, shallow)
    assert match_skill(has_js, "skill.nextjs", small_graph, shallow) == (0.0, NONE, None)
    has_next = _prepare(resume(blob=listed("skill.nextjs")), small_graph, shallow)
    assert match_skill(has_next, "skill.javascript", small_graph, shallow) == (0.0, NONE, None)


def test_sibling_is_the_edge_weight(small_graph, config) -> None:
    # react–vue: the hand-curated 0.65 edge, inside [0.40, 0.80]. A shared parent alone earns nothing.
    prepared = _prepare(resume(blob=listed("skill.vue")), small_graph, config)
    _match(match_skill(prepared, "skill.react", small_graph, config), 0.65 * E_BLOB, SIBLING, "skill.vue")
    assert match_skill(prepared, "skill.angular", small_graph, config) == (0.0, NONE, None)


def test_sibling_weight_is_clamped_to_the_spec_range(config) -> None:
    graph = tiny_graph({"skill.a": 0.5}, edges=[("skill.a", "skill.b", 0.95), ("skill.a", "skill.c", 0.30)])
    strong = _prepare(resume(blob=listed("skill.b")), graph, config)
    _match(match_skill(strong, "skill.a", graph, config), SIBLING_MAX * E_BLOB, SIBLING, "skill.b")  # 0.95 → 0.80
    weak = _prepare(resume(blob=listed("skill.c")), graph, config)
    _match(match_skill(weak, "skill.a", graph, config), SIBLING_MIN * E_BLOB, SIBLING, "skill.c")  # 0.30 → 0.40


def test_an_edge_stored_one_way_counts_both_ways(config) -> None:
    graph = tiny_graph({"skill.a": 0.5, "skill.b": 0.5}, one_way=[("skill.b", "skill.a", 0.70)])
    has_a = _prepare(resume(blob=listed("skill.a")), graph, config)
    _match(match_skill(has_a, "skill.b", graph, config), 0.70 * E_BLOB, SIBLING, "skill.a")
    has_b = _prepare(resume(blob=listed("skill.b")), graph, config)
    _match(match_skill(has_b, "skill.a", graph, config), 0.70 * E_BLOB, SIBLING, "skill.b")


def test_no_path_and_unknown_requirements(small_graph, config) -> None:
    prepared = _prepare(resume(blob=listed("skill.python", "skill.unlisted_tool")), small_graph, config)
    assert match_skill(prepared, "skill.excel", small_graph, config) == (0.0, NONE, None)
    # A requirement absent from the graph matches only the same id exactly.
    _match(match_skill(prepared, "skill.unlisted_tool", small_graph, config), E_BLOB, EXACT, "skill.unlisted_tool")
    assert match_skill(prepared, "skill.another_unknown", small_graph, config) == (0.0, NONE, None)


def test_the_best_credit_wins_whatever_the_relation(small_graph, config) -> None:
    # Wants react. javascript gives PARENT 0.40 × v, vue gives SIBLING 0.65 × v: the larger wins.
    both_listed = resume(blob=listed("skill.javascript", "skill.vue"))
    _match(match_skill(_prepare(both_listed, small_graph, config), "skill.react", small_graph, config),
           0.65 * E_BLOB, SIBLING, "skill.vue")  # 0.4225 > 0.26
    shown_js = resume(roles=(current_role(bullet("skill.javascript")),),
                      blob=listed("skill.vue", polarity=Polarity.ASPIRATIONAL))
    _match(match_skill(_prepare(shown_js, small_graph, config), "skill.react", small_graph, config),
           PARENT_CREDIT * E_OWNERSHIP, PARENT, "skill.javascript")  # 0.40 > 0.65 × 0.15


# ─── Ties keep the order EXACT, CHILD, SIBLING, PARENT ──────────────────────────────────


def test_exact_wins_a_tie_with_child(small_graph, config) -> None:
    # EXACT 1.00 × 0.85 (EXECUTION) == CHILD 0.85 × 1.00 (OWNERSHIP nextjs).
    cv = resume(roles=(current_role(bullet("skill.react", VerbClass.EXECUTION), bullet("skill.nextjs")),))
    assert 1.00 * E_EXECUTION == CHILD_CREDIT * E_OWNERSHIP  # exactly equal floats: a real tie
    _match(match_skill(_prepare(cv, small_graph, config), "skill.react", small_graph, config),
           E_EXECUTION, EXACT, "skill.react")


def test_child_wins_a_tie_with_sibling(small_graph, config) -> None:
    # CHILD 0.85 × 0.65 (listed nextjs) == SIBLING 0.65 (react–vue edge) × 0.85 (EXECUTION vue).
    cv = resume(roles=(current_role(bullet("skill.vue", VerbClass.EXECUTION)),), blob=listed("skill.nextjs"))
    assert CHILD_CREDIT * E_BLOB == 0.65 * E_EXECUTION
    _match(match_skill(_prepare(cv, small_graph, config), "skill.react", small_graph, config),
           CHILD_CREDIT * E_BLOB, CHILD, "skill.nextjs")


def test_sibling_wins_a_tie_with_parent(config) -> None:
    graph = tiny_graph({"skill.want": 0.5}, edges=[("skill.want", "skill.side", 0.40)],
                       parents={"skill.want": ("skill.top",)})
    cv = resume(blob=listed("skill.top", "skill.side"))  # PARENT 0.40 × 0.65 == SIBLING 0.40 × 0.65
    _match(match_skill(_prepare(cv, graph, config), "skill.want", graph, config),
           SIBLING_MIN * E_BLOB, SIBLING, "skill.side")


def test_a_tie_within_one_relation_goes_to_the_smaller_id(config) -> None:
    graph = tiny_graph({"skill.want": 0.5}, edges=[("skill.want", "skill.s2", 0.5), ("skill.want", "skill.s1", 0.5)])
    for blob in (listed("skill.s1", "skill.s2"), listed("skill.s2", "skill.s1")):
        _match(match_skill(_prepare(resume(blob=blob), graph, config), "skill.want", graph, config),
               0.5 * E_BLOB, SIBLING, "skill.s1")


# ─── Provisional nodes × 0.8 (SPEC §16, DECISIONS D-24) ─────────────────────────────────


def test_provisional_nodes_are_discounted_in_every_relation(small_graph, config) -> None:
    assert small_graph.nodes["skill.svelte"].provisional
    has_svelte = _prepare(resume(blob=listed("skill.svelte")), small_graph, config)
    _match(match_skill(has_svelte, "skill.svelte", small_graph, config), E_BLOB * PROVISIONAL, EXACT, "skill.svelte")
    _match(match_skill(has_svelte, "skill.js_framework", small_graph, config),
           CHILD_CREDIT * E_BLOB * PROVISIONAL, CHILD, "skill.svelte")
    has_framework = _prepare(resume(blob=listed("skill.js_framework")), small_graph, config)
    _match(match_skill(has_framework, "skill.svelte", small_graph, config),
           PARENT_CREDIT * E_BLOB * PROVISIONAL, PARENT, "skill.js_framework")
    # Confirmed nodes on both sides: no discount.
    has_vue = _prepare(resume(blob=listed("skill.vue")), small_graph, config)
    _match(match_skill(has_vue, "skill.js_framework", small_graph, config), CHILD_CREDIT * E_BLOB, CHILD, "skill.vue")


def test_a_provisional_sibling_is_discounted_on_either_side(config) -> None:
    graph = tiny_graph({"skill.a": 0.5, "skill.p": 0.5}, edges=[("skill.a", "skill.p", 0.60)],
                       provisional=("skill.p",))
    has_p = _prepare(resume(blob=listed("skill.p")), graph, config)
    _match(match_skill(has_p, "skill.a", graph, config), 0.60 * E_BLOB * PROVISIONAL, SIBLING, "skill.p")
    has_a = _prepare(resume(blob=listed("skill.a")), graph, config)
    _match(match_skill(has_a, "skill.p", graph, config), 0.60 * E_BLOB * PROVISIONAL, SIBLING, "skill.a")


def test_provisional_discount_applies_once(small_graph, config) -> None:
    # The svelte requirement AND the svelte mention are the same provisional node: × 0.8, not × 0.64.
    prepared = _prepare(resume(roles=(current_role(bullet("skill.svelte")),)), small_graph, config)
    _match(match_skill(prepared, "skill.svelte", small_graph, config), E_OWNERSHIP * PROVISIONAL, EXACT,
           "skill.svelte")


# ─── Evidence, recency and polarity reach the match (SPEC §5.1, §15) ────────────────────


def test_recency_floor_for_a_role_that_ended_four_years_ago(small_graph, config) -> None:
    cv = resume(roles=(ended_role(bullet("skill.react"), end=FOUR_YEARS_AGO),), blob=listed("skill.python"))
    post = posting(req("skill.react"), req("skill.python"))
    i_r, i_p = small_graph.idf["skill.react"], small_graph.idf["skill.python"]
    expected = (i_r * E_OWNERSHIP * R_FLOOR + i_p * E_BLOB * R_FLOOR) / (i_r + i_p)
    assert _close(_score(cv, post, small_graph, config).value, expected)


def test_quantified_bonus(small_graph, config) -> None:
    cv = resume(roles=(current_role(bullet("skill.python", VerbClass.EXECUTION, quantified=True)),))
    dim = _score(cv, posting(req("skill.python")), small_graph, config)
    assert _close(dim.value, E_EXECUTION * QUANTIFIED_BONUS)  # 0.9775


def test_negated_mention_scores_zero(small_graph, config) -> None:
    cv = resume(roles=(current_role(bullet("skill.java", polarity=Polarity.NEGATED)),))
    dim = _score(cv, posting(req("skill.java")), small_graph, config)
    assert _close(dim.value, 0.0)
    assert (dim.facts[0].code, dim.facts[0].detail["relation"], dim.facts[0].detail["via"]) == ("REQ_MISS", "NONE",
                                                                                               None)


def test_aspirational_mention_scores_at_most_015(small_graph, config) -> None:
    cv = resume(blob=listed("skill.machine_learning", polarity=Polarity.ASPIRATIONAL))
    dim = _score(cv, posting(req("skill.machine_learning")), small_graph, config)
    assert _close(dim.value, ASPIRATIONAL_M)
    assert dim.facts[0].code == "REQ_PARTIAL"


def test_the_same_skill_fifty_times_scores_as_once(small_graph, config) -> None:
    post = posting(req("skill.python"), req("skill.kubernetes"), req("skill.react"))
    once = _score(resume(blob=listed("skill.python", "skill.docker")), post, small_graph, config)
    stuffed = _score(resume(blob=listed(*["skill.python"] * 50, *["skill.docker"] * 50)), post, small_graph, config)
    assert canonical_json(stuffed) == canonical_json(once)


# ─── OR-groups (SPEC §14.4, DECISIONS D-31) ─────────────────────────────────────────────


def test_or_group_scores_its_best_member_at_its_rarest_idf(small_graph, config) -> None:
    cv = resume(roles=(current_role(bullet("skill.react")),), blob=listed("skill.python"))
    post = posting(req("skill.python", or_group="lang"), req("skill.go", or_group="lang"), req("skill.react"))
    i_go, i_py, i_r = (small_graph.idf[s] for s in ("skill.go", "skill.python", "skill.react"))
    assert i_go > i_py  # the group's importance is its rarest member's, whoever covers it
    prepared = _prepare(cv, small_graph, config)
    lang, react = match_requirements(prepared, post.requirements, small_graph, config)
    assert lang == RequirementMatch(
        group="lang", members=("skill.go", "skill.python"), best_member="skill.python", m=E_BLOB,
        relation=EXACT, via="skill.python", idf=i_go, weight_multiplier=1.0, necessity=REQUIRED, critical=False)
    assert react.group == "skill.react" and react.members == ("skill.react",)
    expected = (i_go * E_BLOB + i_r * E_OWNERSHIP) / (i_go + i_r)
    dim = score_required_skills(prepared, post, small_graph, config)
    assert _close(dim.value, expected)
    assert dim.facts[0].subject == "skill.python" and dim.facts[0].detail["members"] == "skill.go,skill.python"


def test_or_group_takes_the_max_not_the_mean(small_graph, config) -> None:
    cv = resume(blob=listed("skill.python"))
    alone = _score(cv, posting(req("skill.python")), small_graph, config)
    grouped = _score(cv, posting(req("skill.python", or_group="g"), req("skill.r", or_group="g")), small_graph, config)
    assert _close(alone.value, E_BLOB) and _close(grouped.value, E_BLOB)


def test_or_group_weight_multiplier_is_the_max(small_graph, config) -> None:
    post = posting(req("skill.python", or_group="g", weight_multiplier=0.5),
                   req("skill.go", or_group="g", weight_multiplier=1.0))
    (group,) = match_requirements(_prepare(resume(), small_graph, config), post.requirements, small_graph, config)
    assert _close(group.weight_multiplier, 1.0)


def test_a_missed_group_names_its_member_deterministically(small_graph, config) -> None:
    for order in ((req("skill.python", or_group="g"), req("skill.go", or_group="g")),
                  (req("skill.go", or_group="g"), req("skill.python", or_group="g"))):
        (group,) = match_requirements(_prepare(resume(), small_graph, config), order, small_graph, config)
        assert (group.best_member, group.m, group.relation, group.via) == ("skill.go", 0.0, NONE, None)
        assert group.members == ("skill.go", "skill.python")


def test_duplicate_requirements_count_once(small_graph, config) -> None:
    cv = resume(roles=(current_role(bullet("skill.react")),))
    once = _score(cv, posting(req("skill.react"), req("skill.python")), small_graph, config)
    twice = _score(cv, posting(req("skill.react"), req("skill.python"), req("skill.python")), small_graph, config)
    assert canonical_json(twice) == canonical_json(once)


def test_groups_are_sorted_by_key(small_graph, config) -> None:
    post = posting(req("skill.sql"), req("skill.go", or_group="b_group"), req("skill.aws"), req("skill.r", or_group="a"))
    matches = match_requirements(_prepare(resume(), small_graph, config), post.requirements, small_graph, config)
    assert [m.group for m in matches] == ["a", "b_group", "skill.aws", "skill.sql"]


def test_unknown_requirement_takes_the_unseen_idf(small_graph, config) -> None:
    cv = resume(blob=listed("skill.brand_new"))
    post = posting(req("skill.brand_new"), req("skill.react"))
    (new, react) = match_requirements(_prepare(cv, small_graph, config), post.requirements, small_graph, config)
    assert _close(new.idf, UNSEEN_IDF) and _close(new.m, E_BLOB) and new.relation is EXACT
    # A graph node with no corpus count is "unseen" too (DECISIONS D-21).
    assert "skill.js_framework" not in small_graph.idf
    (framework,) = match_requirements(_prepare(cv, small_graph, config), (req("skill.js_framework"),), small_graph,
                                      config)
    assert _close(framework.idf, UNSEEN_IDF)
    i_r = small_graph.idf["skill.react"]
    expected = (UNSEEN_IDF * E_BLOB + i_r * 0.0) / (UNSEEN_IDF + i_r)
    assert _close(_score(cv, post, small_graph, config).value, expected)


# ─── Critical miss (SPEC §5.1, DECISIONS D-43) ──────────────────────────────────────────


def test_critical_miss_needs_a_rare_required_skill_scored_below_02(small_graph, config) -> None:
    idf = small_graph.idf
    assert idf["skill.kubernetes"] > IDF_CRITICAL and idf["skill.angular"] > IDF_CRITICAL
    assert idf["skill.typescript"] > IDF_CRITICAL  # 0.7006: just above
    assert idf["skill.aws"] <= IDF_CRITICAL and idf["skill.machine_learning"] > IDF_CRITICAL
    cv = resume(blob=(*listed("skill.js_framework"),
                      *listed("skill.machine_learning", polarity=Polarity.ASPIRATIONAL)))
    post = posting(*(req(s) for s in ("skill.kubernetes", "skill.typescript", "skill.aws", "skill.angular",
                                      "skill.machine_learning")))
    matches = {m.group: m for m in match_requirements(_prepare(cv, small_graph, config), post.requirements,
                                                       small_graph, config)}
    assert matches["skill.kubernetes"].critical and _close(matches["skill.kubernetes"].m, 0.0)
    assert matches["skill.typescript"].critical
    assert not matches["skill.aws"].critical  # missed, but common
    angular = matches["skill.angular"]  # PARENT via js_framework: 0.40 × 0.65 = 0.26 ≥ 0.20
    assert _close(angular.m, PARENT_CREDIT * E_BLOB) and angular.m >= M_CRITICAL and not angular.critical
    ml = matches["skill.machine_learning"]  # aspirational: 0.15 < 0.20
    assert _close(ml.m, ASPIRATIONAL_M) and ml.critical

    dim = _score(cv, post, small_graph, config)
    # Group facts first (angular, aws, kubernetes, machine_learning, typescript), then the misses.
    assert [f.code for f in dim.facts] == ["REQ_PARTIAL", "REQ_MISS", "REQ_MISS", "REQ_PARTIAL", "REQ_MISS",
                                           "CRITICAL_MISS", "CRITICAL_MISS", "CRITICAL_MISS"]
    critical = dim.facts[5:]
    assert [(f.subject, f.detail) for f in critical] == [
        ("skill.kubernetes", {"idf": idf["skill.kubernetes"]}),
        ("skill.machine_learning", {"idf": idf["skill.machine_learning"]}),
        ("skill.typescript", {"idf": idf["skill.typescript"]}),
    ]
    assert _close(critical[1].value, ASPIRATIONAL_M) and _close(critical[0].value, 0.0)
    assert Flag.CRITICAL_MISS.value == "CRITICAL_MISS"


def test_recency_can_push_a_partial_cover_below_the_critical_threshold(small_graph, config) -> None:
    # PARENT via js_framework, last active four years ago: 0.40 × 0.65 × 0.55 = 0.143 < 0.20.
    cv = resume(roles=(ended_role(end=FOUR_YEARS_AGO),), blob=listed("skill.js_framework"))
    (angular,) = match_requirements(_prepare(cv, small_graph, config), (req("skill.angular"),), small_graph, config)
    assert _close(angular.m, PARENT_CREDIT * E_BLOB * R_FLOOR) and angular.critical


def test_thresholds_are_strict_inequalities(config) -> None:
    # critical: idf > 0.70 and m < 0.20; REQ_MATCH from m >= 0.50. Exact boundary values:
    graph = tiny_graph({"skill.at_idf": 0.70, "skill.rare": 0.90, "skill.half": 0.50},
                       edges=[("skill.half", "skill.side", 0.50)], parents={"skill.rare": ("skill.top",)})
    cfg = with_scoring(config, "relations", parent=0.20)  # PARENT credit 0.20 × 1.0 = m of exactly 0.20
    cv = resume(roles=(current_role(bullet("skill.top"), bullet("skill.side")),))
    post = posting(req("skill.at_idf"), req("skill.rare"), req("skill.half"))
    prepared = prepare_resume(cv, graph, cfg, AS_OF)
    found = {m.group: m for m in match_requirements(prepared, post.requirements, graph, cfg)}
    assert found["skill.at_idf"].m == 0.0 and not found["skill.at_idf"].critical  # idf 0.70 is not > 0.70
    assert _close(found["skill.rare"].m, 0.20) and not found["skill.rare"].critical  # 0.20 is not < 0.20
    assert _close(found["skill.half"].m, 0.50)
    codes = {f.subject: f.code for f in score_required_skills(prepared, post, graph, cfg).facts}
    assert codes == {"skill.at_idf": "REQ_MISS", "skill.rare": "REQ_PARTIAL", "skill.half": "REQ_MATCH"}


def test_a_covered_or_group_is_not_critical(small_graph, config) -> None:
    post = posting(req("skill.python", or_group="g"), req("skill.go", or_group="g"))
    covered = match_requirements(_prepare(resume(blob=listed("skill.python")), small_graph, config),
                                 post.requirements, small_graph, config)
    missed = match_requirements(_prepare(resume(), small_graph, config), post.requirements, small_graph, config)
    assert not covered[0].critical and missed[0].critical  # group idf = go's 0.976


# ─── Partition and abstention (SPEC §14.5, DECISIONS D-30) ──────────────────────────────


def test_required_scores_only_required_requirements_whatever_the_node_kind(small_graph, config) -> None:
    cv = resume(blob=listed("skill.kubernetes"))  # kubernetes is a TOOL node: kind never routes (D-30)
    post = posting(req("skill.kubernetes"), req("skill.python", PREFERRED), req("skill.git", IMPLIED))
    dim = _score(cv, post, small_graph, config)
    assert _close(dim.value, E_BLOB) and [f.subject for f in dim.facts] == ["skill.kubernetes"]


@pytest.mark.parametrize("requirements", [(), (req("skill.python", PREFERRED), req("skill.git", IMPLIED))])
def test_abstains_without_required_requirements(small_graph, config, requirements) -> None:
    dim = _score(resume(blob=listed("skill.python")), posting(*requirements), small_graph, config)
    assert dim.abstained and dim.value is None and dim.abstain_reason is Flag.NO_REQUIRED_REQUIREMENTS
    assert _close(dim.weight, W_REQUIRED) and dim.effective_weight == 0.0 and dim.contribution == 0.0
    assert dim.facts == ()


def test_abstains_when_no_requirement_carries_weight(small_graph, config) -> None:
    cv = resume(blob=listed("skill.python"))
    dim = _score(cv, posting(req("skill.python", weight_multiplier=0.0)), small_graph, config)
    assert dim.abstained and dim.abstain_reason is Flag.NO_REQUIREMENTS_RESOLVED
    assert dim.effective_weight == 0.0 and dim.contribution == 0.0 and dim.facts == ()
    graph = tiny_graph({"skill.everywhere": 0.0})  # the most common skill of the corpus: idf 0
    dim = _score(resume(blob=listed("skill.everywhere")), posting(req("skill.everywhere")), graph, config)
    assert dim.abstained and dim.abstain_reason is Flag.NO_REQUIREMENTS_RESOLVED


def test_zero_overlap_is_a_valid_zero_not_an_abstention(small_graph, config) -> None:
    dim = _score(resume(blob=listed("skill.excel")), posting(req("skill.kubernetes"), req("skill.go")), small_graph,
                 config)
    assert not dim.abstained and _close(dim.value, 0.0) and _close(dim.contribution, 0.0)
    assert [f.code for f in dim.facts] == ["REQ_MISS", "REQ_MISS", "CRITICAL_MISS", "CRITICAL_MISS"]


# ─── coverage_value and the numeric rules (SPEC §13) ────────────────────────────────────


def _rm(m: float, idf: float, wm: float = 1.0, group: str = "skill.x") -> RequirementMatch:
    return RequirementMatch(group=group, members=(group,), best_member=group, m=m, relation=EXACT if m else NONE,
                            via=group if m else None, idf=idf, weight_multiplier=wm, necessity=REQUIRED,
                            critical=False)


def test_coverage_value_is_the_idf_weighted_mean() -> None:
    matches = [_rm(1.0, 0.8, group="skill.a"), _rm(0.5, 0.4, wm=0.5, group="skill.b"), _rm(0.0, 0.6, group="skill.c")]
    expected = (0.8 * 1.0 * 1.0 + 0.4 * 0.5 * 0.5 + 0.6 * 1.0 * 0.0) / (0.8 * 1.0 + 0.4 * 0.5 + 0.6 * 1.0)
    assert _close(coverage_value(matches), expected)  # 0.9 / 1.6 = 0.5625
    assert _close(expected, 0.5625)


def test_coverage_value_is_none_without_weight() -> None:
    assert coverage_value([]) is None
    assert coverage_value([_rm(1.0, 0.0), _rm(1.0, 0.9, wm=0.0)]) is None
    assert _close(coverage_value([_rm(1.0, 1e-6)]), 1.0)  # small but real weight still scores


@pytest.mark.parametrize("field", ["m", "idf", "weight_multiplier"])
def test_a_non_finite_match_raises_a_numeric_fault(field: str) -> None:
    good = _rm(0.5, 0.5).model_dump()
    good[field] = math.nan
    bad = RequirementMatch.model_construct(**good)
    with pytest.raises(EngineFault) as err:
        coverage_value([bad])
    assert err.value.flag is Flag.NUMERIC_FAULT


def _nan_value(skill_id: str) -> SkillValue:
    return SkillValue.model_construct(skill_id=skill_id, value=math.nan, evidence_mult=1.0, recency_mult=1.0,
                                      evidence=Evidence.SKILLS_BLOB, polarity=Polarity.AFFIRMED,
                                      source=SkillSource.OTHER)


@pytest.mark.parametrize("requirement,best,child_cover", [
    # Each case reaches exactly one relation branch with the NaN:
    ("skill.not_in_graph", {"skill.not_in_graph": "nan"}, {}),  # EXACT only: unknown requirement
    ("skill.js_framework", {}, {"skill.js_framework": (math.nan, "skill.vue")}),  # CHILD only
    ("skill.kubernetes", {"skill.docker": "nan"}, {}),  # SIBLING only: docker–kubernetes edge, no hierarchy
    ("skill.angular", {"skill.js_framework": "nan"}, {}),  # PARENT only: js_framework has no edges
], ids=["exact", "child", "sibling", "parent"])
def test_a_non_finite_value_raises_a_numeric_fault_in_every_branch(small_graph, config, requirement, best,
                                                                   child_cover) -> None:
    # A NaN smuggled past validation (model_construct) is never silently coerced (SPEC §13).
    assert not related_neighbors(small_graph, "skill.angular")  # PARENT case: no SIBLING path
    assert not small_graph.nodes["skill.kubernetes"].parents  # SIBLING case: no PARENT path
    assert not related_neighbors(small_graph, "skill.js_framework")  # CHILD case: no SIBLING path
    prepared = PreparedResume.model_construct(
        resume=resume(), as_of=AS_OF, latest_activity=None, graph_version=small_graph.version,
        config_hash=config.content_hash, best={k: _nan_value(k) for k in best}, child_cover=child_cover)
    with pytest.raises(EngineFault) as err:
        match_skill(prepared, requirement, small_graph, config)
    assert err.value.flag is Flag.NUMERIC_FAULT


# ─── Determinism (SPEC §0) ──────────────────────────────────────────────────────────────


def test_scores_do_not_depend_on_input_order(small_graph, config) -> None:
    skills = [bullet("skill.react"), bullet("skill.python", VerbClass.PARTICIPATION), *listed("skill.python"),
              *listed("skill.docker", "skill.vue", "skill.svelte", "skill.javascript")]
    requirements = [req("skill.react"), req("skill.kubernetes"), req("skill.go", or_group="g"),
                    req("skill.python", or_group="g"), req("skill.js_framework"), req("skill.nextjs"),
                    req("skill.angular"), req("skill.excel")]
    reference = _score(resume(roles=(current_role(*skills),)), posting(*requirements), small_graph, config)
    rng = random.Random(11)
    for _ in range(25):
        rng.shuffle(skills)
        rng.shuffle(requirements)
        again = _score(resume(roles=(current_role(*skills),)), posting(*requirements), small_graph, config)
        assert canonical_json(again) == canonical_json(reference)


def test_mathematically_equal_credits_tie_whatever_the_float_rounding(small_graph, config) -> None:
    # Wants react; has nextjs listed and vue in an EXECUTION bullet, both last used N days ago.
    # CHILD 0.85 × (0.65 × R) and SIBLING 0.65 × (0.85 × R) are equal, but the two products differ
    # in the last bit for many R: the tie must still go to CHILD, every time.
    wrong = []
    for days in range(0, 1461, 7):
        role = ended_role(bullet("skill.vue", VerbClass.EXECUTION), end=AS_OF - timedelta(days=days),
                          start=date(2010, 1, 1))
        cv = resume(roles=(role,), blob=listed("skill.nextjs"))
        m, relation, via = match_skill(_prepare(cv, small_graph, config), "skill.react", small_graph, config)
        if (relation, via) != (CHILD, "skill.nextjs") or not _close(m, CHILD_CREDIT * E_BLOB * recency(days)):
            wrong.append(days)
    assert wrong == []


def test_a_one_ulp_difference_never_decides_the_best_member(config) -> None:
    graph = tiny_graph({"skill.a": 0.5, "skill.b": 0.5})
    credit = CHILD_CREDIT * E_BLOB * recency(84)
    prepared = PreparedResume(
        resume=resume(), as_of=AS_OF, latest_activity=None, graph_version=graph.version,
        config_hash=config.content_hash, best={},
        child_cover={"skill.a": (credit, "skill.x"), "skill.b": (math.nextafter(credit, 1.0), "skill.y")})
    post = posting(req("skill.b", or_group="g"), req("skill.a", or_group="g"))
    (group,) = match_requirements(prepared, post.requirements, graph, config)
    assert (group.best_member, group.m, group.via) == ("skill.a", credit, "skill.x")  # the smaller id, unrounded


def test_an_or_group_label_never_merges_with_a_skill_id(small_graph, config) -> None:
    # An OR-group whose label happens to equal a skill id stays a group of its own.
    cv = resume(blob=listed("skill.go"))
    post = posting(req("skill.python", or_group="skill.python"), req("skill.go", or_group="skill.python"),
                   req("skill.python"))
    matches = match_requirements(_prepare(cv, small_graph, config), post.requirements, small_graph, config)
    assert [(m.group, m.members, m.best_member) for m in matches] == [
        ("skill.python", ("skill.go", "skill.python"), "skill.go"),  # the OR-group sorts first on a shared label
        ("skill.python", ("skill.python",), "skill.python"),
    ]
    i_go, i_py = small_graph.idf["skill.go"], small_graph.idf["skill.python"]
    expected = (max(i_go, i_py) * E_BLOB + i_py * 0.0) / (max(i_go, i_py) + i_py)
    assert _close(_score(cv, post, small_graph, config).value, expected)


# ─── A prepared résumé belongs to one graph and one config ──────────────────────────────


def test_a_prepared_resume_is_bound_to_its_graph_and_config(small_graph, config) -> None:
    prepared = _prepare(resume(blob=listed("skill.python")), small_graph, config)
    post = posting(req("skill.python"), req("skill.go", PREFERRED), req("skill.git", IMPLIED))
    calls = [
        lambda g, c: match_skill(prepared, "skill.python", g, c),
        lambda g, c: match_requirements(prepared, post.requirements, g, c),
        lambda g, c: score_required_skills(prepared, post, g, c),
        lambda g, c: score_preferred_skills(prepared, post, g, c),
        lambda g, c: score_tools(prepared, post, g, c),
        lambda g, c: score_required_skills(prepared, posting(), g, c),  # even where it would abstain
    ]
    other_graph = tiny_graph({"skill.python": 0.5})
    other_config = with_scoring(config, "relations", max_depth=2)
    for call in calls:
        call(small_graph, config)
        call(small_graph, load_config())  # an equal config loaded again is the same config
        for graph, cfg in ((other_graph, config), (small_graph, other_config)):
            with pytest.raises(EngineFault) as err:
                call(graph, cfg)
            assert err.value.flag is Flag.INTERNAL_ERROR


# ─── The SIBLING lookup: cached neighbours instead of a scan of the résumé ─────────────


def _full_scan(prepared, requirement_id, graph, config):
    """match_skill as it was before the neighbour index: SIBLING checks every résumé skill."""
    rel = config.scoring.relations
    best, found = prepared.best, []

    def provisional(skill_id):
        node = graph.node(skill_id)
        return node is not None and node.provisional

    def offer(credit, kind, via):
        if provisional(requirement_id) or provisional(via):
            credit *= rel.provisional_multiplier
        found.append((min(1.0, max(0.0, credit)), kind, via))

    if requirement_id in best:
        offer(rel.exact * best[requirement_id].value, EXACT, requirement_id)
    if requirement_id in graph:
        if requirement_id in prepared.child_cover:
            credit, via = prepared.child_cover[requirement_id]
            found.append((credit, CHILD, via))
        for skill_id, value in best.items():
            weight = graph.related_weight(skill_id, requirement_id)
            if skill_id != requirement_id and weight is not None:
                offer(min(rel.sibling_max, max(rel.sibling_min, weight)) * value.value, SIBLING, skill_id)
        for ancestor in ancestors(graph, requirement_id, rel.max_depth):
            if ancestor in best:
                offer(rel.parent * best[ancestor].value, PARENT, ancestor)
    order = {EXACT: 0, CHILD: 1, SIBLING: 2, PARENT: 3}
    if not found:
        return (0.0, NONE, None)
    credit, kind, via = min(found, key=lambda c: (-round(c[0], 12), order[c[1]], c[2]))
    return (0.0, NONE, None) if credit < 1e-12 else (credit, kind, via)


def _assert_same_as_full_scan(prepared, graph, config, requirement_ids) -> int:
    siblings = 0
    for requirement_id in requirement_ids:
        got = match_skill(prepared, requirement_id, graph, config)
        assert got == _full_scan(prepared, requirement_id, graph, config), requirement_id
        siblings += got[1] is SIBLING
    return siblings


def test_sibling_lookup_matches_the_full_scan_on_the_fixture(small_graph, config) -> None:
    rng = random.Random(23)
    ids = sorted(small_graph.nodes)
    resumes = [
        resume(roles=(current_role(bullet("skill.react")),), blob=listed("skill.python", "skill.docker")),
        resume(blob=listed("skill.nextjs", "skill.vue", "skill.svelte", "skill.javascript", "skill.terraform")),
        *(random_resume(rng, ids, k) for k in (3, 10, 25, len(ids))),
    ]
    siblings = 0
    for cv in resumes:
        siblings += _assert_same_as_full_scan(_prepare(cv, small_graph, config), small_graph, config,
                                              [*ids, "skill.unknown"])
    assert siblings > 0  # the comparison really exercised the SIBLING branch


def test_sibling_lookup_matches_the_full_scan_on_a_random_graph_with_one_way_edges(config) -> None:
    graph = random_graph(seed=11, n_nodes=400)
    one_way = sum(1 for a, targets in graph.related.items() for b in targets if a not in graph.related.get(b, {}))
    assert one_way > 100  # the graph really has edges stored one way only
    rng = random.Random(11)
    ids = sorted(graph.nodes)
    siblings = 0
    for k in (5, 40, 120, 300):
        siblings += _assert_same_as_full_scan(_prepare(random_resume(rng, ids, k), graph, config), graph, config, ids)
    assert siblings > 100


def test_matching_cost_does_not_grow_with_resume_size(config) -> None:
    # ~18 related neighbours per node; résumés of 40 and 400 skills; 30 requirements.
    graph = random_graph(seed=3, n_nodes=3000, edges_per_node=9, one_way_share=0.1, asymmetric_share=0.0)
    rng = random.Random(3)
    ids = sorted(graph.nodes)
    requirements = tuple(req(skill_id) for skill_id in rng.sample(ids, 30))

    def seconds_per_call(n_skills: int) -> float:
        prepared = _prepare(resume(blob=listed(*rng.sample(ids, n_skills))), graph, config)
        match_requirements(prepared, requirements, graph, config)  # warm the graph's memo caches
        fastest = math.inf
        for _ in range(7):
            start = time.perf_counter()
            for _ in range(10):
                match_requirements(prepared, requirements, graph, config)
            fastest = min(fastest, (time.perf_counter() - start) / 10)
        return fastest

    small, large = seconds_per_call(40), seconds_per_call(400)
    assert large < 2.0 * small, f"40 skills: {small * 1e3:.3f} ms, 400 skills: {large * 1e3:.3f} ms"


def test_values_are_clamped_and_facts_are_plain_scalars(small_graph, config) -> None:
    cv = resume(roles=(current_role(bullet("skill.react", quantified=True)),), blob=listed("skill.python"))
    dim = _score(cv, posting(req("skill.react"), req("skill.python")), small_graph, config)
    assert 0.0 <= dim.value <= 1.0 and 0.0 <= dim.contribution <= 1.0
    for fact in dim.facts:
        assert all(type(v) in (str, float, type(None)) for v in fact.detail.values())


# ─── Extra groups (the §17.3 interval readings, DECISIONS D-45) ─────────────────────────


def _extra(skill_id: str, m: float, idf: float, necessity: Necessity) -> RequirementMatch:
    return RequirementMatch(group=skill_id, members=(skill_id,), best_member=skill_id, m=m,
                            relation=RelationKind.EXACT if m > 0 else RelationKind.NONE, via=None, idf=idf,
                            weight_multiplier=1.0, necessity=necessity, critical=False)


def test_extra_groups_join_the_dimensions_own(config) -> None:
    # One coverage path for the base run and the interval readings: extra groups are averaged in with
    # the posting's own, IDF-weighted. python 0.6 at m 1.0, sql 0.4 missing, an extra at 0.5 and m 0.
    graph = tiny_graph({"skill.python": 0.6, "skill.sql": 0.4})
    cv = _prepare(resume(roles=(current_role(bullet("skill.python")),)), graph, config)
    post = posting(req("skill.python"), req("skill.sql"))
    extra = (_extra("unresolved:pulumi", 0.0, 0.5, Necessity.REQUIRED),)
    dimension = score_required_skills(cv, post, graph, config, extra=extra)
    assert _close(dimension.value, 0.6 / (0.6 + 0.4 + 0.5))
    assert [fact.subject for fact in dimension.facts] == ["skill.python", "skill.sql", "unresolved:pulumi"]
    # With none of its own requirements the dimension scores from the extra groups alone.
    alone = (_extra("x", 1.0, 0.5, Necessity.PREFERRED),)
    only = score_preferred_skills(cv, post, graph, config, extra=alone)
    assert _close(only.value, 1.0) and not only.abstained
    assert score_tools(cv, post, graph, config).abstain_reason is Flag.NO_IMPLIED_REQUIREMENTS


def test_an_extra_group_of_another_necessity_is_an_engine_fault(config) -> None:
    graph = tiny_graph({"skill.python": 0.6})
    cv = _prepare(resume(blob=listed("skill.python")), graph, config)
    with pytest.raises(EngineFault) as err:
        score_required_skills(cv, posting(req("skill.python")), graph, config,
                              extra=(_extra("x", 0.0, 0.5, Necessity.PREFERRED),))
    assert err.value.flag is Flag.INTERNAL_ERROR
