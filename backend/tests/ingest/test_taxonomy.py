"""ingest/taxonomy.py rules (data/taxonomy.json): accuracy on a hand-labeled
sample of real postings, the guard that today's internship and new-grad rows
keep their labels, and the boundary rules one by one.

labels.json holds 550 postings collected from the live registry boards on
2026-10-25 (220 Canadian, 330 US), each labeled from the posting alone
before the classifier existed. The gates are the spec's: 90% on the primary
career family, 85% on the experience level."""

import json
import time
from collections import Counter
from pathlib import Path

import pytest

import backend.tests.test_entry_level_classifier as entry_level_cases
from backend.ingest import taxonomy
from backend.services.ats_scraper import entry_tier, experience_level_for

LABELS = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "ingest" / "labels.json").read_text(encoding="utf-8"))
RULES = json.loads(
    (Path(taxonomy.__file__).resolve().parent / "data" / "taxonomy.json").read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def setup_db():
    """No database here: the classifiers are pure functions, so this module
    skips the root conftest's per-test create_all/drop_all."""
    yield


def _misses(rows, got, want):
    return "\n".join(f"  {row['title']!r}: labeled {row[want]}, got {value}"
                     for row, value in zip(rows, got) if value != row[want])


# ─── The labeled sample ───────────────────────────────────────────────────


def test_the_labeled_sample_covers_every_family_and_level():
    assert len(LABELS) >= 200
    assert len({row["id"] for row in LABELS}) == len(LABELS)
    families = Counter(row["label_family"] for row in LABELS)
    levels = Counter(row["label_level"] for row in LABELS)
    assert set(families) == set(taxonomy.FAMILIES)
    assert set(levels) == set(taxonomy.LEVELS)
    assert min(families.values()) >= 5, families
    assert min(levels.values()) >= 15, levels
    for row in LABELS:
        assert row["title"] and len(row["description"]) <= 600


def test_family_accuracy_on_the_labeled_sample():
    got = [taxonomy.classify_family(row["title"], row["department"], row["description"])[0]
           for row in LABELS]
    right = sum(value == row["label_family"] for row, value in zip(LABELS, got))
    assert right / len(LABELS) >= 0.90, (
        f"{right}/{len(LABELS)} families right\n" + _misses(LABELS, got, "label_family"))


def test_experience_accuracy_on_the_labeled_sample():
    # As the pipeline calls it: the description's years back up a title
    # that names no level.
    got = [taxonomy.classify_experience(row["title"], row["department"], row["employment_type"],
                                        taxonomy.years_required(row["description"]))
           for row in LABELS]
    right = sum(value == row["label_level"] for row, value in zip(LABELS, got))
    assert right / len(LABELS) >= 0.85, (
        f"{right}/{len(LABELS)} levels right\n" + _misses(LABELS, got, "label_level"))


# ─── Today's internship and new-grad rows ─────────────────────────────────


def _entry_level_titles():
    """(title, department, employment_type) for every case the scraper's
    entry-level tests pin, every labeled posting, and a few more."""
    cases = set()
    for value in vars(entry_level_cases).values():
        for mark in getattr(value, "pytestmark", []):
            if mark.name != "parametrize":
                continue
            names = [name.strip() for name in mark.args[0].split(",")]
            for params in mark.args[1]:
                params = getattr(params, "values", params)
                params = params if isinstance(params, tuple) else (params,)
                case = dict(zip(names, params))
                # Its 40,000-space titles are speed cases, read below.
                if isinstance(case.get("title"), str) and len(case["title"]) <= 300:
                    cases.add((case["title"], case.get("department") or "",
                               case.get("employment_type") or ""))
    cases.update((row["title"], row["department"], row["employment_type"]) for row in LABELS)
    cases.update([
        ("Payload Systems Analyst", "Payload Internships", ""),
        ("Software Engineer", "Early Career FT", ""),
        ("Data Engineer", "", "Intern"),
        ("Junior Accountant, Assurance (Winter 2027 or Fall 2027)", "", ""),
        ("Associate Product Manager", "", ""),
    ])
    return sorted(cases)


ENTRY_LEVEL_TITLES = _entry_level_titles()


def test_the_guard_corpus_holds_both_tiers():
    tiers = Counter(entry_tier(*case) for case in ENTRY_LEVEL_TITLES)
    assert tiers["strong"] >= 100 and tiers["weak"] >= 50, tiers


def test_todays_internship_and_new_grad_rows_keep_their_labels():
    broken = []
    for title, department, employment_type in ENTRY_LEVEL_TITLES:
        tier = entry_tier(title, department, employment_type)
        level = taxonomy.classify_experience(title, department, employment_type)
        if tier == "strong":
            # A named track (intern, co-op, new grad, apprentice, a season's
            # term) is stored with today's label.
            today = experience_level_for(title, department, employment_type)
            if level != today:
                broken.append(f"{title!r} ({department!r}, {employment_type!r}): {today} -> {level}")
        elif tier == "weak" and level not in ("entry", "internship", "new_grad"):
            # A level word (junior, analyst, associate, level I) moves from
            # new_grad to entry (DECISIONS.md 4) unless the title names a track.
            broken.append(f"{title!r} ({department!r}, {employment_type!r}): weak tier -> {level}")
    assert not broken, "\n".join(broken)


# ─── Experience levels, rule by rule ──────────────────────────────────────


@pytest.mark.parametrize("title,department,employment_type,level", [
    ("Software Engineering Intern, Frontend", "", "", "internship"),
    ("Frontend Developer Intern, Winter 2027", "", "", "internship"),
    ("Stagiaire Audit interne", "", "", "internship"),
    ("RF Validation Associate (Winter 2027)", "", "", "internship"),
    ("Data Engineer", "", "Intern", "internship"),
    ("New Grad Software Engineer", "", "", "new_grad"),
    ("Associate Product Manager, New Grad (2027 Start)", "", "", "new_grad"),
    ("Brex Rotational Program", "Risk & Credit", "", "new_grad"),
    ("Early Career Welder Trainee", "", "", "new_grad"),
    ("Junior Accountant, Assurance (Winter 2027 or Fall 2027)", "", "", "new_grad"),
])
def test_named_tracks_keep_todays_labels(title, department, employment_type, level):
    assert taxonomy.classify_experience(title, department, employment_type) == level


@pytest.mark.parametrize("title,level", [
    ("MBA Marketing Summer Associate", "internship"),
    ("Summer Analyst, Investment Banking", "internship"),
    ("Teaching Assistant - NEUR 631 - 001", "internship"),
    ("Software Engineer Intern - Sr. Design", "internship"),
    ("MBA Development Program - General Management", "new_grad"),
])
def test_tracks_the_entry_tier_does_not_name(title, level):
    assert taxonomy.classify_experience(title) == level


@pytest.mark.parametrize("title", [
    "Junior Data Analyst", "Entry-Level Accountant", "Software Engineer I", "Financial Analyst",
    "Associate Consultant", "Underwriting Associate", "Associate People Business Partner",
    "Jr. Sous Chef", "Business Development Representative", "Administrative Coordinator",
    "Receptionist", "Returns Clerk", "Staff Accountant",
])
def test_level_words_and_entry_roles_are_entry(title):
    assert taxonomy.classify_experience(title) == "entry"


@pytest.mark.parametrize("title,employment_type", [
    ("Cashier - 931 Loop 274", ""),
    ("Production Associate - PM Shift", ""),
    ("Retail Store Associate Part Time", "Part time"),
    ("Liquor Barn - Key Holder, Russell Street", ""),
    ("Driver Pick-up & Delivery", ""),
    ("Health Care Aide (HCA) - Part Time (PT)", "Part time"),
    ("Housekeeping Aide - Full Time - Day Shift", ""),
    ("Overnight Customer Experience Representative, Concierge", ""),
    ("Technical Support Specialist", ""),
    ("Custodian I (2nd shift)", ""),
])
def test_frontline_work_without_a_level_is_entry(title, employment_type):
    assert taxonomy.classify_experience(title, "", employment_type) == "entry"


@pytest.mark.parametrize("title", [
    "Part-time Lecturer, Organic Chemistry",
    "Premium Support Engineer (NYC, Weekend Shift)",
    "Nurse Coordinator - Home Care",
])
def test_a_professional_is_not_frontline_for_its_hours(title):
    assert taxonomy.classify_experience(title) == "mid"


@pytest.mark.parametrize("title", [
    "Software Engineer II", "Laboratory Attendant 2", "Intermediate Developer",
    "Mid-Level Accountant", "Experienced FX/Forex Trader", "Legal Counsel (Private Markets)",
    "Associate Counsel, Employment", "Product Manager", "Account Manager, Mid-Market",
    "Customer Success Manager", "Product Marketing Associate Manager", "Associate Finance Manager",
    "Member of Technical Staff", "Military Derivatives Test Pilot (Experienced or Senior)",
    "Registered Nurse", "Software Engineer",
])
def test_mid_level(title):
    assert taxonomy.classify_experience(title) == "mid"


@pytest.mark.parametrize("title", [
    "Senior Software Engineer", "Sr. Accountant", "Systems Administrator Sr", "Software Engineer III",
    "Cook III", "Senior Product Manager, Mobile", "Senior Counsel", "Compliance, Senior Associate",
    "Senior/Staff Analytics Engineer", "Senior / Staff Product Engineer",
    "Epicor ERP Consultant (SME)", "Sr. Customer Experience Manager", "Senior Solution Architect",
])
def test_senior_level(title):
    assert taxonomy.classify_experience(title) == "senior"


@pytest.mark.parametrize("title", [
    "Staff Software Engineer", "Lead Engineer", "Principal Security Specialist", "Software Architect",
    "Engineering Manager", "Store Manager", "Senior Manager, Finance", "Senior Cybersecurity Manager",
    "Warehouse Supervisor", "SPVR ENVIRONMENTAL SERVICES", "Construction Superintendent",
    "Electrical General Foreman", "Chief of Staff", "Group Product Manager",
    "Team Lead, Customer Service", "Product Marketing Sr. Manager",
])
def test_lead_level(title):
    assert taxonomy.classify_experience(title) == "lead"


@pytest.mark.parametrize("title", [
    "Director of Engineering", "Associate Director, Brand Partnerships", "Head of Product",
    "Global Head of Solutions Engineering", "VP, Engineering", "Vice President of Operations",
    "AVP, Supply Chain Program Management", "Deputy Chief Regulatory Officer",
    "Deputy General Counsel - Legal", "VP, Business Group Chief Information Security Officer",
    "General Counsel & Vice President, Legal and Regulatory Affairs", "CEO Communications Director",
    "Chief of Staff to CIO & Head of PMO",
])
def test_director_level(title):
    assert taxonomy.classify_experience(title) == "director"


@pytest.mark.parametrize("title", [
    "Chief Financial Officer", "CEO, AI Services", "CISO", "President - RG&E",
    "Executive Vice President, Sales", "SVP, Marketing", "Senior Vice President, Marketing",
    "General Counsel", "Managing Partner", "Chief Nursing Officer", "Market CMO, Primary Care",
    "Provost & Senior Vice President for Academic Affairs",
])
def test_executive_level(title):
    assert taxonomy.classify_experience(title) == "executive"


@pytest.mark.parametrize("title,level", [
    ("Vice President, Sales", "director"),
    ("Account Executive", "mid"),
    ("Executive Assistant to the Chief Business Officer", "mid"),
    ("Chief Legal and Policy Advisor", "mid"),
])
def test_executive_words_that_are_not_executives(title, level):
    assert taxonomy.classify_experience(title) == level


@pytest.mark.parametrize("title,level", [
    ("Senior Penetration Tester, Vice President", "senior"),
    ("VP Senior Applied AI Full-Stack Engineer", "senior"),
    ("Product Manager - Workplace Platforms - Assistant Vice President", "mid"),
])
def test_a_banks_vice_president_rank_on_an_individual_role(title, level):
    assert taxonomy.classify_experience(title) == level


@pytest.mark.parametrize("title,level", [
    ("Senior Product Manager", "senior"),
    ("Product Marketing Manager", "mid"),
    ("Associate Influencer Marketing Manager", "entry"),
    ("Associate Finance Manager", "mid"),
    ("Manager, Customer Success", "lead"),
    ("Senior Discretionary Portfolio Manager", "senior"),
    ("Gestionnaire de projet - Automatisation et Robotique", "mid"),
])
def test_where_manager_lands(title, level):
    assert taxonomy.classify_experience(title) == level


@pytest.mark.parametrize("years,level", [
    (0, "entry"), (1, "entry"), (2, "mid"), (4, "mid"), (5, "senior"), (7, "senior"),
    (8, "lead"), (15, "lead"), (None, "mid"),
])
def test_years_back_up_a_title_without_a_level(years, level):
    assert taxonomy.classify_experience("Accountant", years_required=years) == level


def test_a_title_level_outranks_the_years():
    assert taxonomy.classify_experience("Senior Accountant", years_required=1) == "senior"
    assert taxonomy.classify_experience("Junior Accountant", years_required=8) == "entry"
    assert taxonomy.classify_experience("Software Engineering Intern", years_required=6) == "internship"


def test_bands_open_at_the_lower_band():
    assert taxonomy.classify_experience("Hwy Maintenance Worker/Senior - Lexington") == "entry"
    assert taxonomy.classify_experience("Senior or Staff Data Scientist") == "senior"


def test_empty_and_odd_input():
    assert taxonomy.classify_experience("") == "mid"
    assert taxonomy.classify_experience(None, None, None) == "mid"
    assert taxonomy.classify_experience("   Senior   Engineer  ") == "senior"


# ─── years_required ───────────────────────────────────────────────────────


@pytest.mark.parametrize("text,years", [
    ("You bring 3+ years of experience building web apps.", 3),
    ("A minimum of 5 years of experience in accounting.", 5),
    ("2-4 years of experience in a fast-paced role.", 2),
    ("2 to 5 years of related experience", 2),
    ("At least five (5) years of professional experience.", 5),
    ("Seven years' experience leading teams.", 7),
    ("5 or more years of progressive experience", 5),
    ("A minimum of 1 year of professional automotive repair experience", 1),
    ("0-2 years of experience", 0),
    ("10+ yrs exp with Java", 10),
    ("Vous avez au moins trois ans d'expérience en recouvrement.", 3),
    ("Applicants must be 21 years old; 2 years of experience preferred.", 2),
    ("5+ years of BA experience, 3+ years in P&C required.", 5),
])
def test_years_required(text, years):
    assert taxonomy.years_required(text) == years


@pytest.mark.parametrize("text", [
    "", None, "No experience required.", "Must be 18 years of age or older.",
    "For over 30 years, we have served our community.",
    "With 25+ years of experience, OpenTable is an industry leader.",
    "Founded in 2015, the company employs 300 people.",
    "Proudly 15 years in business, with experience you can trust.",
])
def test_years_required_ignores_what_is_not_a_requirement(text):
    assert taxonomy.years_required(text) is None


# ─── Career families, rule by rule ────────────────────────────────────────


@pytest.mark.parametrize("title,department,description,family", [
    ("Senior Software Engineer", "", "", "software_engineering"),
    ("Senior Frontend Engineer, Discovery UX", "", "", "software_engineering"),
    ("Lead Salesforce Solution Architect", "", "", "software_engineering"),
    ("Director of Product Marketing, Investing", "", "", "marketing_communications"),
    ("Senior Product Manager, Mobile", "", "", "product_design"),
    ("Solutions Engineer", "Sales", "", "sales_bizdev"),
    ("AI Security Engineer", "", "", "it_security_infrastructure"),
    ("Analyst, Internal Audit and Compliance", "", "", "finance_accounting"),
    ("Chief Compliance Officer", "", "", "legal"),
    ("Registered Nurse - Child & Adolescent Mental Health", "", "", "healthcare_clinical"),
    ("Chief Medical Officer, Mount Auburn Hospital", "", "", "healthcare_clinical"),
    ("Senior Medical Secretary - Outpatient Rehab", "", "", "healthcare_admin_support"),
    ("Dietary Aide- Per Diem", "", "", "hospitality_retail_food"),
    ("Full Time - Sales Associate - Hardware - Day", "", "", "hospitality_retail_food"),
    ("Retail Partner Manager", "", "", "sales_bizdev"),
    ("Inside Sales Representative", "", "", "sales_bizdev"),
    ("Public Affairs Advisor", "Policy", "", "government_public_sector"),
    ("Director, Public Relations & Communications", "", "", "marketing_communications"),
    ("Construction Superintendent", "", "", "skilled_trades"),
    ("Early Career HVAC Mechanical Engineer", "", "", "engineering"),
    ("Senior Payroll Specialist", "", "", "finance_accounting"),
    ("Talent Sourcer (Recruiter)", "", "", "hr_recruiting"),
    ("Creator Campaigns Recruiter Co-Op", "", "", "marketing_communications"),
    ("Executive Assistant to the CEO", "", "", "customer_support_admin"),
    ("Chief Operating Officer", "", "", "operations_supply_chain"),
    ("Complex Cyber Claim Specialist", "", "", "banking_insurance_investment"),
    ("Senior Research Associate", "Bio Development", "", "education_research"),
    ("Senior Research Scientist, Model Evaluation", "", "", "data_ai_analytics"),
])
def test_title_rules(title, department, description, family):
    assert taxonomy.classify_family(title, department, description)[0] == family


@pytest.mark.parametrize("title,description,family", [
    ("Housekeeping Aide", "One of the nation's most advanced health networks.", "healthcare_admin_support"),
    ("Housekeeping Attendant", "Our resort sits on the bay.", "hospitality_retail_food"),
])
def test_support_work_at_a_hospital_is_healthcare_administration(title, description, family):
    assert taxonomy.classify_family(title, "", description)[0] == family


@pytest.mark.parametrize("title,department,family", [
    ("Senior Manager, dbt Fusion", "Engineering Department", "software_engineering"),
    ("Head of Rental & Leasing", "Product Management", "product_design"),
    ("Equity Administrator", "Finance", "finance_accounting"),
    ("Chief Technology Officer", "Information Technology", "it_security_infrastructure"),
    ("Chief Technology Officer", "Engineering", "software_engineering"),
])
def test_the_department_decides_when_the_title_names_no_function(title, department, family):
    assert taxonomy.classify_family(title, department, "")[0] == family


def test_weak_title_words_come_after_the_department():
    assert taxonomy.classify_family("SVT/PV Engineer 1 - New Grad")[0] == "engineering"
    assert taxonomy.classify_family("Host", "Broadcasting - WBEZ")[0] == "marketing_communications"
    assert taxonomy.classify_family("Host, Hot Stove Club")[0] == "hospitality_retail_food"


@pytest.mark.parametrize("title,description,family", [
    ("Regional Manager", "Medavie operates ground ambulance and air medical transport.",
     "healthcare_admin_support"),
    ("District Manager - Orange County", "As a District Manager, you lead a portfolio of stores.",
     "hospitality_retail_food"),
    ("Analyst", "Division: Applied Research, Data & Analytics", "data_ai_analytics"),
    ("ODHS Intern", "Agency: Department of Human Services", "government_public_sector"),
])
def test_the_description_decides_last(title, description, family):
    assert taxonomy.classify_family(title, "", description)[0] == family


def test_the_description_is_read_to_600_characters():
    tail = "x " * 400 + "Our hospital network"
    assert taxonomy.classify_family("Regional Manager", "", tail) == ("other", None)


@pytest.mark.parametrize("title,description", [
    ("Custodian I (2nd shift)", "Our hospital is hiring."),
    ("Asset Protection Specialist - Cross County S/C", "Work the sales floor of our stores."),
    ("Managing Partner", "We build AI models and data tooling."),
    ("General Application", ""),
])
def test_other_only_when_no_family_applies(title, description):
    assert taxonomy.classify_family(title, "", description) == ("other", None)


def test_secondary_family():
    assert taxonomy.classify_family("Senior Data Analyst, Strategic Finance") == (
        "data_ai_analytics", "finance_accounting")
    assert taxonomy.classify_family("Software Engineer", "Engineering") == ("software_engineering", None)
    for row in LABELS:
        primary, secondary = taxonomy.classify_family(row["title"], row["department"], row["description"])
        assert primary in taxonomy.FAMILIES
        assert secondary is None or (secondary in taxonomy.FAMILIES and secondary not in (primary, "other"))


# ─── The rules file ───────────────────────────────────────────────────────


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def test_the_rules_file_names_only_known_families_and_levels():
    assert {rule["family"] for rule in RULES["families"]["rules"]} == set(taxonomy.FAMILIES)
    experience = RULES["experience"]
    assert experience["default"] in taxonomy.LEVELS
    assert {level for _, level in experience["years_to_level"]} <= set(taxonomy.LEVELS)
    assert set(experience["band_words"].values()) <= set(taxonomy.LEVELS)
    assert set(experience["numeral_levels"].values()) <= set(taxonomy.LEVELS)
    # A lone \b in JSON is a backspace, not a word boundary.
    assert not [text for text in _strings(RULES) if any(ord(char) < 32 for char in text)]


def test_the_rules_are_read_once():
    taxonomy._rules.cache_clear()
    taxonomy.classify_family("Software Engineer")
    taxonomy.classify_experience("Software Engineer")
    taxonomy.years_required("3+ years of experience")
    assert taxonomy._rules.cache_info().misses == 1


def test_pathological_input_is_read_quickly():
    started = time.monotonic()
    taxonomy.classify_experience("Engineer" + " " * 40_000 + "I")
    taxonomy.classify_family("Engineer" + " " * 40_000, " " * 40_000, " " * 40_000)
    taxonomy.years_required("1 " * 50_000 + "years")
    assert time.monotonic() - started < 2
