"""
Unit tests for the AI router endpoints.

Tests cover:
- 404 for non-existent jobs on all endpoints
- 400 when no resume profile exists
- the match breakdown is the fit engine's and never touches the AI service (Task 9.2)
- /ai/analyze-fit and /ai/batch-score are gone

Requirements: 3.7
"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.database import Base, get_db
from backend.db.models import ScrapedJob, ResumeProfileDB
from backend.auth.dependencies import get_current_user_id, get_verified_user_id
from backend.routers import ai


@pytest.fixture(autouse=True)
def _trace_salt(monkeypatch):
    # The breakdown also banks the engine's score, which needs a trace salt (never a real one).
    monkeypatch.setenv("FIT_TRACE_SALT", "a-test-salt-of-at-least-16-bytes")

# Create a test-specific app with only the AI router (avoids production lifespan issues)
TEST_DATABASE_URL = "sqlite:///./test_ai_router.db"
test_engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

TEST_USER_ID = 1

ai_app = FastAPI()
ai_app.include_router(ai.router, prefix="/ai", tags=["ai"])


@pytest.fixture(autouse=True)
def setup_test_db():
    """Create all tables before each test, drop after."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session():
    """Yield a test DB session."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    """FastAPI test client with overridden DB and auth dependencies."""
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    async def _override_get_user_id():
        return TEST_USER_ID

    ai_app.dependency_overrides[get_db] = _override_get_db
    ai_app.dependency_overrides[get_current_user_id] = _override_get_user_id
    ai_app.dependency_overrides[get_verified_user_id] = _override_get_user_id
    with TestClient(ai_app) as c:
        yield c
    ai_app.dependency_overrides.clear()


class TestMatchBreakdown:
    """Tests for POST /ai/match-breakdown/{job_id}."""

    def test_returns_404_for_nonexistent_job(self, client):
        """Non-existent job_id should return 404."""
        response = client.post("/ai/match-breakdown/9999")
        assert response.status_code == 404
        assert "Job not found" in response.json()["detail"]

    def test_returns_400_when_no_resume_profile(self, client, db_session):
        """Should return 400 when no resume profile exists."""
        # Create a job but no resume profile
        job = ScrapedJob(
            title="Software Engineer",
            company="TestCo",
            url="https://example.com/job/1",
            description="Build things",
        )
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)

        response = client.post(f"/ai/match-breakdown/{job.id}")
        assert response.status_code == 400
        assert "No resume found" in response.json()["detail"]

    def test_the_breakdown_never_calls_the_ai_service(self, client, db_session, monkeypatch):
        """The breakdown is the deterministic engine's: an unreachable AI service changes
        nothing (no 503 any more), and nothing charges the daily AI limit."""
        def unreachable(*_a, **_k):
            raise httpx.ConnectError("Connection refused")

        monkeypatch.setattr("backend.services.llm.get_llm_service", unreachable)
        monkeypatch.setattr("backend.services.usage_limiter.enforce_llm_limits", unreachable)
        job = ScrapedJob(
            title="Software Engineer",
            company="TestCo",
            url="https://example.com/job/2",
            description="Requirements\n- Python\n- Distributed systems\n- Cloud infrastructure",
        )
        db_session.add(job)
        db_session.add(ResumeProfileDB(
            user_id=TEST_USER_ID,
            profile_name="Test User",
            email="test@example.com",
            raw_text="Experienced software engineer with 5 years of Python...",
        ))
        db_session.commit()
        db_session.refresh(job)

        response = client.post(f"/ai/match-breakdown/{job.id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["outcome"] in {"SCORED", "SCORED_LOW_CONFIDENCE", "NEEDS_REVIEW", "GATE_FAIL"}
        assert "% match" not in str(body)


class TestCoverLetter:
    """Tests for POST /ai/cover-letter/{job_id}."""

    def test_returns_404_for_nonexistent_job(self, client):
        """Non-existent job_id should return 404."""
        response = client.post("/ai/cover-letter/9999")
        assert response.status_code == 404
        assert "Job not found" in response.json()["detail"]


class TestTailorResume:
    """Tests for POST /ai/tailor-resume/{job_id}."""

    def test_returns_404_for_nonexistent_job(self, client):
        """Non-existent job_id should return 404."""
        response = client.post("/ai/tailor-resume/9999")
        assert response.status_code == 404
        assert "Job not found" in response.json()["detail"]


class TestRemovedScoringEndpoints:
    """POST /ai/analyze-fit and /ai/batch-score were AI scoring paths (removed, D-83)."""

    def test_analyze_fit_is_gone(self, client, db_session):
        job = ScrapedJob(title="Engineer", company="TestCo", url="https://example.com/job/9",
                         description="Build things")
        db_session.add(job)
        db_session.commit()
        db_session.refresh(job)
        assert client.post(f"/ai/analyze-fit/{job.id}").status_code == 404

    def test_batch_score_is_gone(self, client):
        assert client.post("/ai/batch-score").status_code == 404
