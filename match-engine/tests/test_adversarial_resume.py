"""Adversarial résumés (SPEC §10, §14.1, §14.5; DECISIONS D-28, D-62), end to end through the parser.

* White-text / tiny-text keyword stuffing is stripped at ingest and flagged; the hidden words give
  no skill and the flag reaches the score's informational flags.
* A résumé that IS the job posting, pasted end to end through `parse_resume_text` and `score`,
  stays below 0.85 raw (§14.5): the evidence tiers defend it, since a posting's text is a list of
  wants, not a record of work.
* One skill listed 50 times is the same résumé, and the same score, as listing it once.
* Credential status written around a licence ("CPA, in progress", "RN (pending NCLEX)", "Professional
  Engineer (P.Eng.) candidate", ...) never yields the licence (D-28: the taxonomy resolves the
  concept, the résumé parser reads the status; D-62: the licence gate would otherwise pass an
  unlicensed candidate, SPEC §4).
All texts are invented.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from matcher.calibrate import FixedCalibrator
from matcher.errors import Flag
from matcher.graph.loader import load_graph, load_resolver
from matcher.models import (
    DegreeLevel,
    EducationRequirement,
    Evidence,
    GateStatus,
    JobPosting,
    JobRequirement,
    LicenseGate,
    Necessity,
    Outcome,
    Polarity,
    ScoreOptions,
    SeniorityLevel,
    Track,
    canonical_json,
)
from matcher.parse.config import load_parse_config
from matcher.parse.resume import (
    credential_status,
    load_resume_resources,
    parse_resume_bytes,
    parse_resume_text,
)
from matcher.score.engine import score
from matcher.score.gates import evaluate_gates

AS_OF = date(2026, 10, 9)
ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "data" / "taxonomy"
EDGE = ROOT / "tests" / "fixtures" / "edge"
HIDDEN = {"skill.kubernetes", "skill.terraform", "skill.go", "skill.scala", "skill.hadoop", "skill.rust",
          "skill.haskell", "skill.elixir", "skill.erlang"}


@pytest.fixture(scope="module")
def deps(config):
    parse_config = load_parse_config()
    return {
        "resolver": load_resolver(TAXONOMY, config),
        "config": config,
        "parse_config": parse_config,
        "resources": load_resume_resources(TAXONOMY, parse_config=parse_config),
    }


@pytest.fixture(scope="module")
def graph():
    return load_graph(TAXONOMY)


def parse(text: str, deps):
    return parse_resume_text(text, as_of=AS_OF, strict=True, **deps)


def every_ref(resume):
    out = list(resume.skills_blob) + list(resume.certifications)
    for container in (*resume.roles, *resume.projects):
        out.extend(container.skills)
    return out


def held(resume) -> set[str]:
    """D-62: the credentials the licence gate counts (affirmed CERTIFICATION or ROLE_TITLE)."""
    return {r.canonical_id for r in every_ref(resume) if r.polarity is Polarity.AFFIRMED
            and r.evidence in (Evidence.CERTIFICATION, Evidence.ROLE_TITLE)}


def run(resume, posting, graph, config):
    return score(resume, posting, as_of=AS_OF, config=config, graph=graph, calibration=FixedCalibrator(),
                 options=ScoreOptions(strict=True))


# ─── stuffing ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["pdf_white_text.pdf", "pdf_tiny_text.pdf", "docx_white_text.docx",
                                  "docx_hidden_text.docx", "docx_tiny_text.docx"])
def test_hidden_text_is_stripped_and_flagged(name: str, deps) -> None:
    parsed = parse_resume_bytes((EDGE / name).read_bytes(), name, as_of=AS_OF, **deps)
    assert parsed.reasons == ()
    assert Flag.STUFFING_SUSPECTED in parsed.flags and Flag.STUFFING_SUSPECTED in parsed.resume.flags
    found = {r.canonical_id for r in every_ref(parsed.resume)}
    assert not found & HIDDEN
    assert "skill.python" in found  # the visible résumé is still read


def test_the_stuffing_flag_reaches_the_score(deps, graph, config) -> None:
    parsed = parse_resume_bytes((EDGE / "docx_white_text.docx").read_bytes(), "cv.docx", as_of=AS_OF, **deps)
    posting = JobPosting(requirements=(
        JobRequirement(skill_id="skill.kubernetes", necessity=Necessity.REQUIRED),
        JobRequirement(skill_id="skill.python", necessity=Necessity.REQUIRED)))
    result = run(parsed.resume, posting, graph, config)
    assert Flag.STUFFING_SUSPECTED in result.flags
    assert result.dimensions["required_skills"].value < 0.6  # Kubernetes, the hidden word, is a miss


# ─── the posting pasted as a résumé ─────────────────────────────────────────────────────────

POSTING = """Junior Data Engineer
Northwind Analytics, Toronto, ON (Hybrid)

About the role
Northwind Analytics helps grocery retailers forecast demand. You will join a team of six engineers
building the data pipelines behind our forecasting products.

