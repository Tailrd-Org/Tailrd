"""Roles: header lines into drafts (`split_roles`), drafts into scored `Role`s (`build_role`).

SPEC §14.2-§14.3; DECISIONS D-11, D-12, D-29, D-32..D-34, D-38. Dates come from the header line
only (`find_date_ranges` on that line), never from a bullet ("Microsoft Office 2019"). Patterns:
"Title, em dash, Employer, Location | dates", "Employer / Title / dates", "Title, Employer (dates)",
"Employer (dates) / Title", plus the two-line template "Title  dates / Employer  Location" and
LinkedIn's "Title / Employer · Internship / dates · 4 mos / Location". Several titles under one
employer are one draft each (promotions); consecutive same-employer drafts with the same title
less than 60 days apart merge (contract-to-hire). The examples are invented.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from matcher.errors import Flag
from matcher.graph.loader import load_resolver
from matcher.models import Evidence, Polarity, SkillSource, VerbClass
from matcher.parse.config import load_parse_config
from matcher.parse.dates import DateRange, parse_range
from matcher.parse.roles import RoleDraft, build_role, past_verbs, split_roles
from matcher.parse.sections import Section, SectionKind
from matcher.parse.taxonomy_extras import load_mos_index, load_title_index
from tests.fixtures.build_parse_taxonomy import FIXTURE_DIR, MOS

AS_OF = date(2026, 10, 9)
TAXONOMY = Path(__file__).resolve().parents[1] / "data" / "taxonomy"


def _section(lines: list[str], kind: SectionKind = SectionKind.EXPERIENCE) -> Section:
    return Section(kind=kind, header="Experience", lines=tuple(lines), start_line=0, language="en")


def _range(text: str, lang: str = "en") -> DateRange:
    found = parse_range(text, as_of=AS_OF, lang=lang)
    assert found is not None and found.raw == text
    return found


def _draft(title: str, employer: str, location: str, dates: str | None, *bullets: str) -> RoleDraft:
    return RoleDraft(title_raw=title, employer=employer, location=location,
                     date_range=_range(dates) if dates else None, bullets=tuple(bullets))


# ─── split_roles ────────────────────────────────────────────────────────────────────────────

SPLIT_CASES = [
    (  # "Title, em dash, Employer, Location | dates"
        ["Software Developer Intern — Shopify, Ottawa, ON | May 2023 – Aug 2023",
         "- Built a React dashboard used by 40 support agents",
         "- Cut API latency by 30% with Redis caching"],
        [_draft("Software Developer Intern", "Shopify", "Ottawa, ON", "May 2023 – Aug 2023",
                "Built a React dashboard used by 40 support agents", "Cut API latency by 30% with Redis caching")],
    ),
    (  # "Employer / Title / dates"
        ["Shopify", "Software Developer Intern", "May 2023 – Aug 2023", "- Built a React dashboard"],
        [_draft("Software Developer Intern", "Shopify", "", "May 2023 – Aug 2023", "Built a React dashboard")],
    ),
    (  # "Title, Employer (dates)"
        ["Data Analyst Co-op, Ontario Ministry of Finance (Jan 2024 – Apr 2024)",
         "- Automated monthly reports in Python"],
        [_draft("Data Analyst Co-op", "Ontario Ministry of Finance", "", "Jan 2024 – Apr 2024",
                "Automated monthly reports in Python")],
    ),
    (  # "Employer (dates) / Title"
        ["Shopify (May 2023 – Aug 2023)", "Software Developer Intern", "- Built a React dashboard"],
        [_draft("Software Developer Intern", "Shopify", "", "May 2023 – Aug 2023", "Built a React dashboard")],
    ),
    (  # the two-line template: "Title    dates" / "Employer    Location"
        ["Software Engineer Intern    May 2023 – Aug 2023", "Shopify    Ottawa, ON", "- Built a React dashboard"],
        [_draft("Software Engineer Intern", "Shopify", "Ottawa, ON", "May 2023 – Aug 2023",
                "Built a React dashboard")],
    ),
    (  # promotions under one employer: one draft each
        ["Royal Bank of Canada", "Senior Data Analyst | Jan 2024 – Present", "- Led a team of 3 analysts",
         "Data Analyst | Jun 2022 – Dec 2023", "- Built SQL pipelines for credit-risk reports"],
        [_draft("Senior Data Analyst", "Royal Bank of Canada", "", "Jan 2024 – Present", "Led a team of 3 analysts"),
         _draft("Data Analyst", "Royal Bank of Canada", "", "Jun 2022 – Dec 2023",
                "Built SQL pipelines for credit-risk reports")],
    ),
    (  # a dated employer line over its promotions
        ["Telus | 2016 – Present", "Senior Software Engineer | 2020 – Present", "- Owned the billing API",
         "Software Engineer | 2016 – 2020", "- Built Java services"],
        [_draft("Senior Software Engineer", "Telus", "", "2020 – Present", "Owned the billing API"),
         _draft("Software Engineer", "Telus", "", "2016 – 2020", "Built Java services")],
    ),
    (  # the same title more than 60 days apart: two internships, never merged
        ["Software Engineering Intern, Shopify | May 2024 – Aug 2024", "- Built a GraphQL API",
         "Software Engineering Intern, Shopify | May 2023 – Aug 2023", "- Fixed checkout bugs"],
        [_draft("Software Engineering Intern", "Shopify", "", "May 2024 – Aug 2024", "Built a GraphQL API"),
         _draft("Software Engineering Intern", "Shopify", "", "May 2023 – Aug 2023", "Fixed checkout bugs")],
    ),
    (  # founder and self-employed: normal roles
        ["Founder & CEO, LoopLearn | Sep 2024 – Present", "- Grew the tutoring marketplace to 1,200 users",
         "Freelance Web Developer | Self-employed | 2021 – 2022", "- Built 8 websites for local businesses"],
        [_draft("Founder & CEO", "LoopLearn", "", "Sep 2024 – Present", "Grew the tutoring marketplace to 1,200 users"),
         _draft("Freelance Web Developer", "Self-employed", "", "2021 – 2022",
                "Built 8 websites for local businesses")],
    ),
    (  # a bullet wrapped onto the next line is joined; a raw glyph and an indented line are bullets
        ["Barista, Second Cup | 2022 – 2024", "• Served 200+ customers a day and trained", "new hires on the bar",
         "  Ran the closing shift on weekends"],
        [_draft("Barista", "Second Cup", "", "2022 – 2024", "Served 200+ customers a day and trained new hires on the bar",
                "Ran the closing shift on weekends")],
    ),
    (  # military: the code stays in the title, the base is the location
        ["Combat Medic Specialist (68W) — U.S. Army, Fort Liberty, NC | Jun 2015 – May 2019",
         "- Treated 300+ casualties in field exercises"],
        [_draft("Combat Medic Specialist (68W)", "U.S. Army", "Fort Liberty, NC", "Jun 2015 – May 2019",
                "Treated 300+ casualties in field exercises")],
    ),
    (  # undated: the role is kept, with no dates
        ["Volunteer Tutor, Ottawa Public Library", "- Tutored 6 high-school students in math"],
        [_draft("Volunteer Tutor", "Ottawa Public Library", "", None, "Tutored 6 high-school students in math")],
    ),
    (  # functional: undated entries with no bullets are roles, the place line below joins its entry
        ["Junior Data Engineer, Clearwater Research", "Chicago, IL", "Retail Associate, Northwind Foods",
         "Toronto, ON", "Barista | Second Cup | Ottawa, ON"],
        [_draft("Junior Data Engineer", "Clearwater Research", "Chicago, IL", None),
         _draft("Retail Associate", "Northwind Foods", "Toronto, ON", None),
         _draft("Barista", "Second Cup", "Ottawa, ON", None)],
    ),
    (  # a dated section: an undated line with no bullets stays text of the role above it
        ["Software Developer, Shopify | 2022 – 2024", "- Built the admin portal", "Mentor, Code Club"],
        [_draft("Software Developer", "Shopify", "", "2022 – 2024", "Built the admin portal", "Mentor, Code Club")],
    ),
    (  # Task 5.4: a narrow column wraps the employer before the dates; its tail line joins it
        ["Junior Marketing Specialist, Redwood    September 2025 – Present", "Creative", "Halifax, NS",
         "- Ran launch campaigns"],
        [_draft("Junior Marketing Specialist", "Redwood Creative", "Halifax, NS", "September 2025 – Present",
                "Ran launch campaigns")],
    ),
    (  # a line naming a job after a complete header line is no wrapped tail
        ["Data Analyst, Acme Corp | 2022 – 2024", "Team Lead", "- Ran a team of 3 analysts"],
        [_draft("Data Analyst", "Acme Corp", "", "2022 – 2024"),
         _draft("Team Lead", "", "", None, "Ran a team of 3 analysts")],
    ),
    (  # Task 5.4: a right-hand date column read after the place line: the head lines are above it
        ["Marketing Specialist I, Pinecrest Media", "Montreal, QC", "November 2025 – Present",
         "- Ran newsletter programs"],
        [_draft("Marketing Specialist I", "Pinecrest Media", "Montreal, QC", "November 2025 – Present",
                "Ran newsletter programs")],
    ),
    (  # Task 5.4: a bullet wrapped after "following" carries on, even with a capital after it
        ["Financial Analyst, Copperleaf Trust | 2023 – 2026", "- Shadowed colleagues on reports following",
         "Financial Modeling practices, lowering errors by 8%", "- Built budget reviews"],
        [_draft("Financial Analyst", "Copperleaf Trust", "", "2023 – 2026",
                "Shadowed colleagues on reports following Financial Modeling practices, lowering errors by 8%",
                "Built budget reviews")],
    ),
    (  # a year inside a bullet is never a role's date
        ["IT Support Assistant, Carleton University | Sep 2021 – Apr 2022",
         "- Rolled out Microsoft Office 2019 to 120 staff laptops"],
        [_draft("IT Support Assistant", "Carleton University", "", "Sep 2021 – Apr 2022",
                "Rolled out Microsoft Office 2019 to 120 staff laptops")],
    ),
    (  # LinkedIn's export: the employment type marks the internship, the duration is dropped
        ["Software Developer", "Shopify · Internship", "May 2023 - Aug 2023 · 4 mos", "Ottawa, Ontario, Canada",
         "- Built internal tools in Ruby"],
        [_draft("Software Developer (Internship)", "Shopify", "Ottawa, Ontario, Canada", "May 2023 - Aug 2023",
                "Built internal tools in Ruby")],
    ),
    (  # one bulleted line per role
        ["- Cashier, Loblaws | 2021 – 2022", "- Barista, Starbucks | 2022 – 2023"],
        [_draft("Cashier", "Loblaws", "", "2021 – 2022"), _draft("Barista", "Starbucks", "", "2022 – 2023")],
    ),
    (  # "Title at Employer"
        ["Teaching Assistant at the University of Ottawa | Jan 2025 – Apr 2025", "- Marked 120 lab reports"],
        [_draft("Teaching Assistant", "the University of Ottawa", "", "Jan 2025 – Apr 2025", "Marked 120 lab reports")],
    ),
]


@pytest.mark.parametrize("lines, expected", SPLIT_CASES, ids=[case[0][0][:40] for case in SPLIT_CASES])
def test_split_roles(lines: list[str], expected: list[RoleDraft]) -> None:
    assert list(split_roles(_section(lines), as_of=AS_OF)) == expected


def test_contract_to_hire_chains_merge() -> None:
    lines = ["Software Developer, Acme Corp | Jul 2022 – Present", "- Built payment APIs",
             "Software Developer (Contract), Acme Corp | Jan 2022 – Jun 2022", "- Built the admin portal"]
    (draft,) = split_roles(_section(lines), as_of=AS_OF)
    assert (draft.title_raw, draft.employer) == ("Software Developer", "Acme Corp")
    assert draft.date_range.start == _range("Jan 2022 – Jun 2022").start
    assert draft.date_range.is_current and draft.date_range.end is None
    assert draft.bullets == ("Built payment APIs", "Built the admin portal")


def test_two_terms_on_one_line_are_two_drafts() -> None:
    lines = ["Teaching Assistant, University of Toronto | Fall 2022, Fall 2023", "- Ran tutorials for 40 students"]
    drafts = split_roles(_section(lines), as_of=AS_OF)
    assert [d.date_range for d in drafts] == [_range("Fall 2022"), _range("Fall 2023")]
    assert {(d.title_raw, d.employer, d.bullets) for d in drafts} == {
        ("Teaching Assistant", "University of Toronto", ("Ran tutorials for 40 students",))
    }


def test_back_to_back_terms_of_one_role_merge() -> None:
    lines = ["Teaching Assistant, University of Toronto | Fall 2022, Winter 2023", "- Ran tutorials for 40 students"]
    (draft,) = split_roles(_section(lines), as_of=AS_OF)
    fall, winter = _range("Fall 2022"), _range("Winter 2023")
    assert (draft.date_range.start, draft.date_range.end) == (fall.start, winter.end)
    assert draft.date_range.raw == "Fall 2022; Winter 2023"
    assert draft.bullets == ("Ran tutorials for 40 students",)  # one line's bullets, once


def test_a_bullet_that_lost_its_glyph_is_never_a_role(config) -> None:
    """Task 5.4 (M6): a scan can drop a bullet glyph. A line opening with a past-tense action verb
    of the verb lexicon ("Optimized ...") is bullet text, never a role's head line."""
    lines = ["Data Analyst, Acme Corp | 2022 – 2024", "- Built dashboards in Tableau",
             "Optimized audience segments for Social Media", "- Cut reporting time by 10%"]
    (draft,) = split_roles(_section(lines), as_of=AS_OF, verbs=past_verbs(config))
    assert draft.bullets == ("Built dashboards in Tableau", "Optimized audience segments for Social Media",
                             "Cut reporting time by 10%")
    assert past_verbs(config) >= {"optimized", "worked", "built", "led"}
    assert not past_verbs(config) & {"lead", "engineer", "design", "registered"}


