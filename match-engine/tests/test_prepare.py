"""The prepared résumé (Task 3.2): every skill's best value E × R after the §15.2 overrides, the
résumé's latest activity, and the child cover of every ancestor.

SPEC §5.1, §15, §16; DECISIONS D-23, D-24, D-32, D-33, D-34. Expected values are closed-form
arithmetic from the spec constants in tests/builders.py (EVAL §2.2).
"""

from __future__ import annotations

import copy
import math
import pickle
import random
from datetime import date

import pytest
from pydantic import ValidationError

from matcher.models import Evidence, Polarity, Role, SkillSource, VerbClass, canonical_json
from matcher.score.prepare import PreparedResume, SkillValue, prepare_resume
from tests.builders import (
    AS_OF,
    ASPIRATIONAL_M,
    CHILD_CREDIT,
    E_BLOB,
    E_CERT,
    E_EXECUTION,
    E_NO_VERB,
    E_OWNERSHIP,
    E_PARTICIPATION,
    E_TITLE,
    FOUR_YEARS_AGO,
    PROVISIONAL,
    QUANTIFIED_BONUS,
    R_FLOOR,
    TWO_YEARS_AGO,
    bullet,
    current_role,
    ended_role,
    listed,
    on,
    project,
    recency,
    ref,
    resume,
    tiny_graph,
    undated_role,
    with_scoring,
)

YEAR_AGO = date(2025, 10, 8)  # 365 days before AS_OF


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12)


# ─── latest activity (DECISIONS D-34) ───────────────────────────────────────────────────


def test_an_undated_resume_has_no_latest_activity_and_full_recency(small_graph, config) -> None:
    cv = resume(blob=listed("skill.python"))
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    assert prepared.latest_activity is None
    python = prepared.best["skill.python"]
    assert _close(python.recency_mult, 1.0)  # "we do not penalise what we cannot date"
    assert _close(python.evidence_mult, E_BLOB) and _close(python.value, E_BLOB)
    assert prepared.as_of == AS_OF and prepared.resume == cv


def test_latest_activity_is_as_of_while_a_role_is_current(small_graph, config) -> None:
    cv = resume(roles=(ended_role(end=date(2020, 6, 30)), current_role()), blob=listed("skill.python"))
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    assert prepared.latest_activity == AS_OF
    assert _close(prepared.best["skill.python"].value, E_BLOB)


def test_a_current_role_needs_no_start_to_date_the_resume(small_graph, config) -> None:
    cv = resume(roles=(Role(is_current=True), ended_role(end=FOUR_YEARS_AGO)), blob=listed("skill.python"))
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    assert prepared.latest_activity == AS_OF  # D-34: "or as_of if a role is current"
    assert _close(prepared.best["skill.python"].recency_mult, 1.0)


def test_a_role_end_dates_the_resume_even_without_a_start(small_graph, config) -> None:
    # D-34 "latest role end": the end alone is a date of activity (its own skills use it too).
    cv = resume(roles=(Role(end=on(TWO_YEARS_AGO)),), blob=listed("skill.python"))
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    assert prepared.latest_activity == TWO_YEARS_AGO
    assert _close(prepared.best["skill.python"].value, E_BLOB * recency(730))


def test_latest_activity_is_the_latest_role_or_project_end(small_graph, config) -> None:
    roles = (ended_role(end=date(2021, 5, 31)), ended_role(end=date(2023, 8, 31)), undated_role())
    with_project = resume(roles=roles, projects=(project(end=date(2024, 4, 30)), project()))
    assert prepare_resume(with_project, small_graph, config, AS_OF).latest_activity == date(2024, 4, 30)
    without_project = resume(roles=roles, projects=(project(),))
    assert prepare_resume(without_project, small_graph, config, AS_OF).latest_activity == date(2023, 8, 31)
    nothing_dated = resume(roles=(undated_role(),), projects=(project(),))
    assert prepare_resume(nothing_dated, small_graph, config, AS_OF).latest_activity is None


# ─── last used: the mention's own date, else its role / project, else the latest activity ─


