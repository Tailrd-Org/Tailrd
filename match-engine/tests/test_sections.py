"""Section segmentation (SPEC §14.3; config/parse/sections.yaml) and the parse config itself.

A header is a short line (at most 6 words, no final period) that matches the English/French
synonym table, compared case-, accent- and punctuation-insensitively (trailing ":" ignored,
creative headers included), or a line styled like the document's known headers (ALL CAPS, bold
or a larger font when the layout is given) that is followed by content. No header anywhere:
NO_SECTIONS and the date-anchored fallback (lines whose header holds a date range open role
blocks; the rest is OTHER).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from matcher.config import ConfigError
from matcher.errors import Flag
from matcher.ingest import TextLine
from matcher.parse.config import ParseConfig, default_parse_config_dir, load_parse_config
from matcher.parse.sections import Section, SectionKind, classify_header, segment

K = SectionKind


@pytest.fixture(scope="module")
def pc() -> ParseConfig:
    return load_parse_config()


def _kinds(sections: tuple[Section, ...]) -> list[SectionKind]:
    return [section.kind for section in sections]


# ─── the header table ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line, kind",
    [
        ("EXPERIENCE", K.EXPERIENCE),
        ("Work Experience:", K.EXPERIENCE),
        ("— EDUCATION —", K.EDUCATION),
        ("technical skills", K.SKILLS),
        ("## Projects", K.PROJECTS),
        ("Relevant Technical Experience", K.EXPERIENCE),  # filler words are ignored
        ("Selected Academic Projects", K.PROJECTS),
        ("Education & Certifications", K.EDUCATION),  # a composite: its first part decides
        ("Honours and Awards", K.AWARDS),
        ("Skills & Interests", K.SKILLS),
        ("Volunteer Experience", K.VOLUNTEER),
        ("Leadership & Involvement", K.VOLUNTEER),
        ("Military Experience", K.EXPERIENCE),
        ("Research Experience", K.EXPERIENCE),
        ("Publications", K.PUBLICATIONS),
        ("References available upon request", K.REFERENCES),
        ("Certifications", K.CERTIFICATIONS),
        ("Licenses & Certifications", K.CERTIFICATIONS),
        ("Relevant Coursework", K.COURSEWORK),
        ("Contact Information", K.CONTACT),
        ("Summary of Qualifications", K.SUMMARY),
        ("Professional Summary", K.SUMMARY),
        ("Interests", K.INTERESTS),
        ("Languages", K.LANGUAGES),
        # Creative headers
        ("Where I've Been", K.EXPERIENCE),
        ("Where I’ve Been", K.EXPERIENCE),  # curly apostrophe
        ("My Toolkit", K.SKILLS),
        ("Things I've Built", K.PROJECTS),
        # French
        ("Expérience professionnelle", K.EXPERIENCE),
        ("EXPÉRIENCE PROFESSIONNELLE", K.EXPERIENCE),
        ("Experience professionnelle", K.EXPERIENCE),  # accents optional
        ("Compétences", K.SKILLS),
        ("Formation", K.EDUCATION),
        ("Projets personnels", K.PROJECTS),
        ("Bénévolat", K.VOLUNTEER),
        ("Langues", K.LANGUAGES),
        ("Centres d'intérêt", K.INTERESTS),
        ("Profil", K.SUMMARY),
        ("Learning", K.EDUCATION),  # Task 5.4: creative headers of a designer's résumé
        ("Badges", K.CERTIFICATIONS),
        ("Digital Badges", K.CERTIFICATIONS),
    ],
)
def test_classify_header(pc, line: str, kind: SectionKind) -> None:
    assert classify_header(line, pc) is kind


@pytest.mark.parametrize(
    "line",
    [
        "Experience.",  # a final period: a sentence
        "Skills and experience gained through four co-op terms at three firms",  # more than 6 words
        "- Skills",  # a bullet
        "Software Developer Intern",
        "Python, SQL",
        "Resume",
        "Curriculum Vitae",
        "",
        "Projects Coordinator",
    ],
)
def test_not_headers(pc, line: str) -> None:
    assert classify_header(line, pc) is None


# ─── segmentation ─────────────────────────────────────────────────────────────────────────

STUDENT = """\
JORDAN LEE
Ottawa, ON | jordan.lee@example.com | 613-555-0142 | github.com/jlee
SUMMARY
Third-year computer science student seeking a Summer 2027 software internship.
EXPERIENCE
Software Developer Intern | Shopify | Ottawa, ON | May 2026 – Aug 2026
- Built a React dashboard used by 40 support agents
- Cut API latency by 30% with Redis caching
Teaching Assistant, Carleton University | Sep 2025 – Present
- Led weekly Python labs for 60 first-year students
PROJECTS
Campus Eats | React Native, Firebase
- Built a food-ordering app for 300 students
EDUCATION
Bachelor of Computer Science (Honours), Carleton University | Expected Apr 2028
TECHNICAL SKILLS
Languages: Python, Java, TypeScript, SQL
Tools: Git, Docker, AWS""".split("\n")


def test_a_student_resume_with_all_caps_headers(pc) -> None:
    sections, flags = segment(STUDENT, parse_config=pc)
    assert flags == ()
    assert _kinds(sections) == [K.CONTACT, K.SUMMARY, K.EXPERIENCE, K.PROJECTS, K.EDUCATION, K.SKILLS]
    contact, summary, experience, projects, education, skills = sections
    assert (contact.header, contact.start_line, contact.lines) == ("", 0, tuple(STUDENT[0:2]))
    assert (summary.header, summary.start_line, summary.lines) == ("SUMMARY", 2, (STUDENT[3],))
    assert (experience.header, experience.start_line) == ("EXPERIENCE", 4)
    assert experience.lines == tuple(STUDENT[5:10])
    assert projects.lines == tuple(STUDENT[11:13])
    assert education.lines == (STUDENT[14],)
    # "Languages:" and "Tools:" are skill categories here, not new sections.
    assert (skills.header, skills.lines) == ("TECHNICAL SKILLS", tuple(STUDENT[16:18]))
    assert experience.language == "en"


FRENCH = """\
Camille Tremblay
Montréal, QC · camille.t@example.com
Profil
Étudiante en génie logiciel à la recherche d'un stage d'été en développement.
Expérience professionnelle
Développeuse logicielle (stage) — Desjardins, Montréal, QC | mai 2025 – août 2025
- Built REST APIs in Java and Spring Boot
Formation
Baccalauréat en génie logiciel, Polytechnique Montréal (en cours)
Compétences
Java, Python, SQL, Git""".split("\n")


def test_french_headers_with_english_bullets(pc) -> None:
    sections, flags = segment(FRENCH, parse_config=pc)
    assert flags == ()
    assert _kinds(sections) == [K.CONTACT, K.SUMMARY, K.EXPERIENCE, K.EDUCATION, K.SKILLS]
    assert sections[1].language == "fr"
    assert sections[2].lines == tuple(FRENCH[5:7])
    assert sections[3].header == "Formation"


CREATIVE = """\
Where I've Been
Product Design Intern, Figma — San Francisco, CA | Jun 2025 – Aug 2025
- Prototyped 12 onboarding flows in Figma
My Toolkit
Figma, Sketch, Adobe XD, HTML, CSS
Things I've Built
Habit Garden — a habit-tracking app (React, Supabase)""".split("\n")


def test_creative_headers(pc) -> None:
    sections, flags = segment(CREATIVE, parse_config=pc)
    assert flags == ()
    assert _kinds(sections) == [K.EXPERIENCE, K.SKILLS, K.PROJECTS]
    assert [s.start_line for s in sections] == [0, 3, 5]


def test_inline_labels_open_a_section_only_outside_entries(pc) -> None:
    lines = [
        "SUMMARY",
        "Bilingual co-op student.",
        "Languages: English, French",
        "EXPERIENCE",
        "Barista | Second Cup | 2024 – Present",
        "- Served 200 customers a day",
        "Technologies: Square POS",
    ]
    sections, _ = segment(lines, parse_config=pc)
    assert _kinds(sections) == [K.SUMMARY, K.LANGUAGES, K.EXPERIENCE]
    assert (sections[1].header, sections[1].lines, sections[1].start_line) == ("Languages", ("English, French",), 2)
    assert sections[2].lines == tuple(lines[4:7])


def test_a_header_word_inside_a_sidebar_skills_list_is_an_item(pc) -> None:
    """Task 5.4 (M6): one skill per line (a sidebar). "Leadership" is a listed header, but the line
    after it continues the list, so it is an item; "Education" is followed by a degree line."""
    lines = ["Jordan Sample", "Core Skills", "Jira", "Leadership", "Mentoring", "Education",
             "Bachelor of Arts in Marketing,", "Fairmont State University"]
    sections, _ = segment(lines, parse_config=pc)
    assert _kinds(sections) == [K.CONTACT, K.SKILLS, K.EDUCATION]
    assert sections[1].lines == ("Jira", "Leadership", "Mentoring")


def test_a_header_word_first_in_a_sidebar_list_is_an_item(pc) -> None:
    """The list's first item: the line after it is written like it, so it continues the list."""
    lines = ["Skills", "Leadership", "Redis", "Git", "Noel Sample", "Junior DevOps Engineer", "Professional Summary",
             "DevOps engineer with a year of experience."]
    sections, _ = segment(lines, parse_config=pc)
    assert _kinds(sections) == [K.SKILLS, K.SUMMARY]
    assert sections[0].lines[0] == "Leadership"


