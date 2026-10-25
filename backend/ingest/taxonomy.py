"""
Experience levels and career families for scraped jobs (scraper v2).

Both are tags computed from the posting after it is fetched, never filters
on what gets fetched: ``classify_experience`` gives scraped_jobs.
experience_level (one of LEVELS) and ``classify_family`` gives career_family
and career_family_secondary (slugs of FAMILIES). ``ENRICHMENT_VERSION`` is
stored beside them, so rows tagged by older rules can be found and re-tagged.

The keyword and boundary rules live in data/taxonomy.json; this module
applies them in a fixed order. The experience rules start from the
crawler's entry tier (services/ats_scraper): a named student or new-grad
track keeps the internship or new_grad label it gets today, and a level
word such as junior, analyst or associate is entry.

Runs on every crawled posting and is read by the jobs API, so it imports
nothing heavy at load and does its setup once: the rules file is read and
compiled on first use, and the entry-tier patterns come from
services/ats_scraper the first time a level is asked for.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

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

_RULES_PATH = Path(__file__).resolve().parent / "data" / "taxonomy.json"

# The scraper reads at most this much of a title (ats_scraper._MAX_TITLE):
# no real title is longer, and some level patterns go quadratic on a long
# run of whitespace.
_MAX_TITLE = 500
_SPACE = re.compile(r"\s+")
_RANK = {level: rank for rank, level in enumerate(LEVELS)}
# "five (5) years": the figure in brackets repeats the word before it.
# Whitespace is bounded here and in the years rule: descriptions are raw
# third-party text, and an unbounded \s* after a digit backtracks
# quadratically through a long run of spaces.
_REPEATED_NUMBER = re.compile(r"\b(\d{1,2})\s{0,3}\(\s{0,3}\1\s{0,3}\)")
# Most descriptions never mention years; those skip the full parse.
_YEARS_HINT = re.compile(r"year|yrs|\bans\b", re.IGNORECASE)


def _alternation(patterns, flags=re.IGNORECASE) -> re.Pattern | None:
    """One compiled alternation of a rule's patterns (a list or one string)."""
    if not patterns:
        return None
    if isinstance(patterns, str):
        patterns = [patterns]
    return re.compile("|".join(f"(?:{p})" for p in patterns), flags)


@lru_cache(maxsize=1)
def _rules() -> SimpleNamespace:
    """data/taxonomy.json, compiled once per process."""
    data = json.loads(_RULES_PATH.read_text(encoding="utf-8"))

    raw = data["experience"]
    band_words = {word.lower(): level for word, level in raw["band_words"].items()}
    band = "|".join(re.escape(word) for word in sorted(band_words, key=len, reverse=True))
    experience = SimpleNamespace(
        default=raw["default"],
        years_to_level=[(int(low), level) for low, level in raw["years_to_level"]],
        boss_phrase=re.compile(raw["boss_phrase"], re.IGNORECASE),
        band_words=band_words,
        band_pair=re.compile(rf"\b({band})\s*(?:/|\bor\b)\s*({band})(?![\w-])", re.IGNORECASE),
        # "Hwy Maintenance Worker/Senior": the lower band is the bare role.
        trailing_senior=re.compile(r"(?<=[a-z])\s*/\s*(?:senior|sr\b\.?)(?=\s*(?:$|[-,(|\u2013\u2014]))",
                                   re.IGNORECASE),
        numeral=re.compile(raw["numeral"], re.IGNORECASE),
        numeral_levels=raw["numeral_levels"],
        ranks=[SimpleNamespace(title=_alternation(rank["title"]),
                               if_title=_alternation(rank.get("if_title")),
                               level=rank["level"])
               for rank in raw["ranks"]],
        **{name: _alternation(raw[name]) for name in (
            "school_word", "school_grade", "school_stage",
            "internship", "new_grad", "deputy_chief", "executive", "vice_president",
            "individual_role", "director", "member_of_staff", "group_manager",
            "individual_manager", "associate_manager", "lead", "senior", "mid", "entry",
            "coordinator", "clinical_coordinator", "frontline", "professional",
        )},
    )

    raw = data["years"]
    words = raw["number_words"]
    years = SimpleNamespace(
        max_years=int(raw["max_years"]),
        number_words=words,
        number_word=re.compile(r"\b(" + "|".join(map(re.escape, words)) + r")\b", re.IGNORECASE),
        requirement=re.compile(raw["requirement"], re.IGNORECASE),
        not_a_requirement=re.compile(raw["not_a_requirement"], re.IGNORECASE),
    )

    families = [
        SimpleNamespace(
            family=rule["family"],
            title=_alternation(rule.get("title")),
            if_description=_alternation(rule.get("if_description")),
            department=_alternation(rule.get("department")),
            title_weak=_alternation(rule.get("title_weak")),
            description=_alternation(rule.get("description")),
        )
        for rule in data["families"]["rules"]
    ]
    unknown = {rule.family for rule in families} - set(FAMILIES)
    if unknown:
        raise ValueError(f"taxonomy.json names unknown families: {sorted(unknown)}")
    return SimpleNamespace(experience=experience, years=years, families=families,
                           description_chars=int(data["families"]["description_chars"]))


