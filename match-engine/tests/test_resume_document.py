"""`resume_from_document`: Tailrd's stored résumé document into the engine's `Resume` (DECISIONS D-80).

The document is what the user sees and edits: `experience`, `education`, `projects`, `skills`,
`technologies`, `custom_sections`, `summary` (and `summary_title`, a heading), `location`. School,
GPA and dates of a degree never reach the output; a degree's dates only tell whether it is in
progress, as in the text path. A document without sections falls back to its `raw_text`. The
document below is invented and shaped like `backend/schemas/resume.py`'s `ResumeProfile`.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from matcher.errors import Flag
from matcher.graph.loader import load_resolver
from matcher.models import DegreeLevel, Evidence, Location, Polarity, SkillSource, VerbClass, canonical_json
from matcher.parse.config import load_parse_config
from matcher.parse.resume import load_resume_resources, parse_resume_text, resume_from_document

AS_OF = date(2026, 10, 9)
TAXONOMY = Path(__file__).resolve().parents[1] / "data" / "taxonomy"


@pytest.fixture(scope="module")
def deps(config):
    parse_config = load_parse_config()
    return {
        "resolver": load_resolver(TAXONOMY, config),
        "config": config,
        "parse_config": parse_config,
        "resources": load_resume_resources(TAXONOMY, parse_config=parse_config),
    }


def from_doc(doc, deps, **kwargs):
    kwargs.setdefault("strict", True)
    return resume_from_document(doc, as_of=AS_OF, **deps, **kwargs)


def ids(refs, **where) -> set[str]:
    return {r.canonical_id for r in refs if all(getattr(r, k) == v for k, v in where.items())}


DOCUMENT = {
    "name": "Priya Sharma",
    "email": "priya.sharma@example.com",
    "phone": "647-555-0199",
    "location": "Waterloo, ON",
    "linkedin_url": "https://linkedin.com/in/priya-example",
    "github_url": "",
    "other_link": "",
    "summary_title": "Profile",
    "summary": "Third-year engineering student interested in robotics. Canadian citizen.",
    "skills": ["Python", "C++", "ROS", "Kubernetes", "Kubernetes", "Hyperflux 9000"],
    "technologies": {"Languages": ["Python", "MATLAB"], "Tools": ["Git", "SolidWorks"]},
    "experience": [
        {
            "id": "e1",
            "company": "Clearpath Robotics",
            "title": "Robotics Software Intern",
            "location": "Kitchener, ON",
            "start_date": "May 2025",
            "end_date": "Aug 2025",
            "bullets": [
                "Developed ROS nodes in C++ for autonomous navigation, cutting path errors by 25%",
                "Helped build a Python test harness for lidar drivers",
            ],
        },
        {
            "id": "e2",
            "company": "University of Waterloo",
            "title": "Teaching Assistant",
            "location": "Waterloo, ON",
            "start_date": "Sep 2025",
            "end_date": "Present",
            "bullets": ["Led weekly MATLAB tutorials for 60 first-year students"],
        },
    ],
    "education": [
        {
            "id": "ed1",
            "school": "University of Waterloo",
            "degree": "Bachelor of Applied Science, Mechatronics Engineering",
            "location": "Waterloo, ON",
            "start_date": "Sep 2023",
            "end_date": "Apr 2028",
            "gpa": "3.8",
            "achievements": ["Dean's Honours List", "Built a line-following robot in C for a design course"],
            "coursework": ["Control Systems", "Data Structures", "Linear Algebra"],
        }
    ],
    "projects": [
        {
            "id": "p1",
            "name": "Rover Arm",
            "link": "https://github.com/example/rover-arm",
            "organization": "UW Robotics Team",
            "location": "",
            "start_date": "Jan 2024",
            "end_date": "Apr 2024",
            "bullets": ["Designed a 4-DOF arm controller in Python using inverse kinematics"],
        }
    ],
    "custom_sections": [
        {"id": "c1", "title": "Certifications", "kind": "certifications", "text": "",
         "bullets": ["AWS Certified Cloud Practitioner", "Professional Engineer (P.Eng.) candidate"], "items": []},
        {"id": "c2", "title": "Awards", "kind": "custom", "text": "Won first place for a TensorFlow vision demo",
         "bullets": [], "items": []},
        {"id": "c3", "title": "Hobbies", "kind": "custom", "text": "Rock climbing, Haskell side projects",
         "bullets": [], "items": []},
        {"id": "c4", "title": "Licenses", "kind": "custom", "text": "", "bullets": [],
         "items": [{"title": "Construction and Maintenance Electrician (309A) Apprentice", "subtitle": "",
                    "location": "", "start_date": "2024", "end_date": "", "detail": "", "link": "",
                    "bullets": []}]},
    ],
    "section_order": ["summary", "experience", "education", "projects", "skills"],
}


@pytest.fixture(scope="module")
def parsed(deps):
    return from_doc(DOCUMENT, deps)


def test_the_document_parses(parsed) -> None:
    assert parsed.reasons == () and parsed.resume is not None


def test_experience_rows_are_roles(parsed) -> None:
    roles = parsed.resume.roles
    assert [(r.title_raw, r.employer) for r in roles] == [
        ("Robotics Software Intern", "Clearpath Robotics"), ("Teaching Assistant", "University of Waterloo")]
    intern, ta = roles
    assert intern.dated and intern.is_internship and not intern.is_current
    assert intern.start.value == date(2025, 5, 1) and intern.end.value == date(2025, 8, 31)
    assert ta.is_current and ta.end is None
    helped = [r for r in intern.skills if r.canonical_id == "skill.python"]
    assert helped[0].verb_class is VerbClass.ASSISTANCE
    assert ids(intern.skills, evidence=Evidence.ROLE_BULLET_QUANTIFIED) >= {"skill.ros", "skill.cpp"}
    assert parsed.resume.total_experience_months is not None


def test_education_ignores_school_and_gpa(parsed) -> None:
    (degree,) = parsed.resume.education
    assert degree.level is DegreeLevel.BACHELOR and degree.in_progress
    assert "Waterloo" not in degree.field_raw and "3.8" not in degree.field_raw
    course = [r for r in parsed.resume.skills_blob if r.source is SkillSource.EDUCATION
              and r.verb_class is VerbClass.EXPOSURE]
    assert "skill.data_structures" in ids(course)
    achievements = [r for r in parsed.resume.skills_blob if r.source is SkillSource.EDUCATION
                    and r.evidence is Evidence.SKILLS_BLOB]
    assert "skill.c" in ids(achievements)


def test_projects_are_projects(parsed) -> None:
    (project,) = parsed.resume.projects
    assert project.name == "Rover Arm"
    assert project.end is not None and project.end.value == date(2024, 4, 30)
    python = [r for r in project.skills if r.canonical_id == "skill.python"]
    assert python[0].verb_class is VerbClass.OWNERSHIP and python[0].source is SkillSource.PROJECT


def test_skills_and_technologies_are_the_blob(parsed) -> None:
    listed = ids(parsed.resume.skills_blob, source=SkillSource.SKILLS_SECTION)
    assert {"skill.python", "skill.cpp", "skill.ros", "skill.kubernetes", "skill.matlab", "skill.git"} <= listed
    assert "hyperflux 9000" in parsed.unresolved_tokens
    kubernetes = [r for r in parsed.resume.skills_blob if r.canonical_id == "skill.kubernetes"]
    assert len(kubernetes) == 1  # listed twice, kept once


def test_custom_sections(parsed) -> None:
    certs = {(c.canonical_id, c.evidence) for c in parsed.resume.certifications}
    assert ("cred.aws_cloud_practitioner", Evidence.CERTIFICATION) in certs
    assert ("cred.eit", Evidence.CERTIFICATION) in certs  # "P.Eng. candidate" is the designation
    assert ("cred.electrician_apprentice", Evidence.CERTIFICATION) in certs
    every = {c.canonical_id for c in parsed.resume.certifications} | ids(parsed.resume.skills_blob)
    assert "cred.professional_engineer" not in every and "cred.electrician_license" not in every
    assert "skill.tensorflow" in ids(parsed.resume.skills_blob, evidence=Evidence.SKILLS_BLOB)
    assert "skill.haskell" not in every  # hobbies are interests


def test_summary_location_and_work_authorisation(parsed) -> None:
    summary = {r.canonical_id: r.polarity for r in parsed.resume.skills_blob if r.source is SkillSource.SUMMARY}
    assert summary["skill.robotics"] is Polarity.ASPIRATIONAL  # "interested in robotics"
    assert parsed.resume.locations == (Location(country="CA", region="ON", city="waterloo"),)
    assert parsed.resume.work_auth.citizenships == ("CA",)


def test_the_document_and_its_text_agree(parsed, deps) -> None:
    """Round trip: the same résumé written out as text parses to the same facts."""
    text = """Priya Sharma