def test_undated_lines_among_bullets_are_no_functional_entries() -> None:
    """A section with bullets is no functional list: a line there that names a job but has no
    bullets under it (a bullet whose glyph a scan lost) is no role."""
    lines = ["- Lab demonstrator, Practical Methods Studio", "- Tutor, Introductory Statistics",
             "Workshop facilitator, Research Skills Series"]
    assert split_roles(_section(lines), as_of=AS_OF) == ()


def test_a_future_range_keeps_the_role_without_dates() -> None:
    lines = ["Incoming Software Engineering Intern, Google | May 2027 – Aug 2027"]
    (draft,) = split_roles(_section(lines), as_of=AS_OF)
    assert (draft.title_raw, draft.employer) == ("Incoming Software Engineering Intern", "Google")
    assert draft.date_range == DateRange(start=None, end=None, is_current=False, flags=(Flag.FUTURE_START,),
                                         raw="May 2027 – Aug 2027")


def test_french_role() -> None:
    lines = ["Développeuse logicielle (stage) — Desjardins, Montréal, QC | mai 2025 – août 2025",
             "- Built REST APIs in Java"]
    (draft,) = split_roles(_section(lines), as_of=AS_OF, lang="fr")
    assert draft == RoleDraft(title_raw="Développeuse logicielle (stage)", employer="Desjardins",
                              location="Montréal, QC", date_range=_range("mai 2025 – août 2025", "fr"),
                              bullets=("Built REST APIs in Java",))