What you'll do
- Build and maintain ETL pipelines in Python and SQL
- Develop dashboards in Tableau for the merchandising team
- Deploy services with Docker and Kubernetes on AWS
- Write unit tests and take part in code reviews

Requirements
- 1+ years of experience with Python and SQL
- Experience with Apache Airflow or a similar orchestration tool
- Familiarity with Docker and Git
- Bachelor's degree in Computer Science or a related field

Nice to have
- Experience with Apache Spark and Kafka
- AWS Certified Cloud Practitioner

Benefits
- Health and dental coverage from day one
- Flexible hours and a hybrid schedule
"""


def _posting(text: str, resolver) -> JobPosting:
    """A posting the way a posting parser reads this text: the Requirements lines REQUIRED, the
    Nice-to-have lines PREFERRED, every other mention IMPLIED, each skill once at its strongest."""
    rank = {Necessity.REQUIRED: 0, Necessity.PREFERRED: 1, Necessity.IMPLIED: 2}
    necessity, found = Necessity.IMPLIED, {}
    for line in text.splitlines():
        heading = line.strip().casefold()
        if heading == "requirements":
            necessity = Necessity.REQUIRED
        elif heading == "nice to have":
            necessity = Necessity.PREFERRED
        elif heading in ("benefits", "about the role", "what you'll do"):
            necessity = Necessity.IMPLIED
        for mention in resolver.extract(line):
            kept = found.get(mention.canonical_id)
            if kept is None or rank[necessity] < rank[kept[0]]:
                found[mention.canonical_id] = (necessity, mention.surface)
    requirements = tuple(JobRequirement(skill_id=skill, necessity=need, surface_form=surface)
                         for skill, (need, surface) in sorted(found.items()))
    return JobPosting(
        title_raw="Junior Data Engineer", seniority=SeniorityLevel(track=Track.IC, level=1),
        requirements=requirements, min_years_total=1.0,
        education_requirement=EducationRequirement(min_level=DegreeLevel.BACHELOR, field_ids=("field.computer_science",)),
        requirement_token_count=len(requirements),
    )


# The same text under résumé headings (Summary, Experience, Skills, Qualifications).
TEMPLATED = (POSTING.replace("About the role", "Summary").replace("What you'll do", "Experience")
             .replace("Requirements", "Skills").replace("Nice to have", "Qualifications"))


@pytest.mark.parametrize("text", [POSTING, TEMPLATED], ids=["as-is", "under-resume-headings"])
def test_a_pasted_posting_scores_below_strong(text: str, deps, graph, config) -> None:
    posting = _posting(POSTING, deps["resolver"])
    assert sum(r.necessity is Necessity.REQUIRED for r in posting.requirements) >= 5
    parsed = parse(text, deps)
    assert parsed.reasons == () and parsed.resume is not None
    assert parsed.resume.roles == ()  # wants, not a work record: no role is invented
    result = run(parsed.resume, posting, graph, config)
    assert result.raw_score is not None and result.raw_score < 0.85


def test_a_real_candidate_outscores_the_pasted_posting(deps, graph, config) -> None:
    candidate = """Morgan Diaz