def test_a_header_word_inside_any_listed_sidebar_is_an_item(pc) -> None:
    """Not only under "Skills": a "Qualifications" sidebar lists skills one per line too."""
    lines = ["Other Qualifications", "Problem Solving", "Leadership", "Excel", "Work Experience",
             "Data Analyst, Harborview Data 07/2023 to Present"]
    sections, _ = segment(lines, parse_config=pc)
    assert _kinds(sections) == [K.SUMMARY, K.EXPERIENCE]
    assert sections[0].lines == ("Problem Solving", "Leadership", "Excel")


def test_a_wrapped_skills_item_never_opens_a_section(pc) -> None:
    """Task 5.4 (M6): a PDF wraps "Market Research" so that "Research" stands alone; followed by
    another header it would open an empty section, so it stays in the list."""
    lines = ["Areas of Expertise", "Technical Skills: Python, SQL", "Professional Strengths: Mentoring, Market",
             "Research", "Employment History", "Data Analyst, Acme Corp"]
    sections, _ = segment(lines, parse_config=pc)
    assert _kinds(sections) == [K.SKILLS, K.EXPERIENCE]
    assert sections[0].lines == tuple(lines[1:4])


@pytest.mark.parametrize("header, after", [
    ("LEADERSHIP", []),  # all capitals
    ("Leadership:", []),  # a colon
    ("Leadership", ["------------"]),  # a rule under it
])
def test_a_strong_header_inside_a_skills_list_opens_a_section(pc, header: str, after: list[str]) -> None:
    lines = ["Skills", "Jira", header, *after, "Mentoring", "Peer Tutor"]
    sections, _ = segment(lines, parse_config=pc)
    assert _kinds(sections) == [K.SKILLS, K.VOLUNTEER]
    assert sections[0].lines == ("Jira",)


