"""Endpoint tests for the extension's /api/tailor-resume, /api/custom-resume(-analysis) and
/api/render-resume.

Every score comes from the deterministic match engine (Task 9.3, D-83); the real engine runs
here. The LLM (``OpenAIService._generate``) is mocked and is called exactly once per rewrite: for
the rewrite text. An analysis never calls it.
"""
import base64
import datetime as dt
from unittest.mock import patch, AsyncMock

import pytest

from backend.db.models import ResumeProfileDB
from backend.services import fit_engine

TEST_USER_ID = 1
AS_OF = dt.date(2026, 10, 1)

JD = ("Requirements\n- 2+ years of Python\n- SQL and Docker\n- Kubernetes\n"
      "Nice to have\n- Go, Terraform")
# The rewrite adds the two missing required skills to the skills section.
EDITED = '{"sections":[{"id":"skills","type":"skills","skills":["Python","SQL","Docker","Kubernetes"]}]}'


@pytest.fixture(autouse=True)
def _fixed_month(monkeypatch):
    monkeypatch.setattr(fit_engine, "fit_as_of", lambda today=None: AS_OF)


def _seed_resume(db):
    db.add(ResumeProfileDB(
        user_id=TEST_USER_ID, profile_name="Jane Doe", is_primary=1,
        location="Toronto, ON",
        skills=["Python", "SQL"],
        experience=[{"title": "Software Engineering Intern", "company": "Acme", "start_date": "May 2025",
                     "end_date": "Aug 2025", "bullets": ["Built REST APIs in Python and Django"]}],
        education=[{"school": "Example University", "degree": "BSc Computer Science",
                    "start_date": "2022", "end_date": "2026"}],
        raw_text="Python engineer.",
    ))
    db.commit()


def _tailor_prompt(gen):
    assert gen.await_count == 1, "the LLM is called once, for the rewrite text only"
    return gen.call_args_list[0].args[0]


