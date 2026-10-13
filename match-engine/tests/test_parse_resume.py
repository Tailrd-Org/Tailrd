"""Résumé assembly from text and files (`parse/resume.py`, Task 6.4).

SPEC §14.1-§14.3, §15; DECISIONS D-11, D-12, D-27, D-28, D-32..D-34, D-36, D-41, D-61, D-62.
Every résumé here is invented. The text path: `segment` -> experience-like sections into roles,
projects into `Project`s, skills/languages lists, certifications into credentials, education and
coursework, summary and unsectioned text into the skills blob; locations only from the contact
lines, work authorisation from its phrases.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from matcher.errors import Flag
from matcher.ingest.normalize_text import normalize_text
from matcher.graph.loader import load_resolver
from matcher.models import (
    DegreeLevel,
    Evidence,
    Location,
    ParseTier,
    Polarity,
    SkillSource,
    VerbClass,
    canonical_json,
)
from matcher.parse.config import load_parse_config
from matcher.parse.resume import (
    ParsedResume,
    load_resume_resources,
    parse_resume_bytes,
    parse_resume_text,
)
from matcher.parse.sections import SectionKind
from tests.builders import with_scoring

AS_OF = date(2026, 10, 9)
ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "data" / "taxonomy"
EDGE = ROOT / "tests" / "fixtures" / "edge"


@pytest.fixture(scope="module")
def deps(config):
    parse_config = load_parse_config()
    return {
        "resolver": load_resolver(TAXONOMY, config),
        "config": config,
        "parse_config": parse_config,
        "resources": load_resume_resources(TAXONOMY, parse_config=parse_config),
    }


def parse(text: str, deps, **kwargs) -> ParsedResume:
    kwargs.setdefault("strict", True)
    return parse_resume_text(text, as_of=AS_OF, **deps, **kwargs)


def ids(refs, **where) -> set[str]:
    return {r.canonical_id for r in refs if all(getattr(r, k) == v for k, v in where.items())}


def all_refs(resume):
    out = list(resume.skills_blob) + list(resume.certifications)
    for role in resume.roles:
        out.extend(role.skills)
    for project in resume.projects:
        out.extend(project.skills)
    return out


STUDENT = """Jordan Lee
Toronto, ON | jordan.lee@example.com | 416-555-0100 | github.com/jordanlee

Summary
Computer science student seeking a role in machine learning. Comfortable with Python and SQL.

Education
University of Toronto
Bachelor of Science, Computer Science    Sep 2022 – Apr 2027
Relevant Coursework: Data Structures, Databases, Operating Systems

Experience
Software Developer Intern — Shopify, Ottawa, ON | May 2025 – Aug 2025
- Built a React dashboard used by 40 support agents
- Cut API latency by 30% with Redis caching
Data Analyst Co-op, Ontario Ministry of Finance (Jan 2024 – Apr 2024)
- Automated monthly reports in Python

Projects
Campus Eats | React, Node.js, PostgreSQL | Jan 2024 – Apr 2024
- Built a food ordering app used by 300 students
- Deployed the backend with Docker
Weather CLI
- Wrote a command-line weather tool in Rust

Skills
Languages: Python, Java, SQL
Tools: Git, Tableau, Quantum Widgetry

Certifications
AWS Certified Cloud Practitioner

