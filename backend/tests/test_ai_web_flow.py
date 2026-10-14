"""
Unit tests for the web "Generate Custom Resume" flow endpoints:
  POST /ai/job-analysis/{job_id}  (Step 1, keywords + scores)
  POST /ai/rewrite/{job_id}       (Step 3, tailor + before/after scores)
  POST /ai/cover-letter/{job_id}  (tone option, backward compatible)

The OpenAI call (`OpenAIService._generate`) is mocked, so no network/API key. Every score is the
deterministic match engine's (Task 9.3, D-83; the real engine runs here): the LLM only writes the
rewrite text.
"""

import datetime as dt
from unittest.mock import patch, AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.database import Base, get_db
from backend.db.models import ScrapedJob, ResumeProfileDB
from backend.auth.dependencies import get_current_user_id, get_verified_user_id
from backend.routers import ai
from backend.services import fit_engine

TEST_DATABASE_URL = "sqlite:///./test_ai_web_flow.db"
test_engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

TEST_USER_ID = 1

ai_app = FastAPI()
ai_app.include_router(ai.router, prefix="/ai", tags=["ai"])

# What the structured tailor LLM call returns: same shape, rewritten bullets +
# an added skill. Most fields are omitted and fall back to schema defaults; the
# merge re-attaches the original structure/facts, so only content is adopted.
EDITED_DOC_JSON = (
    '{"sections": ['
    '{"type": "experience", "items": [{"bullets": ["Built internal tools in Python, SQL and TypeScript for 200+ engineers", "Designed PostgreSQL schemas and REST APIs"]}]},'
    '{"type": "skills", "skills": ["Python", "SQL", "TypeScript"]}'
    ']}'
)


@pytest.fixture(autouse=True)
def setup_test_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture(autouse=True)
def _openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(fit_engine, "fit_as_of", lambda today=None: dt.date(2026, 10, 1))


@pytest.fixture
def db_session():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    def _get_db():
        try:
            yield db_session
        finally:
            pass

    async def _user():
        return TEST_USER_ID

    ai_app.dependency_overrides[get_db] = _get_db
    ai_app.dependency_overrides[get_current_user_id] = _user
    ai_app.dependency_overrides[get_verified_user_id] = _user
    with TestClient(ai_app) as c:
        yield c
    ai_app.dependency_overrides.clear()


def _seed(db_session, with_resume=True):
    job = ScrapedJob(
        title="Software Engineer",
        company="TestCo",
        url="https://example.com/job/1",
        description="Requirements\n- Python and SQL\n- TypeScript\n- AWS",
    )
    db_session.add(job)
    if with_resume:
        db_session.add(
            ResumeProfileDB(
                user_id=TEST_USER_ID,
                profile_name="Test User",
                is_primary=1,
                skills=["Python", "SQL"],
                experience=[
                    {
                        "title": "Software Engineer",
                        "company": "Acme",
                        "location": "NYC",
                        "start_date": "Jan 2021",
                        "end_date": "Dec 2023",
                        "bullets": ["Built internal tools in Python and SQL used by 200 engineers",
                                    "Designed PostgreSQL schemas and REST APIs"],
                    }
                ],
                raw_text="Python and SQL engineer.",
            )
        )
    db_session.commit()
    db_session.refresh(job)
    return job


NO_LLM = "the analysis called the LLM"


