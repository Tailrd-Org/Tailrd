"""Score intervals (SPEC §17.3; DECISIONS D-45, D-11).

"Report a range, not a point ... re-running the scorer under the plausible interpretations of each
ambiguous input (imprecise dates at both range endpoints, unresolved skills counted as matched and
as missed)." Two extra runs (D-45):

* pessimistic: every imprecise date (season, quarter or year precision) at the bound that makes
  roles shortest and last use oldest (a start at its latest, an end at its earliest; where the two
  bounds cross, the role shrinks to a point at its oldest end), and each unresolved requirement
  token an extra REQUIRED requirement `unresolved:<normalized token>` scored as missed;
* optimistic: starts at their earliest, ends at their latest, the tokens scored as matched;
* both at IDF `intervals.unresolved_token_idf` (0.50). low/high = the min/max calibrated score over
  the base run and both variants; the range is displayed when it spans more than 6 points.

Expected values are closed-form arithmetic (EVAL §2.2).
"""

from __future__ import annotations

import math
from datetime import date

import pytest

from matcher.calibrate import FixedCalibrator
from matcher.errors import Flag
from matcher.models import (
    DatePrecision,
    DateValue,
    DimensionName,
    Education,
    DegreeLevel,
    JobPosting,
    JobRequirement,
    Necessity,
    Project,
    Resume,
    Role,
    ScoreInterval,
    canonical_json,
)
from matcher.score.dimensions import score_required_skills
from matcher.score.intervals import (
    UNRESOLVED_PREFIX,
    interval_of,
    required_with_unresolved,
    resume_at_bounds,
    score_interval,
    unresolved_requirement_ids,
    variant_raw,
)
from matcher.score.prepare import prepare_resume
from tests.builders import AS_OF, bullet, current_role, listed, recency, tiny_graph, with_scoring

U_IDF = 0.50  # intervals.unresolved_token_idf
GRAPH = tiny_graph({"skill.python": 0.6, "skill.sql": 0.4})
CALIBRATOR = FixedCalibrator()


def _close(a: float, b: float, tol: float = 1e-12) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


def year(y: int) -> DateValue:
    """A bare year: the parser's midpoint, bounded by the year (SPEC §14.2)."""
    return DateValue(value=date(y, 7, 2), earliest=date(y, 1, 1), latest=date(y, 12, 31),
                     precision=DatePrecision.YEAR)


def summer(y: int) -> DateValue:
    return DateValue(value=date(y, 8, 16), earliest=date(y, 7, 1), latest=date(y, 9, 30),
                     precision=DatePrecision.SEASON)


def month(y: int, m: int, last_day: int) -> DateValue:
    """A month: bounded by the month, yet not imprecise (D-11: seasons, quarters and years are)."""
    return DateValue(value=date(y, m, 1), earliest=date(y, m, 1), latest=date(y, m, last_day),
                     precision=DatePrecision.MONTH)


def at(value: DateValue, day: date) -> DateValue:
    """`value` moved to `day`, its bounds and precision kept."""
    return DateValue(value=day, earliest=value.earliest, latest=value.latest, precision=value.precision)


def _req(skill_id: str, necessity: Necessity = Necessity.REQUIRED) -> JobRequirement:
    return JobRequirement(skill_id=skill_id, necessity=necessity)


# ─── Résumé variants: imprecise dates at their bounds ──────────────────────────────────────


def test_the_pessimistic_resume_has_the_shortest_roles_and_the_oldest_last_use() -> None:
    role = Role(start=summer(2020), end=year(2022), skills=(bullet("skill.python"),))
    variant = resume_at_bounds(Resume(roles=(role,)), optimistic=False).roles[0]
    assert variant.start == at(summer(2020), date(2020, 9, 30))  # the start at its latest
    assert variant.end == at(year(2022), date(2022, 1, 1))  # the end at its earliest
    assert variant.skills == role.skills


def test_the_optimistic_resume_has_the_longest_roles_and_the_newest_last_use() -> None:
    role = Role(start=summer(2020), end=year(2022))
    variant = resume_at_bounds(Resume(roles=(role,)), optimistic=True).roles[0]
    assert variant.start == at(summer(2020), date(2020, 7, 1))
    assert variant.end == at(year(2022), date(2022, 12, 31))


def test_precise_dates_never_move() -> None:
    exact = Role(start=DateValue.exact(date(2019, 3, 4)), end=DateValue.exact(date(2021, 5, 6)))
    by_month = Role(start=month(2021, 6, 30), end=month(2022, 2, 28))
    resume = Resume(roles=(exact, by_month))
    for optimistic in (False, True):
        assert resume_at_bounds(resume, optimistic=optimistic) == resume


