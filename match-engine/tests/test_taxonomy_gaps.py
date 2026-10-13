"""Task 7.3: measured gaps of the committed taxonomy, closed (DECISIONS D-140 to D-143).

Checked against data/taxonomy through `load_resolver`, the way the engine reads it: the 31 common
skills the eval vocabulary could not name, the parked items of earlier tasks (SaaS, B2B, CNO
registration, GN, AWS certification names, sentence-initial Go/French/English, "patients"), and the
review-queue tokens the build controller approved (data/taxonomy/approvals/2026-10-13-controller.txt).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from matcher.graph.loader import load_graph, load_resolver
from matcher.models import SkillKind

ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "data" / "taxonomy"
APPROVALS = TAXONOMY / "approvals" / "2026-10-13-controller.txt"


@pytest.fixture(scope="module")
def resolver(config):
    return load_resolver(TAXONOMY, config)


@pytest.fixture(scope="module")
def graph():
    return load_graph(TAXONOMY)


def _resolved(resolver, surface: str) -> tuple:
    result = resolver.resolve(surface)
    return result.canonical_id, result.method


def _ids(resolver, text: str, context: str = "prose") -> list[str]:
    return [m.canonical_id for m in resolver.extract(text, context=context)]


# ─── the 31 skills the eval vocabulary could not name ─────────────────────────────────────

VOCABULARY_GAPS = [
    ("Amplitude", "skill.amplitude"),
    ("Backlog Prioritization", "skill.backlog_prioritization"),
    ("Backlog Grooming", "skill.backlog_prioritization"),
    ("Budgeting and Forecasting", "skill.fpa"),
    ("FP&A", "skill.fpa"),
    ("Campaign Management", "skill.campaign_management"),
    ("Care Planning", "skill.care_planning"),
    ("Case Management", "skill.case_management"),
    ("Code Review", "skill.code_review"),
    ("Conversion Rate Optimization", "skill.cro"),
    ("Cost Accounting", "skill.cost_accounting"),
    ("Customer Interviews", "skill.customer_interviews"),
    ("Design Systems", "skill.design_systems"),
    ("Discharge Planning", "skill.discharge_planning"),
    ("Experiment Design", "skill.experiment_design"),
    ("General Ledger", "skill.general_ledger"),
    ("Go-to-Market Strategy", "skill.go_to_market"),
    ("HubSpot Inbound Certification", "cred.hubspot_cert"),
    ("Incident Response", "skill.incident_response"),
    ("Miro", "skill.miro"),
    ("Month-End Close", "skill.month_end_close"),
    ("Period Close", "skill.month_end_close"),
    ("OKRs", "skill.okrs"),
    ("Objectives and Key Results", "skill.okrs"),
    ("Patient Education", "skill.patient_education"),
    ("Product Analytics", "skill.product_analytics"),
    ("Product Roadmapping", "skill.product_roadmapping"),
    ("Roadmap Planning", "skill.product_roadmapping"),
    ("Reconciliation", "skill.reconciliation"),
    ("Tableau Desktop Specialist", "cred.tableau_desktop_specialist"),
    ("Telemetry Monitoring", "skill.telemetry_monitoring"),
    ("Time Series Forecasting", "skill.time_series_forecasting"),
    ("UX Design Certificate", "cred.google_ux_certificate"),
    ("User Stories", "skill.user_stories"),
    ("Variance Analysis", "skill.variance_analysis"),
    ("Vital Signs Assessment", "skill.vital_signs"),
]


@pytest.mark.parametrize("surface, expected", VOCABULARY_GAPS)
def test_a_common_skill_the_eval_vocabulary_named_now_resolves(resolver, surface: str, expected: str) -> None:
    node_id, method = _resolved(resolver, surface)
    assert node_id == expected and method in ("EXACT", "ALIAS"), (surface, node_id, method)


def test_every_skill_of_the_default_vocabulary_has_a_spelling_that_resolves(resolver) -> None:
    from eval.generate.vocabulary import default_vocabulary
    from eval.suites import build_id_map

    assert build_id_map(default_vocabulary(), resolver).unknown == frozenset()


@pytest.mark.parametrize(
    "node_id, kind",
    [("skill.amplitude", SkillKind.TOOL), ("skill.miro", SkillKind.TOOL), ("skill.okrs", SkillKind.METHODOLOGY),
     ("cred.tableau_desktop_specialist", SkillKind.CREDENTIAL), ("cred.google_ux_certificate", SkillKind.CREDENTIAL),
     ("skill.saas", SkillKind.DOMAIN), ("skill.b2b", SkillKind.DOMAIN)],
)
def test_new_nodes_have_their_kind(graph, node_id: str, kind: SkillKind) -> None:
    assert graph.nodes[node_id].kind is kind


@pytest.mark.parametrize(
    "text, expected",
    [
        # "Amplitude" is the product only in its casing
        ("Measured the signal amplitude with an oscilloscope.", ["skill.oscilloscope"]),
        ("Built funnels in Amplitude and Mixpanel.", ["skill.amplitude"]),
        # the Indigenous sense of reconciliation is no accounting skill
        ("We are committed to Truth and Reconciliation.", []),
        ("Our commitment to reconciliation with Indigenous Peoples guides us.", []),
        ("Prepared month-end reconciliation and variance analysis.", ["skill.reconciliation",
                                                                      "skill.variance_analysis"]),
    ],
)
def test_new_names_stay_out_of_their_ordinary_senses(resolver, text: str, expected: list) -> None:
    assert [i for i in _ids(resolver, text) if i != "skill.mixpanel"] == expected


# ─── parked items ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "surface, expected",
    [
        ("SaaS", "skill.saas"),
        ("software as a service", "skill.saas"),
        ("B2B", "skill.b2b"),
        ("business-to-business", "skill.b2b"),
        ("B2B sales", "skill.sales"),  # the longer curated alias keeps its node
        ("CNO registration", "cred.registered_nurse"),
        ("College of Nurses of Ontario registration", "cred.registered_nurse"),
        ("Graduate Nurse", "cred.nursing_student"),
        ("AWS Solutions Architect certification", "cred.aws_saa"),
        ("AWS Certified DevOps Engineer - Professional", "cred.aws_devops_pro"),
        ("AWS Certified Data Engineer - Associate", "cred.aws_data_engineer"),
        ("AWS Certified Machine Learning - Specialty", "cred.aws_ml_specialty"),
        ("AWS Certified Machine Learning Engineer - Associate", "cred.aws_ml_engineer"),
        ("AWS Certified Security - Specialty", "cred.aws_security_specialty"),
        ("AWS Certified Advanced Networking - Specialty", "cred.aws_networking_specialty"),
        ("AWS Certified AI Practitioner", "cred.aws_ai_practitioner"),
        ("AWS Certified Data Analytics - Specialty", "cred.aws_data_analytics_specialty"),
    ],
)
def test_a_parked_surface_now_resolves(resolver, surface: str, expected: str) -> None:
    node_id, method = _resolved(resolver, surface)
    assert node_id == expected and method in ("EXACT", "ALIAS"), (surface, node_id, method)


def test_graduate_nurse_is_the_in_progress_node_and_never_the_licence(resolver) -> None:
    # Bare "GN" is not an alias yet (D-140): "RN or Graduate Nurse (GN)" would give the posting parser
    # a second, GN-only gate; the parser already reads "RN or GN" by phrase.
    assert resolver.resolve("GN").method == "NONE"
    assert _ids(resolver, "RN or Graduate Nurse with current CNO registration.") == [
        "cred.registered_nurse", "cred.nursing_student", "cred.registered_nurse"]


def test_an_aws_job_title_is_no_certification(resolver) -> None:
    # D-62 reads a credential in a role title as held: only certification wording names the credential.
    assert resolver.resolve("AWS Solutions Architect").canonical_id != "cred.aws_saa"


def test_phd_stays_a_degree_level_and_never_becomes_a_node(resolver) -> None:
    # D-142: a degree level is read by the education parser (config/parse/sections.yaml); a node would
    # make it a skill requirement or a field of study.
    assert resolver.resolve("PhD").method == "NONE"


@pytest.mark.parametrize("word", ["patients", "patient", "Patients", "parents", "analyse", "analysed",
                                  "supervisors", "heating", "tenants", "seniors"])
def test_an_ordinary_word_never_fuzzes_to_a_skill(resolver, word: str) -> None:
    assert resolver.resolve(word).method == "NONE"


def test_a_typo_of_a_skill_still_fuzzes(resolver) -> None:
    assert _resolved(resolver, "Kubernetess") == ("skill.kubernetes", "FUZZY")


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Go, Python and Rust are our stack.", ["skill.go", "skill.python", "skill.rust"]),
        ("Go above and beyond for our customers.", []),
        ("English and French required.", ["esco.english", "esco.french"]),
        ("French is an asset.", ["esco.french"]),
        ("English proficiency is required.", ["esco.english"]),
        ("English Bay is a short walk away.", []),
    ],
)
def test_go_french_and_english_may_open_a_skill_list_or_a_language_sentence(resolver, text: str,
                                                                            expected: list) -> None:
    assert _ids(resolver, text) == expected


# ─── approved review-queue tokens ─────────────────────────────────────────────────────────


def _approved() -> list[str]:
    lines = APPROVALS.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def test_the_approval_file_says_who_approved_it_and_that_the_owner_audits_it() -> None:
    header = APPROVALS.read_text(encoding="utf-8").split("\n", 8)[:8]
    text = " ".join(header).lower()
    assert "build controller" in text and "standing instruction" in text and "audit" in text


@pytest.mark.parametrize("token", _approved() if APPROVALS.exists() else ["<approval file missing>"])
def test_every_approved_token_resolves(resolver, token: str) -> None:
    node_id, method = _resolved(resolver, token)
    assert node_id is not None and method in ("EXACT", "ALIAS"), (token, node_id, method)


@pytest.mark.parametrize("token", ["SpaceX", "GTM", "STEM", "production environment", "LOB", "analytics", "factory",
                                   "operations", "automation", "manufacturing", "CIO/CTO", "DoD", "HRBP",
                                   "full-cycle quota-carrying", "technical project delivery", "SDK", "DSS",
                                   "SOC", "IPC", "UT", "RT", "CI", "FP", "monitoring", "networking",
                                   "scripting"])
def test_a_rejected_token_stays_unresolved(resolver, token: str) -> None:
    assert token not in _approved()
    assert resolver.resolve(token).canonical_id is None, token
