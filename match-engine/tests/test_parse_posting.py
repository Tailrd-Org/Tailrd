"""Job-posting parser (Task 6.5; SPEC §2, §4, §14.4, §16; DECISIONS D-31, D-60..D-64, D-100..D-106).

Sections and modal phrases decide necessity; OR-groups, years, education, credentials, gates and
the posting scalars. Every posting here is invented. The §14.4 rows themselves are in
tests/test_posting_pathologies.py.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from matcher.errors import Flag
from matcher.models import (
    ClearanceGate,
    DegreeLevel,
    LicenseGate,
    LocationGate,
    Necessity,
    RemotePolicy,
    Track,
    WorkAuthGate,
    canonical_json,
)
from matcher.parse.posting import PostingMeta, clean_description
from tests.posting_helpers import SEATTLE, TORONTO, env, necessities, parse, reqs

R, P, IM = "REQUIRED", "PREFERRED", "IMPLIED"


def _gates(posting, kind):
    return [g for g in posting.gates if isinstance(g, kind)]


# ─── sections (brief behaviour 1) ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("header", [
    "Requirements", "Qualifications:", "What you'll need", "Must have", "Basic Qualifications",
    "Minimum qualifications", "**Requirements**", "## What You'll Need", "REQUIREMENTS:",
    "Exigences", "Qualifications requises", "Your Qualifications",
])
def test_required_headers(header: str) -> None:
    posting = parse(f"{header}\n- Experience with Docker\n- Kubernetes")
    assert necessities(posting) == {"skill.docker": R, "skill.kubernetes": R}


@pytest.mark.parametrize("header", [
    "Preferred qualifications", "Nice to have", "Bonus", "Bonus points", "Pluses", "Assets", "Atouts",
    "Nice-to-haves:", "Preferred Qualifications",
])
def test_preferred_headers(header: str) -> None:
    posting = parse(f"{header}\n- Experience with Docker\n- Kubernetes")
    assert necessities(posting) == {"skill.docker": P, "skill.kubernetes": P}


@pytest.mark.parametrize("header", ["Responsibilities", "What you'll do", "The role", "Tâches", "Your Role"])
def test_responsibility_headers_are_implied(header: str) -> None:
    posting = parse(f"{header}\n- Build services with Docker and Kubernetes")
    assert necessities(posting) == {"skill.docker": IM, "skill.kubernetes": IM}


@pytest.mark.parametrize("header", ["About us", "Benefits", "Perks", "Equal opportunity employer", "How to apply",
                                    "What we offer", "À propos de nous", "Avantages"])
def test_ignored_headers_yield_nothing(header: str) -> None:
    posting = parse(f"Requirements\n- Python\n{header}\n- We use Docker and Kubernetes every day")
    assert necessities(posting) == {"skill.python": R}


def test_a_modal_phrase_overrides_a_responsibility_section() -> None:
    posting = parse("What you'll do\n- Build things. You will need strong SQL skills.\n- Ship features in Python")
    assert necessities(posting) == {"skill.sql": R, "skill.python": IM}


def test_inline_labels_open_a_section() -> None:
    posting = parse("Requirements: Python, SQL\nNice to have: Docker\nBenefits: Kubernetes training")
    assert necessities(posting) == {"skill.python": R, "skill.sql": R, "skill.docker": P}


def test_a_label_inside_a_bullet_holds_for_that_bullet_only() -> None:
    posting = parse("Requirements\n- Bonus: Docker\n- Kubernetes")
    assert necessities(posting) == {"skill.docker": P, "skill.kubernetes": R}


def test_a_colon_line_naming_a_section_opens_it() -> None:
    posting = parse("Requirements for this role include:\n- Python\nBonus points if you have:\n- Docker")
    assert necessities(posting) == {"skill.python": R, "skill.docker": P}


def test_a_preferred_phrase_inside_requirements_softens_the_clause() -> None:
    posting = parse("Requirements\n- Python\n- Kafka is a plus\n- Experience with Docker is an asset")
    assert necessities(posting) == {"skill.python": R, "skill.kafka": P, "skill.docker": P}


def test_a_required_phrase_inside_a_preferred_section_stays_preferred() -> None:
    posting = parse("Nice to have\n- You must know Docker")
    assert necessities(posting) == {"skill.docker": P}


def test_a_skill_named_like_a_cue_is_not_a_cue() -> None:
    # "asset" inside "asset management" (a skill) is no "asset" cue
    posting = parse("Requirements\n- Experience in asset management")
    assert all(r.necessity is Necessity.REQUIRED for r in posting.requirements)
    assert posting.requirements


# ─── prose (Review Focus #5) ──────────────────────────────────────────────────────────────


def test_prose_postings_are_read_through_modal_phrases() -> None:
    text = ("We are hiring a developer to join our platform team. You must have strong Python skills. "
            "Experience with Docker is a bonus. Our services run on Kubernetes.")
    posting = parse(text)
    assert necessities(posting) == {"skill.python": R, "skill.docker": P, "skill.kubernetes": IM}
    assert Flag.POSTING_UNSCORABLE not in posting.flags


@pytest.mark.parametrize("sentence, expected", [
    ("Minimum 2 years with Python.", R),
    ("You have solid Python skills.", R),
    ("You'll need Python.", R),
    ("Python is required.", R),
    ("Python is nice to have.", P),
    ("Python is preferred.", P),
    ("Python would be a plus.", P),
    ("Python is considered an asset.", P),
    ("The team writes Python.", IM),
    ("Vous devez maîtriser Python.", R),
    ("La connaissance de Python est un atout.", P),
])
def test_modal_phrases_in_prose(sentence: str, expected: str) -> None:
    assert necessities(parse(sentence))["skill.python"] == expected


@pytest.mark.parametrize("sentence", [
    "Willingness to learn Kubernetes.",
    "You will have the opportunity to work with Kubernetes.",
    "No Kubernetes experience required.",
    "Interest in Kubernetes.",
    "If you have Kubernetes experience, tell us about it.",
])
def test_learning_interest_and_negation_never_require(sentence: str) -> None:
    posting = parse(f"Requirements\n- {sentence}")
    assert necessities(posting).get("skill.kubernetes") == IM


def test_a_bare_list_takes_the_phrase_that_ends_it() -> None:
    assert necessities(parse("Java, Python or Go is required.")) == {"skill.java": R, "skill.python": R, "skill.go": R}
    assert necessities(parse("Experience with Java, Spring and Hibernate is a must.")) == {
        "skill.java": R, "skill.spring": R, "skill.hibernate": R}


def test_a_bare_list_after_a_phrase_takes_it() -> None:
    assert necessities(parse("Must have: Python, SQL")) == {"skill.python": R, "skill.sql": R}


@pytest.mark.parametrize("line, expected", [
    ("- Rust", {"skill.rust": R}),
    ("- Swift, Spark or React", {"skill.swift": R, "skill.spark": R, "skill.react": R}),
    ("- Excel at problem solving", {"skill.problem_solving": R}),
    ("- Go above and beyond for our clients", {}),
])
def test_a_bare_list_is_a_skills_list_but_a_sentence_is_prose(line: str, expected: dict) -> None:
    assert necessities(parse(f"Requirements\n{line}")) == expected


def test_a_new_clause_does_not_lend_its_phrase_backwards() -> None:
    posting = parse("Strong Python skills, experience with Docker is a plus.")
    assert necessities(posting) == {"skill.python": IM, "skill.docker": P}


# ─── strongest necessity (brief behaviour 2) ──────────────────────────────────────────────


def test_the_same_skill_keeps_its_strongest_necessity() -> None:
    text = "What you'll do\n- Write Python daily\nNice to have\n- Python\nRequirements\n- Python"
    posting = parse(text)
    assert necessities(posting) == {"skill.python": R}
    assert len(posting.requirements) == 1


# ─── OR-groups (brief behaviour 3; D-31) ──────────────────────────────────────────────────


def _group_members(posting):
    groups: dict[str, set[str]] = {}
    for r in posting.requirements:
        if r.or_group:
            groups.setdefault(r.or_group, set()).add(r.skill_id)
    return groups


@pytest.mark.parametrize("text, members", [
    ("Requirements\n- Python or Java", {"skill.python", "skill.java"}),
    ("Requirements\n- Python/Java", {"skill.python", "skill.java"}),
    ("Requirements\n- Java, Python, or Go", {"skill.java", "skill.python", "skill.go"}),
    ("Requirements\n- One of Java, Python and Go", {"skill.java", "skill.python", "skill.go"}),
    ("Requirements\n- A cloud such as AWS, GCP or Azure", {"skill.aws", "skill.gcp", "skill.azure"}),
    ("Requirements\n- A cloud platform (e.g. AWS, GCP, Azure)", {"skill.aws", "skill.gcp", "skill.azure"}),
])
def test_or_groups(text: str, members: set[str]) -> None:
    posting = parse(text)
    assert list(_group_members(posting).values()) == [members]
    assert all(r.necessity is Necessity.REQUIRED for r in posting.requirements)


def test_an_and_list_is_not_a_group() -> None:
    posting = parse("Requirements\n- Python, SQL and Docker")
    assert _group_members(posting) == {}
    assert necessities(posting) == {"skill.python": R, "skill.sql": R, "skill.docker": R}


def test_or_pairs_inside_a_longer_and_list() -> None:
    posting = parse("Requirements\n- Python, SQL, and AWS or GCP")
    assert list(_group_members(posting).values()) == [{"skill.aws", "skill.gcp"}]
    assert reqs(posting)["skill.python"].or_group is None


def test_or_equivalent_is_a_one_member_group() -> None:
    posting = parse("Requirements\n- Jira or equivalent")
    jira = reqs(posting)["skill.jira"]
    assert jira.or_group == "or:1" and jira.or_equivalent
    posting = parse("Requirements\n- AWS, GCP, or other cloud providers")
    assert list(_group_members(posting).values()) == [{"skill.aws", "skill.gcp"}]
    assert all(r.or_equivalent for r in posting.requirements)


def test_group_labels_are_numbered_by_appearance_and_never_skill_ids() -> None:
    posting = parse("Requirements\n- Python or Java\n- AWS or GCP\n- Docker")
    labels = {r.skill_id: r.or_group for r in posting.requirements}
    assert labels == {"skill.python": "or:1", "skill.java": "or:1", "skill.aws": "or:2", "skill.gcp": "or:2",
                      "skill.docker": None}
    ids = {r.skill_id for r in posting.requirements}
    assert not ids & {r.or_group for r in posting.requirements}


def test_groups_sharing_a_member_merge() -> None:
    posting = parse("Requirements\n- Java or Kotlin\n- Java or Scala")
    assert list(_group_members(posting).values()) == [{"skill.java", "skill.kotlin", "skill.scala"}]


def test_a_group_whose_member_is_required_alone_leaves_no_lone_alternative() -> None:
    # "Python" is required anyway, so "Python or Go" asks nothing more: Go must not become a lone
    # REQUIRED requirement the posting never stated.
    posting = parse("Requirements\n- Python\n- Python or Go")
    assert necessities(posting) == {"skill.python": R, "skill.go": IM}
    assert _group_members(posting) == {}


# ─── years (brief behaviour 4) ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("line, years", [
    ("3+ years of Python", 3),
    ("3+ years of experience with Python", 3),
    ("Python (2+ years)", 2),
    ("Minimum of 4 years of Python experience", 4),
    ("At least two years of Python", 2),
    ("1-2 years of Python", 1),
    ("Au moins 3 ans d'expérience en Python", 3),
])
def test_years_of_a_skill(line: str, years: int) -> None:
    posting = parse(f"Requirements\n- {line}")
    assert reqs(posting)["skill.python"].min_years == years
    assert posting.min_years_total is None


def test_years_apply_to_a_whole_list_after_them() -> None:
    posting = parse("Requirements\n- 3+ years of experience with Python and Django")
    assert reqs(posting)["skill.python"].min_years == 3
    assert reqs(posting)["skill.django"].min_years == 3


@pytest.mark.parametrize("line, total", [
    ("3+ years of experience", 3.0),
    ("Minimum 2 years of professional experience", 2.0),
    ("At least 5 years of relevant work experience", 5.0),
    ("1-2 years of experience", 1.0),
    ("0-2 years of experience", 0.0),
    ("2 ans d'expérience", 2.0),
])
def test_total_years(line: str, total: float) -> None:
    posting = parse(f"Requirements\n- {line}\n- Python")
    assert posting.min_years_total == total


@pytest.mark.parametrize("line", [
    "Must be at least 18 years of age",
    "A 4-year degree in Computer Science",
    "This is a 2-year contract",
    "2 years of college",
    "Python skills gained within the last 2 years",
])
def test_years_that_are_not_experience(line: str) -> None:
    posting = parse(f"Requirements\n- {line}\n- Python")
    assert posting.min_years_total is None
    assert reqs(posting)["skill.python"].min_years is None


def test_total_years_come_from_required_clauses_only() -> None:
    posting = parse("Requirements\n- Python\nNice to have\n- 5+ years of experience")
    assert posting.min_years_total is None
    posting = parse("Requirements\n- 3+ years of experience\n- 1+ years of experience in a startup")
    assert posting.min_years_total == 1.0  # the smallest stated: never a phantom higher bar


def test_years_stated_only_in_a_weaker_context_do_not_carry_over() -> None:
    posting = parse("Requirements\n- Python\nNice to have\n- 5+ years of Python")
    assert reqs(posting)["skill.python"].necessity is Necessity.REQUIRED
    assert reqs(posting)["skill.python"].min_years is None


# ─── education (brief behaviour 5) ────────────────────────────────────────────────────────


@pytest.mark.parametrize("line, level, fields, equivalent", [
    ("Bachelor's degree in Computer Science or a related field", DegreeLevel.BACHELOR, ("field.computer_science",), False),
    ("BS/MS in Computer Science", DegreeLevel.BACHELOR, ("field.computer_science",), False),
    ("Currently pursuing a Bachelor's or Master's degree in Computer Engineering", DegreeLevel.BACHELOR,
     ("field.computer_engineering",), False),
    ("Degree in Computer Science or equivalent experience", DegreeLevel.BACHELOR, ("field.computer_science",), True),
    ("Undergraduate degree in Mathematics", DegreeLevel.BACHELOR, ("field.mathematics",), False),
    ("PhD in Physics", DegreeLevel.DOCTORATE, ("field.physics",), False),
    ("College diploma in Computer Science", DegreeLevel.ASSOCIATE, ("field.computer_science",), False),
    ("Baccalauréat en informatique", DegreeLevel.BACHELOR, ("field.computer_science",), False),
])
def test_education_requirements(line, level, fields, equivalent) -> None:
    edu = parse(f"Requirements\n- {line}\n- Python").education_requirement
    assert edu is not None
    assert (edu.min_level, edu.field_ids, edu.or_equivalent, edu.necessity) == (level, fields, equivalent,
                                                                                Necessity.REQUIRED)


def test_schooling_without_a_degree_word() -> None:
    posting = parse("Requirements\n- Enrolled in a post-secondary program in Statistics or Economics\n- SQL")
    edu = posting.education_requirement
    assert edu is not None and edu.min_level is DegreeLevel.ASSOCIATE
    assert edu.field_ids == ("field.statistics", "field.economics")
    assert set(reqs(posting)) == {"skill.sql"}  # the fields are not skill requirements



def test_master_preferred() -> None:
    edu = parse("Nice to have\n- Master's degree in Statistics").education_requirement
    assert edu is not None and edu.min_level is DegreeLevel.MASTER and edu.necessity is Necessity.PREFERRED
    edu = parse("Requirements\n- Bachelor's degree required; Master's preferred").education_requirement
    assert edu is not None and edu.min_level is DegreeLevel.BACHELOR and edu.necessity is Necessity.REQUIRED


def test_a_field_named_in_a_degree_line_is_not_a_skill_requirement() -> None:
    posting = parse("Requirements\n- Bachelor's degree in Statistics\n- Python")
    assert "skill.statistics" not in reqs(posting)
    assert posting.education_requirement.field_ids == ("field.statistics",)


@pytest.mark.parametrize("line", ["Scrum Master certification", "Experience with master data management",
                                  "Our team includes PhDs and Master's graduates"])
def test_words_that_are_not_degree_requirements(line: str) -> None:
    posting = parse(f"Requirements\n- {line}\n- Python") if "team" not in line else parse(f"{line}. Python.")
    assert posting.education_requirement is None


# ─── credentials (brief behaviour 5) ──────────────────────────────────────────────────────


def test_a_held_credential_is_required_and_gated() -> None:
    posting = parse("Requirements\n- Valid CPA designation\n- Accounting", title="Senior Accountant")
    assert posting.certifications_required == ("cred.cpa",)
    assert [g.credential_ids for g in _gates(posting, LicenseGate)] == [("cred.cpa",)]
    assert "cred.cpa" not in reqs(posting)  # a credential is never a skill requirement


def test_credential_alternatives_share_one_gate() -> None:
    posting = parse("Requirements\n- P.Eng. or EIT designation")
    gate = _gates(posting, LicenseGate)
    assert len(gate) == 1 and set(gate[0].credential_ids) == {"cred.professional_engineer", "cred.eit"}


def test_a_credential_opening_its_clause_is_held() -> None:
    posting = parse("Qualifications\n- RN with 3+ years of acute care experience", title="Registered Nurse")
    assert posting.certifications_required == ("cred.registered_nurse",)


def test_a_mentioned_credential_is_not_a_licence_requirement() -> None:
    posting = parse("Requirements\n- Experience working under RN supervision", title="Care Aide")
    assert posting.certifications_required == () and not _gates(posting, LicenseGate)


def test_a_credential_with_another_way_in_is_required_but_gates_nothing() -> None:
    posting = parse("Requirements\n- CPA designation or equivalent", title="Accountant")
    assert posting.certifications_required == ("cred.cpa",) and not _gates(posting, LicenseGate)
    posting = parse("Requirements\n- CPA designation or Excel", title="Accountant")
    assert not _gates(posting, LicenseGate)


def test_a_valid_drivers_licence_is_a_licence_gate() -> None:
    posting = parse("Requirements\n- You have a valid driver's licence and access to a vehicle")
    assert posting.certifications_required == ("cred.drivers_license",)
    assert [g.credential_ids for g in _gates(posting, LicenseGate)] == [("cred.drivers_license",)]


def test_preferred_credentials() -> None:
    posting = parse("Nice to have\n- AWS Certified Solutions Architect")
    assert posting.certifications_preferred == ("cred.aws_saa",)
    assert posting.certifications_required == () and not _gates(posting, LicenseGate)


# ─── gates (brief behaviour 6; D-60..D-64, Task 3.4 review) ───────────────────────────────


@pytest.mark.parametrize("sentence, countries, sponsorship, citizenship", [
    ("Must be legally authorized to work in Canada.", ("CA",), None, False),
    ("Applicants must be eligible to work in the United States.", ("US",), None, False),
    ("You must be legally authorized to work in the country.", (), None, False),
    ("We are unable to sponsor visas.", (), False, False),
    ("No sponsorship is available for this role.", (), False, False),
    ("This role does not offer visa sponsorship.", (), False, False),
    ("Visa sponsorship is available.", (), True, False),
    ("Must be a U.S. citizen.", ("US",), None, True),
    ("Canadian citizenship is required.", ("CA",), None, True),
    ("Must be a Canadian citizen or permanent resident.", ("CA",), None, False),
    ("U.S. Person status required (citizen or green card holder).", ("US",), None, False),
])
def test_work_authorization(sentence, countries, sponsorship, citizenship) -> None:
    gates = _gates(parse(f"Requirements\n- Python\n{sentence}"), WorkAuthGate)
    assert gates == [WorkAuthGate(countries=countries, sponsorship_available=sponsorship,
                                  citizenship_required=citizenship)]


def test_citizenship_without_a_country_takes_the_postings_country() -> None:
    gates = _gates(parse("Requirements\n- Python\nCitizenship is required.", locations=(SEATTLE,)), WorkAuthGate)
    assert gates == [WorkAuthGate(countries=("US",), citizenship_required=True)]


@pytest.mark.parametrize("sentence", [
    "We do not discriminate on the basis of race, religion, national origin or citizenship status.",
    "Our customers across the United States rely on us.",
    "Will you now or in the future require sponsorship?",
    "We are authorized resellers in Canada.",
    "Come work for us!",
])
def test_sentences_that_are_no_work_authorization_gate(sentence: str) -> None:
    assert not _gates(parse(f"Requirements\n- Python\n{sentence}"), WorkAuthGate)


def test_conflicting_sponsorship_statements_are_unknown() -> None:
    gates = _gates(parse("Requirements\n- Python\nWe cannot sponsor H-1B visas. Sponsorship is available for TN."),
                   WorkAuthGate)
    assert gates == [WorkAuthGate(sponsorship_available=None)]


@pytest.mark.parametrize("sentence, level, active, where", [
    ("Must hold an active Secret clearance.", "US_SECRET", True, SEATTLE),
    ("Active TS/SCI clearance required.", "US_TS_SCI", True, TORONTO),
    ("Must be able to obtain a Top Secret clearance.", "US_TOP_SECRET", False, SEATTLE),
    ("Secret clearance required.", "CA_SECRET", False, TORONTO),
    ("Must be eligible for Reliability Status.", "CA_RELIABILITY", False, SEATTLE),
    ("Must hold a valid Government of Canada Enhanced Reliability clearance.", "CA_ENHANCED_RELIABILITY", True, TORONTO),
    ("Must currently hold a U.S. security clearance at the Secret level.", "US_SECRET", True, TORONTO),
    ("Must have an active security clearance.", "", True, TORONTO),
    ("Secret or Top Secret clearance required.", "US_SECRET", False, SEATTLE),
    ("Public Trust clearance required.", "US_PUBLIC_TRUST", False, TORONTO),
])
def test_clearance_levels_are_ladder_tokens(sentence, level, active, where) -> None:
    gates = _gates(parse(f"Requirements\n- Python\n- {sentence}", locations=(where,)), ClearanceGate)
    assert gates == [ClearanceGate(level=level, active_required=active)]
    ladder_tokens = {t for ladder in env()["config"].gates.security_clearance.ladders for t in ladder}
    assert all(g.level == "" or g.level in ladder_tokens for g in gates)


def test_a_bare_level_with_no_country_states_no_level() -> None:
    gates = _gates(parse("Requirements\n- Python\n- Secret clearance required.", locations=()), ClearanceGate)
    assert gates == [ClearanceGate(level="", active_required=False)]


@pytest.mark.parametrize("sentence", [
    "No security clearance required.",
    "Experience with customs clearance for freight.",
    "Our secret sauce is our people.",
    "A security clearance is not required.",
])
def test_no_clearance_gate(sentence: str) -> None:
    assert not _gates(parse(f"Requirements\n- Python\n- {sentence}"), ClearanceGate)


def test_a_preferred_clearance_is_no_gate() -> None:
    assert not _gates(parse("Requirements\n- Python\nNice to have\n- Active Secret clearance"), ClearanceGate)
    assert not _gates(parse("Requirements\n- Python\n- Secret clearance is a plus"), ClearanceGate)


def test_onsite_postings_gate_location() -> None:
    posting = parse("Requirements\n- Python", work_type="On Site", locations=(TORONTO,))
    assert _gates(posting, LocationGate) == [LocationGate(locations=(TORONTO,), onsite_required=True)]
    assert posting.remote_policy is RemotePolicy.ONSITE
    assert not _gates(parse("Requirements\n- Python", work_type="Remote"), LocationGate)
    assert not _gates(parse("Requirements\n- Python", work_type="onsite", locations=()), LocationGate)


# ─── metadata and scalars (brief behaviour 7) ─────────────────────────────────────────────


@pytest.mark.parametrize("raw, normal", [
    ("Remote", "remote"), ("Hybrid", "hybrid"), ("On Site", "onsite"), ("on-site", "onsite"), ("onsite", "onsite"),
    ("", ""), ("Flexible", ""), (None, ""),
])
def test_work_type_spellings(raw, normal) -> None:
    assert PostingMeta(work_type=raw).work_type == normal


def test_remote_policy() -> None:
    assert parse("Python.", work_type="Remote").remote_policy is RemotePolicy.REMOTE
    assert parse("Python.", work_type="Hybrid").remote_policy is RemotePolicy.HYBRID
    assert parse("Python.").remote_policy is RemotePolicy.UNKNOWN


def test_seniority_internship_family_and_title() -> None:
    posting = parse("Requirements\n- Python", title="Software Engineer Intern")
    assert posting.seniority is not None and posting.seniority.code == "IC0"
    assert posting.is_internship
    assert posting.title_canonical == "title.15-1252.00"
    assert posting.job_family == "Computer and Mathematical Occupations"
    posting = parse("Requirements\n- Python", title="Software Developer", experience_level="new_grad")
    assert posting.seniority.code == "IC1" and not posting.is_internship
    posting = parse("Requirements\n- Python", title="Data Analyst", experience_level="internship")
    assert posting.seniority.code == "IC0" and posting.is_internship
    posting = parse("Requirements\n- Python", title="Analyst", job_family="data")
    assert posting.job_family == "data"


def test_industry_from_the_company() -> None:
    assert parse("Python.", company="Shopify").industry_codes == ("5132",)
    junk = parse("Python.", company="Acme Widgets", company_description='{"size": 50}')
    assert junk.industry_codes == ()


def test_language() -> None:
    assert parse("Exigences\n- Vous maîtrisez Python et vous avez une connaissance de SQL.").language == "fr"
    assert parse("Requirements\n- Python").language == "en"


# ─── HTML and text (Tailrd data facts) ────────────────────────────────────────────────────


def test_escaped_html_is_decoded_and_stripped() -> None:
    raw = ("&lt;h3&gt;Requirements&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Python &amp;amp; SQL&lt;/li&gt;"
           "&lt;li&gt;Docker&lt;/li&gt;&lt;/ul&gt;&lt;p&gt;Benefits&lt;/p&gt;&lt;p&gt;Kubernetes training&lt;/p&gt;")
    text = clean_description(raw, env()["parse_config"].posting).text
    assert "<" not in text and "&lt;" not in text and "&amp;" not in text
    assert necessities(parse(raw)) == {"skill.python": R, "skill.sql": R, "skill.docker": R}


def test_raw_html_lists_become_bullets() -> None:
    raw = "<div><b>Requirements</b><ul><li>Python</li><li>Docker</li></ul><script>var k8s='Kubernetes';</script></div>"
    lines = [line for line in clean_description(raw, env()["parse_config"].posting).text.splitlines() if line]
    assert lines == ["Requirements", "- Python", "- Docker"]
    assert necessities(parse(raw)) == {"skill.python": R, "skill.docker": R}


def test_flattened_bullets_are_split() -> None:
    posting = parse("Requirements • Python • Docker is a plus")
    assert necessities(posting) == {"skill.python": R, "skill.docker": P}


# ─── coverage tokens (brief behaviour 9; SPEC §16) ────────────────────────────────────────


def test_unresolved_requirement_tokens() -> None:
    posting = parse("Requirements\n- Experience with Python and Zorblex\n- Knowledge of FooQL\n"
                    "- Experience in a fast-paced environment\nResponsibilities\n- Use QuuxDB daily")
    assert posting.unresolved_requirement_tokens == ("FooQL", "Zorblex")
    assert posting.requirement_token_count == 3  # python + the two unresolved


def test_tech_shaped_tokens_count_but_common_acronyms_do_not() -> None:
    posting = parse("Requirements\n- Python, GraphZap and ZXQ\n- Based in the USA, report to the CEO")
    assert set(posting.unresolved_requirement_tokens) == {"GraphZap", "ZXQ"}


def test_coverage_counts_only_required_and_preferred_clauses() -> None:
    posting = parse("Responsibilities\n- Use Python and Zorblex")
    assert posting.requirement_token_count == 0 and posting.unresolved_requirement_tokens == ()


# ─── determinism and totality (SPEC §0, §13) ──────────────────────────────────────────────


def test_deterministic() -> None:
    text = "Requirements\n- Python or Java\n- 3+ years of experience\nNice to have\n- Docker"
    assert canonical_json(parse(text)) == canonical_json(parse(text))


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.text(max_size=400), st.text(max_size=60))
def test_any_text_parses(description: str, title: str) -> None:
    posting = parse(description, title=title)
    assert posting.requirement_token_count >= len(posting.unresolved_requirement_tokens)
    for r in posting.requirements:
        if r.or_group:
            assert r.or_group.startswith("or:")


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(st.sampled_from([
    "Requirements", "Nice to have", "- Python or Java", "- 3+ years of Go", "Must be a U.S. citizen.",
    "<li>Docker</li>", "&lt;p&gt;SQL&lt;/p&gt;", "- Bachelor's degree in Computer Science", "Secret clearance",
    "- RN required", "Benefits", "...", "- AWS, GCP, or other clouds", ";", "• React",
]), max_size=12))
def test_posting_fragments_parse(lines: list[str]) -> None:
    posting = parse("\n".join(lines), title="Junior Developer")
    assert posting.seniority is not None and Track(posting.seniority.track) is Track.IC