def test_role_mentions_take_the_role_end_and_listed_skills_the_latest_activity(small_graph, config) -> None:
    assert (AS_OF - FOUR_YEARS_AGO).days == 1461  # exactly 4.0 years: e^-0.6 = 0.548812 < 0.55
    old = ended_role(bullet("skill.react"), end=FOUR_YEARS_AGO)
    prepared = prepare_resume(resume(roles=(old,), blob=listed("skill.docker")), small_graph, config, AS_OF)
    react = prepared.best["skill.react"]
    assert _close(react.recency_mult, R_FLOOR) and _close(react.value, E_OWNERSHIP * R_FLOOR)
    assert prepared.latest_activity == FOUR_YEARS_AGO
    assert _close(prepared.best["skill.docker"].value, E_BLOB * R_FLOOR)


def test_current_role_mentions_are_used_as_of(small_graph, config) -> None:
    prepared = prepare_resume(resume(roles=(current_role(bullet("skill.react")),)), small_graph, config, AS_OF)
    assert _close(prepared.best["skill.react"].recency_mult, 1.0)
    assert _close(prepared.best["skill.react"].value, E_OWNERSHIP)


def test_a_mention_last_used_date_wins_over_its_role_end(small_graph, config) -> None:
    role = ended_role(bullet("skill.python", last_used=FOUR_YEARS_AGO), bullet("skill.react"),
                      end=TWO_YEARS_AGO, start=date(2015, 1, 1))
    best = prepare_resume(resume(roles=(role,)), small_graph, config, AS_OF).best
    assert _close(best["skill.python"].value, E_OWNERSHIP * R_FLOOR)  # its own date: 4 years
    assert _close(best["skill.react"].recency_mult, recency(730))  # the role end: 2 years
    assert math.isclose(recency(730), 0.740970, abs_tol=1e-6)  # e^(-0.15 × 730 / 365.25), 1.9986 years


def test_project_mentions_take_the_project_end_else_the_latest_activity(small_graph, config) -> None:
    cv = resume(
        roles=(ended_role(end=YEAR_AGO),),
        projects=(
            project(bullet("skill.django", VerbClass.EXECUTION, source=SkillSource.PROJECT), end=TWO_YEARS_AGO),
            project(bullet("skill.flask", source=SkillSource.PROJECT)),
        ),
    )
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    assert prepared.latest_activity == YEAR_AGO
    assert _close(prepared.best["skill.django"].value, E_EXECUTION * recency(730))  # the project's end
    assert _close(prepared.best["skill.flask"].value, E_OWNERSHIP * recency(365))  # undated project


def test_an_undated_role_falls_back_to_the_latest_activity(small_graph, config) -> None:
    cv = resume(roles=(undated_role(bullet("skill.go", VerbClass.EXECUTION)), ended_role(end=TWO_YEARS_AGO)))
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    assert _close(prepared.best["skill.go"].value, E_EXECUTION * recency(730))


def test_certifications_and_titles_are_collected(small_graph, config) -> None:
    cert = ref("cred.aws_saa", Evidence.CERTIFICATION, source=SkillSource.CERTIFICATION)
    title = ref("skill.frontend", Evidence.ROLE_TITLE, source=SkillSource.ROLE)
    cv = resume(roles=(ended_role(title, end=FOUR_YEARS_AGO),), certifications=(cert,))
    best = prepare_resume(cv, small_graph, config, AS_OF).best
    saa = best["cred.aws_saa"]
    assert _close(saa.evidence_mult, E_CERT) and _close(saa.value, E_CERT * R_FLOOR)
    assert saa.evidence is Evidence.CERTIFICATION and saa.source is SkillSource.CERTIFICATION
    assert _close(best["skill.frontend"].value, E_TITLE * R_FLOOR)


# ─── E and the §15.2 overrides ─────────────────────────────────────────────────────────