def test_bounds_that_cross_shrink_the_role_to_a_point_at_its_oldest() -> None:
    # "2020 – 2020": the start could be as late as the end could be early. Shortest: no time at all,
    # at the oldest moment both allow.
    role = Role(start=year(2020), end=year(2020))
    pessimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=False).roles[0]
    assert pessimistic.start == at(year(2020), date(2020, 1, 1))
    assert pessimistic.end == at(year(2020), date(2020, 1, 1))
    optimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=True).roles[0]
    assert (optimistic.start.value, optimistic.end.value) == (date(2020, 1, 1), date(2020, 12, 31))


def test_the_parser_shape_of_a_one_season_role() -> None:
    # parse/dates.py writes "Summer 2020" as a start bounded [Jul 1, Aug 16] and an end bounded
    # [Aug 16, Sep 30]: pessimistically a point at the midpoint, optimistically the whole season.
    start = DateValue(value=date(2020, 7, 1), earliest=date(2020, 7, 1), latest=date(2020, 8, 16),
                      precision=DatePrecision.SEASON)
    end = DateValue(value=date(2020, 9, 30), earliest=date(2020, 8, 16), latest=date(2020, 9, 30),
                    precision=DatePrecision.SEASON)
    role = Role(start=start, end=end, is_internship=True)
    pessimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=False).roles[0]
    assert (pessimistic.start.value, pessimistic.end.value) == (date(2020, 8, 16), date(2020, 8, 16))
    optimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=True).roles[0]
    assert (optimistic.start.value, optimistic.end.value) == (date(2020, 7, 1), date(2020, 9, 30))
    assert optimistic.is_internship


def test_an_end_bound_wider_than_the_start_is_held_at_the_start() -> None:
    # "Summer 2020 – 2020": parse/dates.py keeps both bounds and values the intersection (start Jul 1,
    # end Sep 30). The end could be as early as Jan 1, but never before the start's earliest, Jul 1.
    start = at(summer(2020), date(2020, 7, 1))
    end = at(year(2020), date(2020, 9, 30))
    role = Role(start=start, end=end)
    pessimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=False).roles[0]
    assert (pessimistic.start.value, pessimistic.end.value) == (date(2020, 7, 1), date(2020, 7, 1))
    assert pessimistic.end.earliest == date(2020, 1, 1)  # bounds stay as parsed
    optimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=True).roles[0]
    assert (optimistic.start.value, optimistic.end.value) == (date(2020, 7, 1), date(2020, 12, 31))


def test_a_precise_end_caps_a_late_start() -> None:
    start = DateValue(value=date(2021, 2, 1), earliest=date(2021, 1, 1), latest=date(2021, 12, 31),
                      precision=DatePrecision.YEAR)
    role = Role(start=start, end=DateValue.exact(date(2021, 3, 1)))
    pessimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=False).roles[0]
    assert (pessimistic.start.value, pessimistic.end.value) == (date(2021, 3, 1), date(2021, 3, 1))
    optimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=True).roles[0]
    assert (optimistic.start.value, optimistic.end.value) == (date(2021, 1, 1), date(2021, 3, 1))


def test_a_current_role_moves_only_its_start() -> None:
    role = Role(start=year(2023), is_current=True)
    pessimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=False).roles[0]
    optimistic = resume_at_bounds(Resume(roles=(role,)), optimistic=True).roles[0]
    assert pessimistic.start.value == date(2023, 12, 31) and optimistic.start.value == date(2023, 1, 1)
    assert pessimistic.is_current and pessimistic.end is None


def test_project_dates_move_like_role_dates() -> None:
    resume = Resume(projects=(Project(start=year(2020), end=year(2021)), Project(end=summer(2022))))
    pessimistic = resume_at_bounds(resume, optimistic=False).projects
    assert (pessimistic[0].start.value, pessimistic[0].end.value) == (date(2020, 12, 31), date(2021, 1, 1))
    assert pessimistic[1].start is None and pessimistic[1].end.value == date(2022, 7, 1)
    optimistic = resume_at_bounds(resume, optimistic=True).projects
    assert (optimistic[0].start.value, optimistic[0].end.value) == (date(2020, 1, 1), date(2021, 12, 31))
    assert optimistic[1].end.value == date(2022, 9, 30)