class TestJobAnalysis:
    def test_returns_the_engine_analysis_without_the_llm(self, client, db_session):
        job = _seed(db_session)
        gen = AsyncMock(side_effect=AssertionError(NO_LLM))
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post(f"/ai/job-analysis/{job.id}", json={})
        assert resp.status_code == 200
        assert gen.await_count == 0
        data = resp.json()
        assert data["outcome"] == "SCORED"
        assert isinstance(data["overall_score"], int)
        assert data["ats_score"] == data["overall_score"]
        assert data["match_label"] in ("Strong match", "Good match", "Partial match", "Limited match")
        assert data["matched_keywords"] == ["Python", "SQL"]
        assert "TypeScript" in data["missing_keywords"]
        assert isinstance(data["keyword_coverage"], int) and 0 < data["keyword_coverage"] < 100

    def test_the_custom_resume_analysis_alias_is_the_same_analysis(self, client, db_session):
        job = _seed(db_session)
        first = client.post(f"/ai/job-analysis/{job.id}", json={}).json()
        second = client.post(f"/ai/custom-resume-analysis/{job.id}", json={}).json()
        assert first == second

    def test_the_analysis_is_free_of_the_ai_limit(self, client, db_session, monkeypatch):
        """No AI is called, so the daily AI limit must not block or count it (D-83)."""
        from fastapi import HTTPException

        def limited(*args, **kwargs):
            raise HTTPException(status_code=429, detail="limit")

        monkeypatch.setattr("backend.services.usage_limiter.enforce_llm_limits", limited)
        job = _seed(db_session)
        assert client.post(f"/ai/job-analysis/{job.id}", json={}).status_code == 200

    def test_a_failed_gate_returns_null_scores_and_the_reason(self, client, db_session):
        """Fix round 1, Minor 5: the route, not only the mapping, says "Not eligible" and why.

        An unscoped sponsorship need fails a no-sponsorship posting; a country-scoped denial only
        warns (D-61 amendment, decide-b round 4).
        """
        from backend.db.models import UserSettings

        _seed(db_session)
        db_session.add(UserSettings(user_id=TEST_USER_ID,
                                    prefilled_answers={"Do you now or in the future require sponsorship?": "Yes"},
                                    address_city="Toronto", address_state="ON", country="Canada"))
        gated = ScrapedJob(title="Software Engineer", company="GateCo", url="https://example.com/job/gate",
                           description=("Requirements\n- 2+ years of Python\n- SQL and Docker\n\n"
                                        "We are unable to sponsor work visas for this role."),
                           locations_json=[{"city": "Seattle", "region": "WA", "region_name": "Washington",
                                            "country": "United States"}],
                           country="US", work_type="onsite")
        db_session.add(gated)
        db_session.commit()
        resp = client.post(f"/ai/job-analysis/{gated.id}", json={})
        assert resp.status_code == 200
        data = resp.json()
        assert data["outcome"] == "GATE_FAIL"
        assert data["overall_score"] is None and data["ats_score"] is None
        assert data["keyword_coverage"] is None
        assert data["match_label"] == "Not eligible"
        assert data["reasons"] and data["weaknesses"][0] == data["reasons"][0]

    def test_the_job_route_shares_the_analysis_rate_limit(self, client, db_session, monkeypatch):
        from backend.services import usage_limiter
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "true")
        monkeypatch.setattr(usage_limiter, "FIT_ANALYSIS_PER_MINUTE", 1)
        job = _seed(db_session)
        assert client.post(f"/ai/job-analysis/{job.id}", json={}).status_code == 200
        assert client.post(f"/ai/custom-resume-analysis/{job.id}", json={}).status_code == 429

    def test_404_for_missing_job(self, client):
        resp = client.post("/ai/job-analysis/9999", json={})
        assert resp.status_code == 404

    def test_400_when_no_resume(self, client, db_session):
        job = _seed(db_session, with_resume=False)
        resp = client.post(f"/ai/job-analysis/{job.id}", json={})
        assert resp.status_code == 400


class TestRewrite:
    def test_returns_structured_document_and_before_after_scores(self, client, db_session):
        job = _seed(db_session)
        # ONE LLM call: the structured rewrite. Both scores are the engine's.
        gen = AsyncMock(side_effect=[EDITED_DOC_JSON])
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post(
                f"/ai/rewrite/{job.id}",
                json={"sections": ["Skills"], "add_keywords": ["TypeScript"]},
            )
        assert resp.status_code == 200
        assert gen.await_count == 1
        assert "use the job's own words for these terms: TypeScript." in gen.call_args_list[0].args[0]
        data = resp.json()

        # Structured document reflects the AI's bullet rewrite + added skill,
        # merged onto the original structure so facts/sections are preserved.
        sections = data["document"]["sections"]
        assert sections[0]["type"] == "experience"
        assert sections[0]["items"][0]["bullets"] == ["Built internal tools in Python, SQL and TypeScript for 200+ engineers", "Designed PostgreSQL schemas and REST APIs"]
        assert sections[0]["items"][0]["title"] == "Software Engineer"  # fact preserved
        assert sections[1]["skills"] == ["Python", "SQL", "TypeScript"]

        # Flattened text (for Copy) + before/after scores still provided.
        assert "Built internal tools in Python, SQL and TypeScript for 200+ engineers" in data["tailored_text"]
        assert data["original_document"]["sections"][0]["items"][0]["title"] == "Software Engineer"
        assert data["diff_summary"]  # non-empty unified diff
        assert data["original_outcome"] == "SCORED" and data["outcome"] == "SCORED"
        assert data["new_overall_score"] > data["original_overall_score"]  # TypeScript now shown
        assert data["new_ats_score"] == data["new_overall_score"]
        assert data["version_id"] is not None

        assert data["match_label"] in ("Strong match", "Good match", "Partial match", "Limited match")

        # The job's posting was parsed once and cached on the row (D-84), like the feed's.
        db_session.refresh(job)
        assert job.match_terms and job.match_terms["v"].startswith("pv1-")


    def test_a_listed_only_skill_can_leave_the_band_only(self, client, db_session):
        """A skill added to the list with no evidence lowers the engine's confidence: the result
        is SCORED_LOW_CONFIDENCE, so the new score is null and only the band is shown."""
        job = _seed(db_session)
        listed_only = ('{"sections": [{"type": "experience", "items": []},'
                       '{"type": "skills", "skills": ["Python", "SQL", "TypeScript"]}]}')
        gen = AsyncMock(side_effect=[listed_only])
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post(f"/ai/rewrite/{job.id}", json={"add_keywords": ["TypeScript"]})
        assert resp.status_code == 200
        data = resp.json()
        assert data["original_outcome"] == "SCORED" and isinstance(data["original_overall_score"], int)
        assert data["outcome"] == "SCORED_LOW_CONFIDENCE"
        assert data["new_overall_score"] is None and data["new_ats_score"] is None
        assert data["match_label"] in ("Strong match", "Good match", "Partial match", "Limited match")
        # The response says why the number went away, in the engine's own words only: exactly
        # the compliance.md section 2 sentence for each reason code (fix round 2).
        from matcher.errors import FLAG_TEXT

        assert data["reasons"]
        assert all(text in FLAG_TEXT.values() for text in data["reasons"]), data["reasons"]

    def test_a_timeout_is_a_503_and_saves_no_version(self, client, db_session):
        """Fix round 1, Important 1: a stalled LLM is an error, never a 200 'rewrite' that is
        the original, and nothing is saved."""
        import httpx
        from backend.db.models import ResumeVersion

        job = _seed(db_session)
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        gen = AsyncMock(side_effect=httpx.ReadTimeout("stalled", request=request))
        with patch("backend.services.openai_service.OpenAIService._generate", gen):
            resp = client.post(f"/ai/rewrite/{job.id}", json={})
        assert resp.status_code == 503
        assert db_session.query(ResumeVersion).count() == 0


