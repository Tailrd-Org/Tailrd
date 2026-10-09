"""The skills the generator writes about (EVAL.md section 2.1).

`default_vocabulary()` is a self-contained, hand-written vocabulary: ~160 skills over eight
families, ids `skill.<slug>`. It exists so the harness can run before the real taxonomy does;
`vocabulary_from_graph()` builds the same structure from a `SkillGraph` so a later task can swap
the real one in. Nothing here is a model of any real person or employer.

Two rules keep the generated ground truth exact. No label or alias carries a digit (a digit in a
bullet is how "quantified" is recognised), and no two skills share a spelling, so the text a
profile renders resolves to exactly the skills the profile claims (`find_mentions`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import NamedTuple

from matcher.graph.model import SkillGraph
from matcher.models import Frozen, SkillKind

# --------------------------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------------------------


class VocabSkill(Frozen):
    id: str
    label: str
    kind: SkillKind
    families: tuple[str, ...]
    aliases: tuple[str, ...] = ()


class Vocabulary(Frozen):
    """Skills (what bullets and skills lines mention) and, apart from them, fields of study
    (what an education entry names). Fields are graph nodes too (DECISIONS D-41) but are never
    written about in a bullet."""

    skills: tuple[VocabSkill, ...]
    families: tuple[str, ...]
    fields: tuple[VocabSkill, ...] = ()


class Mention(NamedTuple):
    skill_id: str
    text: str  # the surface form, exactly as written
    start: int
    end: int


# --------------------------------------------------------------------------------------------
# the default vocabulary
# --------------------------------------------------------------------------------------------

SW, DA, DO, PR, DE, MK, FI, HC = (
    "software",
    "data",
    "devops",
    "product",
    "design",
    "marketing",
    "finance",
    "healthcare",
)
FAMILIES = (SW, DA, DO, PR, DE, MK, FI, HC)
ALL = FAMILIES

T, L, M, D, S, C = (
    SkillKind.TECHNOLOGY,
    SkillKind.TOOL,
    SkillKind.METHODOLOGY,
    SkillKind.DOMAIN,
    SkillKind.SOFT,
    SkillKind.CREDENTIAL,
)

# (slug, label, kind, families, aliases)
_SKILLS: tuple[tuple[str, str, SkillKind, tuple[str, ...], tuple[str, ...]], ...] = (
    # --- software ---------------------------------------------------------------------------
    ("python", "Python", T, (SW, DA, DO), ()),
    ("java", "Java", T, (SW,), ()),
    ("javascript", "JavaScript", T, (SW, DE), ("ECMAScript",)),
    ("typescript", "TypeScript", T, (SW,), ()),
    ("golang", "Golang", T, (SW, DO), ("Go language",)),
    ("cpp", "C++", T, (SW,), ()),
    ("csharp", "C#", T, (SW,), ()),
    ("react", "React", T, (SW, DE), ("ReactJS", "React.js")),
    ("vuejs", "Vue.js", T, (SW,), ("VueJS",)),
    ("nodejs", "Node.js", T, (SW,), ("NodeJS",)),
    ("django", "Django", T, (SW, DA), ()),
    ("springboot", "Spring Boot", T, (SW,), ()),
    ("dotnet", ".NET", T, (SW,), ("dotnet",)),
    ("postgresql", "PostgreSQL", T, (SW, DA), ("Postgres",)),
    ("mysql", "MySQL", T, (SW, DA), ()),
    ("mongodb", "MongoDB", T, (SW, DA), ("Mongo",)),
    ("redis", "Redis", T, (SW, DO), ()),
    ("kafka", "Kafka", T, (SW, DA, DO), ("Apache Kafka",)),
    ("sql", "SQL", T, (SW, DA, FI, MK), ("Structured Query Language",)),
    ("html", "HTML", T, (SW, DE, MK), ()),
    ("css", "CSS", T, (SW, DE), ("Cascading Style Sheets",)),
    ("microservices", "Microservices", M, (SW, DO), ()),
    ("tdd", "Test-Driven Development", M, (SW,), ("TDD",)),
    ("system_design", "System Design", M, (SW,), ()),
    ("rest_api_design", "REST API Design", M, (SW,), ("RESTful APIs",)),
    ("code_review", "Code Review", M, (SW,), ()),
    ("git", "Git", L, (SW, DA, DO, DE), ()),
    ("github", "GitHub", L, (SW, DO), ()),
    ("agile", "Agile", M, (SW, PR, DO), ()),
    ("scrum", "Scrum", M, (SW, PR), ()),
    ("jira", "Jira", L, (SW, PR, DO), ()),
    # --- data -------------------------------------------------------------------------------
    ("pandas", "Pandas", T, (DA,), ()),
    ("numpy", "NumPy", T, (DA,), ()),
    ("scikit_learn", "scikit-learn", T, (DA,), ("sklearn",)),
    ("tensorflow", "TensorFlow", T, (DA,), ()),
    ("pytorch", "PyTorch", T, (DA,), ()),
    ("spark", "Spark", T, (DA,), ("Apache Spark", "PySpark")),
    ("airflow", "Airflow", T, (DA, DO), ("Apache Airflow",)),
    ("tableau", "Tableau", L, (DA, FI, MK), ()),
    ("power_bi", "Power BI", L, (DA, FI), ("PowerBI",)),
    ("excel", "Excel", L, (DA, FI, MK, PR, HC), ("Microsoft Excel",)),
    ("machine_learning", "Machine Learning", D, (DA, SW), ()),
    ("statistical_modeling", "Statistical Modeling", M, (DA, FI), ()),
    ("data_visualization", "Data Visualization", M, (DA, DE), ()),
    ("etl", "ETL", M, (DA, SW), ("Extract Transform Load",)),
    ("ab_testing", "A/B Testing", M, (DA, PR, MK), ("Split Testing",)),
    ("experiment_design", "Experiment Design", M, (DA, PR), ()),
    ("forecasting", "Time Series Forecasting", D, (DA, FI), ()),
    # --- devops -----------------------------------------------------------------------------
    ("docker", "Docker", L, (DO, SW, DA), ()),
    ("kubernetes", "Kubernetes", T, (DO,), ()),
    ("terraform", "Terraform", L, (DO,), ()),
    ("jenkins", "Jenkins", L, (DO, SW), ()),
    ("github_actions", "GitHub Actions", L, (DO, SW), ()),
    ("aws", "AWS", T, (DO, SW, DA), ("Amazon Web Services",)),
    ("azure", "Azure", T, (DO, SW), ("Microsoft Azure",)),
    ("google_cloud", "Google Cloud", T, (DO, DA), ("GCP", "Google Cloud Platform")),
    ("linux", "Linux", T, (DO, SW), ()),
    ("prometheus", "Prometheus", L, (DO,), ()),
    ("grafana", "Grafana", L, (DO,), ()),
    ("ci_cd", "CI/CD", M, (DO, SW), ()),
    ("iac", "Infrastructure as Code", M, (DO,), ("IaC",)),
    ("sre", "Site Reliability Engineering", M, (DO,), ("SRE",)),
    ("incident_response", "Incident Response", M, (DO, SW), ()),
    ("cloud_security", "Cloud Security", D, (DO,), ()),
    ("observability", "Observability", D, (DO, SW), ()),
    # --- product ----------------------------------------------------------------------------
    ("product_roadmapping", "Product Roadmapping", M, (PR,), ("Roadmap Planning",)),
    ("user_stories", "User Stories", M, (PR, SW), ()),
    ("backlog_prioritization", "Backlog Prioritization", M, (PR,), ("Backlog Grooming",)),
    ("market_research", "Market Research", D, (PR, MK), ()),
    ("competitive_analysis", "Competitive Analysis", M, (PR, MK), ()),
    ("go_to_market", "Go-to-Market Strategy", M, (PR, MK), ("GTM",)),
    ("product_analytics", "Product Analytics", D, (PR, DA), ()),
    ("amplitude", "Amplitude", L, (PR, MK), ()),
    ("pricing_strategy", "Pricing Strategy", D, (PR, FI, MK), ()),
    ("customer_interviews", "Customer Interviews", M, (PR, DE), ()),
    ("okrs", "OKRs", M, (PR,), ("Objectives and Key Results",)),
    # --- design -----------------------------------------------------------------------------
    ("figma", "Figma", L, (DE, PR), ()),
    ("adobe_xd", "Adobe XD", L, (DE,), ()),
    ("photoshop", "Adobe Photoshop", L, (DE, MK), ("Photoshop",)),
    ("illustrator", "Adobe Illustrator", L, (DE,), ("Illustrator",)),
    ("miro", "Miro", L, (DE, PR), ()),
    ("user_research", "User Research", M, (DE, PR), ()),
    ("wireframing", "Wireframing", M, (DE, PR), ()),
    ("prototyping", "Prototyping", M, (DE, PR), ()),
    ("usability_testing", "Usability Testing", M, (DE, PR), ()),
    ("interaction_design", "Interaction Design", D, (DE,), ()),
    ("visual_design", "Visual Design", D, (DE, MK), ()),
    ("design_systems", "Design Systems", D, (DE,), ()),
    ("accessibility", "Accessibility", D, (DE, SW), ("WCAG",)),
    ("information_architecture", "Information Architecture", D, (DE,), ()),
    # --- marketing --------------------------------------------------------------------------
    ("seo", "SEO", M, (MK,), ("Search Engine Optimization",)),
    ("google_analytics", "Google Analytics", L, (MK, PR, DA), ()),
    ("hubspot", "HubSpot", L, (MK,), ()),
    ("salesforce", "Salesforce", L, (MK, PR), ()),
    ("content_marketing", "Content Marketing", D, (MK,), ()),
    ("email_marketing", "Email Marketing", D, (MK,), ()),
    ("social_media_marketing", "Social Media Marketing", D, (MK,), ()),
    ("brand_strategy", "Brand Strategy", D, (MK,), ()),
    ("marketing_automation", "Marketing Automation", M, (MK,), ()),
    ("copywriting", "Copywriting", M, (MK,), ()),
    ("campaign_management", "Campaign Management", M, (MK,), ()),
    ("cro", "Conversion Rate Optimization", M, (MK, PR), ("CRO",)),
    ("lead_generation", "Lead Generation", D, (MK,), ()),
    # --- finance ----------------------------------------------------------------------------
    ("financial_modeling", "Financial Modeling", M, (FI,), ()),
    ("financial_reporting", "Financial Reporting", D, (FI,), ()),
    ("budgeting_forecasting", "Budgeting and Forecasting", M, (FI,), ("FP&A",)),
    ("accounts_payable", "Accounts Payable", D, (FI,), ()),
    ("accounts_receivable", "Accounts Receivable", D, (FI,), ()),
    ("general_ledger", "General Ledger", D, (FI,), ()),
    ("month_end_close", "Month-End Close", M, (FI,), ("Period Close",)),
    ("reconciliation", "Reconciliation", M, (FI,), ()),
    ("variance_analysis", "Variance Analysis", M, (FI,), ()),
    ("auditing", "Auditing", D, (FI,), ("Internal Audit",)),
    ("gaap", "GAAP", D, (FI,), ("US GAAP",)),
    ("risk_management", "Risk Management", D, (FI, PR), ()),
    ("quickbooks", "QuickBooks", L, (FI,), ()),
    ("sap", "SAP", L, (FI,), ("SAP ERP",)),
    ("cost_accounting", "Cost Accounting", D, (FI,), ()),
    # --- healthcare -------------------------------------------------------------------------
    ("patient_care", "Patient Care", D, (HC,), ()),
    ("ehr", "Electronic Health Records", D, (HC,), ("EHR", "EMR")),
    ("epic_systems", "Epic Systems", L, (HC,), ()),
    ("cerner", "Cerner", L, (HC,), ()),
    ("medication_administration", "Medication Administration", D, (HC,), ()),
    ("wound_care", "Wound Care", D, (HC,), ()),
    ("triage", "Triage", D, (HC,), ()),
    ("iv_therapy", "IV Therapy", D, (HC,), ()),
    ("patient_education", "Patient Education", D, (HC,), ()),
    ("care_planning", "Care Planning", M, (HC,), ()),
    ("infection_control", "Infection Control", D, (HC,), ()),
    ("telemetry_monitoring", "Telemetry Monitoring", D, (HC,), ()),
    ("hipaa_compliance", "HIPAA Compliance", D, (HC,), ("HIPAA",)),
    ("clinical_documentation", "Clinical Documentation", D, (HC,), ()),
    ("vital_signs_assessment", "Vital Signs Assessment", D, (HC,), ()),
    ("phlebotomy", "Phlebotomy", D, (HC,), ()),
    ("case_management", "Case Management", M, (HC,), ()),
    ("pharmacology", "Pharmacology", D, (HC,), ()),
    ("medical_terminology", "Medical Terminology", D, (HC,), ()),
    ("discharge_planning", "Discharge Planning", M, (HC,), ()),
    # --- soft skills (shared) ---------------------------------------------------------------
    ("communication", "Communication", S, ALL, ()),
    ("leadership", "Leadership", S, ALL, ()),
    ("teamwork", "Teamwork", S, ALL, ()),
    ("problem_solving", "Problem Solving", S, ALL, ()),
    ("time_management", "Time Management", S, ALL, ()),
    ("mentoring", "Mentoring", S, (SW, DA, DO, PR, DE, HC), ()),
    ("negotiation", "Negotiation", S, (PR, MK, FI), ()),
    ("presentation_skills", "Presentation Skills", S, (DA, PR, DE, MK, FI), ()),
    ("customer_service", "Customer Service", S, (PR, MK, FI, HC), ()),
    # --- credentials ------------------------------------------------------------------------
    (
        "aws_solutions_architect",
        "AWS Certified Solutions Architect",
        C,
        (DO, SW, DA),
        ("AWS Solutions Architect",),
    ),
    ("cka", "Certified Kubernetes Administrator", C, (DO,), ("CKA",)),
    ("pmp", "Project Management Professional", C, (PR,), ("PMP",)),
    ("csm", "Certified Scrum Master", C, (PR, SW), ("CSM",)),
    ("cpa", "Certified Public Accountant", C, (FI,), ("CPA",)),
    ("cfa", "Chartered Financial Analyst", C, (FI,), ("CFA",)),
    ("rn_license", "RN License", C, (HC,), ("Registered Nurse License",)),
    ("bls", "Basic Life Support", C, (HC,), ("BLS Certification",)),
    ("acls", "Advanced Cardiovascular Life Support", C, (HC,), ("ACLS",)),
    ("google_analytics_cert", "Google Analytics Certification", C, (MK, PR, DA), ()),
    ("hubspot_cert", "HubSpot Inbound Certification", C, (MK,), ()),
    ("ux_certificate", "UX Design Certificate", C, (DE,), ()),
    ("tableau_specialist", "Tableau Desktop Specialist", C, (DA,), ()),
)

# Fields of study (an `Education` names these; bullets never do).
_FIELDS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("computer_science", "Computer Science", (SW, DA, DO, PR)),
    ("software_engineering", "Software Engineering", (SW, DO)),
    ("computer_engineering", "Computer Engineering", (SW, DO)),
    ("information_technology", "Information Technology", (DO, SW)),
    ("statistics", "Statistics", (DA, FI)),
    ("mathematics", "Mathematics", (DA, FI, SW)),
    ("data_science", "Data Science", (DA,)),
    ("business_administration", "Business Administration", (PR, MK, FI)),
    ("economics", "Economics", (FI, PR, DA)),
    ("marketing_studies", "Marketing", (MK, PR)),
    ("communications", "Communications", (MK, DE)),
    ("graphic_design", "Graphic Design", (DE, MK)),
    ("human_computer_interaction", "Human-Computer Interaction", (DE, PR)),
    ("psychology", "Psychology", (DE, PR, HC)),
    ("finance_studies", "Finance", (FI,)),
    ("accounting_studies", "Accounting", (FI,)),
    ("nursing", "Nursing", (HC,)),
    ("health_sciences", "Health Sciences", (HC,)),
)


@lru_cache(maxsize=1)
def default_vocabulary() -> Vocabulary:
    skills = tuple(
        VocabSkill(id=f"skill.{slug}", label=label, kind=kind, families=families, aliases=aliases)
        for slug, label, kind, families, aliases in _SKILLS
    )
    fields = tuple(
        VocabSkill(id=f"skill.{slug}", label=label, kind=SkillKind.DOMAIN, families=families)
        for slug, label, families in _FIELDS
    )
    return Vocabulary(skills=skills, families=FAMILIES, fields=fields)


# --------------------------------------------------------------------------------------------
# from a real graph
# --------------------------------------------------------------------------------------------


def _root_families(graph: SkillGraph, skill_id: str) -> tuple[str, ...]:
    """The roots of the parent chain, as family names ("skill.programming" -> "programming")."""
    roots: set[str] = set()
    seen: set[str] = set()
    stack = [skill_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        node = graph.nodes[current]
        if node.parents:
            stack.extend(node.parents)
        else:
            roots.add(current.removeprefix("skill."))
    return tuple(sorted(roots))


def vocabulary_from_graph(graph: SkillGraph, ids: Sequence[str]) -> Vocabulary:
    """The same structure from the real taxonomy. A node's family is the root of its parent chain.
    Fields of study are not part of the graph's node kinds, so a graph vocabulary has none."""
    skills: list[VocabSkill] = []
    seen: set[str] = set()
    for skill_id in ids:
        if skill_id in seen:
            continue
        node = graph.node(skill_id)
        if node is None:
            raise ValueError(f"unknown skill id {skill_id!r}: not a node of the graph")
        seen.add(skill_id)
        skills.append(
            VocabSkill(
                id=node.id,
                label=node.label,
                kind=node.kind,
                families=_root_families(graph, skill_id),
                aliases=tuple(node.aliases),
            )
        )
    families = tuple(sorted({f for s in skills for f in s.families}))
    return Vocabulary(skills=tuple(skills), families=families)


