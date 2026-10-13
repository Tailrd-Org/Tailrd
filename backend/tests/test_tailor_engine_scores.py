"""Task 9.3 (D-83): every score the tailoring flow shows comes from the deterministic match engine.

Runs the REAL engine (built once per process, about ten seconds cold) on invented résumés and
postings. The LLM writes the rewrite text and nothing else: these tests make every LLM call fail,
so an analysis that still asked it for a score, a label or a keyword list could not pass.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.db.models import ResumeProfileDB, ScrapedJob, UserSettings
from backend.schemas.ai import JobAnalysisOut
from backend.services import fit_engine
from backend.services.match_engine import MatchEngine, analysis_from_result
from backend.services.resume_document import db_record_to_document, document_to_record

from matcher.models import Outcome  # importable once fit_engine is imported (D-01)

AS_OF = dt.date(2026, 10, 1)
USER = 1

DOC = {
    "location": "Toronto, ON",
    "summary": "Computer science student who builds web services.",
    "experience": [{
        "title": "Software Engineering Intern", "company": "Example Corp", "location": "Toronto, ON",
        "start_date": "May 2025", "end_date": "Aug 2025",
        "bullets": ["Built REST APIs in Python and Django serving 2M requests a day"],
    }],
    "education": [{"school": "Example University", "degree": "BSc Computer Science",
                   "start_date": "2022", "end_date": "2026", "gpa": "3.8",
                   "coursework": ["Databases", "Operating Systems"], "achievements": ["Dean's list"]}],
    "projects": [{"name": "Chat app", "bullets": ["Built with Node.js and PostgreSQL"]}],
    "skills": ["Python", "SQL"],
    "technologies": {"Tools": ["Git", "Linux"]},
    "custom_sections": [{"id": "c1", "title": "AWARDS", "kind": "custom", "text": "",
                         "bullets": ["Hackathon winner"], "items": []}],
}

SOFTWARE = ("Requirements\n- 2+ years of Python\n- SQL and Docker\n- Kubernetes\n"
            "Nice to have\n- Go, Terraform")
NO_SPONSOR = ("Requirements\n- 2+ years of Python\n- SQL and Docker\n\n"
              "We are unable to sponsor work visas for this role.")
SEATTLE = [{"city": "Seattle", "region": "WA", "region_name": "Washington", "country": "United States"}]


@pytest.fixture(autouse=True)
def _fixed_month(monkeypatch):
    monkeypatch.setattr(fit_engine, "fit_as_of", lambda today=None: AS_OF)


@pytest.fixture
def no_llm():
    """Every LLM call fails loudly: the analysis must not need one."""
    boom = AsyncMock(side_effect=AssertionError("the tailoring analysis called the LLM"))
    with patch("backend.services.openai_service.OpenAIService._generate", boom):
        yield boom


def _profile(db, doc=DOC, raw_text="Python engineer.") -> ResumeProfileDB:
    profile = ResumeProfileDB(user_id=USER, profile_name="Jordan Example", is_primary=1, raw_text=raw_text,
                              location=doc.get("location"), summary=doc.get("summary"),
                              experience=doc.get("experience", []), education=doc.get("education", []),
                              projects=doc.get("projects", []), skills=doc.get("skills", []),
                              technologies=doc.get("technologies", {}),
                              custom_sections=doc.get("custom_sections", []))
    db.add(profile)
    db.commit()
    return profile


def _scored(db):
    profile = _profile(db)
    posting = fit_engine.posting_from_text("Software Engineer, New Grad", "Acme", SOFTWARE)
    document = db_record_to_document(profile)
    return profile, posting, fit_engine.score_document(db, USER, document_to_record(document), posting)


# ─── The résumé the engine reads ───────────────────────────────────────────────────────────


def test_a_document_reads_like_the_stored_profile_it_came_from(db_session):
    """The tailoring 'before' must be the same résumé the feed scores (D-80), or the numbers on
    the job card and in the tailoring panel would disagree for no reason."""
    profile = _profile(db_session)
    record = document_to_record(db_record_to_document(profile))
    stored = fit_engine.resume_for_profile(profile, None)
    from_document = fit_engine.resume_for_document(record, None)
    assert stored.resume is not None
    assert from_document.resume == stored.resume


def test_the_record_carries_education_details_back_out_of_the_bullets(db_session):
    record = document_to_record(db_record_to_document(_profile(db_session)))
    education = record["education"][0]
    assert education["coursework"] == ["Databases", "Operating Systems"]
    assert education["achievements"] == ["Dean's list"]
    assert education["gpa"] == "3.8"
    assert record["custom_sections"][0]["bullets"] == ["Hackathon winner"]
    assert record["technologies"] == {"Tools": ["Git", "Linux"]}


# ─── Outcomes ───────────────────────────────────────────────────────────────────────────────


def test_a_scored_result_shows_the_engine_numbers_and_its_requirement_facts(db_session):
    _, _, result = _scored(db_session)
    assert result.outcome is Outcome.SCORED
    analysis = analysis_from_result(result)
    assert analysis.outcome == "SCORED"
    assert analysis.overall_score == result.display_score
    assert analysis.ats_score == result.display_score  # D-123: one engine number, documented
    assert analysis.match_label == result.band.value
    assert analysis.matched_keywords == ["Python", "SQL"]
    assert "Kubernetes" in analysis.missing_keywords and "Docker" in analysis.missing_keywords
    assert "Terraform" in analysis.missing_keywords  # preferred requirements count too
    assert not set(analysis.matched_keywords) & set(analysis.missing_keywords)
    assert analysis.strengths and analysis.weaknesses
    assert analysis.reasons == []

    facts = [fact for name in ("required_skills", "preferred_skills")
             for fact in result.dimensions[name].facts if fact.code.startswith("REQ_")]
    weights = [fact.detail["idf"] * fact.detail["weight_multiplier"] for fact in facts]
    expected = round(100 * math.fsum(w * fact.value for w, fact in zip(weights, facts)) / math.fsum(weights))
    assert analysis.keyword_coverage == expected


def test_suggestions_come_from_the_engine_counterfactual(db_session):
    profile = _profile(db_session)
    posting = fit_engine.posting_from_text("Software Engineer, New Grad", "Acme", SOFTWARE)
    record = document_to_record(db_record_to_document(profile))
    plain = analysis_from_result(fit_engine.score_document(db_session, USER, record, posting))
    detailed = analysis_from_result(fit_engine.score_document(db_session, USER, record, posting,
                                                              with_suggestions=True))
    assert plain.suggestions == []
    assert detailed.suggestions and all("%" not in text for text in detailed.suggestions)


def test_a_low_confidence_result_shows_the_band_only(db_session):
    _, _, result = _scored(db_session)
    low = result.model_copy(update={"outcome": Outcome.SCORED_LOW_CONFIDENCE, "display_score": None})
    analysis = analysis_from_result(low)
    assert analysis.outcome == "SCORED_LOW_CONFIDENCE"
    assert analysis.overall_score is None and analysis.ats_score is None
    assert analysis.match_label == result.band.value


def test_a_posting_without_requirements_needs_review(db_session):
    profile = _profile(db_session)
    posting = fit_engine.posting_from_text("Engineer", "Acme", "")
    result = fit_engine.score_document(db_session, USER, document_to_record(db_record_to_document(profile)), posting)
    analysis = analysis_from_result(result)
    assert analysis.outcome == "NEEDS_REVIEW"
    assert analysis.overall_score is None and analysis.ats_score is None
    assert analysis.keyword_coverage is None
    assert analysis.match_label == "We couldn't score this"
    assert analysis.reasons  # it says why


def test_a_failed_gate_is_not_eligible_with_the_reason(db_session):
    """The user's saved answers are read as the feed reads them: here, not authorised in the US."""
    db_session.add(UserSettings(user_id=USER, prefilled_answers={"Authorized to work in the US": "No"},
                                address_city="Toronto", address_state="ON", country="Canada"))
    db_session.commit()
    profile = _profile(db_session)
    job = ScrapedJob(title="Software Engineer", company="Acme", url="https://example.com/jobs/gate",
                     description=NO_SPONSOR, locations_json=SEATTLE, country="US", work_type="onsite")
    db_session.add(job)
    db_session.commit()
    posting = MatchEngine(db_session).posting(job=job)
    result = fit_engine.score_document(db_session, USER, document_to_record(db_record_to_document(profile)), posting)
    analysis = analysis_from_result(result)
    assert analysis.outcome == "GATE_FAIL"
    assert analysis.overall_score is None and analysis.ats_score is None
    assert analysis.match_label == "Not eligible"
    assert analysis.reasons and analysis.reasons[0] in analysis.weaknesses


