"""
ATS Scraper, directly polls public ATS (Applicant Tracking System) APIs
for job listings with direct company apply links.

Supported platforms:
- Greenhouse (boards-api.greenhouse.io)
- Lever (api.lever.co)
- Ashby (api.ashbyhq.com)
- SmartRecruiters (api.smartrecruiters.com)
- Workday (per-tenant CxS JSON endpoints, registry entries that carry a
  ``workday_url_template``)

These APIs are public and intended for job board consumption.
No authentication required.

Two consumption shapes:
- ``scrape_all`` / ``scrape_company`` / ``_scrape_<platform>`` return filtered
  ``list[ATSJob]`` (the original interface, tests and callers rely on it).
- ``scrape_board`` returns a ``BoardSnapshot``: the filtered jobs PLUS the
  full set of live listing URLs on the board, which is what lets the ingest
  reconcile its rows against reality (mark removed / revive) instead of only
  ever adding.
"""

import asyncio
import json
import logging
import datetime
import os
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from backend.services.description_extractor import clean_html
from backend.services.na_location import (
    CA,
    FOREIGN,
    US,
    hint_region,
    is_north_america,
    names_north_american_place,
    region_of,
)

logger = logging.getLogger(__name__)


@dataclass
class ATSJob:
    """A job listing from an ATS platform."""
    title: str
    company: str
    location: str
    url: str  # Direct apply link
    posted_date: Optional[datetime.datetime] = None
    department: Optional[str] = None
    work_type: Optional[str] = None  # Remote, On Site, Hybrid
    description: str = ""  # Plain text, captured from the board API when it carries content
    external_id: str = ""  # The source's own posting id, stable across URL changes
    employment_type: str = ""  # Source-declared commitment (Intern / Full-time / …)
    salary_text: str = ""  # Source-structured pay range, verbatim-ish
    detail_ref: str = ""  # Connector-specific ref for a lazy detail fetch (Workday externalPath)
    # Where a Workday posting that only says "3 Locations" is primarily based,
    # from its externalPath ("Toronto-ON", "IL-Rosemont"); for Ashby and
    # Lever, the North American ones among the posting's other locations
    # ("San Francisco; Toronto" beside a primary "London"). Read for the NA
    # verdict and the country column only, never displayed or stored.
    location_hint: str = ""
    # The employer's own apply link, when an aggregator adapter's listing
    # gives one (scraped_jobs.apply_url); ``url`` stays the posting it was
    # found on.
    apply_url: str = ""


def _text(value) -> str:
    """A source's title or location, stripped. Boards pad them (Carvana's
    " Entry-level Auto Body Inspector", SoFi's "Frisco, TX "), and the
    padding reached the feed."""
    return value.strip() if isinstance(value, str) else ""


@dataclass
class BoardSnapshot:
    """One board crawl: what to ingest, and what the board says is live.

    ``all_urls`` covers EVERY listing on the board, including ones the
    entry-level/NA filters rejected, reconciliation must never mistake
    "filtered out" for "taken down". ``rejected`` says why each rejected one
    failed (ATSScraper.rejection: "level", "location" or "unplaced"), so
    reconciliation can retire a stored row whose own listing is off target
    instead of confirming it; only scrape_board fills it, and empty means "no
    verdicts", never "everything passed". ``complete`` is False when the fetch
    was partial (a board past the page cap or Workday's listing ceiling, or a
    crawl budget that ran out); an incomplete snapshot must not be used to
    mark rows removed, though every URL it did list is still proof of life.
    """
    platform: str
    slug: str
    company: str
    jobs: list[ATSJob] = field(default_factory=list)
    all_urls: set[str] = field(default_factory=set)
    complete: bool = True
    total_listed: int = 0
    rejected: dict[str, str] = field(default_factory=dict)

    @property
    def board_key(self) -> str:
        return f"{self.platform}:{self.slug}"


# ─── Company → ATS mapping ───────────────────────────────────────────────────
# Each entry: (ats_platform, slug, company_display_name)
# DEPRECATED: the canonical source of truth is backend/data/ats_companies.json,
# loaded via company_registry.load_companies(). This list is kept only as an
# emergency fallback if the registry file cannot be read at runtime.
_LEGACY_ATS_COMPANIES: list[tuple[str, str, str]] = [
    # === Greenhouse companies (69) ===
    ("greenhouse", "affirm", "Affirm"),
    ("greenhouse", "airbnb", "Airbnb"),
    ("greenhouse", "airtable", "Airtable"),
    ("greenhouse", "amplitude", "Amplitude"),
    ("greenhouse", "anthropic", "Anthropic"),
    ("greenhouse", "applovin", "AppLovin"),
    ("greenhouse", "asana", "Asana"),
    ("greenhouse", "astranis", "Astranis"),
    ("greenhouse", "block", "Block"),
    ("greenhouse", "boxinc", "Box"),
    ("greenhouse", "brex", "Brex"),
    ("greenhouse", "chime", "Chime"),
    ("greenhouse", "cloudflare", "Cloudflare"),
    ("greenhouse", "cockroachlabs", "CockroachDB"),
    ("greenhouse", "contentful", "Contentful"),
    ("greenhouse", "databricks", "Databricks"),
    ("greenhouse", "datadog", "Datadog"),
    ("greenhouse", "discord", "Discord"),
    ("greenhouse", "doximity", "Doximity"),
    ("greenhouse", "dropbox", "Dropbox"),
    ("greenhouse", "duolingo", "Duolingo"),
    ("greenhouse", "elastic", "Elastic"),
    ("greenhouse", "epicgames", "Epic Games"),
    ("greenhouse", "faire", "Faire"),
    ("greenhouse", "figma", "Figma"),
    ("greenhouse", "flexport", "Flexport"),
    ("greenhouse", "gitlab", "GitLab"),
    ("greenhouse", "gusto", "Gusto"),
    ("greenhouse", "instacart", "Instacart"),
    ("greenhouse", "janestreet", "Jane Street"),
    ("greenhouse", "jetbrains", "JetBrains"),
    ("greenhouse", "labelbox", "Labelbox"),
    ("greenhouse", "lattice", "Lattice"),
    ("greenhouse", "lucidmotors", "Lucid Motors"),
    ("greenhouse", "lyft", "Lyft"),
    ("greenhouse", "marqeta", "Marqeta"),
    ("greenhouse", "mixpanel", "Mixpanel"),
    ("greenhouse", "mongodb", "MongoDB"),
    ("greenhouse", "netlify", "Netlify"),
    ("greenhouse", "newrelic", "New Relic"),
    ("greenhouse", "nuro", "Nuro"),
    ("greenhouse", "okta", "Okta"),
    ("greenhouse", "oscar", "Oscar Health"),
    ("greenhouse", "pagerduty", "PagerDuty"),
    ("greenhouse", "peloton", "Peloton"),
    ("greenhouse", "pinterest", "Pinterest"),
    ("greenhouse", "reddit", "Reddit"),
    ("greenhouse", "relativity", "Relativity"),
    ("greenhouse", "riotgames", "Riot Games"),
    ("greenhouse", "robinhood", "Robinhood"),
    ("greenhouse", "roblox", "Roblox"),
    ("greenhouse", "roku", "Roku"),
    ("greenhouse", "samsara", "Samsara"),
    ("greenhouse", "scaleai", "Scale AI"),
    ("greenhouse", "sofi", "SoFi"),
    ("greenhouse", "spacex", "SpaceX"),
    ("greenhouse", "squarespace", "Squarespace"),
    ("greenhouse", "stripe", "Stripe"),
    ("greenhouse", "toast", "Toast"),
    ("greenhouse", "twilio", "Twilio"),
    ("greenhouse", "twitch", "Twitch"),
    ("greenhouse", "unity3d", "Unity"),
    ("greenhouse", "vercel", "Vercel"),
    ("greenhouse", "verkada", "Verkada"),
    ("greenhouse", "waymo", "Waymo"),
    ("greenhouse", "webflow", "Webflow"),
    ("greenhouse", "zscaler", "Zscaler"),
    ("greenhouse", "coinbase", "Coinbase"),
    ("greenhouse", "doordash", "DoorDash"),
    ("greenhouse", "snap", "Snap"),
    ("greenhouse", "openai", "OpenAI"),
    # === Lever companies ===
    ("lever", "anyscale", "Anyscale"),
    ("lever", "gopuff", "GoPuff"),
    ("lever", "neon", "Neon"),
    ("lever", "palantir", "Palantir"),
    ("lever", "shieldai", "Shield AI"),
    ("lever", "spotify", "Spotify"),
    ("lever", "veeva", "Veeva Systems"),
    ("lever", "zoox", "Zoox"),
    ("lever", "netflix", "Netflix"),
    ("lever", "wattpad", "Wattpad"),
    ("lever", "fullscript", "Fullscript"),
    # === Ashby companies ===
    ("ashby", "vanta", "Vanta"),
    ("ashby", "notion", "Notion"),
    ("ashby", "ramp", "Ramp"),
    ("ashby", "linear", "Linear"),
    ("ashby", "mercury", "Mercury"),
    ("ashby", "retool", "Retool"),
    ("ashby", "watershed", "Watershed"),
    ("ashby", "anduril", "Anduril"),
    ("ashby", "plaid", "Plaid"),
    ("ashby", "airtable", "Airtable"),
    ("ashby", "deel", "Deel"),
    ("ashby", "rippling", "Rippling"),
    ("ashby", "openphone", "OpenPhone"),
    ("ashby", "loom", "Loom"),
    # === Canadian tech (verified to contain CA jobs: Ottawa/Toronto/Waterloo/Montreal/Vancouver) ===
    ("greenhouse", "geotab", "Geotab"),
    ("greenhouse", "workleap", "Workleap"),
    ("greenhouse", "alayacare", "AlayaCare"),
    ("greenhouse", "flipp", "Flipp"),
    ("greenhouse", "later", "Later"),
    ("greenhouse", "hootsuite", "Hootsuite"),
    ("greenhouse", "thinkific", "Thinkific"),
    ("greenhouse", "canonical", "Canonical"),
    ("greenhouse", "mojio", "Mojio"),
    ("lever", "pointclickcare", "PointClickCare"),
    ("lever", "achievers", "Achievers"),
    ("ashby", "neofinancial", "Neo Financial"),
    ("ashby", "cohere", "Cohere"),
    ("ashby", "wealthsimple", "Wealthsimple"),
    ("ashby", "1password", "1Password"),
    ("ashby", "jobber", "Jobber"),
    ("ashby", "benevity", "Benevity"),
    ("ashby", "jane", "Jane Software"),
    ("ashby", "trulioo", "Trulioo"),
    ("ashby", "hopper", "Hopper"),
    ("ashby", "float", "Float"),
    ("ashby", "klue", "Klue"),
    ("ashby", "loopio", "Loopio"),
    ("ashby", "rewind", "Rewind"),
    ("ashby", "top-hat", "Top Hat"),
    # === SmartRecruiters companies ===
    ("smartrecruiters", "Visa", "Visa"),
    ("smartrecruiters", "BoschGroup", "Bosch"),
    ("smartrecruiters", "Accenture1", "Accenture"),
    ("smartrecruiters", "DHL", "DHL"),
    ("smartrecruiters", "Adidas", "Adidas"),
    ("smartrecruiters", "Sanofi", "Sanofi"),
    ("smartrecruiters", "Ubisoft", "Ubisoft"),
    ("smartrecruiters", "Deloitte4", "Deloitte"),
]