def test_quantified_bullets_earn_the_bonus_capped_at_one(small_graph, config) -> None:
    role = current_role(bullet("skill.python", VerbClass.EXECUTION, quantified=True),
                        bullet("skill.react", VerbClass.OWNERSHIP, quantified=True))
    best = prepare_resume(resume(roles=(role,)), small_graph, config, AS_OF).best
    assert _close(best["skill.python"].value, E_EXECUTION * QUANTIFIED_BONUS)  # 0.9775
    assert _close(best["skill.react"].value, min(1.0, E_OWNERSHIP * QUANTIFIED_BONUS))  # 1.15, capped
    assert best["skill.python"].evidence is Evidence.ROLE_BULLET_QUANTIFIED


def test_a_negated_mention_is_worth_nothing_and_an_aspirational_one_at_most_015(small_graph, config) -> None:
    role = current_role(
        bullet("skill.java", polarity=Polarity.NEGATED),
        bullet("skill.machine_learning", VerbClass.NO_VERB, polarity=Polarity.ASPIRATIONAL),
    )
    best = prepare_resume(resume(roles=(role,)), small_graph, config, AS_OF).best
    java, ml = best["skill.java"], best["skill.machine_learning"]
    assert _close(java.value, 0.0) and java.polarity is Polarity.NEGATED
    assert _close(java.evidence_mult, E_OWNERSHIP)  # E and R are kept; only the value is overridden
    assert _close(ml.value, min(E_NO_VERB, ASPIRATIONAL_M)) and ml.polarity is Polarity.ASPIRATIONAL


def test_another_affirmed_mention_still_counts(small_graph, config) -> None:
    # DECISIONS D-33: "Another, non-negated mention of the same skill still counts."
    role = current_role(bullet("skill.java", polarity=Polarity.NEGATED))
    best = prepare_resume(resume(roles=(role,), blob=listed("skill.java")), small_graph, config, AS_OF).best
    assert _close(best["skill.java"].value, E_BLOB) and best["skill.java"].polarity is Polarity.AFFIRMED


# ─── best: the max-value mention, deterministic on ties ─────────────────────────────────


def test_best_keeps_the_strongest_mention(small_graph, config) -> None:
    role = current_role(bullet("skill.python", VerbClass.PARTICIPATION), bullet("skill.python", VerbClass.EXECUTION))
    best = prepare_resume(resume(roles=(role,), blob=listed("skill.python")), small_graph, config, AS_OF).best
    assert set(best) == {"skill.python"}
    assert best["skill.python"] == SkillValue(
        skill_id="skill.python", value=E_EXECUTION, evidence_mult=E_EXECUTION, recency_mult=1.0,
        evidence=Evidence.ROLE_BULLET, polarity=Polarity.AFFIRMED, source=SkillSource.ROLE,
    )


def test_a_demonstrated_mention_wins_a_tie_with_a_listed_one_in_any_order(small_graph, config) -> None:
    # 0.65 × 1.0 both ways: PARTICIPATION (§15.1) in a current role, SKILLS_BLOB (§5.1) as of today.
    assert _close(E_PARTICIPATION, E_BLOB)
    shown, listed_ref = bullet("skill.python", VerbClass.PARTICIPATION), ref("skill.python")
    for skills in ((shown, listed_ref), (listed_ref, shown)):
        best = prepare_resume(resume(roles=(current_role(*skills),)), small_graph, config, AS_OF).best
        assert best["skill.python"].evidence is Evidence.ROLE_BULLET
    cv = resume(roles=(current_role(shown),), blob=listed("skill.python"))
    assert prepare_resume(cv, small_graph, config, AS_OF).best["skill.python"].evidence is Evidence.ROLE_BULLET


def test_on_a_tie_of_value_and_evidence_the_earlier_source_wins(small_graph, config) -> None:
    from_project = bullet("skill.go", VerbClass.EXECUTION, source=SkillSource.PROJECT)
    from_role = bullet("skill.go", VerbClass.EXECUTION, source=SkillSource.ROLE)
    for skills in ((from_project, from_role), (from_role, from_project)):
        best = prepare_resume(resume(roles=(current_role(*skills),)), small_graph, config, AS_OF).best
        assert best["skill.go"].source is SkillSource.ROLE