def test_an_empty_section_has_no_roles() -> None:
    assert split_roles(_section([]), as_of=AS_OF) == ()
    assert split_roles(_section(["", "   "]), as_of=AS_OF) == ()


_LINE = st.sampled_from([
    "Software Developer Intern — Shopify, Ottawa, ON | May 2023 – Aug 2023", "- Built a React dashboard",
    "Shopify", "Data Analyst", "2016 – Present", "Telus | 2016 – Present", "", "  indented continuation",
    "• glyph bullet", "Self-employed", "Fall 2022, Winter 2023", "(Jan 2024 – Apr 2024)", "Expected May 2027",
    "Technologies: React", "and a lowercase continuation", "Ottawa, ON", "Present", "– – –",
])


@given(st.lists(st.one_of(_LINE, st.text(max_size=40)), max_size=20))
def test_split_roles_is_total(lines: list[str]) -> None:
    drafts = split_roles(_section(lines), as_of=AS_OF)
    assert split_roles(_section(lines), as_of=AS_OF) == drafts
    for draft in drafts:
        assert isinstance(draft, RoleDraft)
        assert all("\n" not in bullet and bullet.strip() == bullet and bullet for bullet in draft.bullets)


# ─── build_role ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def deps(config):
    pc = load_parse_config()
    return {
        "resolver": load_resolver(TAXONOMY, config),
        "titles": load_title_index(FIXTURE_DIR, parse_config=pc),
        "mos": load_mos_index(FIXTURE_DIR),
        "parse_config": pc,
        "config": config,
    }