def _registry_companies() -> list[tuple[str, str, str]]:
    """The canonical company list: backend/data/ats_companies.json through
    company_registry.load_companies(), or the legacy list above when the
    registry is empty or unreadable.

    Read when called, never at import. The registry asks the v2 adapter
    registry which platforms are enabled, and adapters import this module, so
    an import-time read ran adapter discovery while an adapter imported first
    was still half-loaded, and that adapter dropped out of the registry."""
    try:
        from backend.data.company_registry import load_companies

        companies = load_companies()
    except Exception as e:  # pragma: no cover - defensive
        logger.error("Failed to load company registry, using legacy list: %s", e)
        return _LEGACY_ATS_COMPANIES
    if not companies:
        logger.warning("Company registry empty; using legacy fallback list")
        return _LEGACY_ATS_COMPANIES
    return companies


# ─── Entry-level classification ──────────────────────────────────────────────
#
# Tiers, read from the title (and, for a named track only, the board's
# department or the source's own commitment field):
#
# - STRONG: the posting names a student/new-grad TRACK (intern, co-op, new
#   grad, early career, apprentice, trainee, student, stagiaire, a season+year
#   term, "2027 Start", Associate Product Manager). It outranks the SOFT
#   seniority words ("Product Manager Intern", "Member of Technical Staff (New
#   Grad)", "Area Manager - New Grad" are entry roles) and is never read as
#   frontline work ("Warehouse Operations Intern").
# - WEAK: a level word that is not a track (junior, entry-level, analyst,
#   associate, "Level I", "Engineer I"). It passes unless the title is
#   frontline/hourly work ("Operations Associate, Dallas, #118", "Security
#   Associate - 3rd Shift") or the source's commitment is part-time.
# - otherwise not entry level.
#
# HARD seniority (senior, VP, counsel, mid-level, III, "(L5)", "Level 3",
# "Analyst 4", "5+ years") vetoes every tier, and so does recruiting for or
# running an early-career program ("Campus Recruiter", "Intern Program
# Manager"): those people hire the students. A digit veto never reads a
# duration: "Co-op (8 months)" is a term, not a level.

_LEVEL_ROLE = (
    r"(?:engineer|developer|scientist|analyst|specialist|technician|consultant|"
    r"representative|accountant|designer|programmer|administrator|coordinator|"
    r"associate|researcher|tester|writer|architect|manager|agent|advisor|officer)"
)
# "8 months", and "4 or 8 Months" / "4-8 months" too.
_NOT_A_DURATION = (
    r"(?!\s*(?:(?:or|to|[-–/])\s*\d+\s*)?(?:months?|weeks?|days?|hours?|hrs?|years?|yrs?)\b)"
)
# A student, not a product for students ("Student Financial Aid Consultant")
# or the people who serve them ("Dean of Students").
_STUDENT = (
    r"(?<!\bof )\bstudents?\b(?!\s+(?:loans?|success|affairs|services|experience"
    r"|financial|aid|accounts?|records|information|enrollment|housing|recruit\w*))"
)

# "Co-op", "Coop", and Verkada's "(Winter  C0-Op 2027)" with a zero.
_COOP = r"\bc[o0]-?ops?\b"