def test_a_job_row_posting_is_the_cached_feed_posting(db_session):
    job = ScrapedJob(title="Software Engineer, New Grad", company="Acme", url="https://example.com/jobs/1",
                     description=SOFTWARE)
    db_session.add(job)
    db_session.commit()
    posting = MatchEngine(db_session).posting(job=job)
    db_session.refresh(job)
    assert job.match_terms and job.match_terms["v"] == fit_engine.get_engine().posting_version(AS_OF)
    assert posting == fit_engine.posting_for_job(job)


# ─── What the rewrite is asked to weave in (fix round 1, Minor 1) ───────────────────────────


def _with_required_facts(result, facts):
    from matcher.models import Fact

    built = tuple(Fact(code=code, subject=subject, value=value,
                       detail={"relation": "NONE", "via": "", "idf": 1.0, "weight_multiplier": 1.0,
                               "necessity": "REQUIRED", "members": members})
                  for code, subject, value, members in facts)
    required = result.dimensions["required_skills"].model_copy(update={"facts": built})
    return result.model_copy(update={"dimensions": {**result.dimensions, "required_skills": required}})


def test_only_missing_required_skills_are_suggested(db_session):
    _, _, result = _scored(db_session)
    analysis = analysis_from_result(result)
    codes = {fact.subject: fact.code for fact in result.dimensions["required_skills"].facts}
    # This résumé partly shows Docker and Kubernetes (REQ_PARTIAL); Go and Terraform are
    # preferred: all stay chips, none is a term the rewrite is told to add.
    assert codes["skill.docker"] == codes["skill.kubernetes"] == "REQ_PARTIAL"
    assert {"Docker", "Kubernetes", "Go", "Terraform"} <= set(analysis.missing_keywords)
    assert analysis.suggested_keywords == []
    # A REQUIRED skill at REQ_MISS is suggested (test_tailor_api pins Kubernetes for a résumé
    # with no related experience).
    missing = _with_required_facts(result, [("REQ_MISS", "skill.kubernetes", 0.0, "skill.kubernetes"),
                                            ("REQ_PARTIAL", "skill.docker", 0.3, "skill.docker")])
    assert analysis_from_result(missing).suggested_keywords == ["Kubernetes"]


