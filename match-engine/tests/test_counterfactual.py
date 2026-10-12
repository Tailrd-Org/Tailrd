"""`to_improve` (SPEC §8; Task 4.1): the cheapest missing skills that reach the next band.

Every suggestion is checked by re-scoring the résumé with the suggested skills added: the number a
suggestion states must be the number the engine gives.
"""

from __future__ import annotations

import random
from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from matcher.calibrate import FixedCalibrator
from matcher.explain.counterfactual import (
    add_skills,
    improve,
    learning_cost,
    most_recent_role,
    swap_pass,
    target_score,
)
from matcher.graph.traversal import distance, nearest_distance
from matcher.models import (
    Evidence,
    Necessity,
    Outcome,
    Polarity,
    Resume,
    Role,
    ScoreOptions,
    SeniorityLevel,
    Track,
    VerbClass,
)
from matcher.score.engine import score
from tests.builders import AS_OF, close, listed, on, random_graph
from tests.explain_pairs import PAIRS, backend_posting, reqs

FIXED = FixedCalibrator()


def run(resume, posting, graph, config, **options):
    options.setdefault("strict", True)
    return score(resume, posting, as_of=AS_OF, config=config, graph=graph, calibration=FIXED,
                 options=ScoreOptions(**options))


def suggest(resume, posting, graph, config, max_steps: int = 4):
    return improve(resume, posting, as_of=AS_OF, config=config, graph=graph, calibration=FIXED,
                   max_steps=max_steps, strict=True)


# ─── The pieces ───────────────────────────────────────────────────────────────────────────


def test_target_is_the_next_band_minimum(config) -> None:
    assert [target_score(s, config) for s in (0, 54, 55, 69, 70, 76, 84)] == [55, 55, 70, 70, 85, 85, 85]
    assert target_score(85, config) is None and target_score(100, config) is None


def test_learning_cost_closed_form(small_graph, config) -> None:
    # Terraform (TOOL, 2 weeks) is one related edge from AWS: 2 × (1 + 1.0 × 1) = 4.
    assert close(learning_cost("skill.terraform", ["skill.aws"], small_graph, config), 4.0)
    # TypeScript (TECHNOLOGY, 4) is three hops from React (react → js_framework → javascript → ts).
    assert close(learning_cost("skill.typescript", ["skill.react"], small_graph, config), 4.0 * 4)
    # Out of reach, and absent from the graph: the cap of 4 hops, absent costed as TECHNOLOGY.
    assert close(learning_cost("skill.agile", ["skill.python"], small_graph, config), 4.0 * 5)
    assert close(learning_cost("skill.unknown", ["skill.python"], small_graph, config), 4.0 * 5)
    # Already on the résumé (weakly): no distance.
    assert close(learning_cost("cred.registered_nurse", ["cred.registered_nurse"], small_graph, config), 8.0)


@settings(max_examples=60)
@given(st.integers(0, 10_000), st.integers(0, 4))
def test_nearest_distance_is_the_minimum_of_distance(seed: int, depth: int) -> None:
    graph = random_graph(seed % 7, n_nodes=60, edges_per_node=2)
    rng = random.Random(seed)
    ids = sorted(graph.nodes)
    source, targets = rng.choice(ids), rng.sample(ids, k=rng.randrange(0, 6))
    found = [d for d in (distance(graph, source, t, depth) for t in targets) if d is not None]
    assert nearest_distance(graph, source, targets, depth) == (min(found) if found else None)


def test_a_skill_goes_into_the_most_recent_role(config) -> None:
    old = Role(start=on(date(2019, 1, 1)), end=on(date(2021, 1, 1)))
    later = Role(start=on(date(2021, 2, 1)), end=on(date(2023, 6, 1)))
    current = Role(start=on(date(2023, 7, 1)), is_current=True)
    assert most_recent_role(Resume(roles=(old, later)), AS_OF) == 1
    assert most_recent_role(Resume(roles=(old, current, later)), AS_OF) == 1
    assert most_recent_role(Resume(roles=(Role(), Role())), AS_OF) == 0
    assert most_recent_role(Resume(), AS_OF) is None
    added = add_skills(Resume(roles=(old, later)), ["skill.go"], config, AS_OF)
    mention = added.roles[1].skills[-1]
    assert (mention.canonical_id, mention.evidence, mention.verb_class, mention.polarity) == (
        "skill.go", Evidence.ROLE_BULLET, VerbClass.EXECUTION, Polarity.AFFIRMED)
    assert added.roles[0] == old
    student = add_skills(Resume(skills_blob=listed("skill.python")), ["skill.go", "skill.sql"], config, AS_OF)
    assert len(student.projects) == 1 and [r.canonical_id for r in student.projects[0].skills] == ["skill.go", "skill.sql"]
    assert student.projects[0].start is None and student.projects[0].end is None


@settings(max_examples=300)
@given(st.data())
def test_the_swap_pass_never_raises_total_weeks_and_keeps_the_target(data) -> None:
    pool = [f"s{i}" for i in range(data.draw(st.integers(1, 8)))]
    costs = {skill: data.draw(st.integers(1, 40)) for skill in pool}
    gains = {skill: data.draw(st.integers(0, 10)) for skill in pool}
    need = data.draw(st.integers(1, 25))

    def reaches(skills) -> bool:
        return sum(gains[s] for s in skills) >= need

    chosen = data.draw(st.lists(st.sampled_from(pool), min_size=1, max_size=4, unique=True))
    if not reaches(chosen):
        return
    final = swap_pass(chosen, pool, reaches, costs.__getitem__)
    assert reaches(final) and len(set(final)) == len(final) <= len(chosen)
    assert sum(costs[s] for s in final) <= sum(costs[s] for s in chosen)