@lru_cache(maxsize=1)
def _scraper():
    """services/ats_scraper, for the crawler's entry tier. Imported on first
    use: it pulls in httpx and the platform fetchers."""
    from backend.services import ats_scraper
    return ats_scraper


def _clean(text: str | None, limit: int | None = None) -> str:
    text = _SPACE.sub(" ", text or "").strip()
    return text[:limit] if limit else text


def _without_boss(title: str) -> str:
    """'Executive Assistant to the CEO' -> 'Executive Assistant': the person a
    role serves names neither its level nor its family."""
    return _rules().experience.boss_phrase.sub(lambda m: m.group("role"), title)


# ─── Experience level ─────────────────────────────────────────────────────


def _lowest_band(title: str) -> str:
    """'Senior/Staff Engineer' -> 'Senior Engineer', 'Experienced or Senior'
    -> 'Experienced', 'Maintenance Worker/Senior' -> 'Maintenance Worker': a
    title offering two bands is open at the lower one."""
    rules = _rules().experience

    def lower(match: re.Match) -> str:
        first, second = match.group(1), match.group(2)
        first_rank = _RANK[rules.band_words[first.lower()]]
        second_rank = _RANK[rules.band_words[second.lower()]]
        return first if first_rank <= second_rank else second

    previous = None
    while previous != title:
        previous, title = title, rules.band_pair.sub(lower, title)
    return rules.trailing_senior.sub("", title)


def _school_title(title: str) -> str:
    """'Grade 3 Teacher' -> 'Teacher', 'Senior Kindergarten Teacher' ->
    'Kindergarten Teacher': beside a teaching word, a grade, year or class
    is the pupils' level, not the job's. Other titles are returned as is
    (a pay grade still counts)."""
    rules = _rules().experience
    if not rules.school_word.search(title):
        return title
    return _clean(rules.school_grade.sub(" ", rules.school_stage.sub("", title)))


def _rank(title: str) -> str | None:
    """The level of an academic, school or public rank ("Associate
    Professor", "Associate Dean", "Superintendent of Schools"), or None."""
    for rank in _rules().experience.ranks:
        if rank.title.search(title) and (rank.if_title is None or rank.if_title.search(title)):
            return rank.level
    return None


def _named_track(title: str) -> str | None:
    """internship or new_grad for a track the scraper's entry tier does not
    name: a summer associate, a course's teaching assistant, an MBA
    development program, and a title naming the student role itself beside
    a seniority word ("Software Engineer Intern - Sr. Design"), though not
    program staff ("Senior Manager, Internships")."""
    rules = _rules().experience
    scraper = _scraper()
    if rules.internship.search(title):
        return "internship"
    if scraper.STUDENT_ROLE_TITLE.search(title) and not scraper.PROGRAM_STAFF.search(title):
        return "internship" if scraper.INTERNSHIP_TITLE.search(title) else "new_grad"
    if rules.new_grad.search(title):
        return "new_grad"
    return None


def _weak_tier_exception(title: str) -> str | None:
    """The level of a named track or a rank, which outranks the entry tier's
    level words: "MBA Marketing Summer Associate" is an internship and
    "Associate Professor" senior, not entry. None for every other title."""
    title = _without_boss(title)
    return _named_track(title) or _rank(title)


def _title_level(title: str) -> str | None:
    """The level the title names, or None. The title is outside the entry
    tier: no named track and no level word the scraper reads as entry."""
    rules = _rules().experience
    scraper = _scraper()
    title = _lowest_band(_without_boss(title))

    if rules.deputy_chief.search(title):
        return "director"
    vice_president = rules.vice_president.search(title)
    if vice_president and not rules.director.search(title):
        unranked = rules.vice_president.sub(" ", title)
        if rules.individual_role.search(unranked):
            # A bank's corporate rank on an individual contributor's job.
            title, vice_president = unranked, None
    if rules.executive.search(title):
        return "director" if vice_president else "executive"
    if vice_president or rules.director.search(title):
        return "director"

    title = rules.member_of_staff.sub(" ", title)
    if rules.group_manager.search(title):
        return "lead"
    associate = rules.associate_manager.search(title)
    if associate:
        return "entry" if rules.individual_manager.search(associate.group(0)) else "mid"
    title = rules.individual_manager.sub(" individual ", title)
    if rules.lead.search(title):
        return "lead"
    if rules.senior.search(title):
        return "senior"
    if rules.mid.search(title):
        return "mid"
    numeral = rules.numeral.search(title)
    if numeral:
        return rules.numeral_levels[numeral.group("num").lower()]
    if (rules.entry.search(title) or scraper.WEAK_ENTRY.search(title)
            or (rules.coordinator.search(title) and not rules.clinical_coordinator.search(title))):
        return "entry"
    if ((scraper.FRONTLINE.search(title) or rules.frontline.search(title))
            and not rules.professional.search(title)):
        return "entry"
    return None