class TestTailorResume:
    def test_scores_come_from_the_engine_and_the_llm_only_rewrites(self, client, db_session, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        gen = AsyncMock(side_effect=[EDITED])
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post("/api/tailor-resume", json={
                "job_title": "Software Engineer, New Grad", "company": "Acme", "job_description": JD,
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["original_outcome"] == "SCORED" and data["outcome"] == "SCORED"
        before, after = data["original_overall_score"], data["new_overall_score"]
        assert isinstance(before, int) and isinstance(after, int)
        assert after > before  # Docker and Kubernetes are now on the résumé
        assert data["new_ats_score"] == after  # D-123: one engine number
        assert 0 <= data["new_keyword_coverage"] <= 100
        assert data["original_match_label"] and data["match_label"]
        # chip set is the BEFORE candidate set (stable across regenerates), engine display labels
        assert data["matched_keywords"] == ["Python", "SQL"]
        assert {"Docker", "Kubernetes"} <= set(data["missing_keywords"])
        assert data["document"]["sections"][-1]["skills"] == ["Python", "SQL", "Docker", "Kubernetes"]
        # omitted add_keywords -> the engine's MISSING required skills are fed to the rewrite
        # (fix round 1, Minor 1): Kubernetes, not Docker (partly shown through Python) and not
        # the preferred Go / Terraform.
        prompt = _tailor_prompt(gen)
        assert "use the job's own words for these terms: Kubernetes." in prompt
        assert data["reasons"] == []  # SCORED after: nothing to explain

    def test_explicit_keywords_used_exactly(self, client, db_session, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        gen = AsyncMock(side_effect=[EDITED])
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post("/api/tailor-resume", json={
                "job_title": "Engineer", "company": "Acme",
                "job_description": JD, "add_keywords": ["Docker"],
            })
        assert resp.status_code == 200
        assert "use the job's own words for these terms: Docker." in _tailor_prompt(gen)
        assert "Kubernetes" in resp.json()["missing_keywords"]

    def test_explicit_empty_keywords_skip_weaving(self, client, db_session, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        gen = AsyncMock(side_effect=[EDITED])
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post("/api/tailor-resume", json={
                "job_title": "Engineer", "company": "Acme",
                "job_description": JD, "add_keywords": [],
            })
        assert resp.status_code == 200
        # add_keywords=[] -> nothing is woven in, though the engine found missing requirements.
        assert resp.json()["missing_keywords"]
        assert "these terms" not in _tailor_prompt(gen)

    def test_an_unscorable_posting_returns_null_scores_not_zero(self, client, db_session, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        gen = AsyncMock(side_effect=[EDITED])
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post("/api/tailor-resume", json={
                "job_title": "Engineer", "company": "Acme", "job_description": "",
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "NEEDS_REVIEW" and data["original_outcome"] == "NEEDS_REVIEW"
        for key in ("original_overall_score", "new_overall_score", "new_ats_score", "new_keyword_coverage"):
            assert data[key] is None, key
        assert data["match_label"] == "We couldn't score this"
        assert data["missing_keywords"] == []
        assert data["reasons"] == ["We couldn't find any requirements in this job posting."]

    def test_wrapped_contract_surfaces_gaps_changes_and_figures(self, client, db_session, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        wrapped = (
            '{"resume":{"sections":['
            '{"type":"experience","items":[{"bullets":["Owned billing; cut costs 45%"]}]},'
            '{"type":"skills","skills":["Python","AWS"]}'
            ']},"section_order":[],"new_summary":null,'
            '"gaps":["Role wants Kubernetes; not shown"]}'
        )
        gen = AsyncMock(side_effect=[wrapped])
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post("/api/tailor-resume", json={
                "job_title": "Engineer", "company": "Acme", "job_description": JD,
            })
        assert resp.status_code == 200
        data = resp.json()
        assert data["gaps"] == ["Role wants Kubernetes; not shown"]
        assert data["figures_to_verify"] == ["45%"]        # 45% absent from source → flagged
        assert any("entr" in c.lower() or "skill" in c.lower() for c in data["changes"])

    def test_503_on_llm_connection_error(self, client, db_session, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        import httpx
        gen = AsyncMock(side_effect=httpx.ConnectError("boom"))
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post("/api/tailor-resume", json={
                "job_title": "Engineer", "company": "Acme", "job_description": JD,
            })
        assert resp.status_code == 503

    def test_400_when_no_resume(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        resp = client.post("/api/tailor-resume", json={"job_description": "JD"})
        assert resp.status_code == 400


def _request():
    import httpx
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


def _llm_failures():
    import httpx
    return [
        httpx.ReadTimeout("stalled", request=_request()),
        httpx.HTTPStatusError("400", request=_request(), response=httpx.Response(400, request=_request())),
        ValueError("unexpected response format"),
    ]


class TestLlmFailureIsAnError:
    """Fix round 1, Important 1: any LLM failure on a rewrite route is a 503 and saves nothing."""

    @pytest.mark.parametrize("route", ["/api/tailor-resume", "/api/custom-resume"])
    @pytest.mark.parametrize("failure", _llm_failures() + ["not json"], ids=["timeout", "http", "value", "json"])
    def test_a_failed_rewrite_is_a_503_and_saves_no_version(self, client, db_session, monkeypatch, route, failure):
        from backend.db.models import ResumeVersion
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        gen = AsyncMock(side_effect=failure if isinstance(failure, BaseException) else [failure])
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post(route, json={"job_title": "Engineer", "company": "Acme", "job_description": JD})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "AI suggestions are unavailable right now. Please try again in a moment."
        assert db_session.query(ResumeVersion).count() == 0


class TestAnalysisLimits:
    """Fix round 1, Important 2: a pasted description is capped, and the free analysis has its
    own cheap per-user rate limit (never the daily AI quota)."""

    @pytest.mark.parametrize("route", ["/api/custom-resume-analysis", "/api/tailor-resume", "/api/custom-resume"])
    def test_a_description_over_20000_characters_is_refused(self, client, db_session, monkeypatch, route):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        gen = AsyncMock(side_effect=AssertionError("must be refused before any work"))
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post(route, json={"job_title": "Engineer", "company": "Acme",
                                             "job_description": "x" * 20001})
        assert resp.status_code == 422
        assert gen.await_count == 0

    def test_a_description_of_exactly_20000_characters_is_accepted(self, client, db_session):
        _seed_resume(db_session)
        resp = client.post("/api/custom-resume-analysis", json={
            "job_title": "Engineer", "company": "Acme", "job_description": JD + " " * (20000 - len(JD))})
        assert resp.status_code == 200

    def test_the_analysis_has_a_per_user_rate_limit(self, client, db_session, monkeypatch):
        from backend.services import usage_limiter
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
        monkeypatch.setattr(usage_limiter, "FIT_ANALYSIS_PER_MINUTE", 2)

        def no_ai_quota(*args, **kwargs):
            raise AssertionError("the free analysis must not touch the AI quota")

        monkeypatch.setattr(usage_limiter, "enforce_llm_limits", no_ai_quota)
        _seed_resume(db_session)
        body = {"job_title": "Engineer", "company": "Acme", "job_description": JD}
        assert client.post("/api/custom-resume-analysis", json=body).status_code == 200
        assert client.post("/api/custom-resume-analysis", json=body).status_code == 200
        third = client.post("/api/custom-resume-analysis", json=body)
        assert third.status_code == 429
        assert third.headers.get("Retry-After")


class TestCustomResumeAnalysis:
    def test_returns_the_engine_analysis_without_the_llm(self, client, db_session, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        gen = AsyncMock(side_effect=AssertionError("the analysis called the LLM"))
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post("/api/custom-resume-analysis", json={
                "job_title": "Software Engineer, New Grad", "company": "Acme", "job_description": JD,
            })
        assert resp.status_code == 200
        assert gen.await_count == 0
        data = resp.json()
        assert data["outcome"] == "SCORED"
        assert isinstance(data["overall_score"], int)
        assert data["ats_score"] == data["overall_score"]
        assert data["match_label"] in ("Strong match", "Good match", "Partial match", "Limited match")
        assert data["matched_keywords"] == ["Python", "SQL"]
        assert "Kubernetes" in data["missing_keywords"]
        assert data["suggested_keywords"] == ["Kubernetes"]  # missing required skills only
        assert data["suggestions"]  # the engine's counterfactual

    def test_400_when_no_resume(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        resp = client.post("/api/custom-resume-analysis", json={
            "job_title": "Engineer", "company": "Acme", "job_description": "JD",
        })
        assert resp.status_code == 400


class TestCustomResume:
    def test_returns_rewrite_and_saves_version_with_null_job_id(self, client, db_session, monkeypatch):
        from backend.db.models import ResumeVersion
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        gen = AsyncMock(side_effect=[EDITED])
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post("/api/custom-resume", json={
                "job_title": "Engineer", "company": "Acme",
                "job_description": JD,
                "sections": ["Skills"], "add_keywords": ["Docker"],
            })
        assert resp.status_code == 200
        assert gen.await_count == 1
        data = resp.json()
        assert "document" in data and "original_document" in data
        assert data["new_overall_score"] > data["original_overall_score"]
        assert data["new_ats_score"] == data["new_overall_score"]
        assert data["outcome"] == "SCORED" and data["original_outcome"] == "SCORED"
        assert data["version_id"] is not None
        saved = db_session.query(ResumeVersion).filter_by(id=data["version_id"]).one()
        assert saved.job_id is None
        assert saved.source == "ai"
        assert saved.label == "Suggestions · Engineer"

    def test_503_on_llm_connection_error(self, client, db_session, monkeypatch):
        import httpx
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        _seed_resume(db_session)
        gen = AsyncMock(side_effect=httpx.ConnectError("boom"))
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post("/api/custom-resume", json={
                "job_title": "Engineer", "company": "Acme", "job_description": JD,
            })
        assert resp.status_code == 503

    def test_400_when_no_resume(self, client, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")
        resp = client.post("/api/custom-resume", json={"job_description": "JD"})
        assert resp.status_code == 400


class TestRenderResume:
    def test_returns_base64_pdf(self, client):
        doc = {"header": {"name": "Jane Doe"},
               "sections": [{"type": "skills", "title": "SKILLS", "skills": ["Python", "AWS"]}],
               "theme": {}}
        resp = client.post("/api/render-resume", json={"document": doc, "filename": "resume-acme"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["content_type"] == "application/pdf"
        assert data["name"] == "resume-acme.pdf"
        assert base64.b64decode(data["data_base64"])[:5] == b"%PDF-"

    def test_slugs_pdf_suffixed_filename(self, client):
        doc = {"header": {"name": "Jane"}, "sections": [], "theme": {}}
        resp = client.post("/api/render-resume", json={"document": doc, "filename": "My Resume.pdf"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "my-resume.pdf"

    def test_invalid_theme_returns_422(self, client):
        doc = {"header": {"name": "Jane"}, "sections": [],
               "theme": {"accent_color": "not-a-real-color"}}
        resp = client.post("/api/render-resume", json={"document": doc})
        assert resp.status_code == 422


# ─── A legacy undated profile: the extension's "before" is the stored profile's (D-156) ──────


def _legacy_resume(db):
    """A résumé extracted before 2026-07-09 (every role undated, never edited) whose raw text
    holds the dates: as a stored profile it scores through the D-152 fallback."""
    from backend.tests.test_fit_resume_dates_fallback import RAW_DATED, _doc

    doc = _doc()
    profile = ResumeProfileDB(user_id=TEST_USER_ID, profile_name="Alex Sample", is_primary=1, raw_text=RAW_DATED,
                              **{key: doc[key] for key in ("location", "summary", "experience", "education",
                                                           "projects", "skills", "technologies",
                                                           "custom_sections")})
    db.add(profile)
    db.commit()
    assert profile.content_updated_at is None
    return profile


def _legacy_before(db, profile):
    from backend.tests.test_fit_resume_dates_fallback import POSTING_TEXT

    posting = fit_engine.posting_from_text("Software Engineer, New Grad", "Acme", POSTING_TEXT)
    result = fit_engine.score_profile(db, TEST_USER_ID, profile, posting)
    assert result.outcome.value == "SCORED"  # through the D-152 dated-text fallback
    return result


def _legacy_job():
    from backend.tests.test_fit_resume_dates_fallback import POSTING_TEXT

    return {"job_title": "Software Engineer, New Grad", "company": "Acme", "job_description": POSTING_TEXT}


def _echo_rewrite(monkeypatch):
    from backend.services.openai_service import TailorStructuredResult

    async def fake_structured(self, doc, jd, sections=None, keywords=None):
        return TailorStructuredResult(document=doc, gaps=[])

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr("backend.services.openai_service.OpenAIService.tailor_resume_structured", fake_structured)


class TestLegacyProfileBefore:
    def test_analysis_scores_the_stored_profile(self, client, db_session):
        before = _legacy_before(db_session, _legacy_resume(db_session))
        body = client.post("/api/custom-resume-analysis", json=_legacy_job()).json()
        assert body["outcome"] == "SCORED"
        assert body["overall_score"] == fit_engine.stored_score(before)

    def test_tailor_resume_before_is_the_stored_profile_and_the_rewrite_never_falls_back(
        self, client, db_session, monkeypatch
    ):
        before = _legacy_before(db_session, _legacy_resume(db_session))
        _echo_rewrite(monkeypatch)
        body = client.post("/api/tailor-resume", json=_legacy_job()).json()
        assert body["original_outcome"] == "SCORED"
        assert body["original_overall_score"] == fit_engine.stored_score(before)
        # The rewritten side keeps the no-fallback rule: the same undated document, scored as written.
        assert body["outcome"] != "SCORED"

    def test_custom_resume_before_is_the_stored_profile_too(self, client, db_session, monkeypatch):
        before = _legacy_before(db_session, _legacy_resume(db_session))
        _echo_rewrite(monkeypatch)
        body = client.post("/api/custom-resume", json=_legacy_job()).json()
        assert body["original_outcome"] == "SCORED"
        assert body["original_overall_score"] == fit_engine.stored_score(before)
        assert body["outcome"] != "SCORED"