# ─── The search, end to end ───────────────────────────────────────────────────────────────


def test_every_suggestion_rescores_to_its_stated_number(small_graph, config) -> None:
    for name in ("good_match", "critical_miss_binding", "overqualified", "stretch_role"):
        resume, posting = PAIRS[name]()
        base = run(resume, posting, small_graph, config, with_explanation=False)
        improvements = suggest(resume, posting, small_graph, config)
        assert improvements, name
        for improvement in improvements:
            again = run(add_skills(resume, improvement.skill_ids, config, AS_OF), posting, small_graph, config,
                        with_explanation=False)
            assert again.outcome is Outcome.SCORED
            assert again.calibrated_score == improvement.new_score > base.calibrated_score
            assert close(again.raw_score, improvement.new_raw_score) and again.band == improvement.new_band
            assert f"roughly {improvement.new_score}" in improvement.text and "%" not in improvement.text


def test_the_chosen_set_reaches_the_next_band(small_graph, config) -> None:
    resume, posting = PAIRS["good_match"]()
    first = suggest(resume, posting, small_graph, config)[0]
    assert first.new_score >= 85 and len(first.skill_ids) > 1
    assert first.text == "Adding Terraform and TypeScript would move this to roughly 86 (Strong match)"
    singles = suggest(resume, posting, small_graph, config)[1:]
    assert 1 <= len(singles) <= 3 and all(len(s.skill_ids) == 1 for s in singles)
    assert singles[0].text == "Adding demonstrated Terraform experience would move this to roughly 82"


def test_at_most_max_steps_skills(small_graph, config) -> None:
    resume, posting = PAIRS["good_match"]()
    one = suggest(resume, posting, small_graph, config, max_steps=1)
    assert all(len(improvement.skill_ids) == 1 for improvement in one)  # 86 needs two: no set, singles only
    assert all(improvement.new_score < 85 for improvement in one)


def test_already_strong_gets_no_suggestions(small_graph, config) -> None:
    resume, posting = PAIRS["strong_match"]()
    assert run(resume, posting, small_graph, config, with_explanation=False).band.value == "Strong match"
    assert suggest(resume, posting, small_graph, config) == ()
    # Strong with a missing preferred skill (Go): something to add, but no band above.
    posting = posting.model_copy(update={"requirements": (*posting.requirements, *reqs(("skill.go",), Necessity.PREFERRED))})
    result = run(resume, posting, small_graph, config)
    assert result.band.value == "Strong match" and any("Go" in gap.label for gap in result.explanation.gaps)
    assert suggest(resume, posting, small_graph, config) == ()


def test_only_scored_results_get_suggestions(small_graph, config) -> None:
    for name in ("low_confidence", "needs_review_flag", "gate_fail_sponsorship", "undated_student"):
        resume, posting = PAIRS[name]()
        assert suggest(resume, posting, small_graph, config) == (), name


def test_suggestions_are_deterministic_and_ignore_requirement_order(small_graph, config) -> None:
    resume, posting = PAIRS["good_match"]()
    first = suggest(resume, posting, small_graph, config)
    assert suggest(resume, posting, small_graph, config) == first
    shuffled = list(posting.requirements)
    random.Random(7).shuffle(shuffled)
    reordered = posting.model_copy(update={"requirements": tuple(reversed(shuffled))})
    assert [i.skill_ids for i in suggest(resume, reordered, small_graph, config)] == [i.skill_ids for i in first]


def test_the_engine_attaches_improvements_on_request(small_graph, config) -> None:
    resume, posting = PAIRS["good_match"]()
    plain = run(resume, posting, small_graph, config)
    assert plain.improvements == () and plain.explanation.to_improve == ()
    full = run(resume, posting, small_graph, config, with_counterfactual=True, max_counterfactual_steps=4)
    assert full.improvements == suggest(resume, posting, small_graph, config)
    assert full.explanation.to_improve == tuple(i.text for i in full.improvements)
    only = run(resume, posting, small_graph, config, with_explanation=False, with_counterfactual=True)
    assert only.explanation is None and only.improvements == full.improvements


def test_a_posting_with_nothing_missing_but_a_low_score_has_none(small_graph, config) -> None:
    # Every skill covered but the role is far above the candidate: nothing to add skill-wise.
    resume, _ = PAIRS["strong_match"]()
    posting = backend_posting(min_years_total=12.0, seniority=SeniorityLevel(track=Track.IC, level=6))
    result = run(resume, posting, small_graph, config, with_explanation=False)
    assert result.band.value != "Strong match"
    assert suggest(resume, posting, small_graph, config) == ()


def test_improve_accepts_dicts_like_the_engine(small_graph, config) -> None:
    resume, posting = PAIRS["good_match"]()
    as_dicts = suggest(resume.model_dump(mode="json"), posting.model_dump(mode="json"), small_graph, config)
    assert as_dicts == suggest(resume, posting, small_graph, config)