# --------------------------------------------------------------------------------------------
# lookups shared by the generator, expected_resume and the tests
# --------------------------------------------------------------------------------------------


HARD_KINDS = frozenset({SkillKind.TECHNOLOGY, SkillKind.TOOL, SkillKind.METHODOLOGY, SkillKind.DOMAIN})


@dataclass(frozen=True)
class VocabIndex:
    """Lookups derived from one vocabulary. `hard` are the skills a bullet can be about (technology,
    tool, methodology, domain); soft skills and credentials only ever appear on a skills or
    certifications line."""

    by_id: dict[str, VocabSkill]
    form_to_id: dict[str, str]  # casefolded label / alias -> skill id
    pattern: re.Pattern[str]
    field_by_id: dict[str, VocabSkill]
    hard: dict[str, tuple[VocabSkill, ...]]  # family -> skills, in vocabulary order
    soft: dict[str, tuple[VocabSkill, ...]]
    credentials: dict[str, tuple[VocabSkill, ...]]
    fields: dict[str, tuple[VocabSkill, ...]]


@lru_cache(maxsize=16)
def vocabulary_index(vocab: Vocabulary) -> VocabIndex:
    form_to_id: dict[str, str] = {}
    for skill in vocab.skills:
        for form in (skill.label, *skill.aliases):
            key = form.casefold()
            if form_to_id.setdefault(key, skill.id) != skill.id:
                raise ValueError(f"{form!r} is spelled by two skills of the vocabulary")
    # Longest spelling first, so "Google Cloud" is never read as "Google" + "Cloud".
    spellings = sorted(form_to_id, key=lambda f: (-len(f), f))
    body = "|".join(re.escape(f) for f in spellings) or r"(?!)"
    pattern = re.compile(rf"(?<!\w)(?:{body})(?!\w)", re.IGNORECASE)

    def pools(source: Sequence[VocabSkill], kinds: frozenset[SkillKind]) -> dict[str, tuple[VocabSkill, ...]]:
        return {f: tuple(s for s in source if f in s.families and s.kind in kinds) for f in vocab.families}

    return VocabIndex(
        by_id={s.id: s for s in vocab.skills},
        form_to_id=form_to_id,
        pattern=pattern,
        field_by_id={f.id: f for f in vocab.fields},
        hard=pools(vocab.skills, HARD_KINDS),
        soft=pools(vocab.skills, frozenset({SkillKind.SOFT})),
        credentials=pools(vocab.skills, frozenset({SkillKind.CREDENTIAL})),
        fields=pools(vocab.fields, frozenset({SkillKind.DOMAIN})),
    )


def find_mentions(vocab: Vocabulary, text: str) -> tuple[Mention, ...]:
    """Every skill spelled in `text`, left to right, longest spelling first, whole words only."""
    index = vocabulary_index(vocab)
    return tuple(
        Mention(index.form_to_id[m.group(0).casefold()], m.group(0), m.start(), m.end())
        for m in index.pattern.finditer(text)
    )


def resolve_form(vocab: Vocabulary, form: str) -> str | None:
    """The skill id of one label or alias (case-insensitive), or None."""
    return vocabulary_index(vocab).form_to_id.get(form.strip().casefold())