STRONG_ENTRY = re.compile(
    r"\bintern(?:s|ships?)?\b"
    r"|" + _COOP
    + r"|\bgrad(?:uate)?s?\b"                     # new/recent/university/"Dec 2026" grads
    r"|\bearly[- ]careers?\b"
    r"|\b(?:early|emerging)[- ]talent\b"
    r"|\bapprentice(?:ship)?s?\b"
    r"|\btrainees?\b"
    r"|\brotational\b|\brotation program\b"
    r"|\bassociate product manager\b"
    r"|" + _STUDENT
    + r"|\bstagiaires?\b|\bstage\s+coop\w*|\b[ée]tudiant(?:e|s|es)?\b"
    r"|\b(?:summer|fall|winter|spring|autumn)\s*,?\s*(?:19|20)\d\d\b"
    r"|\b(?:19|20)\d\d\s+start\b",
    re.IGNORECASE,
)
# A department only counts when it names the track ("Early Career FT", "Zoox
# Internships", "University Recruiting"), never a level word: a department
# containing "Analyst", or "University" as a street name, is not one.
DEPT_ENTRY = re.compile(
    STRONG_ENTRY.pattern
    + r"|\buniversity\s+(?:recruit\w*|relations|programs?|hir\w*|talent)\b"
      r"|^\s*(?:\d+\s+)?(?:general\s+)?university\s*$|\bcampus\b",
    re.IGNORECASE,
)
# The source's own commitment field (Lever commitment, Ashby employmentType,
# SmartRecruiters typeOfEmployment): "Intern", "Internship", "Intern/Co-op".
EMPLOYMENT_TYPE_ENTRY = re.compile(r"\bintern|\bco-?op\b|\bstudent\b|\bapprentice", re.IGNORECASE)
# "Software Engineer I", "Analyst 1"; not "Rack Repair Specialist-1".
_LEVEL_ONE_ROLE = r"\b" + _LEVEL_ROLE + r"\s*,?\s*(?:i|1)\b(?![-\w])" + _NOT_A_DURATION
WEAK_ENTRY = re.compile(
    r"\bjunior\b|\bjr\b\.?"
    r"|\bentry[- ]level\b"
    r"|\banalyst\b"
    r"|\bassociates?\b"
    r"|\b(?:level|lvl|grade|tier)\s*(?:i|1)\b"
    r"|" + _LEVEL_ONE_ROLE
    + r"|\b0\s*-\s*[12]\s*years\b|\b1\s*-\s*2\s*years\b|\bstarter\b|\bfresh(?:er)?\b",
    re.IGNORECASE,
)
_HARD_SENIOR_WORDS = re.compile(
    r"\bsenior\b|\bsr\b\.?|\bprincipal\b|\bdirector\b|\b[aers]?vp\b|\bvice[- ]president\b"
    r"|\bhead of\b|\bchief\b|\bdistinguished\b|\bfellow\b|\bcounsel\b"
    # A dean is a senior academic post whatever its qualifier ("Associate
    # Dean" must not read as the entry-level "Associate"); "Dean's Office
    # Assistant" names an office, not a rank, as taxonomy.json reads it too.
    r"|\bdean\b(?!['’]s)"
    r"|\bmanaging\b|\bassociate partner\b"
    r"|\bmid[- ]?level\b|\bintermediate\b|\bexperienced\b"
    r"|\biii\b|\biv\b"
    r"|\b" + _LEVEL_ROLE + r"\s*,?\s*(?:v|vi|[3-9])\b" + _NOT_A_DURATION
    + r"|\b(?:level|lvl|grade)\s*[3-9]\b"
    r"|\bl[4-9]\b"                               # Netflix "(L5)", Twilio "(L6)"
    # "5+ years", "3-5 years"; not the top of a range ("1-3 years", "0 to 3").
    r"|(?<![-–])(?<![-–] )(?<!\bto )\b(?:[3-9]|1\d)\s*\+?\s*(?:[-–]\s*\d+\s*)?(?:years?|yrs?)\b",
    re.IGNORECASE,
)
# A title offering an entry band OR a mid-level one is open at the entry band:
# Boeing's "Software Engineer (Associate or Experienced / Mid-Level)"
# (Associate is its entry band), "Junior/Intermediate Developer", "(Entry
# Level and Mid-Level)". Mid-level words only: "Associate or Vice President"
# and "Experienced Financial Analyst" stay out.
_ENTRY_BAND = r"(?:associate|junior|jr\.?|entry[- ]level)"
_MID_BAND = r"(?:experienced|mid[- ]?level|intermediate)"
_BAND_RANGE = re.compile(
    r"\b(" + _ENTRY_BAND + r")(?:\s*(?:,|/|-|–|&|\bor\b|\band\b|\bto\b)\s*" + _MID_BAND + r")+\b",
    re.IGNORECASE,
)
# So is a slash or "or" list of bands that opens at an entry band, where a
# later band restates the opening band's role at another level: Norfolk
# County's "Junior Planner / Planner / Senior Planner (PFT)", Salesforce's
# "Analyst/Sr. Analyst, Global Incentive Compensation" and "Sales Strategy
# Analyst/Sr. Analyst", BDO's "Associate/ Senior Associate", and a bare level
# of the title's own role, Boeing's "Flight Software Engineers
# (Associate/Experienced/Senior)". Only such a band loses its senior and
# mid-level words. One that names another role keeps them: "Analyst / Senior
# Manager", "Junior / Senior Staff Engineer", CIBC's "Associate / Senior
# Analyst". "Senior Analyst" and "Sr. Analyst / Analyst" stay out too.
_BAND_CLAUSE = re.compile(r"[^,;:()\[\]|]+")
_BAND_LIST_SEPARATOR = re.compile(r"(\s*/\s*|\s+or\s+)", re.IGNORECASE)
_OPENING_BAND = re.compile(r"\b(?:" + _ENTRY_BAND + r"|analyst)(?![\w-])", re.IGNORECASE)
_LATER_BAND = re.compile(r"\b(?:senior|sr\b\.?|" + _MID_BAND + r")(?![\w-])", re.IGNORECASE)
# The level words a band's role is read without. "Associate" stays: it is
# the role in "Associate / Senior Analyst".
_LEVEL_WORD = re.compile(
    r"\b(?:junior|jr\b\.?|entry[- ]level|senior|sr\b\.?|" + _MID_BAND + r")(?![\w-])",
    re.IGNORECASE,
)
_ROLE_WORD = re.compile(r"[a-z0-9]+")
# A spaced dash sets a qualifier off a band: the opening band follows it
# ("CIBC – Associate"), a later band precedes it ("Senior Analyst - Tax").
_BAND_QUALIFIER = re.compile(r"\s+[-–\u2014]\s+")
# The veto reads at most this much of a title. A real one is far shorter,
# and on a long run of whitespace the band splits (and the level-role
# words' "\s*,?\s*") go quadratic: 40,000 spaces took 8 s.
_MAX_TITLE = 500


def _band_role(band: str) -> list[str]:
    """A band's role without its level words: "Sr. Analyst" -> ["analyst"],
    "Junior Planner" -> ["planner"], a bare "Experienced" -> []."""
    return _ROLE_WORD.findall(_LEVEL_WORD.sub(" ", band).lower())


def _restates(role: list[str], band_role: list[str]) -> bool:
    """True when a later band's role is the opening band's, whole or its
    last words ("Sr. Analyst" after "Sales Strategy Analyst"), or no role at
    all (a bare level)."""
    return len(band_role) <= len(role) and role[len(role) - len(band_role):] == band_role


def _open_band_list(clause: re.Match) -> str:
    parts = _BAND_LIST_SEPARATOR.split(clause.group(0))
    if len(parts) < 3:
        return clause.group(0)
    opening = _BAND_QUALIFIER.split(parts[0])[-1]
    if not _OPENING_BAND.search(opening):
        return clause.group(0)
    role = _band_role(opening)
    # parts alternates band, separator, band, ...: parts[2::2] are the later bands.
    for index in range(2, len(parts), 2):
        qualifier = _BAND_QUALIFIER.search(parts[index])
        band = parts[index][:qualifier.start()] if qualifier else parts[index]
        if _restates(role, _band_role(band)):
            parts[index] = _LATER_BAND.sub(" ", band) + parts[index][len(band):]
    return "".join(parts)


class _SeniorVeto:
    """HARD_SENIOR: ``search`` reads a band range or a band list as its
    entry band first, in the first _MAX_TITLE characters."""

    def __init__(self, words: re.Pattern):
        self.words = words
        self.pattern = words.pattern

    def search(self, title: str):
        title = _BAND_CLAUSE.sub(_open_band_list, (title or "")[:_MAX_TITLE])
        return self.words.search(_BAND_RANGE.sub(r"\1", title))


