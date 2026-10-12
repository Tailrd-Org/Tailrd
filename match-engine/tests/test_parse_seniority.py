"""Title seniority (SPEC §5.4 rules table, config/parse/seniority.yaml; DECISIONS D-38).

Two ladders: IC0 (intern, co-op, student; Tailrd) to IC6 and M1 to M6. The first rule that
matches wins, so a student marker beats everything ("Product Manager Intern" is IC0) and a
management word beats an IC modifier ("Senior Manager" is M3). Student jobs are IC0 so a
student's current campus job never leaves seniority unknown (Task 3.3 review). A title with no
modifier takes the posting metadata level (`meta_level`) or stays unknown.
"""

from __future__ import annotations

from typing import Optional

import pytest

from matcher.models import SeniorityLevel, Track
from matcher.parse.config import load_parse_config
from matcher.parse.seniority import (
    is_internship_text,
    is_student_organisation,
    strip_seniority_modifiers,
    title_seniority,
)


@pytest.fixture(scope="module")
def pc():
    return load_parse_config()


def _code(level: Optional[SeniorityLevel]) -> Optional[str]:
    return None if level is None else level.code


CASES = [
    # IC0: interns, co-ops, students, apprentices (fr: stagiaire)
    ("Software Engineering Intern", "IC0"),
    ("Data Analyst Co-op", "IC0"),
    ("Co-op Student, Finance", "IC0"),
    ("Software Developer (Coop)", "IC0"),
    ("Summer Student", "IC0"),
    ("Stagiaire en développement logiciel", "IC0"),
    ("Électricien apprenti", "IC0"),
    ("Electrician Apprentice", "IC0"),
    ("Product Manager Intern", "IC0"),  # the student marker beats "manager"
    ("Work-Study Student Assistant", "IC0"),
    # IC0: student jobs (Task 3.3 review)
    ("Teaching Assistant – CS 101", "IC0"),
    ("TA, Linear Algebra", "IC0"),
    ("Research Assistant", "IC0"),
    ("Graduate Research Assistant", "IC0"),
    ("Undergraduate RA, Psychology Lab", "IC0"),
    ("Lab Assistant", "IC0"),
    ("Math Tutor", "IC0"),
    ("Camp Counsellor", "IC0"),
    ("Camp Counselor", "IC0"),
    ("Student Ambassador", "IC0"),
    ("Peer Mentor", "IC0"),
    ("Residence Don", "IC0"),
    ("Don", "IC0"),
    ("Auxiliaire d'enseignement", "IC0"),
    # IC1
    ("Junior Developer", "IC1"),
    ("Jr. Data Analyst", "IC1"),
    ("Entry-Level Accountant", "IC1"),
    ("Associate Software Engineer", "IC1"),
    ("Graduate Engineer", "IC1"),
    ("New Grad Software Engineer", "IC1"),
    ("Software Engineer I", "IC1"),
    ("Analyst 1", "IC1"),
    ("Software Engineer (L3)", "IC1"),
    ("SDE I", "IC1"),
    ("Staff Accountant", "IC1"),  # an accounting firm's entry title, not a staff engineer
    ("Management Trainee", "IC1"),
    # IC2
    ("Software Engineer II", "IC2"),
    ("Developer 2", "IC2"),
    ("Intermediate Developer", "IC2"),
    ("L4 Software Engineer", "IC2"),
    ("SDE II", "IC2"),
    # IC3
    ("Senior Software Engineer", "IC3"),
    ("Sr. Product Designer", "IC3"),
    ("Software Engineer III", "IC3"),
    ("Engineer 3", "IC3"),
    ("Software Engineer, L5", "IC3"),
    ("SDE III", "IC3"),
    ("Analyste principal", "IC3"),  # French post-nominal "principal" means senior
    ("Développeur sénior", "IC3"),
    # IC4
    ("Staff Software Engineer", "IC4"),
    ("L6 Engineer", "IC4"),
    ("Tech Lead", "IC4"),
    ("Technical Lead, Payments", "IC4"),
    ("Lead Software Engineer", "IC4"),
    ("Lead Developer", "IC4"),
    # IC5, IC6
    ("Principal Engineer", "IC5"),
    ("Software Engineer, L7", "IC5"),
    ("L8", "IC5"),
    ("Distinguished Engineer", "IC6"),
    ("Fellow", "IC0"),  # a bare fellowship is a student program (fix round 1)
    ("Postdoctoral Fellow", "IC1"),  # a postdoc, not a distinguished fellow
    # M1
    ("Team Lead, Customer Support", "M1"),
    ("Shift Lead", "M1"),
    ("Lead", "M1"),
    ("Supervisor", "M1"),
    ("Assistant Store Manager", "M1"),
    ("Chef d'équipe", "M1"),
    # M2
    ("Engineering Manager", "M2"),
    ("Product Manager", "M2"),
    ("Gestionnaire de projet", "M2"),
    ("Co-op Program Manager", "M2"),  # staff who run a co-op program are not co-op students
    ("Stage Manager", "M2"),  # theatre; "stage" is French for internship only in French titles
    # M3
    ("Senior Manager, Data", "M3"),
    ("Sr. Engineering Manager", "M3"),
    # M4
    ("Director of Engineering", "M4"),
    ("Head of Product", "M4"),
    ("Directrice des ventes", "M4"),
    ("Director of Student Life", "M4"),
    # M5
    ("VP Engineering", "M5"),
    ("Vice President, Sales", "M5"),
    ("Vice-présidente, Marketing", "M5"),
    ("SVP, Operations", "M5"),
    # M6
    ("Chief Technology Officer", "M6"),
    ("CTO", "M6"),
    ("Co-Founder & CEO", "M6"),
    ("President", "M6"),
    ("Président-directeur général", "M6"),
    # No modifier, or a word that only looks like one
    ("Software Engineer", None),
    ("Marketing Coordinator", None),
    ("Internship Coordinator", None),
    ("Student Services Advisor", None),
    ("Internal Auditor", None),
    ("Python 3 Developer", None),  # a version, not a level: numbers count only at the end
    ("Windows 10 Support Technician", None),
    ("Founder", None),
    ("", None),
]


