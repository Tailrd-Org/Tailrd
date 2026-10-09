"""§5.3 Relevant experience depth (Task 3.3).

SPEC §5.3, §9, §14.2, §14.3, §14.5; DECISIONS D-12, D-35, D-36, D-37. Every expected value is
closed-form arithmetic from the spec (EVAL §2.2): relevance is the cosine between the posting's
IDF vector and the role's IDF × bare-credit vector over every requirement id; months are the
weighted interval union in days / 30.4375 (365.25 / 12); the curve is 0.60 × r / 0.5 below the
knee, 0.60 + 0.40 × (r − 0.5) / 0.5 up to 1, then 1.00. IDF values are read from graph_small.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta
from typing import Optional

import pytest

from matcher.config import load_config
from matcher.errors import EngineFault, Flag
from matcher.models import (
    DatePrecision,
    DateValue,
    DimensionName,
    JobPosting,
    Necessity,
    Polarity,
    Project,
    Resume,
    Role,
    SeniorityLevel,
    Track,
    VerbClass,
)
from matcher.score.dimensions import score_experience
from matcher.score.dimensions.experience import experience_curve, relevant_months, role_relevance
from matcher.score.prepare import prepare_resume
from tests.builders import (
    AS_OF,
    CHILD_CREDIT,
    EXACT_CREDIT,
    PARENT_CREDIT,
    UNSEEN_IDF,
    bullet,
    listed,
    on,
    req,
    tiny_graph,
    with_scoring,
)

KNEE, AT_KNEE = 0.5, 0.60  # SPEC §5.3 curve
INTERNSHIP = 0.5  # SPEC §5.3: internships count at 0.5 duration weight
DAYS_PER_MONTH = 365.25 / 12  # = 30.4375
W_EXPERIENCE = 0.15  # SPEC §5.3
IC0, IC1 = SeniorityLevel(track=Track.IC, level=0), SeniorityLevel(track=Track.IC, level=1)
IC0_MONTHS, IC1_MONTHS = 6.0, 12.0  # DECISIONS D-37: "intern 6, IC1 12"
PYTHON = (bullet("skill.python"),)
PREFERRED = Necessity.PREFERRED


def _close(a: float, b: float, tol: float = 1e-12) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=tol)


def _last_day(year: int, month: int) -> date:
    return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def first_of(year: int, month: int) -> DateValue:
    """A month-precision start as the date parser produces it: the first day of the month."""
    return DateValue(value=date(year, month, 1), earliest=date(year, month, 1), latest=_last_day(year, month),
                     precision=DatePrecision.MONTH)


def end_of(year: int, month: int) -> DateValue:
    """A month-precision end as the date parser produces it: the last day of the month."""
    return DateValue(value=_last_day(year, month), earliest=date(year, month, 1), latest=_last_day(year, month),
                     precision=DatePrecision.MONTH)


def job(start: DateValue, end: Optional[DateValue], *skills, internship: bool = False,
        title: Optional[str] = None) -> Role:
    """A dated role; `end=None` is a current role."""
    return Role(start=start, end=end, is_current=end is None, is_internship=internship, skills=skills,
                title_canonical=title)


def wants(*requirements, years: Optional[float] = None, seniority: Optional[SeniorityLevel] = None) -> JobPosting:
    return JobPosting(requirements=requirements, min_years_total=years, seniority=seniority)


def _curve(ratio: float) -> float:
    """SPEC §5.3, written out."""
    if ratio < KNEE:
        return AT_KNEE * ratio / KNEE
    if ratio < 1.0:
        return AT_KNEE + (1.0 - AT_KNEE) * (ratio - KNEE) / (1.0 - KNEE)
    return 1.0


def _prepare(cv, graph, config):
    return prepare_resume(cv, graph, config, AS_OF)


def _score(cv, post, graph, config):
    return score_experience(_prepare(cv, graph, config), post, graph, config)


def _months(cv, post, graph, config) -> float:
    return relevant_months(_prepare(cv, graph, config), post, graph, config)


def _fact(dim, code: str):
    (fact,) = [f for f in dim.facts if f.code == code]
    return fact


# ─── Role relevance (DECISIONS D-35) ─────────────────────────────────────────────────────


def test_relevance_closed_form_from_the_brief(config) -> None:
    # r1 (idf 0.8) held exactly (credit 1.00); r2 (idf 0.6) only through its parent p (credit 0.40).
    graph = tiny_graph({"skill.r1": 0.8, "skill.r2": 0.6}, parents={"skill.r2": ("skill.p",)})
    role = Role(skills=(bullet("skill.r1"), bullet("skill.p")))
    post = wants(req("skill.r1"), req("skill.r2"))
    v_job = (0.8, 0.6)
    v_role = (0.8 * EXACT_CREDIT, 0.6 * PARENT_CREDIT)
    expected = (v_job[0] * v_role[0] + v_job[1] * v_role[1]) / (math.hypot(*v_role) * math.hypot(*v_job))
    # (0.64 + 0.144) / (sqrt(0.64 + 0.0576) × 1.0) = 0.784 / 0.8352245 = 0.9386698; the brief divides by
    # the rounded 0.835225 and prints 0.938669, so it is checked at the brief's own precision.
    assert _close(expected, 0.938669, tol=1e-6)
    assert _close(role_relevance(role, post, graph, config), expected)


def test_relevance_ignores_evidence_and_recency_and_reads_affirmed_mentions_only(small_graph, config) -> None:
    post = wants(req("skill.python"), req("skill.react"))
    i_p, i_r = small_graph.idf["skill.python"], small_graph.idf["skill.react"]
    expected = i_p * i_p / (math.hypot(i_p, i_r) * i_p)  # python held exactly, react not at all
    weak_and_old = Role(skills=(bullet("skill.python", VerbClass.EXPOSURE, last_used=date(2009, 1, 1)),))
    strong = Role(skills=(bullet("skill.python", VerbClass.OWNERSHIP, quantified=True),))
    blob_like = Role(skills=listed("skill.python"))
    for role in (weak_and_old, strong, blob_like):
        assert _close(role_relevance(role, post, small_graph, config), expected)
    # Negated and aspirational mentions are excluded; another, affirmed mention of the skill still counts.
    excluded = Role(skills=(bullet("skill.python", polarity=Polarity.NEGATED),
                            bullet("skill.react", polarity=Polarity.ASPIRATIONAL)))
    assert role_relevance(excluded, post, small_graph, config) == 0.0
    both = Role(skills=(bullet("skill.python", polarity=Polarity.NEGATED), bullet("skill.python")))
    assert _close(role_relevance(both, post, small_graph, config), expected)


def test_relevance_does_not_penalise_breadth(small_graph, config) -> None:
    # D-35: unrelated skills used in the same role never lower its relevance.
    post = wants(req("skill.python"), req("skill.django"))
    focused = Role(skills=(bullet("skill.python"),))
    broad = Role(skills=(bullet("skill.python"), *(bullet(s) for s in ("skill.excel", "skill.go", "skill.r",
                                                                       "skill.leadership", "skill.cpp"))))
    i_p, i_d = small_graph.idf["skill.python"], small_graph.idf["skill.django"]
    v_role = (i_p * EXACT_CREDIT, i_d * PARENT_CREDIT)  # python is django's parent
    expected = (i_p * v_role[0] + i_d * v_role[1]) / (math.hypot(*v_role) * math.hypot(i_p, i_d))
    assert _close(role_relevance(focused, post, small_graph, config), expected)
    assert _close(role_relevance(broad, post, small_graph, config), expected)


def test_relevance_takes_every_or_group_member_and_each_id_once(small_graph, config) -> None:
    post = wants(req("skill.python", or_group="language"), req("skill.go", or_group="language"),
                 req("skill.python"), req("skill.python", PREFERRED))
    i_p, i_g = small_graph.idf["skill.python"], small_graph.idf["skill.go"]
    expected = i_p * i_p / (math.hypot(i_p, i_g) * i_p)  # v_job = (i_python, i_go): python counted once
    assert _close(role_relevance(Role(skills=PYTHON), post, small_graph, config), expected)


def test_relevance_spans_requirements_of_every_necessity(small_graph, config) -> None:
    # The space is ALL requirement ids: a role that only shows the preferred and implied ones is relevant.
    post = wants(req("skill.python"), req("skill.sql", PREFERRED), req("skill.git", Necessity.IMPLIED))
    i_p, i_s, i_g = (small_graph.idf[s] for s in ("skill.python", "skill.sql", "skill.git"))
    role = Role(skills=(bullet("skill.sql"), bullet("skill.git")))
    expected = (i_s * i_s + i_g * i_g) / (math.hypot(i_s, i_g) * math.hypot(i_p, i_s, i_g))
    assert _close(role_relevance(role, post, small_graph, config), expected)


def test_relevance_credits_children_siblings_and_unseen_ids(small_graph, config) -> None:
    post = wants(req("skill.react"), req("skill.kubernetes"), req("skill.cobol_unlisted"))
    i_r, i_k = small_graph.idf["skill.react"], small_graph.idf["skill.kubernetes"]
    role = Role(skills=(bullet("skill.nextjs"), bullet("skill.docker")))
    # nextjs is a child of react (0.85); docker a 0.70 sibling of kubernetes; the unknown id takes
    # the unseen IDF (D-21) in the job vector and nothing in the role's.
    v_role = (i_r * CHILD_CREDIT, i_k * 0.70)
    expected = (i_r * v_role[0] + i_k * v_role[1]) / (math.hypot(*v_role) * math.hypot(i_r, i_k, UNSEEN_IDF))
    assert _close(role_relevance(role, post, small_graph, config), expected)


def test_relevance_is_zero_when_either_vector_is_zero(small_graph, config) -> None:
    unrelated = Role(skills=(bullet("skill.excel"),))
    assert role_relevance(unrelated, wants(req("skill.python")), small_graph, config) == 0.0
    assert role_relevance(Role(), wants(req("skill.python")), small_graph, config) == 0.0
    assert role_relevance(Role(skills=PYTHON), wants(), small_graph, config) == 0.0
    zero_idf = tiny_graph({"skill.common": 0.0})
    assert role_relevance(Role(skills=(bullet("skill.common"),)), wants(req("skill.common")), zero_idf, config) == 0.0


# ─── The brief's closed form and the curve (SPEC §5.3) ───────────────────────────────────


def test_two_years_fully_relevant_against_a_three_year_minimum(small_graph, config) -> None:
    cv = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *PYTHON, title="title.data_analyst"),))
    post = wants(req("skill.python"), years=3.0)
    months = (date(2021, 12, 31) - date(2020, 1, 1)).days / DAYS_PER_MONTH  # 730 / 30.4375 = 23.983573
    ratio = months / 36.0  # 0.666210
    expected = AT_KNEE + (1.0 - AT_KNEE) * (ratio - KNEE) / (1.0 - KNEE)  # 0.6 + 0.4 × 0.166210 / 0.5
    assert _close(months, 23.983573, tol=5e-7) and _close(ratio, 0.666210, tol=5e-7)
    assert _close(expected, 0.732968, tol=5e-7)

    assert _close(_months(cv, post, small_graph, config), months)
    dim = _score(cv, post, small_graph, config)
    assert dim.name is DimensionName.EXPERIENCE and dim.abstained is False and dim.abstain_reason is None
    assert _close(dim.value, expected)
    assert _close(dim.weight, W_EXPERIENCE) and _close(dim.effective_weight, W_EXPERIENCE)
    assert _close(dim.contribution, W_EXPERIENCE * expected)
    assert [f.code for f in dim.facts] == ["RELEVANT_MONTHS", "ROLE_RELEVANCE"]
    summary, role = dim.facts
    assert summary.subject == "POSTING_YEARS" and _close(summary.value, months)
    assert summary.detail == {"min_months": 36.0, "basis_id": ""}
    assert role.subject == "title.data_analyst" and _close(role.value, 1.0)
    assert role.detail == {"role": 0, "internship": False, "weight": role.value}


@pytest.mark.parametrize("ratio,value", [
    (0.0, 0.0), (0.25, 0.30), (0.5, 0.60), (0.75, 0.80), (1.0, 1.0), (3.0, 1.0), (-1.0, 0.0)])
def test_curve_points(config, ratio, value) -> None:
    # 0.25 → 0.6 × 0.25 / 0.5 = 0.30; 0.75 → 0.6 + 0.4 × 0.25 / 0.5 = 0.80; ≥ 1 saturates (never a deduction).
    assert _close(experience_curve(ratio, config), value)
    assert _close(_curve(max(0.0, ratio)), value)


def test_curve_follows_its_config(config) -> None:
    tuned = with_scoring(config, "experience", knee_ratio=0.4, value_at_knee=0.5)
    assert _close(experience_curve(0.2, tuned), 0.5 * 0.2 / 0.4)  # 0.25
    assert _close(experience_curve(0.7, tuned), 0.5 + 0.5 * (0.7 - 0.4) / (1.0 - 0.4))  # 0.75


def test_curve_is_continuous_and_monotone(config) -> None:
    ratios = [i / 1000 for i in range(0, 1501)]
    values = [experience_curve(r, config) for r in ratios]
    assert all(b >= a for a, b in zip(values, values[1:]))
    assert all(abs(b - a) < 0.0013 for a, b in zip(values, values[1:]))  # steepest slope 1.2 per unit


def test_overqualified_is_never_deducted(small_graph, config) -> None:
    # Nine fully relevant years against a three-year minimum: ratio ~3 → 1.00 (SPEC §5.3).
    cv = Resume(roles=(job(on(date(2017, 1, 1)), on(date(2026, 1, 1)), *PYTHON),))
    dim = _score(cv, wants(req("skill.python"), years=3.0), small_graph, config)
    assert _close(_fact(dim, "RELEVANT_MONTHS").value, (date(2026, 1, 1) - date(2017, 1, 1)).days / DAYS_PER_MONTH)
    assert dim.value == 1.0


# ─── Months: the interval sweep (DECISIONS D-36, SPEC §14.2) ─────────────────────────────


def test_two_identical_overlapping_roles_count_once(small_graph, config) -> None:
    post = wants(req("skill.python"), years=3.0)
    once = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *PYTHON),))
    twice = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *PYTHON),) * 2)
    months = 730 / DAYS_PER_MONTH
    assert _close(_months(twice, post, small_graph, config), months)
    assert _close(_score(twice, post, small_graph, config).value, _score(once, post, small_graph, config).value)


def test_an_internship_counts_at_half_weight(small_graph, config) -> None:
    cv = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *PYTHON, internship=True),))
    post = wants(req("skill.python"), years=3.0)
    months = INTERNSHIP * 730 / DAYS_PER_MONTH  # 11.991787
    ratio = months / 36.0  # 0.333105, below the knee
    expected = AT_KNEE * ratio / KNEE  # 0.399726
    assert _close(expected, 0.399726, tol=5e-7)
    assert _close(_months(cv, post, small_graph, config), months)
    dim = _score(cv, post, small_graph, config)
    assert _close(dim.value, expected)
    detail = _fact(dim, "ROLE_RELEVANCE").detail
    assert detail["role"] == 0 and detail["internship"] is True and _close(detail["weight"], INTERNSHIP)


def test_concurrent_consulting_is_a_union_that_never_exceeds_calendar_time(small_graph, config) -> None:
    # Twelve clients, each 400 days, starting every 30 days: every one starts before the previous
    # ends, so the union is the single window 2020-01-01 → 2021-12-31 (730 days), not 12 × 400.
    window_start, window_end = date(2020, 1, 1), date(2021, 12, 31)
    clients = []
    for k in range(12):
        start = window_start + timedelta(days=30 * k)
        clients.append(job(on(start), on(min(start + timedelta(days=400), window_end)), *PYTHON))
    cv = Resume(roles=tuple(clients))
    months = _months(cv, wants(req("skill.python"), years=3.0), small_graph, config)
    assert _close(months, (window_end - window_start).days / DAYS_PER_MONTH)
    assert months <= (window_end - window_start).days / DAYS_PER_MONTH + 1e-9


def test_each_segment_counts_once_at_the_best_weight_covering_it(small_graph, config) -> None:
    full = job(on(date(2020, 1, 1)), on(date(2021, 1, 1)), *PYTHON)
    intern = job(on(date(2020, 7, 1)), on(date(2021, 7, 1)), *PYTHON, internship=True)
    only_full = (date(2020, 7, 1) - date(2020, 1, 1)).days  # 182 days at 1.0
    both = (date(2021, 1, 1) - date(2020, 7, 1)).days  # 184 days: max(1.0, 0.5) = 1.0
    only_intern = (date(2021, 7, 1) - date(2021, 1, 1)).days  # 181 days at 0.5
    expected = (only_full * 1.0 + both * 1.0 + only_intern * INTERNSHIP) / DAYS_PER_MONTH  # 456.5 / 30.4375
    for roles in ((full, intern), (intern, full)):
        assert _close(_months(Resume(roles=roles), wants(req("skill.python")), small_graph, config), expected)


def test_a_less_relevant_overlapping_role_adds_only_its_own_time(small_graph, config) -> None:
    post = wants(req("skill.python"), req("skill.excel"))
    i_p, i_e = small_graph.idf["skill.python"], small_graph.idf["skill.excel"]
    relevance_excel = i_e * i_e / (math.hypot(i_p, i_e) * i_e)  # holds excel only
    a = job(on(date(2020, 1, 1)), on(date(2021, 1, 1)), bullet("skill.python"), bullet("skill.excel"))  # 1.0
    b = job(on(date(2020, 6, 1)), on(date(2021, 6, 1)), bullet("skill.excel"))
    days_a = (date(2021, 1, 1) - date(2020, 1, 1)).days  # 366
    days_b_alone = (date(2021, 6, 1) - date(2021, 1, 1)).days  # 151
    expected = (days_a * 1.0 + days_b_alone * relevance_excel) / DAYS_PER_MONTH
    assert _close(_months(Resume(roles=(a, b)), post, small_graph, config), expected)


def test_gaps_between_roles_are_neither_subtracted_nor_penalised(small_graph, config) -> None:
    # SPEC §9: the same two roles, back to back or years apart, give the same months and value.
    post = wants(req("skill.python"), years=3.0)

    def pair(gap_days: int) -> Resume:
        first_start = date(2012, 3, 1)
        first_end = first_start + timedelta(days=300)
        second_start = first_end + timedelta(days=gap_days)
        return Resume(roles=(job(on(first_start), on(first_end), *PYTHON),
                             job(on(second_start), on(second_start + timedelta(days=200)), *PYTHON)))

    expected = (300 + 200) / DAYS_PER_MONTH
    scores = set()
    for gap in (0, 1, 90, 400, 2000):
        assert _close(_months(pair(gap), post, small_graph, config), expected)
        scores.add(_score(pair(gap), post, small_graph, config).value)
    assert len(scores) == 1


def test_current_roles_end_at_as_of_and_future_ends_are_clamped(small_graph, config) -> None:
    post = wants(req("skill.python"))
    current = Resume(roles=(job(on(date(2024, 10, 8)), None, *PYTHON),))
    assert _close(_months(current, post, small_graph, config), (AS_OF - date(2024, 10, 8)).days / DAYS_PER_MONTH)
    future_end = Resume(roles=(job(on(date(2025, 10, 8)), on(date(2027, 6, 30)), *PYTHON),))
    assert _close(_months(future_end, post, small_graph, config), (AS_OF - date(2025, 10, 8)).days / DAYS_PER_MONTH)
    not_started = Resume(roles=(job(on(date(2026, 12, 1)), on(date(2027, 6, 30)), *PYTHON),))
    assert _months(not_started, post, small_graph, config) == 0.0


def test_undated_roles_and_projects_add_no_months(small_graph, config) -> None:
    post = wants(req("skill.python"), years=1.0)
    dated = job(first_of(2020, 1), end_of(2021, 12), *PYTHON)
    cv = Resume(roles=(dated, Role(skills=PYTHON), Role(start=on(date(2015, 1, 1)), skills=PYTHON)),
                projects=(Project(skills=PYTHON, start=on(date(2010, 1, 1)), end=on(date(2019, 1, 1))),))
    assert _close(_months(cv, post, small_graph, config), 730 / DAYS_PER_MONTH)
    assert [f.detail["role"] for f in _score(cv, post, small_graph, config).facts if f.code == "ROLE_RELEVANCE"] == [0]


# ─── The minimum (DECISIONS D-37) ────────────────────────────────────────────────────────


def test_minimum_from_total_years_then_required_skill_years_then_seniority(small_graph, config) -> None:
    covering = (bullet("skill.python"), bullet("skill.go"), bullet("skill.react"))  # relevance 1.0 below
    cv = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *covering),))
    months = 730 / DAYS_PER_MONTH  # 23.983573
    years_req = (req("skill.python"), req("skill.go", PREFERRED), req("skill.react"))

    def with_years(**years) -> tuple:
        return tuple(r.model_copy(update={"min_years": years.get(r.skill_id.split(".")[1])}) for r in years_req)

    # 1. min_years_total wins over everything: 12 × 2 = 24 months.
    dim = _score(cv, wants(*with_years(python=4), years=2.0, seniority=IC1), small_graph, config)
    assert _close(dim.value, _curve(months / 24.0))
    assert _fact(dim, "RELEVANT_MONTHS").subject == "POSTING_YEARS"
    # 2. Else the largest REQUIRED per-skill minimum: python 4 and react 1 → 48 (go's 10 is PREFERRED).
    dim = _score(cv, wants(*with_years(python=4, react=1, go=10), seniority=IC1), small_graph, config)
    assert _close(dim.value, _curve(months / 48.0))  # 0.499658 → 0.6 × 0.499658 / 0.5
    summary = _fact(dim, "RELEVANT_MONTHS")
    assert summary.subject == "REQUIREMENT_YEARS" and summary.detail == {"min_months": 48.0, "basis_id": "skill.python"}
    assert "MIN_YEARS_DEFAULTED" not in [f.code for f in dim.facts]
    # 3. Else the posting's seniority default (IC1: 12 months), flagged MIN_YEARS_DEFAULTED.
    short = Resume(roles=(job(first_of(2020, 1), end_of(2020, 6), *covering),))  # 181 days
    dim = _score(short, wants(*years_req, seniority=IC1), small_graph, config)
    assert _close(dim.value, _curve(181 / DAYS_PER_MONTH / IC1_MONTHS))  # 5.946612 / 12 → 0.594661
    defaulted = _fact(dim, "MIN_YEARS_DEFAULTED")
    assert defaulted.subject == "IC1" and _close(defaulted.value, IC1_MONTHS)
    assert _fact(dim, "RELEVANT_MONTHS").detail == {"min_months": IC1_MONTHS, "basis_id": "IC1"}


def test_a_stated_minimum_of_zero_is_met_by_anyone(small_graph, config) -> None:
    for cv in (Resume(), Resume(roles=(job(first_of(2020, 1), end_of(2020, 3), bullet("skill.excel")),))):
        dim = _score(cv, wants(req("skill.python"), years=0.0), small_graph, config)
        assert dim.value == 1.0 and dim.abstained is False


# ─── No employment, and the abstentions (SPEC §14.2, §14.3, §14.5) ────────────────────────


def test_no_roles_at_all_scores_zero_not_unknown(small_graph, config) -> None:
    # D-37: they have none. Projects and a skills section never count as employment (D-12).
    cv = Resume(projects=(Project(skills=PYTHON, start=on(date(2021, 1, 1)), end=on(date(2023, 1, 1))),),
                skills_blob=listed("skill.python"))
    dim = _score(cv, wants(req("skill.python"), seniority=IC0), small_graph, config)
    assert dim.abstained is False and dim.value == 0.0 and dim.contribution == 0.0
    assert _close(dim.effective_weight, W_EXPERIENCE)
    assert [f.code for f in dim.facts] == ["RELEVANT_MONTHS", "MIN_YEARS_DEFAULTED", "NO_WORK_EXPERIENCE"]
    assert _fact(dim, "RELEVANT_MONTHS").value == 0.0
    assert _close(_fact(dim, "MIN_YEARS_DEFAULTED").value, IC0_MONTHS)


def test_zero_relevant_months_is_a_valid_zero(small_graph, config) -> None:
    cv = Resume(roles=(job(first_of(2018, 1), end_of(2021, 12), bullet("skill.excel")),))
    dim = _score(cv, wants(req("skill.python"), years=2.0), small_graph, config)
    assert dim.abstained is False and dim.value == 0.0
    assert _fact(dim, "ROLE_RELEVANCE").value == 0.0


def _assert_abstains(dim, reason: Flag) -> None:
    assert dim.abstained and dim.value is None and dim.abstain_reason is reason
    assert _close(dim.weight, W_EXPERIENCE) and dim.effective_weight == 0.0 and dim.contribution == 0.0
    assert dim.facts == ()


def test_abstains_when_roles_exist_but_none_is_dated(small_graph, config) -> None:
    cv = Resume(roles=(Role(skills=PYTHON), Role(start=on(date(2019, 1, 1)), skills=PYTHON),
                       Role(end=on(date(2020, 1, 1)), skills=PYTHON)))
    _assert_abstains(_score(cv, wants(req("skill.python"), years=2.0), small_graph, config), Flag.DATES_MISSING)


def test_abstains_on_a_functional_resume(small_graph, config) -> None:
    cv = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *PYTHON),), flags=(Flag.FUNCTIONAL_RESUME,))
    _assert_abstains(_score(cv, wants(req("skill.python"), years=2.0), small_graph, config), Flag.FUNCTIONAL_RESUME)
    _assert_abstains(_score(Resume(flags=(Flag.FUNCTIONAL_RESUME,)), wants(req("skill.python"), years=2.0),
                            small_graph, config), Flag.FUNCTIONAL_RESUME)


def test_abstains_without_years_or_seniority(small_graph, config) -> None:
    dated = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *PYTHON),))
    preferred_years = wants(req("skill.python", PREFERRED).model_copy(update={"min_years": 3}))
    for cv in (dated, Resume()):
        for post in (wants(req("skill.python")), preferred_years):
            _assert_abstains(_score(cv, post, small_graph, config), Flag.NO_EXPERIENCE_REQUIREMENT)


def test_abstains_when_the_posting_has_no_requirement_space(small_graph, config) -> None:
    # Relevance is undefined against an empty or all-zero-IDF requirement vector: "we don't know",
    # not "they have none" (SPEC §14.5), the same reason the coverage dimensions give.
    dated = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *PYTHON),))
    _assert_abstains(_score(dated, wants(years=2.0), small_graph, config), Flag.NO_REQUIREMENTS_RESOLVED)
    zero_idf = tiny_graph({"skill.common": 0.0})
    cv = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), bullet("skill.common")),))
    _assert_abstains(_score(cv, wants(req("skill.common"), years=2.0), zero_idf, config),
                     Flag.NO_REQUIREMENTS_RESOLVED)
    # Without any role the months are 0 whatever the requirements: still a valid zero.
    assert _score(Resume(), wants(years=2.0), small_graph, config).value == 0.0


# ─── Determinism, numeric safety, binding ────────────────────────────────────────────────


def test_value_does_not_depend_on_role_or_requirement_order(small_graph, config) -> None:
    roles = [job(on(date(2016, 1, 1)), on(date(2018, 6, 1)), bullet("skill.django"), bullet("skill.sql")),
             job(on(date(2018, 1, 1)), on(date(2020, 1, 1)), bullet("skill.python"), internship=True),
             job(on(date(2019, 6, 1)), None, bullet("skill.react"), bullet("skill.nextjs")),
             job(on(date(2021, 1, 1)), on(date(2022, 1, 1)), bullet("skill.excel"))]
    requirements = [req("skill.python"), req("skill.react"), req("skill.sql", PREFERRED),
                    req("skill.go", or_group="g"), req("skill.django", or_group="g")]
    reference = _score(Resume(roles=tuple(roles)), wants(*requirements, years=4.0), small_graph, config)
    rng = random.Random(7)
    for _ in range(20):
        rng.shuffle(roles)
        rng.shuffle(requirements)
        again = _score(Resume(roles=tuple(roles)), wants(*requirements, years=4.0), small_graph, config)
        assert again.value == reference.value
        assert _fact(again, "RELEVANT_MONTHS") == _fact(reference, "RELEVANT_MONTHS")
        assert sorted(f.value for f in again.facts) == sorted(f.value for f in reference.facts)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_input_raises_a_numeric_fault(small_graph, config, bad) -> None:
    with pytest.raises(EngineFault) as err:
        experience_curve(bad, config)
    assert err.value.flag is Flag.NUMERIC_FAULT
    post = JobPosting.model_construct(requirements=(req("skill.python"),), min_years_total=bad)
    cv = Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *PYTHON),))
    with pytest.raises(EngineFault) as err:
        _score(cv, post, small_graph, config)
    assert err.value.flag is Flag.NUMERIC_FAULT


def test_a_prepared_resume_is_bound_to_its_graph_and_config(small_graph, config) -> None:
    prepared = _prepare(Resume(roles=(job(first_of(2020, 1), end_of(2021, 12), *PYTHON),)), small_graph, config)
    calls = [
        lambda g, c: score_experience(prepared, wants(req("skill.python"), years=2.0), g, c),
        lambda g, c: score_experience(prepared, wants(), g, c),  # even where it would abstain
        lambda g, c: relevant_months(prepared, wants(req("skill.python")), g, c),
    ]
    other_graph = tiny_graph({"skill.python": 0.5})
    other_config = with_scoring(config, "experience", internship_weight=0.25)
    for call in calls:
        call(small_graph, config)
        call(small_graph, load_config())  # an equal config loaded again is the same config
        for graph, cfg in ((other_graph, config), (small_graph, other_config)):
            with pytest.raises(EngineFault) as err:
                call(graph, cfg)
            assert err.value.flag is Flag.INTERNAL_ERROR


def test_values_are_clamped_and_facts_are_plain_scalars(small_graph, config) -> None:
    cv = Resume(roles=(job(on(date(2000, 1, 1)), None, *PYTHON), job(on(date(2010, 1, 1)), None, *PYTHON)))
    dim = _score(cv, wants(req("skill.python"), years=0.5), small_graph, config)
    assert dim.value == 1.0 and 0.0 <= dim.contribution <= 1.0
    for fact in dim.facts:
        assert all(type(v) in (str, int, float, bool, type(None)) for v in fact.detail.values())