def test_a_header_after_a_skills_list_still_opens_its_section(pc) -> None:
    lines = ["Skills", "Jira", "Mentoring", "Experience", "Data Analyst, Acme Corp | 2022 – 2024",
             "- Built dashboards", "Leadership", "President, Debate Club | 2021 – 2022"]
    sections, _ = segment(lines, parse_config=pc)
    assert _kinds(sections) == [K.SKILLS, K.EXPERIENCE, K.VOLUNTEER]


def test_unknown_all_caps_lines(pc) -> None:
    lines = [
        "EXPERIENCE",
        "SHOPIFY",  # an employer in capitals: never a header inside an experience section
        "Software Developer Intern | May 2026 – Aug 2026",
        "- Built a React dashboard",
        "SKILLS",
        "Python, SQL",
        "FUN FACTS",  # styled like the known headers, followed by content: an OTHER section
        "Ran a 200-member board-game club",
        "MORE",  # last line: not followed by content, so not a header
    ]
    sections, _ = segment(lines, parse_config=pc)
    assert _kinds(sections) == [K.EXPERIENCE, K.SKILLS, K.OTHER]
    assert sections[0].lines == tuple(lines[1:4])
    assert (sections[2].header, sections[2].lines) == ("FUN FACTS", tuple(lines[7:9]))