Toronto, ON
Experience
Data Engineer Intern, Loblaw Digital | May 2025 – Aug 2025
- Built ETL pipelines in Python and SQL orchestrated with Apache Airflow
- Deployed services with Docker and Kubernetes on AWS, cutting run time by 40%
- Developed Tableau dashboards for the merchandising team
Education
Bachelor of Science, Computer Science, University of Toronto | 2022 – 2026
Skills
Python, SQL, Airflow, Docker, Git, Tableau, Spark, Kafka
Certifications
AWS Certified Cloud Practitioner
"""
    posting = _posting(POSTING, deps["resolver"])
    real = run(parse(candidate, deps).resume, posting, graph, config)
    pasted = run(parse(POSTING, deps).resume, posting, graph, config)
    assert real.raw_score > pasted.raw_score


# ─── repetition ─────────────────────────────────────────────────────────────────────────────


def test_a_skill_listed_fifty_times_counts_once(deps, graph, config) -> None:
    head = "Experience\nAnalyst, Acme Inc | 2022 – 2024\n- Built reports in Excel\nSkills\n"
    once = parse(head + "Python, SQL\n", deps)
    fifty = parse(head + ", ".join(["Python"] * 50) + ", SQL\n", deps)
    lines = parse(head + "Python\n" * 50 + "SQL\n", deps)
    assert canonical_json(once.resume) == canonical_json(fifty.resume) == canonical_json(lines.resume)
    posting = JobPosting(requirements=(JobRequirement(skill_id="skill.python", necessity=Necessity.REQUIRED),
                                       JobRequirement(skill_id="skill.java", necessity=Necessity.REQUIRED)))
    assert run(once.resume, posting, graph, config).raw_score == run(fifty.resume, posting, graph, config).raw_score


# ─── credential status ──────────────────────────────────────────────────────────────────────

IN_PROGRESS = [
    ("Registered Nurse (RN) candidate awaiting the NCLEX.", "cred.registered_nurse", "cred.nursing_student"),
    ("Primary Care Paramedic (PCP) student at Humber College.", "cred.paramedic_license", "cred.paramedic_student"),
    ("Construction and Maintenance Electrician (309A) Apprentice", "cred.electrician_license",
     "cred.electrician_apprentice"),
    ("Professional Engineer (P.Eng.) candidate", "cred.professional_engineer", "cred.eit"),
    ("CPA, in progress", "cred.cpa", "cred.cpa_in_progress"),
    ("RN (pending NCLEX)", "cred.registered_nurse", "cred.nursing_student"),
    ("CDL-A trainee", "cred.commercial_drivers_license", "cred.cdl_trainee"),
    ("RN students", "cred.registered_nurse", "cred.nursing_student"),
    ("R.N. (in progress)", "cred.registered_nurse", "cred.nursing_student"),
    ("Primary Care Paramedic Diploma, Centennial College (expected 2026)", "cred.paramedic_license",
     "cred.paramedic_student"),
    ("Pursuing a CPA designation", "cred.cpa", "cred.cpa_in_progress"),
    ("CPA (in progress)", "cred.cpa", "cred.cpa_in_progress"),
    ("Currently completing the CPA Professional Education Program", "cred.cpa", "cred.cpa_in_progress"),
    ("Registered Nurse, writing the NCLEX in June", "cred.registered_nurse", "cred.nursing_student"),
    ("CFA, Level II", "cred.cfa", "cred.cfa_candidate"),
    ("Registered Nurse (RN)\nExpected June 2027", "cred.registered_nurse", "cred.nursing_student"),
]


@pytest.mark.parametrize(("line", "licence", "designation"), IN_PROGRESS)
def test_status_words_never_give_the_licence(line: str, licence: str, designation: str, deps) -> None:
    for text in (f"Certifications\n{line}\n", f"Summary\n{line}\n"):
        found = {r.canonical_id for r in every_ref(parse(text, deps).resume)}
        assert licence not in found
        assert designation in found


@pytest.mark.parametrize(("line", "licence", "designation"), IN_PROGRESS[:4])
def test_status_words_in_a_role_title(line: str, licence: str, designation: str, deps) -> None:
    title = line.rstrip(".").split("\n")[0]
    parsed = parse(f"Experience\n{title}, Toronto General Hospital | Jan 2025 – Present\n- Cared for patients\n",
                   deps)
    assert licence not in held(parsed.resume)
    assert designation in held(parsed.resume)


@pytest.mark.parametrize("line", [
    "AWS Certified Solutions Architect – Associate (expected 2026)",
    "Pursuing the AWS Certified Cloud Practitioner",
])
def test_a_pursued_credential_without_a_designation_is_dropped(line: str, deps) -> None:
    resume = parse(f"Certifications\n{line}\n", deps).resume
    assert {r.canonical_id for r in every_ref(resume) if r.canonical_id.startswith("cred.")} == set()


@pytest.mark.parametrize(("line", "expected"), [
    ("Registered Nurse (RN), College of Nurses of Ontario", {"cred.registered_nurse"}),
    ("CPA, CPA Ontario, 2023", {"cred.cpa"}),
    ("AWS Certified Cloud Practitioner; CPA (in progress)", {"cred.aws_cloud_practitioner", "cred.cpa_in_progress"}),
    ("CPA candidate", {"cred.cpa_in_progress"}),
    ("CFA Level I", {"cred.cfa_candidate"}),
])
def test_held_credentials_stay(line: str, expected: set[str], deps) -> None:
    resume = parse(f"Certifications\n{line}\n", deps).resume
    assert {c.canonical_id for c in resume.certifications} == expected


def test_the_licence_gate_fails_a_candidate(deps, graph, config) -> None:
    gate = LicenseGate(credential_ids=("cred.registered_nurse",), surface_form="RN licence")
    posting = JobPosting(requirements=(JobRequirement(skill_id="skill.python", necessity=Necessity.REQUIRED),),
                         gates=(gate,))
    candidate = parse("Certifications\nRegistered Nurse (RN) candidate awaiting the NCLEX.\n", deps).resume
    licensed = parse("Certifications\nRegistered Nurse (RN), College of Nurses of Ontario\n", deps).resume
    assert evaluate_gates(candidate, posting, graph, config)[0] is GateStatus.FAIL
    assert evaluate_gates(licensed, posting, graph, config)[0] is GateStatus.PASS
    assert run(candidate, posting, graph, config).outcome is Outcome.GATE_FAIL


def test_credential_status_reads_segments(deps) -> None:
    resolver, resources = deps["resolver"], deps["resources"]

    def read(text: str) -> list[str]:
        return [m.canonical_id for m in credential_status(text, resolver.extract(text), resources)]

    assert read("CPA, CMA (in progress)") == ["cred.cpa"] + [i for i in read("CMA (in progress)")]
    assert read("CPA and CFA Level I candidate") == ["cred.cpa", "cred.cfa_candidate"]
    assert read("Registered Nurse") == ["cred.registered_nurse"]
    assert read("Python developer, in progress of learning Rust") == read("Python developer, Rust")