class TestCoverLetterTone:
    def test_tone_path_passes_through(self, client, db_session):
        job = _seed(db_session)
        mock = AsyncMock(return_value="Dear Hiring Team, ...")
        with patch("backend.services.openai_service.OpenAIService._generate", mock):
            resp = client.post(
                f"/ai/cover-letter/{job.id}", json={"tone": "enthusiastic"}
            )
        assert resp.status_code == 200
        assert resp.json()["text"].startswith("Dear Hiring Team")

    def test_no_body_still_works(self, client, db_session):
        job = _seed(db_session)
        with patch(
            "backend.services.openai_service.OpenAIService._generate",
            new_callable=AsyncMock,
            return_value="A letter.",
        ):
            resp = client.post(f"/ai/cover-letter/{job.id}")
        assert resp.status_code == 200
        assert resp.json()["text"] == "A letter."


class TestResumeVersions:
    DOC = {
        "header": {"name": "Jane Doe"},
        "sections": [{"type": "skills", "title": "SKILLS", "skills": ["Python", "AWS"]}],
        "theme": {},
    }

    def test_save_list_get_roundtrip(self, client, db_session):
        job = _seed(db_session)
        resume = db_session.query(ResumeProfileDB).first()

        save = client.post(
            "/ai/resume-versions",
            json={"resume_id": resume.id, "job_id": job.id, "source": "user", "label": "My edits", "document": self.DOC},
        )
        assert save.status_code == 200
        body = save.json()
        vid = body["id"]
        assert body["source"] == "user"
        assert body["document"]["sections"][0]["skills"] == ["Python", "AWS"]

        lst = client.get("/ai/resume-versions", params={"job_id": job.id})
        assert lst.status_code == 200
        assert any(v["id"] == vid for v in lst.json())

        got = client.get(f"/ai/resume-versions/{vid}")
        assert got.status_code == 200
        assert got.json()["document"]["header"]["name"] == "Jane Doe"

    def test_get_missing_returns_404(self, client):
        assert client.get("/ai/resume-versions/99999").status_code == 404


class TestEditSnippet:
    def test_edits_only_the_snippet(self, client, db_session):
        job = _seed(db_session)
        with patch(
            "backend.services.openai_service.OpenAIService._generate",
            new_callable=AsyncMock,
            return_value="Architected scalable APIs serving 1M requests/day.",
        ):
            resp = client.post("/ai/edit-snippet", json={"text": "built apis", "action": "impact", "job_id": job.id})
        assert resp.status_code == 200
        assert resp.json()["text"] == "Architected scalable APIs serving 1M requests/day."

    def test_empty_text_returns_422(self, client):
        assert client.post("/ai/edit-snippet", json={"text": "  ", "action": "rewrite"}).status_code == 422