# Also the one title veto for LinkedIn/Indeed rows (jobs.ingest_batch,
# listing_freshness.retire_senior_aggregator_rows).
HARD_SENIOR = _SeniorVeto(_HARD_SENIOR_WORDS)
SOFT_SENIOR = re.compile(
    r"\bstaff\b|\blead\b|\bmanager\b|\barchitect\b|\bsupervisor\b|\bleader\b"
    r"|\b(?:business|people|hr|talent|client|account)\s+partner\b"
    r"|\bii\b(?<!i/ii)(?<!i / ii)(?<!i or ii)|\b(?:level|lvl|grade)\s*2\b|\bl[23]\b(?!\s*support)"
    r"|\b" + _LEVEL_ROLE + r"\s*,?\s*2\b" + _NOT_A_DURATION,
    re.IGNORECASE,
)
# "Sourcing" alone is procurement ("Associate Sourcing Specialist").
RECRUITING = re.compile(
    r"\brecruit\w*|\bsourcers?\b|\b(?:talent|candidate)\s+sourcing\b"
    r"|\btalent (?:acquisition|partner|operations)\b"
    r"|\b(?:university|campus) relations\b"
    r"|\bprogram manager\b(?<!associate program manager)",
    re.IGNORECASE,
)
# Staff who run an early-career program, with no unlock: "Intern Program
# Manager" and "Early Careers & Interns Specialist" run the program, while
# "Program Coordinator Intern" is in it.
_PROGRAM_ROLE = r"(?:lead|manager|coordinator|specialist|director|partner|advisor)"
_PROGRAM_OBJECT = r"(?:programs?|recruiting|relations|hiring|partnerships?)"
PROGRAM_STAFF = re.compile(
    r"\b(?:early[- ]careers?|early[- ]talent|emerging[- ]talent|internships?|interns)\s+"
    r"(?:&\s+\w+\s+)?(?:" + _PROGRAM_OBJECT + r"\s+)?" + _PROGRAM_ROLE + r"\b"
    r"|\b(?:university|campus|graduate|intern)\s+" + _PROGRAM_OBJECT + r"\s+" + _PROGRAM_ROLE + r"\b"
    r"|\b" + _PROGRAM_ROLE + r",?\s+(?:of\s+)?(?:early[- ]careers?|early[- ]talent|emerging[- ]talent"
    r"|university|campus|graduate|internships?|intern)\s+" + _PROGRAM_OBJECT + r"\b",
    re.IGNORECASE,
)
# ...unless the recruiting job is itself a student/new-grad job.
STUDENT_JOB = re.compile(
    r"\bintern(?:ship)?\b|" + _COOP + r"|\bstudent\b|\bstagiaire\b|\bapprentice\b"
    r"|\b(?:new|recent)[- ]?grad(?:uate)?s?\b|\btrainee\b",
    re.IGNORECASE,
)
# Hourly/frontline work. Vetoes the weak tier only: a named track stays one.
_SHIFT = (
    r"\b(?:\d(?:st|nd|rd|th)|night|overnight|evening|weekend|day|am|pm|swing|graveyard)[- ]shift\b"
    r"|\bshift\s*\d\b"
)
FRONTLINE = re.compile(
    _SHIFT + r"|\bpart[- ]time\b|\bseasonal\b|#\s?\d+\b"
    r"|\b(?:warehouse|forklift|barista|cashier|key holder|lot attendant|detailer|driver"
    r"|crew member|sous chef|cook|dishwasher|bartender|store|merchandising"
    r"|production associate|security associate)\b"
    # Retail floor work ("Retail Sales Associate, Albany", "Sales Associate -
    # Event Retail"), not a bank's retail business line ("Internal Sales
    # Associate, Retail Distribution").
    r"|\bretail\s+(?:sales\s+)?(?:associates?|clerks?|cashiers?|team members?|crew"
    r"|stock\w*|merchandis\w*|specialists?|consultants?|advisors?|representatives?)\b"
    r"|\b(?:event|store)\s+retail\b",
    re.IGNORECASE,
)
_SHIFT_WORDS = re.compile(_SHIFT, re.IGNORECASE)
# The level-I roles a shift can't make floor work: engineering, analysis,
# science, design and support desks on a rota.
_ROTA_ROLE = (
    r"(?:engineer|developer|analyst|scientist|programmer|designer|support\s+" + _LEVEL_ROLE + r")"
)
_ROTA_LEVEL_ONE = re.compile(
    r"\b" + _ROTA_ROLE + r"\s*,?\s*(?:i|1)\b(?![-\w])" + _NOT_A_DURATION, re.IGNORECASE,
)
_PART_TIME_COMMITMENT = re.compile(r"part[- ]?time", re.IGNORECASE)


def _frontline(title: str) -> bool:
    """FRONTLINE, except that a shift alone does not make a level-I desk
    role (_ROTA_ROLE) hourly floor work: Replit's "Support Engineer I (FC,
    Weekend Shift)" is a support engineer on a weekend rota. A production
    floor's level-I role on a shift is floor work ("Factory Test Technical
    Specialist I - 1st Shift", "Technician I - 2nd Shift"), and a desk
    role's other words still veto ("Software Engineer I, Night Shift,
    #12")."""
    if _ROTA_LEVEL_ONE.search(title):
        title = _SHIFT_WORDS.sub(" ", title)
    return bool(FRONTLINE.search(title))


# Title words that file an entry-level row under internships, not new grad.
INTERNSHIP_TITLE = re.compile(
    r"\bintern(?:s|ships?)?\b|" + _COOP + r"|\bstagiaires?\b|\bstage\s+coop\w*|" + _STUDENT,
    re.IGNORECASE,
)
_INTERNSHIP_DEPARTMENT = re.compile(r"\bintern(?:s|ships?)?\b|" + _COOP, re.IGNORECASE)
# A work term: "RF Validation Associate (Winter 2027)".
_TERM_TITLE = re.compile(r"\b(?:summer|fall|winter|spring|autumn)\s*,?\s*(?:19|20)\d\d\b",
                         re.IGNORECASE)
# ...but a start date for a junior hire: BDO's full-time "Junior Accountant,
# Assurance (Winter 2027 or Fall 2027 or Winter 2028)" is a new-grad job.
_JUNIOR_TITLE = re.compile(r"\bjunior\b|\bjr\b", re.IGNORECASE)
_NEW_GRAD_TITLE = re.compile(
    r"\b(?:new|recent|university|college)[- ]?grad(?:uate)?s?\b|\bearly[- ]careers?\b"
    r"|\bentry[- ]level\b|\b(?:19|20)\d\d\s+start\b",
    re.IGNORECASE,
)
# A title naming the student role itself (an intern, a co-op, a stagiaire, a
# student, a new or recent grad), not an early-career program or its staff:
# "Chief of Staff Intern" and "Software Engineer Intern - Sr. Design" are
# interns, while "Head of Internships", "Senior Manager, Early Careers",
# "Senior Director, Graduate Programs" and "Principal Engineer (New Grad
# Mentor)" run or serve the program.
STUDENT_ROLE_TITLE = re.compile(
    r"(?:\bintern(?:ship)?\b|" + _COOP + r"|\bstagiaires?\b|" + _STUDENT
    + r"|\b(?:new|recent|university|college)[- ]?grad(?:uate)?s?\b)"
    r"(?!\s+(?:" + _PROGRAM_OBJECT + r"|" + _PROGRAM_ROLE + r"|mentors?|recruit\w*)\b)",
    re.IGNORECASE,
)


def entry_tier(title: str, department: str = "", employment_type: str = "") -> Optional[str]:
    """'strong' | 'weak' | None (not an entry-level listing), from the title,
    the board's department and the source's commitment field. Reads the
    first _MAX_TITLE characters: the level-role patterns go quadratic on a
    pathological run of whitespace, and no real title is that long."""
    title = (title or "")[:_MAX_TITLE]
    if HARD_SENIOR.search(title) or PROGRAM_STAFF.search(title):
        return None
    # A recruiting title is staff, unless it names a student track or sits in
    # an internship department (Astranis files "Recruiting Coordination
    # Associate" under "Talent Acquisition Internships").
    if (RECRUITING.search(title) and not STUDENT_JOB.search(title)
            and not _INTERNSHIP_DEPARTMENT.search(department or "")):
        return None
    strong_title = bool(STRONG_ENTRY.search(title)) or bool(
        EMPLOYMENT_TYPE_ENTRY.search(employment_type or "")
    )
    if SOFT_SENIOR.search(title) and not strong_title:
        return None
    if strong_title or DEPT_ENTRY.search(department or ""):
        return "strong"
    if (WEAK_ENTRY.search(title) and not _frontline(title)
            and not _PART_TIME_COMMITMENT.search(employment_type or "")):
        return "weak"
    return None