def test_an_or_group_suggests_its_best_member_not_the_a_or_b_string(db_session):
    _, _, result = _scored(db_session)
    grouped = _with_required_facts(result, [("REQ_MISS", "skill.rust", 0.0, "skill.haskell,skill.rust")])
    analysis = analysis_from_result(grouped)
    assert analysis.missing_keywords[0] == "Haskell or Rust"  # the chip keeps the group
    assert analysis.suggested_keywords == ["Rust"]


def test_suggestions_are_capped_at_ten(db_session):
    _, _, result = _scored(db_session)
    names = ["azure", "gcp", "kubernetes", "redis", "elasticsearch", "kafka", "spark", "hadoop", "terraform",
             "rust", "haskell", "scala"]
    many = _with_required_facts(result, [("REQ_MISS", f"skill.{n}", 0.0, f"skill.{n}") for n in names])
    analysis = analysis_from_result(many)
    assert len(analysis.missing_keywords) >= 12  # the 12 required, then the preferred chips
    assert len(analysis.suggested_keywords) == 10
    assert analysis.suggested_keywords == analysis.missing_keywords[:10]


def test_match_engine_uses_only_public_fit_engine_names():
    """Fix round 1, Minor 3: a rename inside fit_engine must not break tailoring silently."""
    import re

    source = (Path(__file__).resolve().parents[1] / "services" / "match_engine.py").read_text(encoding="utf-8")
    used = set(re.findall(r"(?<![\w.])fit_engine\.(\w+)\(", source))
    assert used and all(not name.startswith("_") for name in used), used
    assert used <= set(fit_engine.__all__), used - set(fit_engine.__all__)


# ─── No LLM in the analysis ────────────────────────────────────────────────────────────────


def test_the_analysis_never_calls_the_llm(db_session, no_llm):
    profile = _profile(db_session)
    analysis = MatchEngine(db_session).analyze_job(
        db_record_to_document(profile), user_id=USER, job_title="Software Engineer, New Grad",
        company="Acme", job_description=SOFTWARE, raw_text=profile.raw_text)
    assert isinstance(analysis, JobAnalysisOut)
    assert analysis.outcome == "SCORED"
    assert no_llm.await_count == 0