def _layout(rows: list[tuple[str, float, bool]]) -> list[TextLine]:
    return [TextLine(text=text, page=0, x0=0.0, y0=12.0 * i, x1=200.0, y1=12.0 * i + size, size=size, bold=bold)
            for i, (text, size, bold) in enumerate(rows)]


def test_layout_styled_headers(pc) -> None:
    rows = [
        ("Alex Kim", 18.0, True),
        ("alex@example.com | Waterloo, ON", 10.5, False),
        ("Experience", 13.0, True),
        ("Data Analyst Co-op", 10.5, True),  # a bold job title at body size: not a header
        ("Sun Life | Waterloo, ON | Jan 2026 – Apr 2026", 10.5, False),
        ("- Automated weekly reports in Python", 10.5, False),
        ("Side Quests", 13.0, True),  # unknown words, the known headers' size: a header
        ("Ran a 200-member board-game club", 10.5, False),
        ("Skills", 13.0, True),
        ("Python, SQL, Tableau", 10.5, False),
    ]
    layout = _layout(rows)
    sections, flags = segment([text for text, _, _ in rows], parse_config=pc, layout=layout)
    assert flags == ()
    assert _kinds(sections) == [K.CONTACT, K.EXPERIENCE, K.OTHER, K.SKILLS]
    assert sections[1].lines == ("Data Analyst Co-op", rows[4][0], rows[5][0])
    assert sections[2].header == "Side Quests"
    # Without the layout, "Side Quests" is plain text inside the experience section.
    plain, _ = segment([text for text, _, _ in rows], parse_config=pc)
    assert _kinds(plain) == [K.CONTACT, K.EXPERIENCE, K.SKILLS]


def test_a_military_transition_resume_keeps_its_civilian_roles(pc) -> None:
    """Task 5.3 format-invariance finding: in a PDF the styled "Civilian Work Experience" header was an
    unknown (OTHER) section, so every civilian role was lost; the plain-text rendering kept them."""
    rows = [
        ("Blake Specimen", 18.0, True),
        ("Military Service", 13.0, True),
        ("Branch: U.S. Army", 10.5, False),
        ("Civilian Work Experience", 13.0, True),
        ("DevOps Engineer III, Pinecrest Platforms July 2024 to Current", 10.5, False),
        ("- Implemented backup routines using Golang and AWS", 10.5, False),
        ("Skills", 13.0, True),
        ("Python, SQL", 10.5, False),
    ]
    sections, _ = segment([text for text, _, _ in rows], parse_config=pc, layout=_layout(rows))
    assert _kinds(sections) == [K.CONTACT, K.EXPERIENCE, K.EXPERIENCE, K.SKILLS]
    for header in ("Civilian Work Experience", "Civilian Experience", "Civilian Employment"):
        assert classify_header(header, pc) is K.EXPERIENCE, header