def experience_level_for(title: str, department: str = "", employment_type: str = "") -> str:
    """'internship' | 'new_grad' for an entry-level listing. Word-bounded:
    "Internal Audit Analyst", "International Payroll" and "Cooper, #559" are
    not internships. Past an intern/co-op/student title, and unless the title
    names a new-grad role, a work term in the title ("RF Validation Associate
    (Winter 2027)", not a junior hire's start date: "Junior Accountant - Fall
    2026"), a department naming internships or co-ops ("Payload
    Internships") or the source's own commitment ("Intern") files it under
    internships. ``employment_type`` is that commitment (ATSJob.
    employment_type), never the one extracted from a description: "prior
    internship experience" made SpaceX's "Financial Analyst" read as one."""
    title = (title or "")[:_MAX_TITLE]
    if INTERNSHIP_TITLE.search(title or ""):
        return "internship"
    if _NEW_GRAD_TITLE.search(title or ""):
        return "new_grad"
    if ((_TERM_TITLE.search(title or "") and not _JUNIOR_TITLE.search(title or ""))
            or _INTERNSHIP_DEPARTMENT.search(department or "")
            or EMPLOYMENT_TYPE_ENTRY.search(employment_type or "")):
        return "internship"
    return "new_grad"


# US/Canada location classification lives in na_location (whole words and
# codes where codes sit, never substrings: "usa" is not in "Busan", "IN" after
# "Bangalore" is India), shared with the country column every ingest path
# stores. The substring lists that used to live here let about 80 visible
# foreign rows through (2026-09) and dropped real US ones ("Newaygo,
# Michigan, US", "McLean, Virginia").

# Rejection reasons a board crawl may act on: the listing itself says the
# posting is off target, so a stored row at its URL is retired (listing_status
# off_target) instead of confirmed. "unplaced" (no North American location,
# and no evidence against one either: Workday's "2 Locations", "Hybrid") never
# retires a row. Only a crawl that filters by level gives a "level" verdict:
# cron-ats while CRON_ATS_V2_CATALOGUE is off, which keeps the student
# catalogue as before scraper v2. An every-level crawl (the switch on, the v2
# runner) never does, so there only a posting outside the US and Canada is
# off target.
RETIRABLE_REJECTIONS = frozenset({"level", "location"})

# Workday names one location and counts the rest ("PRAGUE DC (2 Locations)",
# "3 Locations"): the unnamed ones may be North American, so a foreign name
# beside a count is not a retire verdict.
_MORE_LOCATIONS = re.compile(r"\b\d+\s+locations?\b", re.IGNORECASE)


# ─── Per-host pacing (ToS hygiene) ───────────────────────────────────────────
# Greenhouse/Lever/Ashby boards all share one API host each, so a registry of
# 100+ boards means 100+ back-to-back requests to the same host. Space them.

_HOST_MIN_INTERVAL = float(os.getenv("ATS_PER_HOST_INTERVAL", "0.35"))
_host_last_request: dict[str, float] = {}
# One lock per host, so a board waiting out its host's interval never stalls a
# concurrent crawl of a different host. asyncio locks bind to the event loop
# that first waits on them, so the table is rebuilt when the loop changes.
_host_locks: dict[str, asyncio.Lock] = {}
_host_locks_loop: Optional[asyncio.AbstractEventLoop] = None


def _host_lock(host: str) -> asyncio.Lock:
    global _host_locks_loop
    loop = asyncio.get_running_loop()
    if loop is not _host_locks_loop:
        _host_locks.clear()
        _host_locks_loop = loop
    lock = _host_locks.get(host)
    if lock is None:
        lock = _host_locks[host] = asyncio.Lock()
    return lock


async def _pace(host: str) -> None:
    """Enforce a minimum interval between requests to the same host."""
    if _HOST_MIN_INTERVAL <= 0 or not host:
        return
    async with _host_lock(host):
        now = time.monotonic()
        wait = _host_last_request.get(host, 0.0) + _HOST_MIN_INTERVAL - now
        if wait > 0:
            await asyncio.sleep(wait)
            now = time.monotonic()
        _host_last_request[host] = now