Waterloo, ON | priya.sharma@example.com | 647-555-0199
Profile
Third-year engineering student interested in robotics. Canadian citizen.
Experience
Robotics Software Intern, Clearpath Robotics, Kitchener, ON | May 2025 – Aug 2025
- Developed ROS nodes in C++ for autonomous navigation, cutting path errors by 25%
- Helped build a Python test harness for lidar drivers
Teaching Assistant, University of Waterloo, Waterloo, ON | Sep 2025 – Present
- Led weekly MATLAB tutorials for 60 first-year students
Education
University of Waterloo
Bachelor of Applied Science, Mechatronics Engineering | Sep 2023 – Apr 2028
Relevant Coursework: Control Systems, Data Structures, Linear Algebra
Projects
Rover Arm | Jan 2024 – Apr 2024
- Designed a 4-DOF arm controller in Python using inverse kinematics
Skills
Python, C++, ROS, Kubernetes, MATLAB, Git, SolidWorks
Certifications
AWS Certified Cloud Practitioner
Professional Engineer (P.Eng.) candidate
Construction and Maintenance Electrician (309A) Apprentice
"""
    from_text = parse_resume_text(text, as_of=AS_OF, strict=True, **deps).resume
    doc = parsed.resume

    def role_facts(resume):
        return [(r.title_canonical, r.employer, r.start, r.end, r.is_current, r.is_internship, r.seniority,
                 sorted({(s.canonical_id, s.evidence, s.verb_class, s.polarity) for s in r.skills}))
                for r in resume.roles]

    assert role_facts(doc) == role_facts(from_text)
    assert doc.education == from_text.education
    assert doc.total_experience_months == from_text.total_experience_months
    assert ids(doc.certifications) == ids(from_text.certifications)
    assert doc.locations == from_text.locations and doc.work_auth == from_text.work_auth
    assert [(p.start, p.end) for p in doc.projects] == [(p.start, p.end) for p in from_text.projects]


def test_json_documents_round_trip(parsed, deps) -> None:
    again = from_doc(json.loads(json.dumps(DOCUMENT)), deps)
    assert canonical_json(again) == canonical_json(parsed)


# ─── fallbacks and malformed documents ──────────────────────────────────────────────────────

RAW = "Experience\nCashier, Loblaws | Jun 2022 – Aug 2023\n- Handled cash and Excel inventory sheets\n"


def test_a_document_without_sections_parses_its_raw_text(deps) -> None:
    empty = {"summary": "", "skills": [], "experience": [], "education": [], "projects": [], "technologies": {}}
    parsed = from_doc(empty, deps, raw_text=RAW)
    text = parse_resume_text(RAW, as_of=AS_OF, strict=True, **deps)
    assert canonical_json(parsed) == canonical_json(text)


def test_a_document_with_sections_ignores_raw_text(deps) -> None:
    parsed = from_doc({"skills": ["Python"]}, deps, raw_text=RAW)
    assert parsed.resume.roles == () and ids(parsed.resume.skills_blob) == {"skill.python"}


def test_an_empty_document_without_raw_text_is_an_empty_resume(deps) -> None:
    parsed = from_doc({}, deps)
    assert parsed.reasons == () and parsed.resume.roles == () and parsed.resume.skills_blob == ()


def test_a_document_that_is_not_a_mapping_is_invalid(deps) -> None:
    for bad in (None, [], "text", 42):
        parsed = resume_from_document(bad, as_of=AS_OF, **deps)
        assert parsed.reasons == (Flag.INVALID_INPUT,) and parsed.resume is None


def test_malformed_fields_are_skipped(deps) -> None:
    doc = {
        "experience": [None, "Cashier", {"title": 7, "company": ["x"], "bullets": "Built Excel sheets"},
                       {"title": "Cashier", "company": "Loblaws", "start_date": "garbage", "end_date": None,
                        "bullets": [None, "Counted cash in Excel"]}],
        "education": ["BSc"], "projects": [{"name": None, "bullets": [3]}], "skills": [None, "Python", 5],
        "technologies": {"Tools": "Git", "Other": [None, "Docker"]}, "custom_sections": [{"title": None}],
        "summary": ["not a string"],
    }
    parsed = from_doc(doc, deps)
    assert parsed.reasons == ()
    cashier = [r for r in parsed.resume.roles if r.title_raw == "Cashier"]
    assert cashier and not cashier[0].dated and "skill.excel" in ids(cashier[0].skills)
    assert {"skill.python", "skill.docker"} <= ids(parsed.resume.skills_blob)


def test_one_sided_dates(deps) -> None:
    doc = {"experience": [{"title": "Barista", "company": "Second Cup", "start_date": "2021", "end_date": ""},
                          {"title": "Cook", "company": "Harvey's", "start_date": "", "end_date": "Present"}]}
    barista, cook = from_doc(doc, deps).resume.roles
    assert barista.start is not None and barista.start.value.year == 2021
    assert not cook.dated