def _level_for_years(years: int) -> str:
    level = _rules().experience.default
    for minimum, years_level in _rules().experience.years_to_level:
        if years >= minimum:
            level = years_level
    return level


def classify_experience(title: str, department: str = "", employment_type: str = "",
                        years_required: int | None = None) -> str:
    """The posting's experience level, one of LEVELS, from the title, the
    board's department and the source's own employment type.

    ``years_required`` is the minimum years the description asks for
    (``years_required(text)``), read only when the title names no level:
    0-1 entry, 2-4 mid, 5-7 senior, 8+ lead. With neither, mid."""
    scraper = _scraper()
    department, employment_type = department or "", employment_type or ""
    # The entry tier reads the title exactly as the crawler always has.
    tier = scraper.entry_tier(title or "", department, employment_type)
    if tier == "strong":
        return scraper.experience_level_for(title or "", department, employment_type)
    title = _clean(title, _MAX_TITLE)
    school_title = _school_title(title)
    if school_title != title:
        # The tier reads a teacher's title without the pupils' grade.
        title = school_title
        tier = scraper.entry_tier(title, department, employment_type)
        if tier == "strong":
            return scraper.experience_level_for(title, department, employment_type)
    level = _weak_tier_exception(title)
    if level:
        return level
    if tier == "weak":
        return "entry"
    level = _title_level(title)
    if level:
        return level
    if years_required is not None:
        return _level_for_years(years_required)
    return _rules().experience.default


# ─── Years of experience ──────────────────────────────────────────────────


def years_required(text: str) -> int | None:
    """The minimum years of experience a description asks for, or None when
    it names none: the lower bound of the first requirement it states ("3+
    years", "minimum of 5 years", "2-4 years of experience", "trois ans
    d'expérience"). A figure must sit next to the word experience, so ages,
    dates and "for over 30 years, we have" never count, and a figure above
    max_years is the company's own age ("25+ years of experience")."""
    if not text or not _YEARS_HINT.search(text):
        return None
    rules = _rules().years
    text = rules.number_word.sub(lambda m: str(rules.number_words[m.group(1).lower()]), text)
    text = _REPEATED_NUMBER.sub(r"\1", text)
    for match in rules.requirement.finditer(text):
        if rules.not_a_requirement.search(match.group("gap") or ""):
            continue
        low = int(match.group("low"))
        if low <= rules.max_years:
            return low
    return None


# ─── Career family ────────────────────────────────────────────────────────


def _family_matches(title: str, department: str, description: str):
    """Every family the rules find, in the order they decide: the title
    against each rule (priority order), the department, the title's weak
    words, then the description."""
    rules = _rules().families
    for rule in rules:
        if (rule.title and title and rule.title.search(title)
                and (rule.if_description is None or rule.if_description.search(description))):
            yield rule.family, "title"
    for rule in rules:
        if rule.department and department and rule.department.search(department):
            yield rule.family, "department"
    for rule in rules:
        if rule.title_weak and title and rule.title_weak.search(title):
            yield rule.family, "weak"
    for rule in rules:
        if rule.description and description and rule.description.search(description):
            yield rule.family, "description"


def classify_family(title: str, department: str = "",
                    description: str = "") -> tuple[str, str | None]:
    """(primary, secondary or None): career family slugs from FAMILIES, from
    keyword rules over the title, the department and the first 600
    characters of the description. The secondary is the next family the
    title or department names. "other" when no rule matches, or when a rule
    says the job has no family (custodians, security guards)."""
    title = _without_boss(_clean(title, _MAX_TITLE))
    department = _clean(department, _MAX_TITLE)
    # Cut before collapsing whitespace: a full description runs to thousands
    # of characters and only its opening is read.
    chars = _rules().description_chars
    description = _clean((description or "")[:4 * chars], chars)
    primary, secondary = None, None
    for family, source in _family_matches(title, department, description):
        if primary is None:
            primary = family
            if family == "other":
                break
        elif source not in ("title", "department"):
            break
        elif family not in (primary, "other"):
            secondary = family
            break
    return primary or "other", secondary
