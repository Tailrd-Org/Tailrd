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
    ("bank reconciliation", "skill.reconciliation"),
    ("balance sheet reconciliation", "skill.reconciliation"),
    ("Tableau Desktop Specialist", "cred.tableau_desktop_specialist"),
    ("cardiac telemetry", "skill.cardiac_telemetry"),
    ("telemetry nursing", "skill.cardiac_telemetry"),
    ("telemetry unit", "skill.cardiac_telemetry"),
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


def test_every_skill_of_the_default_vocabulary_but_two_has_a_spelling_that_resolves(resolver) -> None:
    # Fix round 1 (D-140): bare "Reconciliation" and "Telemetry Monitoring" stay unresolved; each also
    # names something else (Indigenous reconciliation, software telemetry).
    from eval.generate.vocabulary import default_vocabulary
    from eval.suites import build_id_map

    assert build_id_map(default_vocabulary(), resolver).unknown == frozenset(
        {"skill.reconciliation", "skill.telemetry_monitoring"})


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
        ("Prepared bank reconciliations and variance analysis.", ["skill.reconciliation",
                                                                   "skill.variance_analysis"]),
        # fix round 1: bare reconciliation, telemetry monitoring, RL and Miro keep their other senses
        ("Demonstrated commitment to equity, diversity, inclusion and reconciliation.", []),
        ("Understanding of reconciliation and Indigenous self-determination.", []),
        ("Knowledge of Indigenous history and reconciliation.", []),
        ("Built telemetry monitoring dashboards for our services.", []),
        ("Analyzed RL circuits in the electronics lab.", ["esco.electronics"]),  # no reinforcement learning
        ("Measured RL and RC circuits.", []),
        ("Worked with Miro Kovac on the launch.", []),
        ("Miro Petrov\nmiro.petrov@example.com", []),
        ("Ran design workshops in Figma and Miro.", ["skill.figma", "skill.miro"]),
        ("Experience with Miro", ["skill.miro"]),
        ("Facilitated workshops in Miro.", ["skill.miro"]),
        ("Worked with Miro Kovac", []),
        ("Mapped journeys on a Miro board.", ["skill.miro"]),
        ("Member of ASNT.", ["skill.ndt"]),
        ("Holds ASNT Level II in UT.", ["cred.ndt_certification"]),
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
        "cred.registered_nurse", "cred.nursing_student"]


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


@pytest.mark.parametrize("token", ["SpaceX", "GTM", "RL", "SparkTM", "STEM", "production environment", "LOB", "analytics", "factory",
                                   "operations", "automation", "manufacturing", "CIO/CTO", "DoD", "HRBP",
                                   "full-cycle quota-carrying", "technical project delivery", "SDK", "DSS",
                                   "SOC", "IPC", "UT", "RT", "CI", "FP", "monitoring", "networking",
                                   "scripting", "HPO"])
def test_a_rejected_token_stays_unresolved(resolver, token: str) -> None:
    assert token not in _approved()
    assert resolver.resolve(token).canonical_id is None, token


# ─── owner audit (2026-10-13, DECISIONS D-155) ───────────────────────────────────────────────


def test_hpo_is_withdrawn_but_its_long_forms_still_resolve(resolver) -> None:
    # HPO is also the Human Phenotype Ontology, and rare: only the spelled-out forms name the skill.
    assert resolver.resolve("HPO").canonical_id is None
    for surface in ("hyperparameter optimization", "hyperparameter optimisation", "hyper-parameter tuning"):
        assert resolver.resolve(surface).canonical_id == "skill.hyperparameter_tuning", surface


def test_asnt_stays_the_testing_domain(resolver) -> None:
    assert resolver.resolve("ASNT").canonical_id == "skill.ndt"


# ─── fix round 1 ──────────────────────────────────────────────────────────────────────────


def test_rl_circuits_name_nothing(resolver) -> None:
    assert resolver.resolve("RL circuits").canonical_id is None
    assert resolver.resolve("RL").canonical_id is None


@pytest.mark.parametrize("surface", ["CNO registration", "College of Nurses of Ontario registration"])
def test_cno_registration_names_no_credential(resolver, surface: str) -> None:
    # The College of Nurses of Ontario registers RNs, RPNs and NPs: the registration names no licence.
    assert resolver.resolve(surface).canonical_id is None


@pytest.mark.parametrize("line", ["Current CNO registration in good standing", "RPN with current CNO registration",
                                  "Current CNO registration as an RPN"])
def test_an_rpn_posting_with_cno_registration_has_no_rn_gate(line: str) -> None:
    from matcher.models import LicenseGate
    from tests.posting_helpers import parse

    posting = parse(f"Requirements\n- {line}\n- Wound care", title="Registered Practical Nurse")
    gated = {i for g in posting.gates if isinstance(g, LicenseGate) for i in g.credential_ids}
    assert "cred.registered_nurse" not in gated


def _resume(text: str):
    import datetime as dt

    from matcher.config import load_config
    from matcher.parse.config import load_parse_config
    from matcher.parse.resume import load_resume_resources, parse_resume_text

    config, parse_config = load_config(), load_parse_config()
    return parse_resume_text(text, as_of=dt.date(2026, 10, 9), strict=False, resolver=load_resolver(TAXONOMY, config),
                             config=config, parse_config=parse_config,
                             resources=load_resume_resources(TAXONOMY, parse_config=parse_config)).resume


def test_an_rpn_resume_with_cno_registration_does_not_pass_an_rn_gate(graph) -> None:
    from matcher.config import load_config
    from matcher.models import GateStatus
    from matcher.score.gates import evaluate_gates
    from tests.posting_helpers import parse

    config = load_config()
    posting = parse("Requirements\n- Registered Nurse (RN) licence required\n- Wound care", title="Registered Nurse")
    rpn = _resume("Jane Doe\njane@example.com\n\nLicences\nCNO registration (RPN), 2021\n\nExperience\n"
                  "Registered Practical Nurse, Sunnybrook, 2021 - Present\n- Wound care and medication administration")
    rn = _resume("Ann Roe\nann@example.com\n\nLicences\nRegistered Nurse (RN), 2021\n\nExperience\n"
                 "Registered Nurse, Sunnybrook, 2021 - Present\n- Wound care and medication administration")
    assert evaluate_gates(rpn, posting, graph, config)[0] is not GateStatus.PASS
    assert evaluate_gates(rn, posting, graph, config)[0] is GateStatus.PASS  # the gate itself works


def test_a_trademark_sign_never_glues_letters_to_a_name() -> None:
    from tests.posting_helpers import parse

    posting = parse("Requirements\n- Experience with Tableau\u2122 and Apache Spark\u2122\n- Python\u00ae scripting")
    assert {"skill.tableau", "skill.spark", "skill.python"} <= {r.skill_id for r in posting.requirements}
    assert not posting.unresolved_requirement_tokens


def test_case_management_and_time_series_analysis_have_their_own_place(graph) -> None:
    from matcher.graph.traversal import ancestors

    assert "skill.patient_care" not in set(ancestors(graph, "skill.case_management", 10))
    assert graph.nodes["skill.time_series_analysis"].label == "Time series analysis"
    assert "skill.time_series_analysis" in graph.nodes["skill.time_series_forecasting"].parents


def test_miro_on_a_posting_tools_line_is_required() -> None:
    from tests.posting_helpers import parse

    posting = parse("Requirements\n- Tools: Miro\n- Experience with Miro and Figma")
    assert "skill.miro" in {r.skill_id for r in posting.requirements}