def test_the_analysis_is_deterministic(db_session, no_llm):
    profile = _profile(db_session)
    engine = MatchEngine(db_session)
    kwargs = dict(user_id=USER, job_title="Software Engineer, New Grad", company="Acme", job_description=SOFTWARE)
    assert engine.analyze_job(db_record_to_document(profile), **kwargs) == \
        engine.analyze_job(db_record_to_document(profile), **kwargs)


def test_no_tailoring_path_asks_the_llm_for_a_score_label_or_keywords():
    backend = Path(__file__).resolve().parents[1]
    repo = backend.parent
    match_engine_py = (backend / "services" / "match_engine.py").read_text(encoding="utf-8")
    for gone in ("JOB_ANALYSIS_PROMPT", "get_llm_service", "_generate", "match.analyze", "score_to_label"):
        assert gone not in match_engine_py, gone
    for path in backend.rglob("*.py"):
        if "tests" in path.parts:
            continue
        assert 'op="match.analyze"' not in path.read_text(encoding="utf-8"), path
    for prompt in ("tailor_resume_structured.txt", "tailor_resume.txt", "tailor_resume_guided.txt"):
        text = (repo / "prompts" / prompt).read_text(encoding="utf-8").lower()
        for asked in ("overall_score", "ats_score", "match_label", "matched_keywords", "missing_keywords"):
            assert asked not in text, (prompt, asked)


# ─── A legacy undated profile: the card and Suggest Edits agree (I-3, D-123 with D-152) ──────


@pytest.fixture
def ai_client(db_session, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.auth.dependencies import get_current_user_id, get_verified_user_id
    from backend.db.database import get_db
    from backend.routers.ai import router as ai_router

    monkeypatch.setenv("FIT_TRACE_SALT", "test-salt-0123456789abcdef")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    app = FastAPI()
    app.include_router(ai_router, prefix="/ai")

    def _db():
        yield db_session

    async def _uid():
        return USER

    for dep in (get_current_user_id, get_verified_user_id):
        app.dependency_overrides[dep] = _uid
    app.dependency_overrides[get_db] = _db
    with TestClient(app) as client:
        yield client


def _legacy_profile_and_job(db):
    """A résumé extracted before 2026-07-09 (every role undated, never edited) whose raw text
    holds the dates, and a job row it fits."""
    from backend.db.models import User
    from backend.tests.test_fit_resume_dates_fallback import POSTING_TEXT, RAW_DATED, _doc

    db.add(User(id=USER, email="u1@example.com", email_verified=True))
    db.commit()
    profile = _profile(db, doc=_doc(), raw_text=RAW_DATED)
    assert profile.content_updated_at is None
    job = ScrapedJob(title="Software Engineer, New Grad", company="Acme", url="https://example.com/jobs/legacy",
                     description=POSTING_TEXT, locations_json=[{"city": "Toronto", "region": "ON",
                                                                "country": "Canada"}], country="CA")
    db.add(job)
    db.commit()
    return profile, job


def _card(db, profile, job):
    from backend.db.models import JobMatchScore

    fit_engine.bank_scores(db, USER, profile, [job.id])
    row = db.query(JobMatchScore).filter_by(user_id=USER, job_id=job.id).one()
    assert row.outcome == "SCORED"  # through the D-152 dated-text fallback
    return row


def test_a_legacy_profile_job_analysis_before_equals_its_card(db_session, ai_client, no_llm):
    profile, job = _legacy_profile_and_job(db_session)
    card = _card(db_session, profile, job)
    body = ai_client.post(f"/ai/job-analysis/{job.id}").json()
    assert body["outcome"] == card.outcome
    assert body["overall_score"] == card.score
    assert body["match_label"] == card.band


def test_a_legacy_profile_suggest_edits_before_equals_its_card(db_session, ai_client, monkeypatch):
    from backend.services.openai_service import TailorStructuredResult

    profile, job = _legacy_profile_and_job(db_session)
    card = _card(db_session, profile, job)
    seen = []

    async def fake_structured(self, doc, jd, sections=None, keywords=None):
        seen.append(doc)
        return TailorStructuredResult(document=doc, gaps=[])

    monkeypatch.setattr("backend.services.openai_service.OpenAIService.tailor_resume_structured", fake_structured)
    body = ai_client.post(f"/ai/rewrite/{job.id}").json()
    assert seen
    assert body["original_outcome"] == card.outcome
    assert body["original_overall_score"] == card.score
    assert body["original_match_label"] == card.band
    # The rewritten side keeps the no-fallback rule: the same undated document, scored as written.
    assert body["outcome"] != "SCORED"