@pytest.mark.parametrize("title, expected", CASES, ids=[f"{t or '<empty>'}" for t, _ in CASES])
def test_title_seniority(pc, title: str, expected: Optional[str]) -> None:
    assert _code(title_seniority(title, pc)) == expected


@pytest.mark.parametrize(
    "title, expected",
    [("Treasurer, Students' Union", "IC0"), ("Captain, Varsity Soccer Team", "IC0"),
     # An executive title at a student organisation abstains (fix round 1 ruling).
     ("President, Computer Science Club", None), ("VP Finance, Engineering Society", None),
     ("Présidente, Association étudiante de génie", None)],
)
def test_student_organisation_titles(pc, title: str, expected) -> None:
    assert _code(title_seniority(title, pc)) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Robotics Club", True),
        ("UBC Engineering Undergraduate Society", True),
        ("Association étudiante de l'ÉTS", True),
        ("Golf Club of Ottawa", False),  # a business
        ("Boys and Girls Club of Canada", False),
        ("Canadian Cancer Society", False),
        ("Shopify", False),
        ("", False),
    ],
)
def test_is_student_organisation(pc, text: str, expected: bool) -> None:
    assert is_student_organisation(text, pc) is expected


@pytest.mark.parametrize(
    "meta_level, expected",
    [("internship", "IC0"), ("new_grad", "IC1"), ("Internship", "IC0"), ("entry_level", "IC1"), ("", None),
     (None, None), ("unknown", None)],
)
def test_meta_level_applies_only_without_a_modifier(pc, meta_level: Optional[str], expected: Optional[str]) -> None:
    assert _code(title_seniority("Software Engineer", pc, meta_level=meta_level)) == expected
    assert _code(title_seniority("Senior Software Engineer", pc, meta_level=meta_level)) == "IC3"


def test_levels_are_models_on_their_track(pc) -> None:
    assert title_seniority("Senior Manager", pc) == SeniorityLevel(track=Track.MGMT, level=3)
    assert title_seniority("Junior Developer", pc) == SeniorityLevel(track=Track.IC, level=1)


@pytest.mark.parametrize(
    "title, stripped",
    [
        ("Senior Software Engineer", "Software Engineer"),
        ("Software Engineer II", "Software Engineer"),
        ("Jr. Data Analyst", "Data Analyst"),
        ("Software Developer (Co-op)", "Software Developer"),
        ("Lead Software Engineer", "Software Engineer"),
        ("Entry-Level Accountant", "Accountant"),
        ("Teaching Assistant", "Teaching Assistant"),  # an occupation, never stripped
        ("Engineering Manager", "Engineering Manager"),
        ("Intern", ""),
    ],
)
def test_strip_seniority_modifiers(pc, title: str, stripped: str) -> None:
    assert strip_seniority_modifiers(title, pc) == stripped


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Software Engineering Intern", True),
        ("Marketing Internship", True),
        ("Data Analyst (Co-op)", True),
        ("Co-op Work Term 2", True),
        ("Stagiaire en finance", True),
        ("Stage en développement web", True),
        ("Analyste de données (stage)", True),
        ("Stage Manager", False),
        ("Stagehand", False),
        ("Internal Auditor", False),
        ("Software Engineer", False),
        ("", False),
    ],
)
def test_is_internship_text(pc, text: str, expected: bool) -> None:
    assert is_internship_text(text, pc) is expected


# ─── fix round 1 ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title, expected",
    [
        ("MLH Fellow", "IC0"), ("Data Science Fellow", "IC0"), ("Fellowship", "IC0"),
        ("Distinguished Fellow", "IC6"), ("Technical Fellow", "IC6"), ("Senior Fellow", "IC6"),
        ("Associate Product Manager", "IC1"), ("APM", "IC1"),
        ("Crew Chief", "M1"), ("Section Chief", "M1"),
        ("President, Women in STEM", None), ("President, UofT Hacks", None),
        ("VP Finance, Enactus Laurier", None), ("Director of Sponsorship, Hack the North", None),
        ("President, Computer Science Club", None), ("Treasurer, Students' Union", "IC0"),
    ],
)
def test_fix_round_1_titles(pc, title: str, expected) -> None:
    assert _code(title_seniority(title, pc)) == expected


def test_every_multi_word_entry_beats_its_single_word_head(pc) -> None:
    for rule in pc.seniority.rules:
        if rule.whole or rule.at_end or rule.preceded_by or rule.followed_within or rule.with_any:
            continue
        for phrase in rule.phrases:
            if len(phrase.split()) < 2:
                continue
            got = title_seniority(phrase, pc)
            assert got is not None and got.code == rule.level, (phrase, rule.level, got)