def _build(draft: RoleDraft, deps):
    return build_role(draft, as_of=AS_OF, **deps)


def test_a_co_op_role(deps) -> None:
    draft = _draft("Software Developer Co-op", "Shopify", "Ottawa, ON", "May 2023 – Aug 2023",
                   "Developed REST APIs in Python and deployed them on AWS, reducing latency by 40%.",
                   "Collaborated with designers on accessibility fixes")
    role = _build(draft, deps)
    assert role.title_raw == "Software Developer Co-op"
    assert role.title_canonical == "title.15-1252.00"
    assert role.seniority.code == "IC0"
    assert role.is_internship
    assert role.industry_codes == ("5132",)
    assert (role.start, role.end, role.is_current) == (draft.date_range.start, draft.date_range.end, False)
    by_id = {ref.canonical_id: ref for ref in role.skills}
    python = by_id["skill.python"]
    assert python.evidence is Evidence.ROLE_BULLET_QUANTIFIED
    assert python.verb_class is VerbClass.EXECUTION
    assert python.polarity is Polarity.AFFIRMED
    assert (python.source, python.surface_form) == (SkillSource.ROLE, "Python")
    assert (python.first_used, python.last_used) == (date(2023, 5, 1), date(2023, 8, 31))
    assert "skill.aws" in by_id and "skill.rest" in by_id
    assert all(ref.source is SkillSource.ROLE for ref in role.skills)
    assert all((ref.first_used, ref.last_used) == (date(2023, 5, 1), date(2023, 8, 31)) for ref in role.skills)