def test_an_ocr_fused_header_is_still_a_header(pc) -> None:
    """Task 5.3 format-invariance finding: OCR of a scanned page read "Professional Experience" as
    "ProfessionalExperience", the experience section disappeared and every role with it."""
    assert classify_header("ProfessionalExperience", pc) is K.EXPERIENCE
    assert classify_header("WORKHISTORY", pc) is K.EXPERIENCE
    assert classify_header("Researchand Professional Experience", pc) is K.EXPERIENCE
    assert classify_header("ResearchandProfessionalExperience", pc) is K.EXPERIENCE
    assert classify_header("Research Profile", pc) is K.SUMMARY
    assert classify_header("Teamwork", pc) is None and classify_header("Workshops", pc) is None
    assert classify_header("Networking", pc) is None  # an ordinary word is not a fused header
    sections, _ = segment(["Alex Kim", "ProfessionalExperience", "Barista, Cafe Uno | 2024 - Present",
                           "- Served 200 customers a day"], parse_config=pc)
    assert _kinds(sections) == [K.CONTACT, K.EXPERIENCE]


def test_a_layout_that_does_not_match_the_lines_is_ignored(pc) -> None:
    layout = _layout([("Something else", 13.0, True)])
    sections, _ = segment(["EXPERIENCE", "Barista | 2024 – Present"], parse_config=pc, layout=layout)
    assert _kinds(sections) == [K.EXPERIENCE]


NO_HEADERS = """\
Priya Sharma
priya.sharma@example.com | Toronto, ON
Data Analyst Intern, Ontario Ministry of Finance | Jan 2026 – Apr 2026
- Built Power BI dashboards tracking 12 KPIs
- Automated a monthly SQL report
Shopify
Software Developer Co-op
May 2025 – Aug 2025
- Wrote Ruby on Rails services
B.Sc. Statistics, University of Toronto | 2023 – 2027
Python, SQL, Power BI, Excel""".split("\n")


def test_no_headers_falls_back_to_date_anchored_blocks(pc) -> None:
    sections, flags = segment(NO_HEADERS, parse_config=pc)
    assert flags == (Flag.NO_SECTIONS,)
    assert _kinds(sections) == [K.OTHER, K.EXPERIENCE, K.EDUCATION, K.OTHER]
    other, experience, education, tail = sections
    assert (other.start_line, other.lines) == (0, tuple(NO_HEADERS[0:2]))
    assert (experience.header, experience.start_line, experience.lines) == ("", 2, tuple(NO_HEADERS[2:9]))
    assert education.lines == (NO_HEADERS[9],)
    assert tail.lines == (NO_HEADERS[10],)


def test_styled_lines_alone_never_replace_the_fallback(pc) -> None:
    lines = ["JORDAN LEE", "jordan@example.com", "SOFTWARE DEVELOPER INTERN, SHOPIFY | MAY 2026 – AUG 2026",
             "- Built a React dashboard"]
    sections, flags = segment(lines, parse_config=pc)
    assert flags == (Flag.NO_SECTIONS,)
    assert _kinds(sections) == [K.OTHER, K.EXPERIENCE]


def test_no_dated_lines_at_all_is_one_other_section(pc) -> None:
    sections, flags = segment(["Jordan Lee", "Python, SQL, Docker", "Enjoys hiking."], parse_config=pc)
    assert flags == (Flag.NO_SECTIONS,)
    assert _kinds(sections) == [K.OTHER]


@pytest.mark.parametrize("lines", [[], [""], ["   ", "\t"]])
def test_empty_input(pc, lines: list[str]) -> None:
    assert segment(lines, parse_config=pc) == ((), ())


def test_blank_lines_and_crlf_are_tolerated(pc) -> None:
    lines = ["", "EXPERIENCE\r", "", "Barista | 2024 – Present", "", "SKILLS", "Python"]
    sections, _ = segment(lines, parse_config=pc)
    assert _kinds(sections) == [K.EXPERIENCE, K.SKILLS]
    assert sections[0].start_line == 1
    assert "Barista | 2024 – Present" in sections[0].lines