def test_everything_but_the_dates_is_kept() -> None:
    resume = Resume(
        roles=(Role(title_canonical="title.data_analyst", start=year(2019), end=year(2020),
                    industry_codes=("5415",), skills=(bullet("skill.sql"),)),),
        projects=(Project(name="Thesis", end=year(2021), skills=(bullet("skill.python"),)),),
        education=(Education(level=DegreeLevel.BACHELOR, field_ids=("field.cs",)),),
        skills_blob=listed("skill.python"),
        flags=(Flag.IMPRECISE_DATE,),
    )
    variant = resume_at_bounds(resume, optimistic=False)
    dated = {"roles", "projects"}
    assert variant.model_dump(exclude=dated) == resume.model_dump(exclude=dated)
    dates = {"start", "end"}
    assert variant.roles[0].model_dump(exclude=dates) == resume.roles[0].model_dump(exclude=dates)
    assert variant.projects[0].model_dump(exclude={"end"}) == resume.projects[0].model_dump(exclude={"end"})


def test_the_moved_end_dates_the_last_use(config) -> None:
    resume = Resume(roles=(Role(start=year(2024), end=year(2025), skills=(bullet("skill.python"),)),))
    oldest = prepare_resume(resume_at_bounds(resume, optimistic=False), GRAPH, config, AS_OF)
    newest = prepare_resume(resume_at_bounds(resume, optimistic=True), GRAPH, config, AS_OF)
    # Both above the 0.55 floor: 645 and 281 days before as_of.
    assert _close(oldest.best["skill.python"].recency_mult, recency((AS_OF - date(2025, 1, 1)).days))
    assert _close(newest.best["skill.python"].recency_mult, recency((AS_OF - date(2025, 12, 31)).days))
    assert oldest.best["skill.python"].recency_mult < newest.best["skill.python"].recency_mult


# ─── Posting variants: unresolved tokens as pseudo-requirements ─────────────────────────────


def test_unresolved_tokens_become_normalized_distinct_ids() -> None:
    posting = JobPosting(unresolved_requirement_tokens=(
        "Kubernetes Operators", "kubernetes   operators", "Fly.io", "  ", "",
    ))
    assert UNRESOLVED_PREFIX == "unresolved:"
    assert unresolved_requirement_ids(posting) == ("unresolved:fly.io", "unresolved:kubernetes operators")


def test_a_long_token_still_makes_a_valid_id() -> None:
    posting = JobPosting(unresolved_requirement_tokens=("x" * 1500,))
    (only,) = unresolved_requirement_ids(posting)
    assert only.startswith("unresolved:x") and len(only) <= 200


def test_unresolved_tokens_widen_the_required_coverage_closed_form(config) -> None:
    # Required: python (idf 0.6, matched by an ownership bullet in a current role: m = 1.0) and sql
    # (idf 0.4, missing). Two distinct unresolved tokens at idf 0.5 each.
    posting = JobPosting(
        requirements=(_req("skill.python"), _req("skill.sql")),
        unresolved_requirement_tokens=("Fly.io", "Kubernetes Operators"),
    )
    prepared = prepare_resume(Resume(roles=(current_role(bullet("skill.python")),)), GRAPH, config, AS_OF)
    base = score_required_skills(prepared, posting, GRAPH, config)
    missed = required_with_unresolved(prepared, posting, GRAPH, config, matched=False)
    matched = required_with_unresolved(prepared, posting, GRAPH, config, matched=True)
    assert _close(base.value, 0.6 / (0.6 + 0.4))
    assert _close(missed.value, 0.6 / (0.6 + 0.4 + 2 * U_IDF))  # 0.3
    assert _close(matched.value, (0.6 + 2 * U_IDF) / (0.6 + 0.4 + 2 * U_IDF))  # 0.8
    pseudo = [fact for fact in missed.facts if fact.subject.startswith("unresolved:")]
    assert [fact.code for fact in pseudo] == ["REQ_MISS", "REQ_MISS"]
    assert all(fact.detail["idf"] == U_IDF and fact.detail["necessity"] == "REQUIRED" for fact in pseudo)
    assert not any(fact.code == "CRITICAL_MISS" for fact in missed.facts)  # idf 0.5: never critical


def test_the_unresolved_idf_comes_from_config(config) -> None:
    tuned = with_scoring(config, "intervals", unresolved_token_idf=0.9)
    posting = JobPosting(requirements=(_req("skill.python"),), unresolved_requirement_tokens=("Fly.io",))
    prepared = prepare_resume(Resume(roles=(current_role(bullet("skill.python")),)), GRAPH, tuned, AS_OF)
    missed = required_with_unresolved(prepared, posting, GRAPH, tuned, matched=False)
    assert _close(missed.value, 0.6 / (0.6 + 0.9))
    # An unresolved token is as important as config says: at 0.9 a miss is a critical miss.
    assert [fact.subject for fact in missed.facts if fact.code == "CRITICAL_MISS"] == ["unresolved:fly.io"]