Interests
Chess, hiking, Haskell
"""


@pytest.fixture(scope="module")
def student(deps) -> ParsedResume:
    return parse(STUDENT, deps)


def test_a_student_resume_parses(student: ParsedResume, deps) -> None:
    assert student.reasons == ()
    assert student.resume is not None
    assert student.parse_config_version == deps["parse_config"].version
    assert student.sections == (
        SectionKind.CONTACT, SectionKind.SUMMARY, SectionKind.EDUCATION, SectionKind.EXPERIENCE,
        SectionKind.PROJECTS, SectionKind.SKILLS, SectionKind.CERTIFICATIONS, SectionKind.INTERESTS,
    )


def test_roles_come_from_the_experience_section(student: ParsedResume) -> None:
    roles = student.resume.roles
    assert [(r.title_raw, r.employer) for r in roles] == [
        ("Software Developer Intern", "Shopify"), ("Data Analyst Co-op", "Ontario Ministry of Finance")]
    assert all(r.dated and not r.is_current for r in roles)
    assert all(r.is_internship for r in roles)
    assert "skill.react" in ids(roles[0].skills, evidence=Evidence.ROLE_BULLET, verb_class=VerbClass.OWNERSHIP)
    assert "skill.redis" in ids(roles[0].skills, evidence=Evidence.ROLE_BULLET_QUANTIFIED)
    assert len(student.scope) == len(roles)
    assert student.scope[0].scale_tier is None


def test_total_experience_is_the_union_of_dated_roles(student: ParsedResume) -> None:
    # May 1 - Aug 31 2025 and Jan 1 - Apr 30 2024 (a leap year): 122 + 120 days at 30.4375 a month
    days = (date(2025, 8, 31) - date(2025, 5, 1)).days + (date(2024, 4, 30) - date(2024, 1, 1)).days
    assert days == 242
    expected = days / 30.4375
    assert student.resume.total_experience_months == pytest.approx(expected, abs=1e-9)


def test_projects_carry_bullet_evidence(student: ParsedResume) -> None:
    projects = student.resume.projects
    assert [p.name for p in projects] == ["Campus Eats | React, Node.js, PostgreSQL", "Weather CLI"]
    eats = projects[0]
    assert eats.start is not None and eats.end is not None and eats.end.value == date(2024, 4, 30)
    assert all(r.source is SkillSource.PROJECT for r in eats.skills)
    assert ids(eats.skills) >= {"skill.react", "skill.nodejs", "skill.postgresql", "skill.docker"}
    docker = [r for r in eats.skills if r.canonical_id == "skill.docker"]
    assert docker[0].evidence is Evidence.ROLE_BULLET and docker[0].verb_class is VerbClass.EXECUTION
    assert ids(projects[1].skills) == {"skill.rust"}


def test_skills_section_is_the_skills_blob(student: ParsedResume) -> None:
    blob = student.resume.skills_blob
    assert ids(blob, source=SkillSource.SKILLS_SECTION, evidence=Evidence.SKILLS_BLOB) == {
        "skill.python", "skill.java", "skill.sql", "skill.git", "skill.tableau"}
    assert "quantum widgetry" in student.unresolved_tokens


def test_summary_aspiration_is_aspirational(student: ParsedResume) -> None:
    summary = [r for r in student.resume.skills_blob if r.source is SkillSource.SUMMARY]
    polarity = {r.canonical_id: r.polarity for r in summary}
    assert polarity["skill.machine_learning"] is Polarity.ASPIRATIONAL
    assert polarity["skill.python"] is Polarity.AFFIRMED
    assert all(r.evidence is Evidence.SKILLS_BLOB for r in summary)


def test_coursework_is_exposure_evidence(student: ParsedResume) -> None:
    course = [r for r in student.resume.skills_blob if r.source is SkillSource.EDUCATION]
    assert {r.canonical_id for r in course} == {"skill.data_structures", "skill.databases", "skill.operating_systems"}
    assert all(r.evidence is Evidence.ROLE_BULLET and r.verb_class is VerbClass.EXPOSURE for r in course)


def test_education_and_certifications(student: ParsedResume) -> None:
    (degree,) = student.resume.education
    assert degree.level is DegreeLevel.BACHELOR and degree.in_progress
    assert "field.computer_science" in degree.field_ids
    certs = student.resume.certifications
    assert [(c.canonical_id, c.evidence, c.source) for c in certs] == [
        ("cred.aws_cloud_practitioner", Evidence.CERTIFICATION, SkillSource.CERTIFICATION)]


def test_interests_give_no_skills(student: ParsedResume) -> None:
    assert "skill.haskell" not in ids(all_refs(student.resume))


def test_location_comes_from_the_contact_lines(student: ParsedResume) -> None:
    assert student.resume.locations == (Location(country="CA", region="ON", city="toronto"),)


def test_role_places_are_not_the_candidates_location(deps) -> None:
    parsed = parse("Experience\nAnalyst — RBC, Vancouver, BC | 2021 – 2023\n- Built Excel models\n", deps)
    assert parsed.resume.locations == ()


def test_unmapped_titles_are_reported(deps) -> None:
    parsed = parse("Experience\nChief Vibes Wrangler, Acme Inc | 2022 – 2024\n- Ran Python scripts\n", deps)
    assert parsed.unmapped_titles == ("Chief Vibes Wrangler",)


def test_volunteer_sections_are_roles(deps) -> None:
    parsed = parse("Volunteer Experience\nTutor, Toronto Public Library | Sep 2023 – Present\n"
                   "- Taught Python to high school students\n", deps)
    (role,) = parsed.resume.roles
    assert role.employer == "Toronto Public Library" and role.is_current
    assert "skill.python" in ids(role.skills)


def test_languages_sections_are_skills(deps) -> None:
    parsed = parse("Languages\nEnglish, French\n", deps)
    assert ids(parsed.resume.skills_blob, evidence=Evidence.SKILLS_BLOB) == {"esco.english", "esco.french"}


def test_publications_and_references_give_no_skills(deps) -> None:
    text = ("Publications\nLee, J. (2025). Deep learning with PyTorch for crop yields. Journal of AgTech.\n"
            "References\nAvailable on request; Dr. Smith teaches Kubernetes.\n")
    assert ids(all_refs(parse(text, deps).resume)) == set()


# ─── roles only from structure ─────────────────────────────────────────────────────────────


def test_roles_never_come_from_unstructured_text(deps) -> None:
    text = ("Summary\nSoftware Developer at Shopify building React apps for merchants.\n"
            "Skills\nReact, Python\n")
    parsed = parse(text, deps)
    assert parsed.resume.roles == ()
    assert "skill.react" in ids(parsed.resume.skills_blob)


def test_experience_text_that_forms_no_role_is_blob_evidence(deps) -> None:
    parsed = parse("Experience\n- Build and maintain ETL pipelines in Python\n- Write unit tests in Java\n", deps)
    assert parsed.resume.roles == ()
    assert ids(parsed.resume.skills_blob) == {"skill.etl", "skill.python", "skill.unit_testing", "skill.java"}
    assert all(r.evidence is Evidence.SKILLS_BLOB for r in parsed.resume.skills_blob)


def test_date_anchored_blocks_are_roles_without_headers(deps) -> None:
    text = ("Jordan Lee\nToronto, ON\n"
            "Software Developer, Shopify | May 2023 – Aug 2024\n- Built React dashboards\n"
            "Python, SQL, Git\n")
    parsed = parse(text, deps)
    assert Flag.NO_SECTIONS in parsed.flags
    (role,) = parsed.resume.roles
    assert role.employer == "Shopify" and role.dated
    assert {"skill.python", "skill.sql", "skill.git"} <= ids(parsed.resume.skills_blob)
    assert parsed.resume.locations == (Location(country="CA", region="ON", city="toronto"),)


# ─── truncation and functional résumés ─────────────────────────────────────────────────────


def _long_cv(roles: int) -> str:
    lines = ["Experience"]
    for k in range(roles):
        year = 1990 + k
        lines.append(f"Research Analyst, Acme Labs {k} | Jan {year} – Dec {year}")
        lines.append("- " + " ".join(["Analysed"] + ["survey data with Python"] * 40))
    return "\n".join(lines) + "\n"


def test_experience_beyond_the_token_cap_is_cut(deps, config) -> None:
    small = with_scoring(config, "ingest", max_experience_tokens=1000)
    parsed = parse(_long_cv(10), {**deps, "config": small})
    assert Flag.TRUNCATED in parsed.flags and Flag.TRUNCATED in parsed.resume.flags
    assert 0 < len(parsed.resume.roles) < 10
    whole = parse(_long_cv(3), {**deps, "config": small})
    assert Flag.TRUNCATED not in whole.flags and len(whole.resume.roles) == 3


def test_a_forty_thousand_token_cv_is_truncated(deps) -> None:
    parsed = parse(_long_cv(400), deps)  # 400 roles x ~164 tokens
    assert Flag.TRUNCATED in parsed.flags
    assert len(parsed.resume.roles) < 300


FUNCTIONAL = """Alex Morgan
Summary of Qualifications
- Eight years of customer service and team leadership
- Skilled in Salesforce, Excel and conflict resolution
- Trained new staff on point-of-sale systems
Areas of Expertise
Customer Service, Salesforce, Excel, Scheduling
Employment History
Store Supervisor, Maple Grocers
- Scheduled a team of 12 cashiers
Customer Service Representative, Northwind Telecom
- Answered billing questions in Salesforce
"""


def test_a_functional_resume_is_flagged(deps) -> None:
    parsed = parse(FUNCTIONAL, deps)
    assert Flag.FUNCTIONAL_RESUME in parsed.flags and Flag.FUNCTIONAL_RESUME in parsed.resume.flags
    assert parsed.resume.roles and not any(r.dated for r in parsed.resume.roles)
    assert parsed.resume.total_experience_months is None


def test_qualifications_that_dominate_with_no_chronology_are_functional(deps) -> None:
    text = FUNCTIONAL.split("Employment History")[0]
    parsed = parse(text, deps)
    assert parsed.resume.roles == ()
    assert Flag.FUNCTIONAL_RESUME in parsed.flags


UNDERLINED_FUNCTIONAL = """Rowan Illustrative
rowan.illustrative@example.com | Denver, CO
Summary of Qualifications
-------------------------
Dependable product designer with 5+ years of experience turning requests into finished deliverables.
Areas of Expertise
------------------
Technical Skills: HTML, CSS, React, JavaScript
Tools and Platforms: Adobe XD, Git, Adobe Illustrator
Methods and Practices: Prototyping, Data Visualization, Wireframing, User Research
Domain Expertise: Accessibility
Professional Strengths: Problem Solving, Mentoring, Communication
Employment History
------------------
Product Designer III, Silverline Studio
Vancouver, BC
Product Designer, Juniper Design
Ottawa, ON
Education
---------
B.Sc. Human-Computer Interaction, Fairmont College
Certifications
--------------
- UX Design Certificate
"""


def test_underlines_do_not_count_as_lines_for_the_functional_reading(deps) -> None:
    """Task 5.3 format-invariance finding: the plain-text rendering of a functional résumé underlines
    its headers; the "-----" lines were counted as body lines, so the PDF of the same résumé was
    functional (experience abstains) and the text was not (experience 0)."""
    plain = "".join(line for line in UNDERLINED_FUNCTIONAL.splitlines(keepends=True) if set(line.strip()) != {"-"})
    assert "---" not in plain
    assert Flag.FUNCTIONAL_RESUME in parse(plain, deps).flags
    assert Flag.FUNCTIONAL_RESUME in parse(UNDERLINED_FUNCTIONAL, deps).flags


def test_undated_entries_without_bullets_are_roles(deps) -> None:
    """Task 5.4 (M6): a functional résumé lists "Title, Employer / City" with no dates and no
    bullets. Each entry is a role with unknown dates (its title is evidence, it adds no months)."""
    parsed = parse(UNDERLINED_FUNCTIONAL, deps)
    roles = parsed.resume.roles
    assert [(r.title_raw, r.employer) for r in roles] == [
        ("Product Designer III", "Silverline Studio"), ("Product Designer", "Juniper Design")]
    assert not any(r.dated for r in roles)
    assert roles[0].seniority is not None and roles[0].seniority.level == 3
    assert parsed.resume.total_experience_months is None
    assert Flag.FUNCTIONAL_RESUME in parsed.flags


_QUALIFICATIONS = ("Dependable office coordinator with eight years of experience keeping calendars, budgets, "
                   "vendors and travel running smoothly for busy teams, trusted to handle confidential records, "
                   "train new staff and keep every project moving")
_NO_ROLES = "Taylor Sample\nSummary of Qualifications\n{summary}\nEducation\nB.A. History, Fairmont College\n" \
            "Interests\nHiking\nChess\nCooking\n"


def test_the_functional_reading_does_not_depend_on_line_wrapping(deps) -> None:
    """Task 5.4 (M6): the same qualifications on one line (a text file) or wrapped over four lines
    (a PDF) are the same résumé: the functional reading counts words, never lines."""
    words = _QUALIFICATIONS.split()
    wrapped = "\n".join(" ".join(words[k:k + 10]) for k in range(0, len(words), 10))
    one_line = parse(_NO_ROLES.format(summary=_QUALIFICATIONS), deps)
    four_lines = parse(_NO_ROLES.format(summary=wrapped), deps)
    assert Flag.FUNCTIONAL_RESUME in four_lines.flags
    assert Flag.FUNCTIONAL_RESUME in one_line.flags


_EXPERTISE = ("Jordan Sample\nAreas of Expertise\n"
              "Methods and Practices: A/B Testing, Go-to-Market Strategy, User Stories\n"
              "Domain Expertise: Pricing Strategy, Product Analytics\n"
              "Professional Strengths: Time Management, Presentation Skills\n")
_EXPERTISE_WRAPPED = ("Jordan Sample\nAreas of Expertise\n"
                      "Methods and Practices: A/B\nTesting, Go-to-Market Strategy, User\nStories\n"
                      "Domain Expertise: Pricing\nStrategy, Product Analytics\n"
                      "Professional Strengths: Time\nManagement, Presentation\nSkills\n")


def test_a_wrapped_skills_list_reads_like_the_unwrapped_one(deps) -> None:
    """Task 5.4 (M6): a narrow PDF column wraps a skills list inside its items ("A/B" / "Testing");
    a line that carries on a list line not ending in a separator is the rest of that line."""
    flat, wrapped = parse(_EXPERTISE, deps).resume, parse(_EXPERTISE_WRAPPED, deps).resume
    assert {"skill.ab_testing", "skill.time_management", "skill.public_speaking"} <= ids(flat.skills_blob)
    assert ids(wrapped.skills_blob) == ids(flat.skills_blob)


def test_a_dated_resume_is_not_functional(student: ParsedResume) -> None:
    assert Flag.FUNCTIONAL_RESUME not in student.flags


def test_a_student_without_jobs_is_not_functional(deps) -> None:
    text = ("Education\nBachelor of Commerce, Accounting, Queen's University, 2023 – 2027\n"
            "Skills\nExcel, QuickBooks\n")
    parsed = parse(text, deps)
    assert parsed.resume.roles == () and Flag.FUNCTIONAL_RESUME not in parsed.flags


# ─── work authorisation and places ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(("line", "countries", "citizenships", "sponsorship"), [
    ("Canadian citizen", ("CA",), ("CA",), None),
    ("Permanent resident of Canada", ("CA",), (), None),
    ("Canadian permanent resident; open to relocation", ("CA",), (), None),
    ("Open work permit holder", ("CA",), (), None),
    ("US citizen", ("US",), ("US",), None),
    ("U.S. Citizen | Willing to relocate", ("US",), ("US",), None),
    ("Will require sponsorship for US roles", (), (), True),
    ("Requires visa sponsorship", (), (), True),
    ("Do not require sponsorship", (), (), False),
    ("Authorized to work in Canada and the United States", ("CA", "US"), (), None),
])
def test_work_authorisation_phrases(line, countries, citizenships, sponsorship, deps) -> None:
    parsed = parse(f"Sam Rivera\nToronto, ON | sam@example.com | {line}\nSkills\nPython\n", deps)
    auth = parsed.resume.work_auth
    assert auth is not None
    assert (auth.authorized_countries, auth.citizenships, auth.needs_sponsorship) == (
        countries, citizenships, sponsorship)


def test_a_bare_permanent_resident_takes_the_contact_country(deps) -> None:
    parsed = parse("Sam Rivera\nVancouver, BC\nPermanent resident\nSkills\nPython\n", deps)
    assert parsed.resume.work_auth.authorized_countries == ("CA",)


def test_no_phrase_no_work_authorisation(student: ParsedResume) -> None:
    assert student.resume.work_auth is None


@pytest.mark.parametrize(("line", "expected"), [
    ("Seattle, WA", Location(country="US", region="WA", city="seattle")),
    ("Montréal, Québec, Canada", Location(country="CA", region="QC", city="montréal")),
    ("123 Main St, Halifax, NS B3H 1A1", Location(country="CA", region="NS", city="halifax")),
    ("London, United Kingdom", Location(country="GB", city="london")),
    ("Toronto, Canada", Location(country="CA", city="toronto")),
    ("Calgary", Location(city="calgary")),
])
def test_contact_places(line, expected, deps) -> None:
    parsed = parse(f"Casey Ng\n{line} | casey@example.com\nSkills\nExcel\n", deps)
    assert parsed.resume.locations == (expected,)


def test_a_name_that_is_a_city_is_no_place(deps) -> None:
    parsed = parse("Victoria Chen\nvictoria@example.com\nSkills\nExcel\n", deps)
    assert parsed.resume.locations == ()


# ─── language, tiers, flags ─────────────────────────────────────────────────────────────────


GERMAN = """Lebenslauf
Berufserfahrung
Softwareentwickler bei der Firma Beispiel GmbH in München von 2020 bis 2024
Entwicklung von Anwendungen mit Python und Java für unsere Kunden in ganz Deutschland
Ausbildung
Bachelor der Informatik an der Technischen Universität München
"""


def test_an_unsupported_language_is_a_reason(deps) -> None:
    parsed = parse(GERMAN, deps)
    assert parsed.reasons == (Flag.UNSUPPORTED_LANGUAGE,) and parsed.resume is None


def test_a_stated_unsupported_language_is_a_reason(deps) -> None:
    parsed = parse(STUDENT, deps, language="de")
    assert parsed.reasons == (Flag.UNSUPPORTED_LANGUAGE,) and parsed.resume is None


def test_tier_language_and_ingest_flags_are_kept(deps) -> None:
    parsed = parse(STUDENT, deps, tier=ParseTier.C, language="en", flags=(Flag.OCR_USED,))
    assert parsed.resume.parse_tier is ParseTier.C and parsed.resume.language == "en"
    assert Flag.OCR_USED in parsed.flags and Flag.OCR_USED in parsed.resume.flags


def test_imprecise_role_dates_are_flagged(deps) -> None:
    parsed = parse("Experience\nBarista, Second Cup | Summer 2023\n- Served customers\n", deps)
    assert Flag.IMPRECISE_DATE in parsed.flags


def test_text_that_is_not_a_string_is_invalid(deps) -> None:
    parsed = parse_resume_text(None, as_of=AS_OF, **deps)  # type: ignore[arg-type]
    assert parsed.reasons == (Flag.INVALID_INPUT,) and parsed.resume is None



def test_parsing_is_deterministic(deps) -> None:
    first, second = parse(STUDENT, deps), parse(STUDENT, deps)
    assert canonical_json(first) == canonical_json(second)


@settings(max_examples=60)
@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=400))
def test_any_text_parses_without_raising(deps, text: str) -> None:
    parsed = parse(text, deps)
    if not normalize_text(text).strip():
        assert parsed.reasons == (Flag.NOT_A_RESUME,)
    else:
        assert parsed.reasons in ((), (Flag.UNSUPPORTED_LANGUAGE,))


@settings(max_examples=40)
@given(st.lists(st.sampled_from(STUDENT.splitlines() + ["", "- ", "Experience", "2020 – 2021", "CPA candidate"]),
                max_size=40))
def test_shuffled_resume_lines_parse(deps, lines: list[str]) -> None:
    text = "\n".join(lines)
    parsed = parse(text, deps)
    if normalize_text(text).strip():
        assert parsed.reasons == () and parsed.resume is not None
    else:
        assert parsed.reasons == (Flag.NOT_A_RESUME,)


# ─── files ──────────────────────────────────────────────────────────────────────────────────


def test_bytes_go_through_ingest(deps) -> None:
    parsed = parse_resume_bytes(STUDENT.encode("utf-8"), "resume.txt", as_of=AS_OF, **deps)
    assert parsed.reasons == ()
    text = parse(STUDENT, deps)
    assert canonical_json(parsed.resume) == canonical_json(text.resume)


def test_an_ingest_failure_is_its_reasons(deps) -> None:
    parsed = parse_resume_bytes(b"", "resume.pdf", as_of=AS_OF, **deps)
    assert parsed.reasons == (Flag.CORRUPT,) and parsed.resume is None


def test_a_clean_pdf_parses(deps) -> None:
    parsed = parse_resume_bytes((EDGE / "pdf_clean_one_column.pdf").read_bytes(), "cv.pdf", as_of=AS_OF, **deps)
    assert parsed.reasons == () and parsed.resume is not None
    assert parsed.resume.roles and parsed.resume.parse_tier is ParseTier.A


def test_a_headline_first_line_is_not_a_name(deps) -> None:
    parsed = parse("Python Developer\nEli Moss | Calgary, AB | eli@example.com\nSkills\nSQL\n", deps)
    assert "skill.python" in ids(parsed.resume.skills_blob)


def test_blank_text_is_not_a_resume(deps) -> None:
    for text in ("", "  \n\n "):
        parsed = parse(text, deps)
        assert parsed.reasons == (Flag.NOT_A_RESUME,) and parsed.resume is None