def test_a_current_role_uses_as_of_and_title_skills(deps) -> None:
    role = _build(_draft("Machine Learning Engineer", "Cohere", "", "Jan 2025 – Present",
                         "Fine-tuned retrieval models in PyTorch"), deps)
    assert role.is_current and role.end is None
    assert role.title_canonical == "title.15-1221.00"
    title_refs = [ref for ref in role.skills if ref.evidence is Evidence.ROLE_TITLE]
    assert [ref.canonical_id for ref in title_refs] == ["skill.machine_learning"]
    assert title_refs[0].last_used == AS_OF and title_refs[0].first_used == date(2025, 1, 1)
    assert role.seniority is None
    assert role.industry_codes == ("5132",)


def test_bullet_polarity_is_kept(deps) -> None:
    role = _build(_draft("Junior Developer", "Northwind", "", "2024 – Present",
                         "Currently learning Kubernetes on weekends.", "Not familiar with Docker yet."), deps)
    by_id = {ref.canonical_id: ref for ref in role.skills}
    assert by_id["skill.kubernetes"].polarity is Polarity.ASPIRATIONAL
    assert by_id["skill.docker"].polarity is Polarity.NEGATED
    assert role.seniority.code == "IC1"


def test_military_codes_add_the_crosswalks_skills(deps) -> None:
    role = _build(_draft("Combat Medic Specialist (68W)", "U.S. Army", "Fort Liberty, NC", "Jun 2015 – May 2019",
                         "Treated 300+ casualties in field exercises"), deps)
    mos_refs = [ref for ref in role.skills if ref.surface_form == "68W"]
    assert [ref.canonical_id for ref in mos_refs] == list(MOS["codes"]["68W"]["skill_ids"])
    assert all(ref.evidence is Evidence.ROLE_TITLE and ref.source is SkillSource.ROLE for ref in mos_refs)
    assert role.industry_codes == ("9281",)
    assert role.title_raw == "Combat Medic Specialist (68W)"