def _covered(lines: list[str], sections: tuple[Section, ...]) -> list[int]:
    """Indexes of the non-blank input lines each section accounts for. A header line counts once;
    an inline header ("Languages: English") is one input line holding the header and its first line."""
    out: list[int] = []
    for section in sections:
        index = section.start_line
        written = lines[index].replace("\r", "").replace("\n", " ").strip()
        inline = bool(section.header) and written != section.header
        if section.header:
            out.append(index)
            content, first = (section.lines[1:] if inline else section.lines), index + 1
        else:
            content, first = section.lines, index
        out.extend(first + k for k, line in enumerate(content) if line.strip())
    return out


_WORDS = st.sampled_from([
    "EXPERIENCE", "Education", "Skills:", "Projects", "Where I've Been", "Compétences", "- Built X", "Python, SQL",
    "Shopify | May 2023 – Aug 2023", "2020 – 2022", "Languages: English", "SIDE QUESTS", "", "Founder",
    "B.Sc. Physics | 2019 – 2023", "• Led a team", "    indented", "Experience.", "x" * 40,
])


@given(lines=st.lists(st.one_of(_WORDS, st.text(max_size=30)), max_size=25))
def test_segment_is_total_and_covers_every_line_once(pc, lines: list[str]) -> None:
    sections, flags = segment(lines, parse_config=pc)
    assert segment(lines, parse_config=pc) == (sections, flags)  # deterministic
    starts = [section.start_line for section in sections]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)
    assert flags in ((), (Flag.NO_SECTIONS,))
    nonblank = [i for i, line in enumerate(lines) if line.strip()]
    covered = _covered(lines, sections)
    assert sorted(covered) == nonblank


# ─── the parse config ───────────────────────────────────────────────────────────────────────


def test_parse_config_loads_with_a_content_version(pc) -> None:
    assert len(pc.version) == 16 and all(c in "0123456789abcdef" for c in pc.version)
    assert load_parse_config().version == pc.version  # deterministic
    # The engine config directory and the parse directory itself are both accepted.
    assert load_parse_config(default_parse_config_dir().parent).version == pc.version
    assert load_parse_config(default_parse_config_dir()).version == pc.version


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "parse"
    shutil.copytree(default_parse_config_dir(), target)
    return target


def test_the_version_follows_the_content_not_the_bytes(tmp_path: Path, pc) -> None:
    target = _copy(tmp_path)
    path = target / "sections.yaml"
    path.write_text("# a comment changes nothing\n" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert load_parse_config(target).version == pc.version
    text = path.read_text(encoding="utf-8").replace("my toolkit", "my toolkit, my toolbelt", 1)
    path.write_text(text, encoding="utf-8")
    changed = load_parse_config(target)
    assert changed.version != pc.version
    assert classify_header("My Toolbelt", changed) is K.SKILLS


@pytest.mark.parametrize(
    "file, old, new, message",
    [
        ("sections.yaml", "my toolkit", "my toolkit, where i've been", "two section kinds"),
        ("seniority.yaml", "level: IC0", "level: IC9", "level"),
        ("industries.yaml", "\"5221\"", "\"52x1\"", "NAICS"),
    ],
)
def test_invalid_tables_are_refused(tmp_path: Path, file: str, old: str, new: str, message: str) -> None:
    target = _copy(tmp_path)
    path = target / file
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_parse_config(target)


def test_a_missing_table_is_refused(tmp_path: Path) -> None:
    target = _copy(tmp_path)
    (target / "industries.yaml").unlink()
    with pytest.raises(ConfigError, match="industries.yaml"):
        load_parse_config(target)


def test_section_kinds_are_the_briefs() -> None:
    assert [kind.value for kind in SectionKind] == [
        "CONTACT", "SUMMARY", "EXPERIENCE", "EDUCATION", "SKILLS", "PROJECTS", "CERTIFICATIONS", "COURSEWORK",
        "PUBLICATIONS", "AWARDS", "VOLUNTEER", "LANGUAGES", "INTERESTS", "REFERENCES", "OTHER",
    ]


def test_section_is_frozen() -> None:
    section = Section(kind=K.SKILLS, header="Skills", lines=("Python",), start_line=0, language=None)
    with pytest.raises(Exception):
        section.header = "x"  # type: ignore[misc]