def test_the_same_skill_fifty_times_equals_once(small_graph, config) -> None:
    once = prepare_resume(resume(blob=listed("skill.python")), small_graph, config, AS_OF)
    fifty = prepare_resume(resume(blob=listed(*["skill.python"] * 50)), small_graph, config, AS_OF)
    assert fifty.best == once.best and fifty.child_cover == once.child_cover


def test_unknown_skill_ids_are_kept_for_exact_matching_only(small_graph, config) -> None:
    prepared = prepare_resume(resume(blob=listed("skill.not_in_graph")), small_graph, config, AS_OF)
    assert _close(prepared.best["skill.not_in_graph"].value, E_BLOB)
    assert prepared.child_cover == {}


# ─── child cover (DECISIONS D-23, D-24) ─────────────────────────────────────────────────


def test_child_cover_reaches_every_ancestor_within_max_depth(small_graph, config) -> None:
    prepared = prepare_resume(resume(blob=listed("skill.nextjs")), small_graph, config, AS_OF)
    # nextjs -> react -> js_framework -> javascript: depth 1, 2 and 3, all at the CHILD credit.
    assert set(prepared.child_cover) == {"skill.react", "skill.js_framework", "skill.javascript"}
    for credit, via in prepared.child_cover.values():
        assert _close(credit, CHILD_CREDIT * E_BLOB) and via == "skill.nextjs"
    shallow = with_scoring(config, "relations", max_depth=2)
    cover = prepare_resume(resume(blob=listed("skill.nextjs")), small_graph, shallow, AS_OF).child_cover
    assert set(cover) == {"skill.react", "skill.js_framework"}


def test_child_cover_keeps_the_best_child(small_graph, config) -> None:
    cv = resume(roles=(current_role(bullet("skill.vue")),), blob=listed("skill.react", "skill.angular"))
    credit, via = prepare_resume(cv, small_graph, config, AS_OF).child_cover["skill.js_framework"]
    assert via == "skill.vue" and _close(credit, CHILD_CREDIT * E_OWNERSHIP)


def test_child_cover_ties_go_to_the_smaller_id(small_graph, config) -> None:
    for blob in (listed("skill.vue", "skill.angular"), listed("skill.angular", "skill.vue")):
        credit, via = prepare_resume(resume(blob=blob), small_graph, config, AS_OF).child_cover["skill.js_framework"]
        assert via == "skill.angular" and _close(credit, CHILD_CREDIT * E_BLOB)


def test_child_cover_from_a_provisional_node_is_discounted(small_graph, config) -> None:
    assert small_graph.nodes["skill.svelte"].provisional
    credit, via = prepare_resume(resume(blob=listed("skill.svelte")), small_graph, config, AS_OF).child_cover[
        "skill.js_framework"]
    assert via == "skill.svelte" and _close(credit, CHILD_CREDIT * E_BLOB * PROVISIONAL)


def test_child_cover_of_a_provisional_ancestor_is_discounted(config) -> None:
    graph = tiny_graph({"skill.leaf": 0.5}, parents={"skill.leaf": ("skill.new_parent",)},
                       provisional=("skill.new_parent",))
    credit, via = prepare_resume(resume(blob=listed("skill.leaf")), graph, config, AS_OF).child_cover[
        "skill.new_parent"]
    assert via == "skill.leaf" and _close(credit, CHILD_CREDIT * E_BLOB * PROVISIONAL)


def test_a_negated_skill_gives_no_cover_credit(small_graph, config) -> None:
    cv = resume(roles=(current_role(bullet("skill.nextjs", polarity=Polarity.NEGATED)),))
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    assert _close(prepared.best["skill.nextjs"].value, 0.0)
    assert all(_close(credit, 0.0) for credit, _ in prepared.child_cover.values())


def test_child_cover_survives_a_parent_cycle(small_graph, config) -> None:
    cover = prepare_resume(resume(blob=listed("skill.cycle_a")), small_graph, config, AS_OF).child_cover
    assert set(cover) == {"skill.cycle_b", "skill.cycle_c"}  # never the node itself


# ─── affirmed evidence (SPEC §17.1 evidence density) ────────────────────────────────────


