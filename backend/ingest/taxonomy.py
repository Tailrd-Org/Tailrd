"""
Experience levels and career families for scraped jobs (scraper v2).

Both are tags computed from the posting after it is fetched, never filters
on what gets fetched: ``classify_experience`` gives scraped_jobs.
experience_level (one of LEVELS) and ``classify_family`` gives career_family
and career_family_secondary (slugs of FAMILIES). ``ENRICHMENT_VERSION`` is
stored beside them, so rows tagged by older rules can be found and re-tagged.

The constants are final. The classifiers are placeholders with the final
signatures until the keyword rules land (data/taxonomy.json): the experience
placeholder keeps today's internship and new-grad labels and calls everything
else mid level.

Read on every ingest and by the jobs API, so this module imports nothing
heavy at load: the entry-level patterns it reuses come from
services/ats_scraper inside the function that needs them.
"""

from __future__ import annotations

# scraped_jobs.experience_level values, most junior first.
LEVELS: tuple[str, ...] = (
    "internship", "new_grad", "entry", "mid", "senior", "lead", "director", "executive",
)

# scraped_jobs.career_family values, slug -> label, in display order. Rows
# and filters carry the slug: five labels contain commas, and every list
# filter on GET /jobs is comma-separated.
FAMILIES: dict[str, str] = {
    "software_engineering": "Software Engineering",
    "data_ai_analytics": "Data, AI & Analytics",
    "it_security_infrastructure": "IT, Security & Infrastructure",
    "product_design": "Product & Design",
    "engineering": "Engineering",
    "legal": "Legal",
    "healthcare_clinical": "Healthcare, Clinical",
    "healthcare_admin_support": "Healthcare, Administration & Support",
    "finance_accounting": "Finance & Accounting",
    "banking_insurance_investment": "Banking, Insurance & Investment",
    "sales_bizdev": "Sales & Business Development",
    "marketing_communications": "Marketing & Communications",
    "operations_supply_chain": "Operations, Supply Chain & Logistics",
    "hr_recruiting": "Human Resources & Recruiting",
    "education_research": "Education & Research",
    "government_public_sector": "Government & Public Sector",
    "skilled_trades": "Skilled Trades, Construction & Manufacturing",
    "customer_support_admin": "Customer Support & Administrative",
    "hospitality_retail_food": "Hospitality, Retail & Food Service",
    "other": "Other",
}

ENRICHMENT_VERSION = 1

# The 17 old job functions (role_classifier.CANONICAL_CATEGORIES, the
# role_category column) -> the families they become, so a saved job-function
# filter migrates. The frontend holds an identical copy.
LEGACY_CATEGORY_TO_FAMILIES: dict[str, list[str]] = {
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


def classify_experience(title: str, department: str = "", employment_type: str = "",
                        years_required: int | None = None) -> str:
    """The posting's experience level, one of LEVELS. ``years_required`` is
    the minimum years its description asks for (``years_required(text)``),
    the fallback when the title names no level.

    Placeholder until the rules land: an entry-level title by
    services/ats_scraper.entry_tier keeps the internship or new_grad label it
    gets today (experience_level_for); everything else is "mid"."""
    from backend.services.ats_scraper import entry_tier, experience_level_for

    if entry_tier(title, department, employment_type):
        return experience_level_for(title, department, employment_type)
    return "mid"


def classify_family(title: str, department: str = "",
                    description: str = "") -> tuple[str, str | None]:
    """(primary, secondary or None): career family slugs from FAMILIES.
    Placeholder until the rules land: ("other", None)."""
    return "other", None


def years_required(text: str) -> int | None:
    """The minimum years of experience a description asks for, or None when
    it names none. Placeholder until the rules land: None."""
    return None
