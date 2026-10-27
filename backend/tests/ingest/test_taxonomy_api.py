"""ingest/taxonomy.py: the public API every scraper v2 task codes against.

Pins what holds however the classifier rules evolve: the stored values,
their order, the signatures, the legacy job-function map, and that importing
the module costs nothing heavy. The rules have their own tests."""

import inspect
import os
import subprocess
import sys
from pathlib import Path

from backend.ingest import taxonomy
from backend.services.role_classifier import CANONICAL_CATEGORIES

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_levels_are_the_stored_values_most_junior_first():
    assert taxonomy.LEVELS == (
        "internship", "new_grad", "entry", "mid", "senior", "lead", "director", "executive",
    )


def test_families_are_slugs_with_labels_in_display_order():
    assert list(taxonomy.FAMILIES.items()) == [
        ("software_engineering", "Software Engineering"),
        ("data_ai_analytics", "Data, AI & Analytics"),
        ("it_security_infrastructure", "IT, Security & Infrastructure"),
        ("product_design", "Product & Design"),
        ("engineering", "Engineering"),
        ("legal", "Legal"),
        ("healthcare_clinical", "Healthcare, Clinical"),
        ("healthcare_admin_support", "Healthcare, Administration & Support"),
        ("finance_accounting", "Finance & Accounting"),
        ("banking_insurance_investment", "Banking, Insurance & Investment"),
        ("sales_bizdev", "Sales & Business Development"),
        ("marketing_communications", "Marketing & Communications"),
        ("operations_supply_chain", "Operations, Supply Chain & Logistics"),
        ("hr_recruiting", "Human Resources & Recruiting"),
        ("education_research", "Education & Research"),
        ("government_public_sector", "Government & Public Sector"),
        ("skilled_trades", "Skilled Trades, Construction & Manufacturing"),
        ("customer_support_admin", "Customer Support & Administrative"),
        ("hospitality_retail_food", "Hospitality, Retail & Food Service"),
        ("other", "Other"),
    ]
    # A slug fits scraped_jobs.career_family (VARCHAR(48)) and a
    # comma-separated filter parameter.
    assert all(len(slug) <= 48 and "," not in slug for slug in taxonomy.FAMILIES)


def test_enrichment_version_is_a_positive_int():
    assert isinstance(taxonomy.ENRICHMENT_VERSION, int) and taxonomy.ENRICHMENT_VERSION >= 1


def test_every_legacy_job_function_maps_to_families():
    assert taxonomy.LEGACY_CATEGORY_TO_FAMILIES == {
        "Software Engineering": ["software_engineering"],
        "Engineering and Development": ["engineering", "it_security_infrastructure"],
        "Data Analysis": ["data_ai_analytics"],
        "Machine Learning and AI": ["data_ai_analytics"],
        "Accounting and Finance": ["finance_accounting", "banking_insurance_investment"],
        "Management and Executive": ["operations_supply_chain"],
        "Sales": ["sales_bizdev"],
        "Marketing": ["marketing_communications"],
        "Human Resources": ["hr_recruiting"],
        "Product Management": ["product_design"],
        "Business Analyst": ["data_ai_analytics"],
        "Creatives and Design": ["product_design", "marketing_communications"],
        "Legal and Compliance": ["legal"],
        "Customer Service and Support": ["customer_support_admin"],
        "Operations": ["operations_supply_chain"],
        "Consultant": ["other"],
        "Other": ["other"],
    }
    assert set(taxonomy.LEGACY_CATEGORY_TO_FAMILIES) == set(CANONICAL_CATEGORIES)


def _params(function):
    return [(p.name, p.default) for p in inspect.signature(function).parameters.values()]


def test_the_classifiers_keep_their_signatures():
    empty = inspect.Parameter.empty
    assert _params(taxonomy.classify_experience) == [
        ("title", empty), ("department", ""), ("employment_type", ""), ("years_required", None)]
    assert _params(taxonomy.classify_family) == [
        ("title", empty), ("department", ""), ("description", "")]
    assert _params(taxonomy.years_required) == [("text", empty)]


def test_the_classifiers_answer_in_the_stored_vocabulary():
    # Today's internship and new-grad rows keep their labels.
    assert taxonomy.classify_experience("Software Engineering Intern") == "internship"
    assert taxonomy.classify_experience("New Grad Software Engineer") == "new_grad"
    for title in ("Senior Software Engineer", "Registered Nurse", "VP, Finance", ""):
        assert taxonomy.classify_experience(title, years_required=6) in taxonomy.LEVELS
        primary, secondary = taxonomy.classify_family(title, "", "A role on our team.")
        assert primary in taxonomy.FAMILIES
        assert secondary is None or (secondary in taxonomy.FAMILIES and secondary != primary)
    years = taxonomy.years_required("You bring 3+ years of experience.")
    assert years is None or isinstance(years, int)


def test_importing_the_taxonomy_loads_nothing_heavy():
    probe = (
        "import sys, backend.ingest.taxonomy\n"
        "heavy = ['httpx', 'sqlalchemy', 'fastapi', 'backend.services.ats_scraper',"
        " 'backend.db.database']\n"
        "print(','.join(m for m in heavy if m in sys.modules))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            env=env, timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