def test_affirmed_evidence_is_the_strongest_tier_among_affirmed_mentions(small_graph, config) -> None:
    # Whether a skill is shown beyond the skills list is not the evidence of its best E × R mention:
    # a listed line dated to a current role (0.65 × 1.0) beats an ownership bullet that ended
    # 2023-06-30 (1.0 × R of 1,196 days, 0.612). Negated and aspirational mentions show nothing.
    old = ended_role(bullet("skill.python"), bullet("skill.docker", polarity=Polarity.NEGATED),
                     bullet("skill.go", polarity=Polarity.ASPIRATIONAL),
                     bullet("skill.kubernetes", polarity=Polarity.NEGATED), end=date(2023, 6, 30))
    current = current_role(ref("skill.sql", Evidence.ROLE_TITLE), bullet("skill.git", VerbClass.EXECUTION,
                                                                           quantified=True))
    cv = resume(roles=(current, old), blob=listed("skill.python", "skill.docker", "skill.go", "skill.sql"),
                certifications=(ref("skill.aws", Evidence.CERTIFICATION),))
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    assert prepared.best["skill.python"].evidence is Evidence.SKILLS_BLOB
    assert _close(prepared.best["skill.python"].value, E_BLOB)
    assert prepared.affirmed_evidence == {
        "skill.aws": Evidence.CERTIFICATION,
        "skill.docker": Evidence.SKILLS_BLOB,  # its bullet is negated
        "skill.git": Evidence.ROLE_BULLET_QUANTIFIED,
        "skill.go": Evidence.SKILLS_BLOB,  # its bullet is aspirational
        "skill.python": Evidence.ROLE_BULLET,
        "skill.sql": Evidence.ROLE_TITLE,
    }  # kubernetes, only negated, has no affirmed mention at all
    assert "skill.kubernetes" in prepared.best
    assert list(prepared.affirmed_evidence) == sorted(prepared.affirmed_evidence)
    with pytest.raises(TypeError):
        prepared.affirmed_evidence["skill.x"] = Evidence.CERTIFICATION  # read-only like best


def test_affirmed_evidence_does_not_depend_on_mention_order(small_graph, config) -> None:
    mentions = [ref("skill.python"), bullet("skill.python", VerbClass.ASSISTANCE),
                ref("skill.python", Evidence.ROLE_TITLE)]
    seen = set()
    for order in (mentions, mentions[::-1], mentions[1:] + mentions[:1]):
        prepared = prepare_resume(resume(roles=(current_role(*order),)), small_graph, config, AS_OF)
        seen.add(prepared.affirmed_evidence["skill.python"])
    assert seen == {Evidence.ROLE_TITLE}


# ─── shape, determinism ─────────────────────────────────────────────────────────────────


def test_prepared_resume_is_frozen_validated_and_ordered(small_graph, config) -> None:
    cv = resume(blob=listed("skill.vue", "skill.python", "skill.django"))
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    assert isinstance(prepared, PreparedResume)
    assert list(prepared.best) == sorted(prepared.best)
    assert list(prepared.child_cover) == sorted(prepared.child_cover)
    with pytest.raises(ValidationError):
        prepared.as_of = date(2000, 1, 1)  # type: ignore[misc]
    with pytest.raises(ValidationError):
        SkillValue(skill_id="skill.x", value=1.5, evidence_mult=1.0, recency_mult=1.0,
                   evidence=Evidence.SKILLS_BLOB, polarity=Polarity.AFFIRMED, source=SkillSource.OTHER)


def test_prepared_resume_records_its_graph_and_config(small_graph, config) -> None:
    prepared = prepare_resume(resume(blob=listed("skill.python")), small_graph, config, AS_OF)
    assert prepared.graph_version == small_graph.version
    assert prepared.config_hash == config.content_hash
    other = prepare_resume(resume(blob=listed("skill.python")), small_graph,
                           with_scoring(config, "relations", max_depth=2), AS_OF)
    assert other.config_hash != prepared.config_hash