def test_without_unresolved_tokens_the_required_dimension_is_the_base_one(config) -> None:
    posting = JobPosting(requirements=(_req("skill.python"), _req("skill.sql")))
    prepared = prepare_resume(Resume(roles=(current_role(bullet("skill.python")),)), GRAPH, config, AS_OF)
    base = score_required_skills(prepared, posting, GRAPH, config)
    for matched in (False, True):
        assert required_with_unresolved(prepared, posting, GRAPH, config, matched=matched) == base


def test_a_posting_with_no_required_skill_gets_them_from_its_tokens(config) -> None:
    posting = JobPosting(requirements=(_req("skill.python", Necessity.PREFERRED),),
                         unresolved_requirement_tokens=("Fly.io", "Pulumi"))
    prepared = prepare_resume(Resume(roles=(current_role(bullet("skill.python")),)), GRAPH, config, AS_OF)
    base = score_required_skills(prepared, posting, GRAPH, config)
    assert base.abstain_reason is Flag.NO_REQUIRED_REQUIREMENTS
    missed = required_with_unresolved(prepared, posting, GRAPH, config, matched=False)
    matched = required_with_unresolved(prepared, posting, GRAPH, config, matched=True)
    assert missed.value == 0.0 and matched.value == 1.0
    assert missed.name is DimensionName.REQUIRED_SKILLS and not missed.abstained


# ─── The variants' raw scores and the interval ──────────────────────────────────────────────


INTERVAL_POSTING = JobPosting(
    requirements=(_req("skill.python"), _req("skill.sql")),
    unresolved_requirement_tokens=("Fly.io", "Kubernetes Operators"),
)
INTERVAL_RESUME = Resume(roles=(current_role(bullet("skill.python")),))
# Only §5.1 and §5.6 score (education 1.0: no requirement), so raw = (0.35 S + 0.10) / 0.45.


def _raw(s_required: float) -> float:
    return (0.35 * s_required + 0.10 * 1.0) / (0.35 + 0.10)


def _calibrate(raw: float) -> int:
    return CALIBRATOR.calibrate(raw)[0]


def test_variant_raw_scores_closed_form(config) -> None:
    pessimistic = variant_raw(INTERVAL_RESUME, INTERVAL_POSTING, as_of=AS_OF, config=config, graph=GRAPH,
                              optimistic=False)
    optimistic = variant_raw(INTERVAL_RESUME, INTERVAL_POSTING, as_of=AS_OF, config=config, graph=GRAPH,
                             optimistic=True)
    assert _close(pessimistic, _raw(0.6 / 2.0))  # 0.455556
    assert _close(optimistic, _raw(1.6 / 2.0))  # 0.844444


def test_score_interval_spans_the_base_and_both_variants(config) -> None:
    base = round(100 * _raw(0.6))  # 69
    interval = score_interval(base, INTERVAL_RESUME, INTERVAL_POSTING, as_of=AS_OF, config=config,
                              graph=GRAPH, calibrate=_calibrate)
    assert interval == ScoreInterval(low=round(100 * _raw(0.3)), high=round(100 * _raw(0.8)), display=True)
    assert (interval.low, interval.high) == (46, 84)


def test_nothing_ambiguous_gives_a_point(config) -> None:
    posting = JobPosting(requirements=(_req("skill.python"), _req("skill.sql")))
    base = round(100 * _raw(0.6))
    interval = score_interval(base, INTERVAL_RESUME, posting, as_of=AS_OF, config=config, graph=GRAPH,
                              calibrate=_calibrate)
    assert interval == ScoreInterval(low=base, high=base, display=False)


@pytest.mark.parametrize("scores, expected", [
    ((70, 64, 78), ScoreInterval(low=64, high=78, display=True)),
    ((70, 67, 73), ScoreInterval(low=67, high=73, display=False)),  # spread 6: not more than 6
    ((70, 66, 73), ScoreInterval(low=66, high=73, display=True)),  # spread 7
    ((55, 55, 55), ScoreInterval(low=55, high=55, display=False)),
    ((40, 52, 38), ScoreInterval(low=38, high=52, display=True)),  # a variant may land on either side
])
def test_interval_of(config, scores, expected) -> None:
    assert interval_of(scores, config) == expected


def test_the_display_spread_comes_from_config(config) -> None:
    wide = with_scoring(config, "intervals", display_spread=10)
    assert interval_of((70, 64, 78), wide).display is True  # 14 > 10
    assert interval_of((70, 60, 70), wide).display is False  # 10 is not more than 10


def test_intervals_are_deterministic(config) -> None:
    def run() -> str:
        interval = score_interval(69, INTERVAL_RESUME, INTERVAL_POSTING, as_of=AS_OF, config=config,
                                  graph=GRAPH, calibrate=_calibrate)
        return canonical_json(interval)

    assert run() == run()