def test_a_labelled_code_without_skill_data_adds_nothing_and_never_fails(deps) -> None:
    role = _build(_draft("Information Technology Specialist (MOS 25B)", "U.S. Army", "", "2012 – 2016",
                         "Maintained 40 field laptops"), deps)
    assert not [ref for ref in role.skills if ref.surface_form == "25B"]


def test_a_civilian_abbreviation_is_not_a_rating(deps) -> None:
    role = _build(_draft("Information Systems Technician (IT)", "Acme Corp", "", "2020 – 2022",
                         "Reset passwords for 300 staff"), deps)
    navy_it = set(MOS["codes"]["IT"]["skill_ids"])
    assert not navy_it & {ref.canonical_id for ref in role.skills}
    in_the_navy = _build(_draft("Information Systems Technician (IT)", "U.S. Navy", "", "2020 – 2022",
                                "Reset passwords for 300 sailors"), deps)
    assert navy_it <= {ref.canonical_id for ref in in_the_navy.skills}


def test_a_student_organisation_role_is_ic0(deps) -> None:
    role = _build(_draft("Treasurer", "Robotics Club", "", "Sep 2025 – Present", "Led a team of 12 members"), deps)
    assert role.seniority.code == "IC0"
    assert not role.is_internship


def test_an_undated_role_has_undated_skills(deps) -> None:
    role = _build(_draft("Volunteer Tutor", "Ottawa Public Library", "", None, "Taught Python to 6 teens"), deps)
    assert (role.start, role.end, role.is_current) == (None, None, False)
    assert role.skills and all(ref.first_used is None and ref.last_used is None for ref in role.skills)


def test_a_dropped_range_builds_an_undated_role(deps) -> None:
    dropped = DateRange(start=None, end=None, is_current=False, flags=(Flag.FUTURE_START,), raw="May 2027 – Aug 2027")
    draft = RoleDraft(title_raw="Incoming Software Engineering Intern", employer="Google", location="",
                      date_range=dropped, bullets=())
    role = _build(draft, deps)
    assert (role.start, role.end, role.is_current) == (None, None, False)
    assert role.is_internship


def test_industry_from_the_first_bullet_when_the_employer_is_unknown(deps) -> None:
    role = _build(_draft("Developer", "Northwind", "", "2023 – 2024",
                         "Built billing features for a SaaS software company's clients"), deps)
    assert role.industry_codes == ("5132", "5415")


@pytest.mark.parametrize(
    "title, internship",
    [("Stagiaire en analyse de données", True), ("Analyste de données (stage)", True), ("Stage Manager", False),
     ("Marketing Coordinator", False)],
)
def test_is_internship_from_the_title(deps, title: str, internship: bool) -> None:
    assert _build(_draft(title, "Acme", "", "2024 – 2025"), deps).is_internship is internship


def test_is_internship_from_the_date_text(deps) -> None:
    draft = RoleDraft(title_raw="Software Developer", employer="Shopify", location="",
                      date_range=_range("2024 – 2025").model_copy(update={"raw": "Co-op term: 2024 – 2025"}),
                      bullets=())
    assert _build(draft, deps).is_internship


def test_soft_wraps_inside_a_bullet_are_joined(deps) -> None:
    role = _build(_draft("Cloud Developer", "Acme", "", "2024 – 2025", "Deployed services on Amazon Web\nServices"), deps)
    assert "skill.aws" in {ref.canonical_id for ref in role.skills}