def test_best_and_child_cover_are_read_only(small_graph, config) -> None:
    prepared = prepare_resume(resume(blob=listed("skill.nextjs")), small_graph, config, AS_OF)
    some = SkillValue(skill_id="skill.x", value=0.5, evidence_mult=0.5, recency_mult=1.0,
                      evidence=Evidence.SKILLS_BLOB, polarity=Polarity.AFFIRMED, source=SkillSource.OTHER)
    for mapping, item in ((prepared.best, some), (prepared.child_cover, (0.5, "skill.x"))):
        key = next(iter(mapping))
        for mutate in (lambda: mapping.__setitem__("skill.x", item), lambda: mapping.__delitem__(key),
                       lambda: mapping.update({"skill.x": item}), lambda: mapping.pop(key),
                       lambda: mapping.popitem(), lambda: mapping.setdefault("skill.x", item),
                       lambda: mapping.clear()):
            with pytest.raises(TypeError):
                mutate()
        with pytest.raises(TypeError):
            mapping |= {"skill.x": item}
        assert key in mapping and "skill.x" not in mapping
    # Still a plain value object: serialises, round-trips, copies and pickles.
    assert PreparedResume.model_validate_json(canonical_json(prepared)) == prepared
    assert copy.deepcopy(prepared) == prepared and prepared.model_copy(deep=True) == prepared
    assert pickle.loads(pickle.dumps(prepared)) == prepared
    with pytest.raises(TypeError):
        copy.deepcopy(prepared).best["skill.x"] = some


def test_a_one_ulp_difference_never_decides_which_mention_is_kept() -> None:
    from matcher.score.prepare import _rank

    value = E_BLOB * recency(84)
    listed_value = SkillValue(skill_id="skill.python", value=math.nextafter(value, 1.0), evidence_mult=E_BLOB,
                              recency_mult=recency(84), evidence=Evidence.SKILLS_BLOB, polarity=Polarity.AFFIRMED,
                              source=SkillSource.SKILLS_SECTION)
    shown_value = listed_value.model_copy(update={"value": value, "evidence": Evidence.ROLE_BULLET,
                                                  "source": SkillSource.ROLE})
    for pair in ((listed_value, shown_value), (shown_value, listed_value)):
        assert max(pair, key=_rank) is shown_value  # a tie: the demonstrated mention, not the last bit


def test_a_one_ulp_difference_never_decides_the_child_cover(small_graph, config) -> None:
    from matcher.score.prepare import _child_cover

    value = E_BLOB * recency(84)

    def listed_at(skill_id: str, v: float) -> SkillValue:
        return SkillValue(skill_id=skill_id, value=v, evidence_mult=E_BLOB, recency_mult=1.0,
                          evidence=Evidence.SKILLS_BLOB, polarity=Polarity.AFFIRMED, source=SkillSource.SKILLS_SECTION)

    best = {"skill.angular": listed_at("skill.angular", value),
            "skill.vue": listed_at("skill.vue", math.nextafter(value, 1.0))}
    credit, via = _child_cover(best, small_graph, config)["skill.js_framework"]
    assert via == "skill.angular" and credit == CHILD_CREDIT * value  # the smaller id, its own credit


def test_preparation_does_not_depend_on_input_order(small_graph, config) -> None:
    skills = [bullet("skill.react"), bullet("skill.python", VerbClass.PARTICIPATION), ref("skill.python"),
              bullet("skill.nextjs", VerbClass.EXECUTION), ref("skill.vue"), ref("skill.svelte"),
              ref("skill.angular"), bullet("skill.docker", polarity=Polarity.NEGATED), ref("skill.docker")]
    reference = prepare_resume(resume(roles=(current_role(*skills),)), small_graph, config, AS_OF)
    rng = random.Random(7)
    for _ in range(20):
        shuffled = skills[:]
        rng.shuffle(shuffled)
        again = prepare_resume(resume(roles=(current_role(*shuffled),)), small_graph, config, AS_OF)
        assert again.best == reference.best and again.child_cover == reference.child_cover
        assert list(again.best) == list(reference.best)
        assert canonical_json(again.model_copy(update={"resume": reference.resume})) == canonical_json(reference)