def _host_of(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


_PLATFORM_API_HOSTS = {
    "greenhouse": "boards-api.greenhouse.io",
    "lever": "api.lever.co",
    "ashby": "api.ashbyhq.com",
    "smartrecruiters": "api.smartrecruiters.com",
}


def board_host(platform: str, slug: str) -> str:
    """The API host a board's crawl hits. Greenhouse/Lever/Ashby/SmartRecruiters
    boards share one host per platform; every Workday tenant is its own host.
    Callers crawling boards concurrently keep one board in flight per host."""
    if platform == "workday":
        from backend.data.company_registry import load_workday_bases

        return _host_of(load_workday_bases().get(slug, "")) or f"workday:{slug}"
    return _PLATFORM_API_HOSTS.get(platform, platform)


# ─── Workday helpers ─────────────────────────────────────────────────────────

_WORKDAY_PAGE_SIZE = 20  # CxS caps at 20
# CxS never lists past 2000 postings ("total" pins at 2000 on Hitachi-sized
# boards), so such a board can never be crawled completely; its rows rely on
# per-row verification instead of list membership.
_WORKDAY_LIST_CEILING = 2000
# Per-board page cap: enough pages to list a board right up to the ceiling.
_WORKDAY_MAX_PAGES = max(1, int(os.getenv("WORKDAY_MAX_PAGES", "100")))
# The newest-first head of the list, where new postings land. Always fetched;
# pages past it only serve reconciliation, so they stop when the crawl budget
# runs out or the board is past the ceiling.
_WORKDAY_HEAD_PAGES = min(_WORKDAY_MAX_PAGES, 8)
_POSTED_AGO_RE = re.compile(r"posted\s+(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.IGNORECASE)


def _parse_workday_posted(posted_on: str) -> Optional[datetime.datetime]:
    """"Posted Today" / "Posted 3 Days Ago" / "Posted 30+ Days Ago" → datetime."""
    m = _POSTED_AGO_RE.search(posted_on or "")
    if not m:
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    token = m.group(1).lower()
    if token == "today":
        return now
    if token == "yesterday":
        return now - datetime.timedelta(days=1)
    try:
        return now - datetime.timedelta(days=int(m.group(2)))
    except (TypeError, ValueError):
        return None


def workday_public_base(cxs_base: str) -> str:
    """CxS API base → public posting base.

    "https://bmo.wd3.myworkdayjobs.com/wday/cxs/bmo/external"
      → "https://bmo.wd3.myworkdayjobs.com/external"

    A myworkdaysite.com host serves every tenant from one pod ("wd1") and
    names the tenant in the path instead, under /recruiting/. Checked live
    2026-10-25: Workday's own externalUrl for a posting of each of the 23
    boards is this URL (one site name differs in letter case only):
    "https://wd1.myworkdaysite.com/wday/cxs/upenn/careers-at-penn"
      → "https://wd1.myworkdaysite.com/recruiting/upenn/careers-at-penn"
    """
    m = re.match(r"(https?://[^/]+)/wday/cxs/([^/]+)/([^/?#]+)", (cxs_base or "").rstrip("/"))
    if not m:
        return (cxs_base or "").rstrip("/")
    host, tenant, site = m.groups()
    if host.lower().endswith(".myworkdaysite.com"):
        return f"{host}/recruiting/{tenant}/{site}"
    return f"{host}/{site}"


SMARTRECRUITERS_POSTING_BASE = "https://jobs.smartrecruiters.com/"
# Rows stored before the URL fix point here instead; it redirects to the
# company's careers home, so those rows are migrated: when their board lists
# them (cron-ats), and from the URL alone for the rest (cron-backfill,
# legacy_urls.migrate_legacy_smartrecruiters).
SMARTRECRUITERS_LEGACY_BASE = "https://careers.smartrecruiters.com/"


def smartrecruiters_legacy_url(url: str) -> str:
    """jobs.smartrecruiters.com posting URL → the careers.smartrecruiters.com
    URL an older crawl stored for the same posting ("" for other URLs)."""
    if not (url or "").startswith(SMARTRECRUITERS_POSTING_BASE):
        return ""
    return SMARTRECRUITERS_LEGACY_BASE + url[len(SMARTRECRUITERS_POSTING_BASE):]


def _workday_unlisted_key(posting: dict) -> str:
    """Identity of a CxS posting that can't be listed as a job (no title or
    no externalPath): its path or req-id bullets when it has them, else the
    whole payload. Stable across pages, so a stub read twice counts once."""
    ident = posting.get("externalPath") or posting.get("bulletFields") or posting
    return json.dumps(ident, sort_keys=True, default=str)


# A requisition id bullet ("R186419", "JR-12345", "2026-0042"), never a place.
_WORKDAY_REQ_ID = re.compile(r"[A-Za-z]{0,4}[-_ ]?\d[\w-]*")
_WORKDAY_COUNTED_LOCATIONS = re.compile(r"\s*\d+\s+locations?\s*", re.IGNORECASE)


def _workday_location(posting: dict) -> str:
    """The list row's location: ``locationsText``, or for tenants that omit it
    (Parsons lists all ~1,950 postings without one, so the NA filter dropped
    every one) the first bullet that is not a requisition id, e.g.
    "US - CA, Pasadena"."""
    text = _text(posting.get("locationsText"))
    if text:
        return text  # otherwise verbatim: stored rows compare against it for edits
    for item in posting.get("bulletFields") or []:
        item = item.strip() if isinstance(item, str) else ""
        if item and not _WORKDAY_REQ_ID.fullmatch(item):
            return item
    return ""


def _workday_location_hint(location: str, external_path: str) -> str:
    """For a posting whose location is only a count ("3 Locations"), the
    primary location Workday puts in its path: "/job/Toronto-ON/Dev_R1" ->
    "Toronto-ON". "" otherwise. TD, Boeing, PwC and Adobe list ~300 entry-level
    postings this way, and ~215 of them name a US/Canadian place there."""
    if not _WORKDAY_COUNTED_LOCATIONS.fullmatch(location or ""):
        return ""
    parts = (external_path or "").split("/")
    if len(parts) > 3 and parts[1] == "job":
        return parts[2]
    return ""


def _other_na_locations(primary: str, others) -> str:
    """The North American places among a posting's other locations (Ashby
    ``secondaryLocations``, Lever ``categories.allLocations``), "; "-joined.
    A posting whose primary location is abroad but which is also open in New
    York or Toronto is a North American posting, not a "location" reject.
    A bare "Remote" ("Remote - Worldwide") names no place, so it vouches for
    nothing: Perplexity's "Belgrade" posting, also open "Remote", is not
    North American."""
    places: list[str] = []
    for item in others or []:
        place = item.get("location") if isinstance(item, dict) else item
        place = place.strip() if isinstance(place, str) else ""
        if place and place != (primary or "").strip() and place not in places \
                and names_north_american_place(place):
            places.append(place)
    return "; ".join(places)


def _workday_external_id(external_path: str, bullet_fields: list) -> str:
    """Prefer the req id Workday appends to the path ("…_R-12345"); fall back
    to the first bulletField (usually the same req id)."""
    tail = (external_path or "").rsplit("/", 1)[-1]
    if "_" in tail:
        candidate = tail.rsplit("_", 1)[-1]
        if candidate and len(candidate) <= 40:
            return candidate
    for item in bullet_fields or []:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return tail[:80]


_SMARTRECRUITERS_PAGE_SIZE = 100  # API maximum
_SMARTRECRUITERS_MAX_PAGES = max(1, int(os.getenv("SMARTRECRUITERS_MAX_PAGES", "20")))
_SMARTRECRUITERS_HEAD_PAGES = min(_SMARTRECRUITERS_MAX_PAGES, 5)


class ATSScraper:
    """Scrapes job listings from public ATS APIs.

    ``deadline`` (a ``time.monotonic()`` instant) bounds the reconciliation-only
    paging of big boards across one run: past it, a paged board stops after
    its head pages and reports an incomplete snapshot instead of eating the
    rest of the cron's time.
    """

    def __init__(self, filter_entry_level: bool = True, filter_north_america: bool = True,
                 deadline: Optional[float] = None):
        self.filter_entry_level = filter_entry_level
        self.filter_north_america = filter_north_america
        self.deadline = deadline

    def _out_of_time(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    # ── Batch interfaces ────────────────────────────────────────────────────

    async def scrape_all(
        self, companies: Optional[list[tuple[str, str, str]]] = None
    ) -> list[ATSJob]:
        """Scrape the given (platform, slug, name) companies, the full
        registry when omitted. Returns filtered job list."""
        all_jobs: list[ATSJob] = []
        if companies is None:
            companies = _registry_companies()

        async with httpx.AsyncClient(timeout=30) as client:
            for platform, slug, company_name in companies:
                try:
                    snapshot = await self.scrape_board(client, platform, slug, company_name)
                    all_jobs.extend(snapshot.jobs)
                    logger.info(f"Scraped {len(snapshot.jobs)} jobs from {platform}/{slug}")
                except httpx.HTTPStatusError as e:
                    logger.warning(f"HTTP error scraping {platform}/{slug}: {e.response.status_code}")
                except httpx.TimeoutException:
                    logger.warning(f"Timeout scraping {platform}/{slug}")
                except Exception as e:
                    logger.warning(f"Error scraping {platform}/{slug}: {e}")

        return all_jobs

    async def scrape_company(self, platform: str, slug: str, company_name: str) -> list[ATSJob]:
        """Scrape a single company. Returns filtered job list."""
        async with httpx.AsyncClient(timeout=30) as client:
            try:
                snapshot = await self.scrape_board(client, platform, slug, company_name)
            except Exception:
                return []
            return snapshot.jobs

    async def scrape_board(
        self, client: httpx.AsyncClient, platform: str, slug: str, company_name: str
    ) -> BoardSnapshot:
        """Fetch one board completely: filtered jobs + the full live-URL set.

        Raises on fetch failure (callers isolate per-board errors), a failed
        board must never produce an empty snapshot that reads as "everything
        was taken down"."""
        if platform == "greenhouse":
            listings = await self._fetch_greenhouse(client, slug, company_name)
            complete, total = True, len(listings)
        elif platform == "lever":
            listings = await self._fetch_lever(client, slug, company_name)
            complete, total = True, len(listings)
        elif platform == "ashby":
            listings = await self._fetch_ashby(client, slug, company_name)
            complete, total = True, len(listings)
        elif platform == "smartrecruiters":
            listings, complete, total = await self._fetch_smartrecruiters(client, slug, company_name)
        elif platform == "workday":
            listings, complete, total = await self._fetch_workday(client, slug, company_name)
        else:
            # Every other platform is a v2 adapter (backend/ingest/adapters),
            # which lists the whole board unfiltered; the filters below apply
            # as they do to the built-ins. A platform with no adapter, or one
            # whose API keys are missing, keeps the empty incomplete snapshot:
            # nothing ingested, and nothing reconciled away.
            from backend.data.company_registry import load_board_config
            from backend.ingest.adapters import adapter_enabled, get_adapter

            adapter = get_adapter(platform)
            if adapter is None or not adapter_enabled(platform):
                return BoardSnapshot(platform=platform, slug=slug, company=company_name,
                                     complete=False)
            listings, complete, total = await adapter.list_jobs(
                client, slug, company_name,
                config=load_board_config(platform, slug), deadline=self.deadline,
            )
            listings = list(listings)

        from backend.data.company_registry import load_board_countries

        home_country = load_board_countries().get(f"{platform}:{slug}", "")
        snapshot = BoardSnapshot(
            platform=platform,
            slug=slug,
            company=company_name,
            all_urls={job.url for job in listings if job.url},
            complete=complete,
            total_listed=total,
        )
        # One verdict per listing: the passing ones are ingested, the rest
        # keep their reason for reconciliation (a stored row at a "level" or
        # "location" URL is retired rather than confirmed).
        for job in listings:
            reason = self.rejection(job, home_country)
            if reason is None:
                snapshot.jobs.append(job)
            elif job.url:
                snapshot.rejected[job.url] = reason
        # A URL listed twice keeps its passing verdict.
        for job in snapshot.jobs:
            snapshot.rejected.pop(job.url, None)
        return snapshot

    # ── Back-compat filtered single-platform methods ────────────────────────

    async def _scrape_greenhouse(self, client: httpx.AsyncClient, slug: str, company_name: str) -> list[ATSJob]:
        return [j for j in await self._fetch_greenhouse(client, slug, company_name)
                if self._passes_filters(j)]

    async def _scrape_lever(self, client: httpx.AsyncClient, slug: str, company_name: str) -> list[ATSJob]:
        return [j for j in await self._fetch_lever(client, slug, company_name)
                if self._passes_filters(j)]

    async def _scrape_ashby(self, client: httpx.AsyncClient, slug: str, company_name: str) -> list[ATSJob]:
        return [j for j in await self._fetch_ashby(client, slug, company_name)
                if self._passes_filters(j)]

    async def _scrape_smartrecruiters(self, client: httpx.AsyncClient, identifier: str, company_name: str) -> list[ATSJob]:
        listings, _complete, _total = await self._fetch_smartrecruiters(client, identifier, company_name)
        return [j for j in listings if self._passes_filters(j)]

    # ── Platform fetchers (unfiltered) ──────────────────────────────────────

    async def _fetch_greenhouse(self, client: httpx.AsyncClient, slug: str, company_name: str) -> list[ATSJob]:
        """Fetch jobs from Greenhouse boards API.

        API docs: https://developers.greenhouse.io/job-board.html
        """
        url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
        params = {"content": "true"}  # Same single request, but with descriptions

        await _pace(_host_of(url))
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        jobs: list[ATSJob] = []
        for job_data in data.get("jobs", []):
            title = _text(job_data.get("title"))
            location = _text((job_data.get("location") or {}).get("name"))
            job_url = job_data.get("absolute_url", "")
            updated_at = job_data.get("updated_at", "")

            # Parse date
            posted_date = None
            if updated_at:
                try:
                    posted_date = datetime.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            # Determine department
            departments = job_data.get("departments", [])
            department = departments[0].get("name", "") if departments else ""

            # Pay transparency ranges, when the employer publishes them.
            salary_text = ""
            for pay_range in job_data.get("pay_input_ranges") or []:
                min_cents = pay_range.get("min_cents")
                max_cents = pay_range.get("max_cents")
                if min_cents and max_cents:
                    currency = pay_range.get("currency_type", "USD")
                    salary_text = f"{min_cents / 100:.0f}-{max_cents / 100:.0f} {currency}"
                    break

            job = ATSJob(
                title=title,
                company=company_name,
                location=location,
                url=job_url,
                posted_date=posted_date,
                department=department,
                work_type=self._detect_work_type(location, title),
                description=clean_html(job_data.get("content", "") or ""),
                external_id=str(job_data.get("id") or ""),
                salary_text=salary_text,
            )
            jobs.append(job)

        return jobs

    async def _fetch_lever(self, client: httpx.AsyncClient, slug: str, company_name: str) -> list[ATSJob]:
        """Fetch jobs from Lever postings API.

        API docs: https://github.com/lever/postings-api
        """
        url = f"https://api.lever.co/v0/postings/{slug}"
        params = {"mode": "json"}

        await _pace(_host_of(url))
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

        if not isinstance(data, list):
            return []

        jobs: list[ATSJob] = []
        for posting in data:
            title = _text(posting.get("text"))
            categories = posting.get("categories", {})
            location = _text(categories.get("location"))
            job_url = posting.get("hostedUrl", "")
            created_at = posting.get("createdAt")

            # Lever uses millisecond timestamps
            posted_date = None
            if created_at:
                try:
                    posted_date = datetime.datetime.fromtimestamp(created_at / 1000)
                except (ValueError, TypeError, OSError):
                    pass

            department = categories.get("department", "")
            commitment = categories.get("commitment", "")  # e.g., "Full-time", "Intern"

            description = posting.get("descriptionPlain") or ""
            for lst in posting.get("lists", []) or []:
                content = clean_html(lst.get("content", ""))
                if content:
                    description += f"\n\n{lst.get('text', '')}\n{content}"
            description = description.strip()[:10000]

            salary_text = ""
            salary_range = posting.get("salaryRange") or {}
            if salary_range.get("min") and salary_range.get("max"):
                currency = salary_range.get("currency", "USD")
                interval = salary_range.get("interval", "")
                salary_text = f"{salary_range['min']}-{salary_range['max']} {currency} {interval}".strip()

            job = ATSJob(
                title=title,
                company=company_name,
                location=location,
                url=job_url,
                posted_date=posted_date,
                department=department,
                work_type=self._detect_work_type(location, title),
                description=description,
                external_id=str(posting.get("id") or ""),
                employment_type=commitment,
                salary_text=salary_text,
                location_hint=_other_na_locations(location, categories.get("allLocations")),
            )
            jobs.append(job)

        return jobs

    async def _fetch_ashby(self, client: httpx.AsyncClient, slug: str, company_name: str) -> list[ATSJob]:
        """Fetch jobs from Ashby posting API.

        API: https://api.ashbyhq.com/posting-api/job-board/{slug}
        """
        url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"

        await _pace(_host_of(url))
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

        jobs: list[ATSJob] = []
        for job_data in data.get("jobs", []):
            title = _text(job_data.get("title"))
            location = _text(job_data.get("location"))
            job_url = job_data.get("jobUrl", "")
            published_at = job_data.get("publishedAt", "")
            department = job_data.get("departmentName", "")

            # Parse date
            posted_date = None
            if published_at:
                try:
                    posted_date = datetime.datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            job = ATSJob(
                title=title,
                company=company_name,
                location=location,
                url=job_url,
                posted_date=posted_date,
                department=department,
                work_type=self._detect_work_type(location, title),
                description=clean_html(
                    job_data.get("descriptionHtml") or job_data.get("descriptionPlain") or ""
                ),
                external_id=str(job_data.get("id") or ""),
                employment_type=job_data.get("employmentType", "") or "",
                salary_text=job_data.get("compensationTierSummary", "") or "",
                location_hint=_other_na_locations(location, job_data.get("secondaryLocations")),
            )
            jobs.append(job)

        return jobs

    async def _fetch_smartrecruiters(
        self, client: httpx.AsyncClient, identifier: str, company_name: str
    ) -> tuple[list[ATSJob], bool, int]:
        """Fetch jobs from SmartRecruiters postings API, following pagination.

        API: https://api.smartrecruiters.com/v1/companies/{identifier}/postings
        Returns (listings, complete, total_on_board). Pages past the head stop
        at the page cap or the crawl deadline; Bosch-sized boards (~4800)
        stay partial and only confirm what they listed.
        """
        base_url = f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings"

        jobs: list[ATSJob] = []
        seen_urls: set[str] = set()
        total_found = 0
        offset = 0
        stopped_early = False
        for page in range(_SMARTRECRUITERS_MAX_PAGES):
            if page >= _SMARTRECRUITERS_HEAD_PAGES and self._out_of_time():
                stopped_early = True
                break
            await _pace(_host_of(base_url))
            response = await client.get(
                base_url,
                params={"limit": str(_SMARTRECRUITERS_PAGE_SIZE), "offset": str(offset)},
            )
            response.raise_for_status()
            data = response.json()
            content = data.get("content", [])
            total_found = max(total_found, int(data.get("totalFound") or len(content)))

            for job_data in content:
                title = _text(job_data.get("name"))

                # Build location from city, region, country
                loc_info = job_data.get("location", {})
                loc_parts = [
                    _text(loc_info.get("city")),
                    _text(loc_info.get("region")),
                    _text(loc_info.get("country")),
                ]
                location = ", ".join(part for part in loc_parts if part)

                # The list payload carries no public URL ("ref" is the API
                # resource). jobs.smartrecruiters.com is the posting page;
                # careers.smartrecruiters.com redirects to the company's
                # careers home for live and closed postings alike.
                job_id = job_data.get("id", "")
                if not job_id:
                    continue
                job_url = f"{SMARTRECRUITERS_POSTING_BASE}{identifier}/{job_id}"
                if job_url in seen_urls:
                    continue  # the list shifted under us between pages
                seen_urls.add(job_url)

                released_date = job_data.get("releasedDate", "")
                department_info = job_data.get("department", {})
                department = department_info.get("label", "") if department_info else ""

                # Parse date
                posted_date = None
                if released_date:
                    try:
                        posted_date = datetime.datetime.fromisoformat(released_date.replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        pass

                employment_info = job_data.get("typeOfEmployment") or {}

                job = ATSJob(
                    title=title,
                    company=company_name,
                    location=location,
                    url=job_url,
                    posted_date=posted_date,
                    department=department,
                    work_type=self._detect_work_type(location, title),
                    external_id=str(job_id or ""),
                    employment_type=(employment_info.get("label") or "") if isinstance(employment_info, dict) else "",
                )
                jobs.append(job)

            offset += len(content)
            if not content or offset >= total_found:
                break

        # Complete only when every posting the board counts was listed: a
        # page cap, the deadline, or a posting shifting between pages
        # (duplicates in, one skipped) all leave the unique count short.
        complete = not stopped_early and len(seen_urls) >= total_found
        return jobs, complete, total_found

    async def _fetch_workday(
        self, client: httpx.AsyncClient, slug: str, company_name: str
    ) -> tuple[list[ATSJob], bool, int]:
        """Fetch jobs from a Workday tenant's CxS job board API.

        The endpoint base comes from the registry's ``workday_url_template``
        ("https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}").
        POST {base}/jobs pages 20 at a time, newest first. Descriptions are NOT
        in the list payload, fetch_workday_detail() fills them per new job.

        The whole list is paged (list POSTs only, no detail calls) so the
        snapshot can reconcile the board: BMO's ~1000 postings are ~50 POSTs.
        Returns (listings, complete, total_on_board). complete=False (board at
        the 2000-posting ceiling, page cap or deadline hit, or the list moved
        mid-crawl) tells reconciliation not to vote on removals.
        """
        from backend.data.company_registry import load_workday_bases

        cxs_base = load_workday_bases().get(slug, "")
        if not cxs_base:
            logger.warning("workday/%s has no workday_url_template; skipping", slug)
            return [], False, 0

        cxs_base = cxs_base.rstrip("/")
        public_base = workday_public_base(cxs_base)
        list_url = f"{cxs_base}/jobs"
        host = _host_of(list_url)

        jobs: list[ATSJob] = []
        seen_urls: set[str] = set()
        # Postings "total" counts that can't be listed as jobs (P&G carries a
        # stub with no title and no externalPath). Without them the unique
        # count stays one short and the board never reconciles. Keyed by
        # identity: a stub read twice across a page shift counts once, so it
        # can't stand in for a live posting the shift skipped.
        unlisted: set[str] = set()
        total = 0
        fetched = 0
        stopped_early = False
        for page in range(_WORKDAY_MAX_PAGES):
            if page >= _WORKDAY_HEAD_PAGES and (
                total >= _WORKDAY_LIST_CEILING or self._out_of_time()
            ):
                stopped_early = True
                break
            await _pace(host)
            response = await client.post(
                list_url,
                json={
                    "appliedFacets": {},
                    "limit": _WORKDAY_PAGE_SIZE,
                    "offset": page * _WORKDAY_PAGE_SIZE,
                    "searchText": "",
                },
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
            postings = data.get("jobPostings", []) or []
            # Some tenants only report "total" on the first page (BMO returns
            # 0 afterwards), keep the largest figure seen, never regress.
            total = max(total, int(data.get("total") or 0))

            for posting in postings:
                external_path = posting.get("externalPath", "") or ""
                title = _text(posting.get("title"))
                location = _workday_location(posting)
                if not title or not external_path:
                    unlisted.add(_workday_unlisted_key(posting))
                    continue
                job_url = f"{public_base}{external_path}"
                if job_url in seen_urls:
                    continue  # the list shifted under us between pages
                seen_urls.add(job_url)
                job = ATSJob(
                    title=title,
                    company=company_name,
                    location=location,
                    url=job_url,
                    posted_date=_parse_workday_posted(posting.get("postedOn", "") or ""),
                    department="",
                    work_type=self._detect_work_type(location, title),
                    external_id=_workday_external_id(external_path, posting.get("bulletFields")),
                    detail_ref=external_path,
                    location_hint=_workday_location_hint(location, external_path),
                )
                jobs.append(job)

            fetched += len(postings)
            if not postings or fetched >= total:
                break

        # Complete only when every posting the board counts is accounted for,
        # listed or unlistable. A removal between two page requests shifts
        # the list up by one and skips a live posting, which leaves the count
        # short of "total"; that must read as partial, never as a takedown. A
        # board that never reported a total can't prove completeness either.
        complete = (
            not stopped_early
            and total < _WORKDAY_LIST_CEILING
            and len(seen_urls) + len(unlisted) >= total
            and (total > 0 or not (seen_urls or unlisted))
        )
        return jobs, complete, total


    def _passes_filters(self, job: ATSJob, home_country: str = "") -> bool:
        """Check if a job passes the configured filters. ``home_country`` is
        the registry's country for a one-country board: every listing on it
        is in North America, whatever its location text says."""
        return self.rejection(job, home_country) is None

    def rejection(self, job: ATSJob, home_country: str = "") -> Optional[str]:
        """Why the configured filters reject ``job``, or None when it passes.

        The one place that decides, for ingest (a listing is inserted only on
        None) and for reconciliation (a stored row whose listing says
        "level" or "location" is retired, RETIRABLE_REJECTIONS):

        - "level": not entry level (_is_entry_level: entry_tier of the title,
          department and commitment)
        - "location": positive evidence the posting is outside the US and
          Canada (na_location.region_of says FOREIGN, and the text does not
          count further unnamed locations: "PRAGUE DC (2 Locations)")
        - "unplaced": no North American location, and nothing against one
          either ("3 Locations", "Hybrid", ""): never retires a row

        A Workday "3 Locations" posting passes on its path's primary
        location, and an Ashby/Lever posting on any North American one of
        its other locations (``location_hint``)."""
        if self.filter_entry_level and not self._is_entry_level(job):
            return "level"
        if self.filter_north_america and not home_country:
            region = region_of(job.location)
            if region in (US, CA):
                return None
            if job.location_hint and hint_region(job.location_hint):
                return None
            if region == FOREIGN and not _MORE_LOCATIONS.search(job.location or ""):
                return "location"
            return "unplaced"
        return None

    def _is_entry_level(self, job: ATSJob) -> bool:
        """Intern/new-grad/entry-level: entry_tier() of the title, the
        board's department (a named track only) and the source's commitment
        field is strong or weak."""
        return entry_tier(job.title, job.department or "", job.employment_type or "") is not None

    def _is_north_america(self, location: str) -> bool:
        """Check if location is in US or Canada (na_location decides)."""
        return is_north_america(location)

    def _detect_work_type(self, location: str, title: str) -> str:
        """Detect Remote/Hybrid/On Site from location and title text."""
        combined = f"{location} {title}".lower()
        if "remote" in combined:
            if "hybrid" in combined:
                return "Hybrid"
            return "Remote"
        if "hybrid" in combined:
            return "Hybrid"
        return "On Site"


async def fetch_workday_detail(
    client: httpx.AsyncClient, slug: str, detail_ref: str
) -> dict:
    """Fetch one Workday posting's detail (description + timeType).

    GET {cxs_base}{externalPath} → jobPostingInfo. Called for NEW jobs only,
    one request per job, same budget shape as the SmartRecruiters detail fetch.
    Returns {"description": str, "employment_type": str}; empty dict on miss.
    """
    from backend.data.company_registry import load_workday_bases

    cxs_base = (load_workday_bases().get(slug, "") or "").rstrip("/")
    if not cxs_base or not detail_ref:
        return {}

    url = f"{cxs_base}{detail_ref}"
    await _pace(_host_of(url))
    response = await client.get(url, headers={"Accept": "application/json"})
    response.raise_for_status()
    info = (response.json() or {}).get("jobPostingInfo") or {}

    return {
        "description": clean_html(info.get("jobDescription", "") or "")[:10000],
        "employment_type": info.get("timeType", "") or "",
    }
