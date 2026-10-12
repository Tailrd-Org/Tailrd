"""Education entries (SPEC §5.6, §9; DECISIONS D-14, D-27, D-41; config/parse/sections.yaml).

Degree level from the degree words, English and French; fields of study are the `field.*` ids of
the degree line, read with `include_fields=True` so "Accounting" and "Statistics" are the FIELD
there (in prose they are skills). A degree name that is also a broad field ("Bachelor of
Science" -> field.science) counts only when the line names no specific field. `in_progress` from
"expected", "candidate", "in progress", "present", "current", "en cours". The institution and
every date stay out of the output (SPEC §5.6 forbids prestige and graduation year).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from matcher.graph.loader import load_resolver
from matcher.models import DegreeLevel, Education
from matcher.parse.config import load_parse_config
from matcher.parse.education import parse_education
from matcher.parse.sections import Section, SectionKind

TAXONOMY = Path(__file__).resolve().parents[1] / "data" / "taxonomy"
L = DegreeLevel


@pytest.fixture(scope="module")
def deps(config):
    return {"resolver": load_resolver(TAXONOMY, config), "parse_config": load_parse_config()}


def _parse(lines: list[str], deps) -> tuple[Education, ...]:
    section = Section(kind=SectionKind.EDUCATION, header="Education", lines=tuple(lines), start_line=0,
                      language=None)
    return parse_education(section, **deps)


def _summary(entries: tuple[Education, ...]) -> list[tuple]:
    return [(e.level, e.field_ids, e.in_progress) for e in entries]


CASES = [
    (["Bachelor of Science in Computer Science, Minor in Statistics — University of Ottawa | Sep 2021 – Expected Apr 2025"],
     [(L.BACHELOR, ("field.computer_science", "field.statistics"), True)]),
    (["B.Sc. Honours, Mathematics — McGill University, 2019 – 2023"],
     [(L.BACHELOR, ("field.mathematics",), False)]),
    (["BASc, Mechanical Engineering (Co-op) — University of Waterloo — 2020 – 2025"],
     [(L.BACHELOR, ("field.mechanical_engineering",), False)]),
    (["Bachelor of Commerce (Accounting), Toronto Metropolitan University"],  # the FIELD, not the skill
     [(L.BACHELOR, ("field.accounting",), False)]),
    (["BCom, Finance and Economics"], [(L.BACHELOR, ("field.finance", "field.economics"), False)]),
    (["MBA, Finance — Rotman School of Management, 2024 – Present"], [(L.MASTER, ("field.finance",), True)]),
    (["Master of Science in Data Science"], [(L.MASTER, ("field.data_science",), False)]),
    (["MEng, Electrical and Computer Engineering"], [(L.MASTER, ("field.electrical_engineering",), False)]),
    (["PhD Candidate, Physics — University of British Columbia"], [(L.DOCTORATE, ("field.physics",), True)]),
    (["Juris Doctor (JD), Osgoode Hall Law School"], [(L.DOCTORATE, ("field.law",), False)]),
    (["Diploma in Interior Design — Algonquin College, 2021"], [(L.ASSOCIATE, ("field.design",), False)]),
    (["Associate of Applied Science in Nursing"], [(L.ASSOCIATE, ("field.nursing",), False)]),
    (["Bachelor of Science in Nursing (BScN), Queen's University"], [(L.BACHELOR, ("field.nursing",), False)]),
    (["B.Sc."], [(L.BACHELOR, ("field.science",), False)]),  # a degree name alone keeps its broad field
    (["High School Diploma — Lisgar Collegiate Institute, 2019"], [(L.HIGH_SCHOOL, (), False)]),
    (["Ontario Secondary School Diploma (OSSD)"], [(L.HIGH_SCHOOL, (), False)]),
    # French
    (["Baccalauréat en génie logiciel, Polytechnique Montréal (en cours)"],
     [(L.BACHELOR, ("field.software_engineering",), True)]),
    (["Maîtrise en administration des affaires, HEC Montréal"], [(L.MASTER, ("field.business_admin",), False)]),
    (["Doctorat en informatique, Université Laval"], [(L.DOCTORATE, ("field.computer_science",), False)]),
    (["DEC en sciences de la nature — Cégep du Vieux Montréal"], [(L.ASSOCIATE, ("field.science",), False)]),
    (["AEC en programmation, Collège LaSalle"], [(L.ASSOCIATE, (), False)]),
    (["DES — École secondaire Jeanne-Mance, 2018"], [(L.HIGH_SCHOOL, (), False)]),
]


@pytest.mark.parametrize("lines, expected", CASES, ids=[case[0][0][:45] for case in CASES])
def test_degree_lines(deps, lines: list[str], expected: list[tuple]) -> None:
    assert _summary(_parse(lines, deps)) == expected


def test_a_student_education_section(deps) -> None:
    lines = [
        "University of British Columbia — Vancouver, BC",
        "Bachelor of Applied Science",
        "Electrical Engineering",
        "Sep 2021 – Present",
        "- Relevant Coursework: Signals and Systems, Statistics, Data Structures",
        "- Dean's Honour List (2022, 2023)",
        "Langara College",
        "Diploma in Computer Science, 2019 – 2021",
    ]
    entries = _parse(lines, deps)
    assert _summary(entries) == [
        (L.BACHELOR, ("field.electrical_engineering",), True),  # the field from the next line; "Present"
        (L.ASSOCIATE, ("field.computer_science",), False),
    ]


def test_coursework_and_award_lines_add_no_fields(deps) -> None:
    lines = ["BA, Psychology", "Relevant coursework: Statistics, Economics, Marketing", "Awards: Dean's List"]
    assert _summary(_parse(lines, deps)) == [(L.BACHELOR, ("field.psychology",), False)]


def test_field_raw_is_the_field_text_only(deps) -> None:
    (entry,) = _parse(["Bachelor of Science in Computer Science, Minor in Statistics — University of Ottawa | "
                       "Sep 2021 – Expected Apr 2025"], deps)
    assert entry.field_raw == "Computer Science, Statistics"
    for forbidden in ("University", "Ottawa", "2021", "2025", "Expected", "Sep"):
        assert forbidden not in entry.field_raw


def test_institution_and_dates_never_reach_the_model(deps) -> None:
    (entry,) = _parse(["Honours Bachelor of Arts, Economics — University of Toronto, Class of 2024, GPA 3.9"], deps)
    dumped = entry.model_dump_json()
    for forbidden in ("Toronto", "2024", "3.9", "University"):
        assert forbidden not in dumped
    assert set(Education.model_fields) == {"level", "field_ids", "field_raw", "in_progress"}


def test_a_university_program_without_a_degree_word_is_a_bachelors(deps) -> None:
    entries = _parse(["University of Toronto — Computer Science (Honours), Expected 2026"], deps)
    assert _summary(entries) == [(L.BACHELOR, ("field.computer_science",), True)]
    entries = _parse(["Seneca College", "Computer Engineering Technology, 2022 – 2024"], deps)
    assert [e.level for e in entries] == [L.ASSOCIATE]


@pytest.mark.parametrize(
    "lines",
    [
        ["Certificate in Project Management, University of Toronto School of Continuing Studies"],
        ["Exchange semester, Lund University, Sweden"],
        ["Proficient in MS Office and Excel"],
        ["Relevant coursework: Statistics, Data Structures"],
        ["Google Data Analytics Certificate (Coursera), 2024"],
        [],
        ["", "   "],
    ],
)
def test_not_degrees(deps, lines: list[str]) -> None:
    assert _parse(lines, deps) == ()


def test_a_month_is_not_a_degree(deps) -> None:
    entries = _parse(["Bachelor of Arts, Psychology — SEP 2019 – DEC 2023"], deps)
    assert _summary(entries) == [(L.BACHELOR, ("field.psychology",), False)]


def test_ms_before_a_product_is_not_a_masters(deps) -> None:
    entries = _parse(["BS in Information Systems; MS Office Specialist"], deps)
    assert [e.level for e in entries] == [L.BACHELOR]


def test_two_degrees_in_order(deps) -> None:
    entries = _parse(["MSc Statistics, University of Waterloo, 2024 – 2026",
                      "BSc Mathematics, Dalhousie University, 2020 – 2024"], deps)
    assert _summary(entries) == [(L.MASTER, ("field.statistics",), False), (L.BACHELOR, ("field.mathematics",), False)]


@given(st.lists(st.text(max_size=60), max_size=8))
def test_parse_education_is_total(lines: list[str]) -> None:
    from matcher.config import load_config

    deps = _TOTALITY_DEPS.get("deps")
    if deps is None:
        deps = _TOTALITY_DEPS["deps"] = {"resolver": load_resolver(TAXONOMY, load_config()),
                                         "parse_config": load_parse_config()}
    for entry in _parse(lines, deps):
        assert isinstance(entry, Education)
        assert all(field.startswith("field.") for field in entry.field_ids)


_TOTALITY_DEPS: dict = {}
