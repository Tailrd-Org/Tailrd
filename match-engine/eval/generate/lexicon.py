"""Word banks for the synthetic corpus: invented people, employers and schools, the work each
profession does, and the phrases bullets are built from (EVAL.md section 2.1).

Everything here is invented. Names pair common given names with obviously fictional surnames,
phones sit in the 555-01xx block that is reserved for fiction, mail goes to example.com, and
employers are assembled from generic words. Pure data plus `title_for`; no I/O, no clock.

Hygiene rules the tests enforce on these banks (so the ground truth stays exact):
  * every bullet the banks can build reads, to the engine (`matcher.score.evidence.analyze_bullet`),
    as the verb class its template declares and as AFFIRMED, whatever the lexicon holds now;
  * every quantified tail reads as quantified (`is_quantified`), every other phrase carries no digit;
  * no phrase is a negation or aspiration term, and none spells a skill of the default vocabulary.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from matcher.models import SeniorityLevel, SkillKind, Track, VerbClass

# --------------------------------------------------------------------------------------------
# people, places, employers, schools
# --------------------------------------------------------------------------------------------

FIRST_NAMES = (
    "Alex",
    "Sam",
    "Jordan",
    "Taylor",
    "Morgan",
    "Casey",
    "Riley",
    "Jamie",
    "Avery",
    "Quinn",
    "Rowan",
    "Skyler",
    "Devon",
    "Robin",
    "Drew",
    "Kendall",
    "Reese",
    "Parker",
    "Emerson",
    "Harper",
    "Blake",
    "Cameron",
    "Dakota",
    "Finley",
    "Hayden",
    "Jesse",
    "Kai",
    "Logan",
    "Micah",
    "Noel",
    "Peyton",
    "Remy",
    "Sage",
    "Tatum",
    "Wren",
)
LAST_NAMES = (
    "Example",
    "Sample",
    "Placeholder",
    "Specimen",
    "Testcase",
    "Mockup",
    "Demo",
    "Fictional",
    "Illustrative",
    "Prototype",
    "Hypothetical",
    "Template",
)
AREA_CODES = ("212", "312", "415", "416", "514", "604", "647", "206")

# (city, region, country)
LOCATIONS = (
    ("Toronto", "ON", "CA"),
    ("Vancouver", "BC", "CA"),
    ("Montreal", "QC", "CA"),
    ("Calgary", "AB", "CA"),
    ("Ottawa", "ON", "CA"),
    ("Halifax", "NS", "CA"),
    ("New York", "NY", "US"),
    ("Austin", "TX", "US"),
    ("Seattle", "WA", "US"),
    ("Boston", "MA", "US"),
    ("Chicago", "IL", "US"),
    ("Denver", "CO", "US"),
    ("Atlanta", "GA", "US"),
    ("San Francisco", "CA", "US"),
    ("Raleigh", "NC", "US"),
    ("Minneapolis", "MN", "US"),
)

EMPLOYER_PREFIXES = (
    "Brightfield",
    "Harborview",
    "Cobalt Ridge",
    "Northgate",
    "Summit Peak",
    "Lakeshore",
    "Pinecrest",
    "Ironwood",
    "Silverline",
    "Redwood",
    "Bluepeak",
    "Maplewood",
    "Orion Point",
    "Willowbrook",
    "Granite Bay",
    "Foxglove",
    "Evergreen",
    "Clearwater",
    "Stonebridge",
    "Meridian",
    "Copperleaf",
    "Highland",
    "Riverbend",
    "Sunfield",
    "Windmere",
    "Oakhaven",
    "Starling",
    "Tidewater",
    "Juniper",
    "Larkspur",
)
SCHOOL_PREFIXES = (
    "Northfield",
    "Lakeview",
    "Ridgemont",
    "Eastbrook",
    "Westhaven",
    "Clearfield",
    "Pinehurst",
    "Stonegate",
    "Riverside",
    "Hillcrest",
    "Fairmont",
    "Oakridge",
)
SCHOOL_KINDS = ("State University", "Institute of Technology", "College", "University")

# --------------------------------------------------------------------------------------------
# professions
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FamilyData:
    key: str
    ic_title: str  # the IC2 title; the ladder derives the others
    function: str  # management titles: "<function> Manager", "Director of <function>"
    naics: tuple[str, ...]  # industries employers of this profession sit in (NAICS, 4 digits)
    employer_words: tuple[str, ...]
    objects: tuple[str, ...]  # what the work produces, as noun phrases for bullets
    metrics: tuple[str, ...]  # quantities that can be pushed down
    scale_tail: str  # a quantified tail with {n}: a glued magnitude ("40K") or a counting noun
    scale_range: tuple[int, int] = (2, 90)  # the numbers {n} is drawn from
    title_pattern: str | None = None  # "{skill} Developer": a title that names a skill (ROLE_TITLE)
    title_kinds: tuple[SkillKind, ...] = ()
    mos: tuple[tuple[str, str, str], ...] = ()  # (code, title, civilian translation)


T, L, M, D = SkillKind.TECHNOLOGY, SkillKind.TOOL, SkillKind.METHODOLOGY, SkillKind.DOMAIN

FAMILY_DATA: dict[str, FamilyData] = {
    "software": FamilyData(
        key="software",
        ic_title="Software Engineer",
        function="Software Engineering",
        naics=("5112", "5415", "5182", "5191"),
        employer_words=("Software", "Systems", "Labs", "Cloud", "Digital"),
        objects=(
            "backend services",
            "internal tooling",
            "customer-facing features",
            "batch processing jobs",
            "integration layers",
            "service endpoints",
            "release tooling",
            "data access layers",
        ),
        metrics=("latency", "error rates", "release cycle time", "page load times", "incident volume"),
        scale_tail=", serving {n}K monthly users",
        title_pattern="{skill} Developer",
        title_kinds=(T,),
        mos=(
            (
                "25B",
                "Information Technology Specialist",
                "Network and systems support, help-desk operations and equipment accountability",
            ),
            (
                "25U",
                "Signal Support Systems Specialist",
                "Communications equipment setup, troubleshooting and field support",
            ),
            (
                "35T",
                "Military Intelligence Systems Maintainer/Integrator",
                "Systems integration, hardware maintenance and secure configuration",
            ),
        ),
    ),
    "data": FamilyData(
        key="data",
        ic_title="Data Analyst",
        function="Data Analytics",
        naics=("5415", "5416", "5417", "5182"),
        employer_words=("Analytics", "Insights", "Data", "Research"),
        objects=(
            "forecasting models",
            "reporting dashboards",
            "analysis notebooks",
            "experiment readouts",
            "scoring models",
            "metric definitions",
            "weekly reports",
        ),
        metrics=("report turnaround", "forecast error", "query times", "manual effort"),
        scale_tail=", covering {n}K records",
        title_pattern="{skill} Engineer",
        title_kinds=(T, D),
        mos=(
            (
                "35F",
                "Intelligence Analyst",
                "Data collection, pattern analysis and written briefings for decision makers",
            ),
            (
                "17C",
                "Cyber Operations Specialist",
                "Network defense, threat monitoring and incident reporting",
            ),
        ),
    ),
    "devops": FamilyData(
        key="devops",
        ic_title="DevOps Engineer",
        function="Infrastructure Engineering",
        naics=("5182", "5415", "5416", "5191"),
        employer_words=("Cloud", "Systems", "Networks", "Platforms"),
        objects=(
            "deployment pipelines",
            "cluster tooling",
            "environment provisioning",
            "monitoring stacks",
            "alerting rules",
            "backup routines",
            "release tooling",
        ),
        metrics=("deployment time", "downtime", "cloud spend", "recovery time"),
        scale_tail=", handling {n}K requests per minute",
        title_pattern="{skill} Engineer",
        title_kinds=(T,),
        mos=(
            (
                "25N",
                "Nodal Network Systems Operator-Maintainer",
                "Network operations, circuit maintenance and uptime reporting",
            ),
            (
                "25B",
                "Information Technology Specialist",
                "Network and systems support, help-desk operations and equipment accountability",
            ),
        ),
    ),
    "product": FamilyData(
        key="product",
        ic_title="Product Manager",
        function="Product Management",
        naics=("5112", "5415", "5418", "4541"),
        employer_words=("Labs", "Digital", "Commerce", "Software"),
        objects=(
            "product roadmaps",
            "feature specifications",
            "release plans",
            "pilot programs",
            "requirements documents",
            "launch plans",
        ),
        metrics=("time to market", "churn", "activation time", "ticket volume"),
        scale_tail=", adopted by {n}K customers",
        mos=(
            (
                "92A",
                "Automated Logistical Specialist",
                "Inventory control, supply planning and requisition tracking",
            ),
            (
                "42A",
                "Human Resources Specialist",
                "Personnel records, onboarding coordination and policy guidance",
            ),
        ),
    ),
    "design": FamilyData(
        key="design",
        ic_title="Product Designer",
        function="Design",
        naics=("5414", "5112", "5415", "5418"),
        employer_words=("Studio", "Creative", "Design", "Labs"),
        objects=(
            "component libraries",
            "prototype flows",
            "user journeys",
            "interface mockups",
            "style guides",
            "onboarding screens",
        ),
        metrics=("task completion time", "service requests", "drop-off", "rework"),
        scale_tail=", reaching {n}K people",
        mos=(
            (
                "25V",
                "Combat Documentation/Production Specialist",
                "Photo, video and layout production for command publications",
            ),
        ),
    ),
    "marketing": FamilyData(
        key="marketing",
        ic_title="Marketing Specialist",
        function="Marketing",
        naics=("5418", "5191", "4541", "5121"),
        employer_words=("Media", "Brands", "Creative", "Digital"),
        objects=(
            "outreach campaigns",
            "landing pages",
            "newsletter programs",
            "launch campaigns",
            "reporting packs",
            "audience segments",
        ),
        metrics=("cost per inquiry", "bounce rate", "response time", "acquisition cost"),
        scale_tail=", reaching {n}K subscribers",
        mos=(
            (
                "25V",
                "Combat Documentation/Production Specialist",
                "Messaging, publications and audience outreach for command audiences",
            ),
        ),
    ),
    "finance": FamilyData(
        key="finance",
        ic_title="Financial Analyst",
        function="Finance",
        naics=("5221", "5231", "5239", "5241", "5412"),
        employer_words=("Capital", "Financial", "Advisors", "Trust", "Partners"),
        objects=(
            "forecast packages",
            "audit workpapers",
            "reporting packages",
            "budget reviews",
            "variance reports",
            "account schedules",
        ),
        metrics=("close time", "reporting errors", "processing time", "outstanding balances"),
        scale_tail=", covering {n}K transactions",
        mos=(
            ("36B", "Financial Management Technician", "Disbursing, ledger upkeep and budget tracking"),
            (
                "92Y",
                "Unit Supply Specialist",
                "Inventory accountability, requisition processing and equipment records",
            ),
        ),
    ),
    "healthcare": FamilyData(
        key="healthcare",
        ic_title="Registered Nurse",
        function="Nursing",
        naics=("6211", "6214", "6221", "6231"),
        employer_words=("Health", "Medical", "Care", "Clinic", "Wellness"),
        objects=(
            "care protocols",
            "patient intake processes",
            "unit procedures",
            "shift handoffs",
            "discharge plans",
            "bedside routines",
        ),
        metrics=("patient wait times", "medication errors", "readmissions", "documentation time"),
        scale_tail=", covering {n} patients per shift",
        scale_range=(4, 14),
        mos=(
            (
                "68W",
                "Combat Medic Specialist",
                "Emergency treatment, patient transport and clinical charting",
            ),
            ("68C", "Practical Nursing Specialist", "Bedside care, medication rounds and patient monitoring"),
        ),
    ),
}

GENERIC_FAMILY = FamilyData(
    key="generic",
    ic_title="Specialist",
    function="Operations",
    naics=("5416", "5412", "5418", "5415"),
    employer_words=("Group", "Partners", "Solutions", "Services"),
    objects=(
        "internal processes",
        "reporting routines",
        "project plans",
        "client deliverables",
        "quality checks",
        "planning cycles",
    ),
    metrics=("turnaround time", "rework", "error rates", "handling time"),
    scale_tail=", covering {n}K client accounts",
    mos=(
        (
            "92A",
            "Automated Logistical Specialist",
            "Inventory control, supply planning and requisition tracking",
        ),
    ),
)


def family_data(family: str) -> FamilyData:
    """The profession data of a family; a family the bank does not know gets a generic profession."""
    return FAMILY_DATA.get(family, GENERIC_FAMILY)


_IC_PREFIX = {4: "Staff", 5: "Principal", 6: "Distinguished"}


def title_for(fam: FamilyData, seniority: SeniorityLevel, rng: random.Random, base: str | None = None) -> str:
    """A title on the two-track ladder of SPEC 5.4 (IC0 intern ... IC6, M1 lead ... M6)."""
    base = base or fam.ic_title
    level = seniority.level
    if seniority.track is Track.IC:
        if level == 0:
            return f"{base} Intern"
        if level == 1:
            return rng.choice((f"Junior {base}", f"{base} I"))
        if level == 2:
            return rng.choice((base, base, f"{base} II"))
        if level == 3:
            return rng.choice((f"Senior {base}", f"{base} III"))
        return f"{_IC_PREFIX[level]} {base}"
    return {
        1: f"Lead {base}",
        2: f"{fam.function} Manager",
        3: f"Senior {fam.function} Manager",
        4: f"Director of {fam.function}",
        5: f"VP of {fam.function}",
        6: f"Chief {fam.function} Officer",
    }[level]


# --------------------------------------------------------------------------------------------
# education
# --------------------------------------------------------------------------------------------

ARTS_FIELDS = frozenset({"Marketing", "Communications", "Graphic Design", "Psychology", "Economics"})


# --------------------------------------------------------------------------------------------
# bullets
# --------------------------------------------------------------------------------------------
# Layouts: "obj"   -> "<verb> <object> <skill clause>"      Built backend services using Python
#          "first" -> "<verb> <skills> for <object>"         Used Python and SQL for backend services
#          "only"  -> "<verb> <skills>"                      Coursework in Python and SQL
VERB_PHRASES: dict[VerbClass, tuple[tuple[str, str], ...]] = {
    VerbClass.OWNERSHIP: (
        ("Architected", "obj"),
        ("Designed", "obj"),
        ("Built", "obj"),
        ("Led the rollout of", "obj"),
        ("Created", "obj"),
        ("Spearheaded", "obj"),
        ("Established", "obj"),
        ("Pioneered", "obj"),
        ("Drove adoption of", "obj"),
        ("Owned", "obj"),
    ),
    VerbClass.EXECUTION: (
        ("Developed", "obj"),
        ("Implemented", "obj"),
        ("Engineered", "obj"),
        ("Delivered", "obj"),
        ("Migrated", "obj"),
        ("Automated", "obj"),
        ("Optimized", "obj"),
        ("Deployed", "obj"),
        ("Maintained", "obj"),
        ("Scaled", "obj"),
    ),
    VerbClass.PARTICIPATION: (
        ("Contributed to", "obj"),
        ("Collaborated on", "obj"),
        ("Participated in", "obj"),
        ("Worked on", "obj"),
        ("Used", "first"),
        ("Utilized", "first"),
    ),
    VerbClass.ASSISTANCE: (
        ("Assisted with", "obj"),
        ("Supported", "obj"),
        ("Helped the team with", "obj"),
        ("Shadowed senior colleagues on", "obj"),
    ),
    VerbClass.EXPOSURE: (
        ("Exposed to", "first"),
        ("Familiar with", "first"),
        ("Coursework in", "only"),
        ("Basic knowledge of", "only"),
    ),
}

# Joiners for the skill clause of an "obj" bullet whose skills are tools or technologies.
INSTRUMENT_JOINERS = ("using", "with", "in")

# Quantified tails: the number is drawn from the range. "{metric}" is one of the family's metrics.
METRIC_TAILS = (
    (", reducing {metric} by {n}%", (10, 60)),
    (", cutting {metric} by {n}%", (12, 55)),
    (", lowering {metric} by {n}%", (8, 45)),
)
FIXED_TAILS = (
    (", saving {n} hours per week", (3, 30)),
    (" for {n} internal teams", (3, 15)),
)
# Tails without a number.
PLAIN_TAILS = (
    ", improving day-to-day reliability",
    ", simplifying handoffs between teams",
    ", keeping stakeholders informed",
    ", improving consistency across teams",
    ", making routine tasks easier to repeat",
)
TEAMS = ("the core team", "internal customers", "regional offices", "business partners", "operations staff")

# --------------------------------------------------------------------------------------------
# summaries
# --------------------------------------------------------------------------------------------

SUMMARY_ADJECTIVES = (
    "Detail-oriented",
    "Results-driven",
    "Collaborative",
    "Resourceful",
    "Dependable",
    "Analytical",
    "Self-directed",
)
SUMMARY_CLAUSES = (
    "delivering dependable results for fast-moving teams",
    "turning ambiguous requests into clear, finished deliverables",
    "keeping complex projects organized and on schedule",
    "earning trust through steady, careful execution",
    "improving the way teams plan and finish their projects",
)
STUDENT_SUMMARY = (
    "{adj} {field} student with hands-on project experience and a steady record of finishing what they start."
)

# --------------------------------------------------------------------------------------------
# presentation extras (never part of the ground truth; none of them spells a skill)
# --------------------------------------------------------------------------------------------

KIND_THEMES = {
    SkillKind.TECHNOLOGY: "Technical Skills",
    SkillKind.TOOL: "Tools and Platforms",
    SkillKind.METHODOLOGY: "Methods and Practices",
    SkillKind.DOMAIN: "Domain Expertise",
    SkillKind.SOFT: "Professional Strengths",
}

PUB_ADJECTIVES = (
    "Adaptive",
    "Scalable",
    "Robust",
    "Efficient",
    "Distributed",
    "Incremental",
    "Probabilistic",
    "Hybrid",
    "Lightweight",
    "Resilient",
)
PUB_NOUNS = ("Frameworks", "Mechanisms", "Strategies", "Approaches", "Architectures", "Protocols", "Models")
PUB_TOPICS = (
    "Resource Allocation",
    "Error Recovery",
    "Workload Balancing",
    "Signal Filtering",
    "Schedule Planning",
    "Quality Assurance",
    "Capacity Estimation",
    "Event Detection",
)
PUB_VENUES = (
    "Journal of Applied Studies",
    "Proceedings of the Example Symposium",
    "Annual Review of Practice",
    "Transactions on Applied Methods",
    "Workshop on Practical Systems",
)
TEACHING = (
    "Teaching assistant, Foundations of Professional Practice",
    "Guest lecturer, Seminar in Applied Methods",
    "Workshop facilitator, Research Skills Series",
    "Tutor, Introductory Quantitative Reasoning",
    "Lab demonstrator, Practical Methods Studio",
)
FEDERAL_DUTIES = (
    "Prepared weekly status briefings for program managers and tracked every action item to closure.",
    "Coordinated with partner offices to resolve escalated issues within the agreed timelines.",
    "Reviewed incoming requests, ranked the queue and routed each item to the responsible staff member.",
    "Kept accurate records and documentation in line with agency policies and procedures.",
    "Trained new staff on office procedures and answered questions about internal processes.",
    "Compiled monthly summaries of activity for management review and recommended process improvements.",
    "Acted as point of contact for external partners and relayed their questions to the right team.",
    "Attended planning meetings and recorded the decisions for distribution to the whole group.",
    "Reconciled competing priorities across offices and escalated conflicts that needed a decision.",
    "Drafted correspondence, briefing notes and procedural guidance for supervisor approval.",
)
FEDERAL_KSA = (
    "Ability to manage competing priorities and complete assignments accurately within tight deadlines, "
    "raising risks early so that managers can act on them.",
    "Strong written and oral briefing ability, with experience preparing clear summaries for audiences "
    "that range from front-line staff to senior officials.",
    "Working knowledge of agency policies, procedures and reporting requirements, and the discipline "
    "to apply them consistently across teams and offices.",
    "Experience coordinating across organizational boundaries, building agreement among partners with "
    "different priorities and resolving disputes at the lowest practical level.",
    "Proven record of handling sensitive information with care and following the required storage, "
    "handling and disclosure procedures.",
    "Commitment to continuous improvement, including documenting lessons learned and updating procedures "
    "so the next team starts from a better baseline.",
    "Ability to learn new processes quickly, train colleagues and keep accurate records that stand up "
    "to review.",
    "Dependable attendance and punctuality, with a record of taking ownership of assignments from first "
    "request to final delivery.",
)
MILITARY_BRANCHES = ("U.S. Army", "U.S. Army Reserve", "Army National Guard")
MILITARY_RANKS = ("Specialist (E-4)", "Sergeant (E-5)", "Staff Sergeant (E-6)")
PROJECT_NAMES = (
    "Campus Event Planner",
    "Inventory Tracker",
    "Study Group Matcher",
    "Recipe Recommender",
    "Budget Planner",
    "Trail Finder",
    "Library Catalogue",
    "Volunteer Scheduler",
    "Habit Tracker",
    "Course Planner",
    "Carpool Coordinator",
    "Lab Notebook Organizer",
    "Fitness Log",
    "Neighbourhood Map",
    "Appointment Booker",
    "Reading List Manager",
)
SUPERVISOR_ROLES = ("Program Manager", "Branch Chief", "Team Supervisor", "Director of Operations")