def test_repeated_mentions_are_one_ref(deps) -> None:
    role = _build(_draft("Data Analyst", "Acme", "", "2024 – 2025", *(["Wrote SQL queries"] * 50)), deps)
    keys = [(r.canonical_id, r.evidence, r.verb_class, r.polarity) for r in role.skills]
    assert len(keys) == len(set(keys)) and len(keys) >= 1


# ─── fix round 1 ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "line, title, employer, location",
    [
        ("Software Engineer Intern, RBC, Toronto | May 2024 - Aug 2024", "Software Engineer Intern", "RBC", "Toronto"),
        ("Software Developer Co-op, Ciena, Ottawa | Jan 2024 - Apr 2024", "Software Developer Co-op", "Ciena", "Ottawa"),
        ("Barista, Starbucks, Toronto | 2022 - 2024", "Barista", "Starbucks", "Toronto"),
    ],
)
def test_a_trailing_city_is_the_location_not_the_employer(line, title, employer, location) -> None:
    (draft,) = split_roles(_section([line, "- Did the work"]), as_of=AS_OF)
    assert (draft.title_raw, draft.employer, draft.location) == (title, employer, location)


@pytest.mark.parametrize(
    "second, employer, location",
    [
        ("DESJARDINS    Montréal, QC", "DESJARDINS", "Montréal, QC"),
        ("Desjardins    Montréal, QC", "Desjardins", "Montréal, QC"),
        ("SHOPIFY INC.\tOttawa, ON", "SHOPIFY INC.", "Ottawa, ON"),
        ("Shopify Ottawa, ON", "Shopify", "Ottawa, ON"),
    ],
)
def test_a_two_column_employer_line_keeps_the_employer(second, employer, location) -> None:
    lines = ["Software Developer Intern    May 2023 – Aug 2023", second, "- Built a React dashboard"]
    (draft,) = split_roles(_section(lines), as_of=AS_OF)
    assert (draft.title_raw, draft.employer, draft.location) == ("Software Developer Intern", employer, location)


@pytest.mark.parametrize(
    "title, employer",
    [("President", "Robotics Club"), ("President", "Women in STEM"), ("Founder & CEO", "StudyBuddy"),
     ("VP Finance", "Enactus Laurier"), ("Director of Sponsorship", "Hack the North"),
     ("Director of Engineering", "Northwind Labs"), ("President", "")],
)
def test_executive_titles_at_student_or_unknown_organisations_abstain(deps, title, employer) -> None:
    assert _build(_draft(title, employer, "", "Sep 2025 – Present"), deps).seniority is None


def test_executive_titles_at_known_employers_keep_their_level(deps) -> None:
    assert _build(_draft("Director of Engineering", "Shopify", "", "2020 – Present"), deps).seniority.code == "M4"
    assert _build(_draft("Treasurer", "Robotics Club", "", "2024 – Present"), deps).seniority.code == "IC0"


@pytest.mark.parametrize(
    "line, skills",
    [
        ("Campus Eats | React, Node.js, PostgreSQL | Jan 2024 – Apr 2024", {"skill.react", "skill.nodejs"}),
        ("Capstone: Autonomous Rover — ROS, C++ | Sep 2024 – Apr 2025", {"skill.cpp"}),
    ],
)
def test_a_header_tech_stack_is_skills_not_an_employer(deps, line, skills) -> None:
    (draft,) = split_roles(_section([line, "- Built it"], SectionKind.PROJECTS), as_of=AS_OF)
    role = _build(draft, deps)
    assert role.employer == ""
    title_ids = {ref.canonical_id for ref in role.skills if ref.evidence is Evidence.ROLE_TITLE}
    assert skills <= title_ids


@pytest.mark.parametrize("first", ["DESJARDINS    Montréal, QC", "Desjardins\tMontréal, QC"])
def test_a_two_column_employer_line_above_the_title(first) -> None:
    lines = [first, "Data Analyst Intern | May 2023 – Aug 2023", "- Automated reports"]
    (draft,) = split_roles(_section(lines), as_of=AS_OF)
    assert (draft.title_raw, draft.employer, draft.location) == ("Data Analyst Intern", first.split()[0], "Montréal, QC")
