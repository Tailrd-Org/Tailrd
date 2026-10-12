"""Confidence and the outcome it decides (SPEC §17.1, §17.2, §13, §14.5; DECISIONS D-42, D-44).

    confidence = exp(Σ wᵢ ln max(cᵢ, 1e-6) / Σ wᵢ)

over the components defined for the pair; an undefined one is left out and the weights renormalised
(D-44), so "zero overlap" stays a valid score. The components: parse quality (tier A 1.0, B 0.8,
C 0.55); taxonomy coverage, the resolved share of the posting's requirement tokens (undefined
without tokens); evidence density, the share of matched requirement groups whose matching résumé
skill is shown beyond the skills list (undefined when nothing matched); temporal completeness, dated
roles over roles (undefined without roles); requirement count, 1 → 0.40, 5 → 0.85, 10+ → 1.0,
linear between; dimension coverage, the aggregate's scored weight (the §14.5 transfer counts as
scored). At or above 0.80 the pair is SCORED, at or above 0.60 SCORED_LOW_CONFIDENCE, else
NEEDS_REVIEW. A component below 0.70 is named among the reasons; when the value misses 0.80 and no
component is below 0.70, the weakest one is named (SPEC §17.2: the reason is always specific).

Expected values are closed-form arithmetic from those definitions (EVAL §2.2).
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from matcher.errors import EngineFault, Flag
from matcher.models import (
    Confidence,
    DateValue,
    Evidence,
    JobPosting,
    JobRequirement,
    Necessity,
    Outcome,
    ParseTier,
    Polarity,
    Project,
    Resume,
    Role,
    SeniorityLevel,
    Track,
    VerbClass,
)
from matcher.score.aggregate import aggregate
from matcher.score.confidence import (
    COMPONENTS,
    compute_confidence,
    confidence_from_components,
    outcome_for,
    requirement_count_factor,
)
from matcher.score.coverage import match_requirements
from matcher.score.dimensions import score_dimensions
from matcher.score.prepare import prepare_resume
from tests.builders import AS_OF, bullet, close, current_role, listed, on, ref, with_scoring

FLOOR = 1e-6  # D-44
SCORED_AT, LOW_AT, WEAK = 0.80, 0.60, 0.70  # §17.2 bands; the weak-component line
IC1 = SeniorityLevel(track=Track.IC, level=1)
NAMES = (
    "parse_quality",
    "taxonomy_coverage",
    "evidence_density",
    "temporal_completeness",
    "requirement_count",
    "dimension_coverage",
)
LOW = {
    "parse_quality": Flag.LOW_PARSE_QUALITY,
    "taxonomy_coverage": Flag.LOW_TAXONOMY_COVERAGE,
    "evidence_density": Flag.LOW_EVIDENCE_DENSITY,
    "temporal_completeness": Flag.LOW_TEMPORAL_COMPLETENESS,
    "requirement_count": Flag.FEW_REQUIREMENTS,
    "dimension_coverage": Flag.LOW_DIMENSION_COVERAGE,
}


def _components(*values) -> dict:
    """The six components in SPEC §17.1 order; None = undefined for the pair."""
    assert len(values) == 6
    return dict(zip(NAMES, values))


def _geo(*values: float) -> float:
    """The equal-weight geometric mean of the defined values, each floored at 1e-6."""
    return math.exp(math.fsum(math.log(max(v, FLOOR)) for v in values) / len(values))


def _confidence(value: float) -> Confidence:
    return Confidence(value=value, components=_components(value, None, None, None, None, None))


# ─── The geometric mean (§17.1, D-44) ─────────────────────────────────────────────────────


def test_the_component_names_are_the_spec_six_in_order() -> None:
    assert COMPONENTS == NAMES


def test_the_brief_example_is_0_820893_and_scored(config) -> None:
    conf = confidence_from_components(_components(1.0, 0.8, 0.5, 1.0, 0.85, 0.9), config)
    assert close(conf.value, (1.0 * 0.8 * 0.5 * 1.0 * 0.85 * 0.9) ** (1 / 6))  # 0.306^(1/6) = 0.8208937
    assert abs(conf.value - 0.820893) < 1e-6  # the brief's figure, to its six places
    assert all(close(conf.components[name], value)
               for name, value in _components(1.0, 0.8, 0.5, 1.0, 0.85, 0.9).items())
    assert conf.reasons == (Flag.LOW_EVIDENCE_DENSITY,)  # 0.5 is the one component under 0.70
    assert outcome_for(conf, config) is Outcome.SCORED


def test_an_undefined_component_is_left_out_and_the_rest_renormalised(config) -> None:
    conf = confidence_from_components(_components(1.0, 0.8, 0.5, None, 0.85, 0.9), config)
    assert close(conf.value, (1.0 * 0.8 * 0.5 * 0.85 * 0.9) ** (1 / 5))  # the mean over five
    assert conf.components["temporal_completeness"] is None
    assert outcome_for(conf, config) is Outcome.SCORED_LOW_CONFIDENCE  # 0.7890: under 0.80


def test_weights_weight_the_logarithms(config) -> None:
    weights = dict.fromkeys(NAMES, 1.0) | {"parse_quality": 2.0, "evidence_density": 3.0}
    tuned = with_scoring(config, "confidence", weights=weights)
    conf = confidence_from_components(_components(0.8, 0.9, 0.5, None, 1.0, 0.7), tuned)
    logs = 2 * math.log(0.8) + math.log(0.9) + 3 * math.log(0.5) + math.log(1.0) + math.log(0.7)
    assert close(conf.value, math.exp(logs / (2 + 1 + 3 + 1 + 1)))


def test_a_zero_component_is_floored_not_fatal(config) -> None:
    conf = confidence_from_components(_components(1.0, 1.0, 0.0, 1.0, 1.0, 1.0), config)
    assert close(conf.value, math.exp(math.log(FLOOR) / 6))  # one catastrophic input drags it down
    assert conf.value > 0.0 and outcome_for(conf, config) is Outcome.NEEDS_REVIEW
    assert close(conf.components["evidence_density"], 0.0)  # reported as measured, not as the floor


def test_all_components_at_one_is_full_confidence(config) -> None:
    conf = confidence_from_components(_components(1.0, 1.0, 1.0, 1.0, 1.0, 1.0), config)
    assert close(conf.value, 1.0) and conf.reasons == ()


def test_a_geometric_mean_landing_on_the_threshold_is_scored(config) -> None:
    # exp(6 ln 0.8 / 6) may come out one ulp under 0.8; a threshold is not decided by float noise.
    conf = confidence_from_components(_components(0.8, 0.8, 0.8, 0.8, 0.8, 0.8), config)
    assert close(conf.value, 0.8)
    assert outcome_for(conf, config) is Outcome.SCORED


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_non_finite_component_is_a_numeric_fault(config, bad) -> None:
    with pytest.raises(EngineFault) as err:
        confidence_from_components(_components(1.0, bad, 1.0, 1.0, 1.0, 1.0), config)
    assert err.value.flag is Flag.NUMERIC_FAULT


def test_the_component_set_is_fixed(config) -> None:
    with pytest.raises(EngineFault) as err:
        confidence_from_components({**_components(1.0, 1.0, 1.0, 1.0, 1.0, 1.0), "charm": 1.0}, config)
    assert err.value.flag is Flag.INTERNAL_ERROR
    with pytest.raises(EngineFault):
        confidence_from_components({"parse_quality": 1.0}, config)


def test_out_of_range_components_are_clamped(config) -> None:
    conf = confidence_from_components(_components(1.5, 1.0, 1.0, 1.0, 1.0, -0.2), config)
    assert close(conf.components["parse_quality"], 1.0)
    assert close(conf.components["dimension_coverage"], 0.0)


# ─── Reasons (§17.2) ───────────────────────────────────────────────────────────────────────


def test_every_component_under_0_70_is_named_in_component_order(config) -> None:
    conf = confidence_from_components(_components(0.69, 0.70, 0.71, 0.5, None, 0.2), config)
    assert conf.reasons == (
        Flag.LOW_PARSE_QUALITY, Flag.LOW_TEMPORAL_COMPLETENESS, Flag.LOW_DIMENSION_COVERAGE,
    )


def test_each_component_has_its_own_reason(config) -> None:
    for name in NAMES:
        values = dict.fromkeys(NAMES, 1.0) | {name: 0.3}
        assert confidence_from_components(values, config).reasons == (LOW[name],)


def test_a_low_confidence_result_names_the_weakest_component_when_none_is_under_0_70(config) -> None:
    values = (0.72, 0.75, 0.78, 0.79, 0.79, 0.79)
    conf = confidence_from_components(_components(*values), config)
    assert close(conf.value, _geo(*values)) and conf.value < SCORED_AT
    assert outcome_for(conf, config) is Outcome.SCORED_LOW_CONFIDENCE
    assert conf.reasons == (Flag.LOW_PARSE_QUALITY,)


def test_tied_weakest_components_are_all_named(config) -> None:
    values = (0.75, 0.75, 0.9, 0.75, 0.8)
    conf = confidence_from_components(_components(0.75, 0.75, None, 0.9, 0.75, 0.8), config)
    assert close(conf.value, _geo(*values)) and conf.value < SCORED_AT  # 0.7880
    assert outcome_for(conf, config) is Outcome.SCORED_LOW_CONFIDENCE
    assert conf.reasons == (Flag.LOW_PARSE_QUALITY, Flag.LOW_TAXONOMY_COVERAGE, Flag.FEW_REQUIREMENTS)


def test_a_scored_pair_with_no_weak_component_has_no_reason(config) -> None:
    conf = confidence_from_components(_components(1.0, 0.9, 0.85, 1.0, 0.8, 0.9), config)
    assert outcome_for(conf, config) is Outcome.SCORED and conf.reasons == ()


# ─── Requirement count (§17.1: 1 → 0.4, 5 → 0.85, 10+ → 1.0) ────────────────────────────────


@pytest.mark.parametrize("n, expected", [
    (1, 0.40),
    (2, 0.40 + 0.45 * 1 / 4),
    (3, 0.40 + 0.45 * 2 / 4),  # 0.625
    (5, 0.85),
    (7, 0.85 + 0.15 * 2 / 5),  # 0.91
    (10, 1.0),
    (20, 1.0),
    (0, 0.40),  # clamped to the first point
])
def test_requirement_count_factor(config, n, expected) -> None:
    assert close(requirement_count_factor(n, config), expected)


def test_requirement_count_factor_brief_values(config) -> None:
    brief = {1: 0.40, 3: 0.625, 5: 0.85, 7: 0.91, 10: 1.0, 20: 1.0}
    assert all(close(requirement_count_factor(n, config), value) for n, value in brief.items())


def test_requirement_count_factor_is_monotone(config) -> None:
    values = [requirement_count_factor(n, config) for n in range(0, 40)]
    assert values == sorted(values) and all(0.0 <= v <= 1.0 for v in values)


# ─── Outcome (§17.2) ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, outcome", [
    (1.0, Outcome.SCORED),
    (0.80, Outcome.SCORED),
    (0.7999, Outcome.SCORED_LOW_CONFIDENCE),
    (0.60, Outcome.SCORED_LOW_CONFIDENCE),
    (0.5999, Outcome.NEEDS_REVIEW),
    (0.0, Outcome.NEEDS_REVIEW),
])
def test_outcome_thresholds(config, value, outcome) -> None:
    assert outcome_for(_confidence(value), config) is outcome


def test_outcome_thresholds_come_from_config(config) -> None:
    strict = with_scoring(config, "confidence", scored_threshold=0.9, low_confidence_threshold=0.85)
    assert outcome_for(_confidence(0.88), strict) is Outcome.SCORED_LOW_CONFIDENCE
    assert outcome_for(_confidence(0.84), strict) is Outcome.NEEDS_REVIEW
    assert outcome_for(_confidence(0.88), config) is Outcome.SCORED


# ─── The components, from a real pair ──────────────────────────────────────────────────────


def _pair_confidence(resume: Resume, posting: JobPosting, graph, config) -> Confidence:
    prepared = prepare_resume(resume, graph, config, AS_OF)
    matches = match_requirements(prepared, posting.requirements, graph, config)
    aggregated = aggregate(score_dimensions(prepared, posting, graph, config), config)
    assert aggregated is not None
    # By keyword, with the brief's parameter names (`dimensions` is the pair's Aggregate).
    return compute_confidence(resume=resume, posting=posting, prepared=prepared, dimensions=aggregated,
                              matches=matches, config=config)


def _req(skill_id: str, necessity: Necessity = Necessity.REQUIRED, **extra) -> JobRequirement:
    return JobRequirement(skill_id=skill_id, necessity=necessity, **extra)


# Five distinct ids (0.85); python matched by a role bullet, sql only by the skills list (density
# 1/2); docker, git and excel unmatched (no relation in graph_small to anything the résumé holds).
SPEC_POSTING = JobPosting(
    seniority=IC1,
    min_years_total=1.0,
    requirements=(
        _req("skill.python"),
        _req("skill.sql"),
        _req("skill.docker", Necessity.PREFERRED),
        _req("skill.excel", Necessity.IMPLIED),
        _req("skill.git", Necessity.IMPLIED),
    ),
    requirement_token_count=10,
    unresolved_requirement_tokens=("quantum yodelling", "blockchain synergy"),  # (10 − 2) / 10 = 0.8
)
SPEC_RESUME = Resume(
    roles=(Role(start=DateValue.exact(date(2024, 1, 15)), is_current=True, seniority=IC1,
                skills=(bullet("skill.python"),)),),
    skills_blob=listed("skill.sql"),
)


def test_the_brief_example_from_a_real_pair(small_graph, config) -> None:
    # The posting names no industry, so §5.5 abstains: dimension coverage 1 − 0.10 = 0.9.
    conf = _pair_confidence(SPEC_RESUME, SPEC_POSTING, small_graph, config)
    expected = {"parse_quality": 1.0, "taxonomy_coverage": 0.8, "evidence_density": 0.5,
                "temporal_completeness": 1.0, "requirement_count": 0.85, "dimension_coverage": 0.9}
    assert set(conf.components) == set(expected)
    assert all(close(conf.components[name], value) for name, value in expected.items())
    assert close(conf.value, 0.306 ** (1 / 6)) and abs(conf.value - 0.820893) < 1e-6
    assert conf.reasons == (Flag.LOW_EVIDENCE_DENSITY,)
    assert outcome_for(conf, config) is Outcome.SCORED


@pytest.mark.parametrize("tier, expected", [(ParseTier.A, 1.0), (ParseTier.B, 0.8), (ParseTier.C, 0.55)])
def test_parse_quality_comes_from_the_tier(small_graph, config, tier, expected) -> None:
    resume = SPEC_RESUME.model_copy(update={"parse_tier": tier})
    conf = _pair_confidence(resume, SPEC_POSTING, small_graph, config)
    assert close(conf.components["parse_quality"], expected)


@pytest.mark.parametrize("tier, expected", [(ParseTier.A, 0.8), (ParseTier.B, 0.8), (ParseTier.C, 0.55)])
def test_a_truncated_posting_caps_parse_quality_at_tier_b(small_graph, config, tier, expected) -> None:
    # A stored description cut mid-text (Task 6.5, D-104): its requirements may go on past the cut.
    resume = SPEC_RESUME.model_copy(update={"parse_tier": tier})
    posting = SPEC_POSTING.model_copy(update={"flags": (Flag.POSTING_TRUNCATED,)})
    truncated = _pair_confidence(resume, posting, small_graph, config)
    assert close(truncated.components["parse_quality"], expected)
    whole = _pair_confidence(SPEC_RESUME, SPEC_POSTING, small_graph, config)
    if tier is ParseTier.A:
        assert truncated.value < whole.value
    assert Flag.POSTING_TRUNCATED in truncated.reasons and Flag.POSTING_TRUNCATED not in whole.reasons
    # a résumé page cap keeps its own TRUNCATED flag and never reaches the posting rule
    resume_capped = SPEC_POSTING.model_copy(update={"flags": (Flag.TRUNCATED,)})
    other = _pair_confidence(SPEC_RESUME, resume_capped, small_graph, config)
    assert close(other.components["parse_quality"], 1.0)


@pytest.mark.parametrize("count, unresolved, expected", [
    (0, (), None),  # no requirement tokens: undefined (D-44)
    (4, (), 1.0),
    (4, ("a",), 0.75),
    (4, ("a", "b", "c", "d"), 0.0),
    (2, ("a", "b", "c"), 0.0),  # more unresolved than counted: clamped, never negative
])
def test_taxonomy_coverage(small_graph, config, count, unresolved, expected) -> None:
    posting = SPEC_POSTING.model_copy(
        update={"requirement_token_count": count, "unresolved_requirement_tokens": unresolved}
    )
    conf = _pair_confidence(SPEC_RESUME, posting, small_graph, config)
    measured = conf.components["taxonomy_coverage"]
    assert measured is None if expected is None else close(measured, expected)


def test_evidence_density_counts_matched_groups_by_their_matching_skill(small_graph, config) -> None:
    # js_framework is met through react (CHILD, via react: a bullet); sql through the skills list;
    # docker through kubernetes (SIBLING, via kubernetes: a bullet); python not at all. 2 of 3.
    posting = JobPosting(requirements=(
        _req("skill.js_framework"), _req("skill.sql"), _req("skill.docker"), _req("skill.python"),
    ))
    resume = Resume(
        roles=(current_role(bullet("skill.react"), bullet("skill.kubernetes")),),
        skills_blob=listed("skill.sql"),
    )
    conf = _pair_confidence(resume, posting, small_graph, config)
    assert close(conf.components["evidence_density"], 2 / 3)


def test_a_listed_line_never_hides_a_bullet_on_the_same_skill(small_graph, config) -> None:
    # The review's probe: react shown in the current role; python shown by an ownership bullet in a
    # role that ended 2023-06-30, and also listed. The listed line has the higher E x R (0.65 x 1.0
    # beats 1.0 x 0.612), yet python is shown beyond the list (SPEC §17.1): density 1.0, as without
    # the listed line.
    posting = JobPosting(requirements=(_req("skill.python"), _req("skill.react")))
    current = Role(start=on(date(2024, 1, 15)), is_current=True, skills=(bullet("skill.react"),))
    old = Role(start=on(date(2020, 1, 1)), end=on(date(2023, 6, 30)), skills=(bullet("skill.python"),))
    for blob in (listed("skill.python"), ()):
        conf = _pair_confidence(Resume(roles=(current, old), skills_blob=blob), posting, small_graph, config)
        assert close(conf.components["evidence_density"], 1.0)
        assert Flag.LOW_EVIDENCE_DENSITY not in conf.reasons
    both = Resume(roles=(current, old), skills_blob=listed("skill.python"))
    prepared = prepare_resume(both, small_graph, config, AS_OF)
    assert prepared.best["skill.python"].evidence is Evidence.SKILLS_BLOB  # what the old rule read


def test_a_weak_verb_bullet_still_shows_the_skill(small_graph, config) -> None:
    # An assistance bullet (E 0.45) loses to the listed line (0.65) on value, yet shows the skill.
    posting = JobPosting(requirements=(_req("skill.python"), _req("skill.react")))
    role = current_role(bullet("skill.react"), bullet("skill.python", VerbClass.ASSISTANCE))
    resume = Resume(roles=(role,), skills_blob=listed("skill.python"))
    conf = _pair_confidence(resume, posting, small_graph, config)
    assert close(conf.components["evidence_density"], 1.0)


def test_an_aspirational_bullet_does_not_show_a_listed_skill(small_graph, config) -> None:
    # "Seeking to learn Python" is interest, not experience (SPEC §15.2): only affirmed mentions show
    # a skill. python counts as listed only, sql as shown: 1 of 2.
    posting = JobPosting(requirements=(_req("skill.python"), _req("skill.sql")))
    role = current_role(bullet("skill.python", polarity=Polarity.ASPIRATIONAL), bullet("skill.sql"))
    resume = Resume(roles=(role,), skills_blob=listed("skill.python"))
    conf = _pair_confidence(resume, posting, small_graph, config)
    assert close(conf.components["evidence_density"], 0.5)


def test_evidence_density_ignores_a_negated_only_skill(small_graph, config) -> None:
    posting = JobPosting(requirements=(_req("skill.python"), _req("skill.sql")))
    resume = Resume(roles=(current_role(bullet("skill.python", polarity=Polarity.NEGATED),
                                        bullet("skill.sql")),))
    # The negated python is worth 0: unmatched. sql via a bullet: 1 of 1.
    conf = _pair_confidence(resume, posting, small_graph, config)
    assert close(conf.components["evidence_density"], 1.0)


def test_evidence_density_is_undefined_when_nothing_matched(small_graph, config) -> None:
    posting = JobPosting(requirements=(_req("skill.python"),))
    resume = Resume(roles=(current_role(bullet("skill.excel")),))
    assert _pair_confidence(resume, posting, small_graph, config).components["evidence_density"] is None


def test_a_listed_only_resume_has_zero_evidence_density(small_graph, config) -> None:
    posting = JobPosting(requirements=(_req("skill.python"), _req("skill.sql")))
    resume = Resume(skills_blob=listed("skill.python", "skill.sql"))
    conf = _pair_confidence(resume, posting, small_graph, config)
    assert close(conf.components["evidence_density"], 0.0)
    assert Flag.LOW_EVIDENCE_DENSITY in conf.reasons


def test_project_bullets_are_evidence_beyond_the_list(small_graph, config) -> None:
    posting = JobPosting(requirements=(_req("skill.python"), _req("skill.sql")))
    resume = Resume(projects=(Project(skills=(bullet("skill.python"),)),), skills_blob=listed("skill.sql"))
    conf = _pair_confidence(resume, posting, small_graph, config)
    assert close(conf.components["evidence_density"], 0.5)
    assert conf.components["temporal_completeness"] is None  # no roles


def test_temporal_completeness_is_dated_roles_over_roles(small_graph, config) -> None:
    posting = JobPosting(requirements=(_req("skill.python"),))
    dated = Role(start=DateValue.exact(date(2020, 1, 1)), end=DateValue.exact(date(2021, 1, 1)))
    current = Role(start=DateValue.exact(date(2022, 1, 1)), is_current=True)
    no_end = Role(start=DateValue.exact(date(2019, 1, 1)))  # neither ends nor is current
    undated = Role()
    resume = Resume(roles=(dated, current, no_end, undated), skills_blob=listed("skill.python"))
    conf = _pair_confidence(resume, posting, small_graph, config)
    assert close(conf.components["temporal_completeness"], 2 / 4)


def test_requirement_count_counts_distinct_requirement_ids(small_graph, config) -> None:
    posting = JobPosting(requirements=(
        _req("skill.python"),
        _req("skill.python"),  # twice: one id
        _req("skill.react", or_group="or:1"),
        _req("skill.vue", or_group="or:1"),  # an OR-group's members each count
        _req("skill.git", Necessity.IMPLIED),
    ))
    conf = _pair_confidence(SPEC_RESUME, posting, small_graph, config)
    assert close(conf.components["requirement_count"], 0.40 + 0.45 * (4 - 1) / 4)


def test_an_all_preferred_posting_has_full_dimension_coverage(small_graph, config) -> None:
    # §14.5: required abstains (NO_REQUIRED_REQUIREMENTS) and its weight moves to preferred, which
    # counts as scored. Every other dimension scores here, so the coverage is 1.0, not 0.65.
    posting = JobPosting(
        seniority=IC1,
        min_years_total=1.0,
        industry_codes=("5415",),
        requirements=(_req("skill.python", Necessity.PREFERRED), _req("skill.git", Necessity.IMPLIED)),
    )
    resume = Resume(roles=(Role(start=DateValue.exact(date(2024, 1, 15)), is_current=True, seniority=IC1,
                                industry_codes=("541511",), skills=(bullet("skill.python"),)),))
    conf = _pair_confidence(resume, posting, small_graph, config)
    assert close(conf.components["dimension_coverage"], 1.0)


def test_dimension_coverage_is_the_scored_weight(small_graph, config) -> None:
    # Only the required skills and education score: 0.35 + 0.10.
    posting = JobPosting(requirements=(_req("skill.python"),))
    resume = Resume(skills_blob=listed("skill.python"))
    conf = _pair_confidence(resume, posting, small_graph, config)
    assert close(conf.components["dimension_coverage"], 0.35 + 0.10)
    assert Flag.LOW_DIMENSION_COVERAGE in conf.reasons


def test_compute_confidence_refuses_a_resume_prepared_for_another(small_graph, config) -> None:
    prepared = prepare_resume(SPEC_RESUME, small_graph, config, AS_OF)
    matches = match_requirements(prepared, SPEC_POSTING.requirements, small_graph, config)
    aggregated = aggregate(score_dimensions(prepared, SPEC_POSTING, small_graph, config), config)
    other = Resume(skills_blob=(ref("skill.go", Evidence.SKILLS_BLOB),))
    with pytest.raises(EngineFault) as err:
        compute_confidence(other, SPEC_POSTING, prepared, aggregated, matches, config)
    assert err.value.flag is Flag.INTERNAL_ERROR
    # An equal résumé (not the same object) is the same résumé.
    twin = Resume.model_validate(SPEC_RESUME.model_dump())
    compute_confidence(twin, SPEC_POSTING, prepared, aggregated, matches, config)


def test_compute_confidence_refuses_a_resume_prepared_under_another_config(small_graph, config) -> None:
    prepared = prepare_resume(SPEC_RESUME, small_graph, config, AS_OF)
    matches = match_requirements(prepared, SPEC_POSTING.requirements, small_graph, config)
    aggregated = aggregate(score_dimensions(prepared, SPEC_POSTING, small_graph, config), config)
    other = with_scoring(config, "confidence", weak_component=0.5)
    with pytest.raises(EngineFault) as err:
        compute_confidence(SPEC_RESUME, SPEC_POSTING, prepared, aggregated, matches, other)
    assert err.value.flag is Flag.INTERNAL_ERROR


def test_compute_confidence_refuses_a_match_the_resume_does_not_hold(small_graph, config) -> None:
    # A match computed for another résumé names a skill this one does not have.
    prepared = prepare_resume(SPEC_RESUME, small_graph, config, AS_OF)
    elsewhere = prepare_resume(Resume(skills_blob=listed("skill.go")), small_graph, config, AS_OF)
    posting = JobPosting(requirements=(_req("skill.go"),))
    matches = match_requirements(elsewhere, posting.requirements, small_graph, config)
    aggregated = aggregate(score_dimensions(prepared, posting, small_graph, config), config)
    with pytest.raises(EngineFault) as err:
        compute_confidence(SPEC_RESUME, posting, prepared, aggregated, matches, config)
    assert err.value.flag is Flag.INTERNAL_ERROR
