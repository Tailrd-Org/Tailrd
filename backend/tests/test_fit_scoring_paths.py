"""Every job-scoring producer and reader runs on the deterministic fit engine (Task 9.2, D-83, D-85).

What is pinned here:
* the feed overlay, ``sort=match``, ``strong=1``, the sidebar, the breakdown and the alerts read
  fit-engine rows only: old AI (sha256) and "local1:" rows are never served
  (``test_stale_fingerprints_are_never_served``);
* the feed, sidebar and job list never build the engine (about ten seconds cold): they read the
  remembered fingerprint (match_fingerprints, D-107) and bank only when the engine is built;
* the cron sweep banks in chunks of at most 500 with no LLM call of any kind, alerts only for
  SCORED Strong matches, refreshes calibration once, and reports drift (a skipped PSI is listed);
* ``/ai/match-breakdown`` is the engine's explanation and never charges the AI daily limit;
  ``/ai/batch-score`` and ``/ai/analyze-fit`` are gone;
* no scoring module calls OpenAI (a source grep).

Résumés and postings here are invented; none is user data or scraped text.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from backend.auth.dependencies import get_current_user_id, get_optional_user_id, get_verified_user_id
from backend.db.database import get_db
from backend.db.models import (
    JobMatchNotification,
    JobMatchScore,
    MatchFingerprint,
    MatchTrace,
    ResumeProfileDB,
    ScrapedJob,
    User,
)
from backend.services import fit_reads, match_notifier

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
SALT = "a-test-salt-of-at-least-16-bytes"
FIT_FP = "fit1:" + "a" * 59  # a remembered fit fingerprint (seeded without building the engine)
OTHER_FIT_FP = "fit1:" + "b" * 59

RAW_TEXT = """Jordan Example
Toronto, ON

EXPERIENCE
Software Engineering Intern, Example Corp
May 2025 - Aug 2025
- Built REST APIs in Python and Django serving 2M requests a day

SKILLS
Python, SQL, Docker
"""
DOC = {
    "location": "Toronto, ON",
    "summary": "Computer science student who builds web services.",
    "experience": [{
        "title": "Software Engineering Intern", "company": "Example Corp",
        "start_date": "May 2025", "end_date": "Aug 2025",
        "bullets": ["Built REST APIs in Python and Django serving 2M requests a day",
                    "Wrote React components in TypeScript"],
    }],
    "education": [{"school": "Example University", "degree": "BSc Computer Science",
                   "start_date": "2022", "end_date": "2026"}],
    "projects": [{"name": "Chat app", "bullets": ["Built with Node.js and PostgreSQL"]}],
    "skills": ["Python", "SQL", "Docker", "Kubernetes"],
}
SOFTWARE = ("Requirements\n- 2+ years of Python\n- SQL and Docker\n- Kubernetes\n"
            "Nice to have\n- Go, Terraform")
TORONTO = [{"city": "Toronto", "region": "ON", "region_name": "Ontario", "country": "Canada"}]


@pytest.fixture(autouse=True)
def _salt(monkeypatch):
    monkeypatch.setenv("FIT_TRACE_SALT", SALT)


@pytest.fixture
def cold(monkeypatch):
    """This process has not built the engine, and nothing on a request path may build it."""
    from backend.services import fit_engine

    def forbidden(*_a, **_k):
        raise AssertionError("a request path built the fit engine")

    monkeypatch.setattr(fit_reads, "engine_ready", lambda: False)
    monkeypatch.setattr(fit_engine, "get_engine", forbidden)
    monkeypatch.setattr(fit_engine, "build_engine", forbidden)


@pytest.fixture
def no_llm(monkeypatch):
    """Any LLM construction or call fails the test."""
    def refuse(*_a, **_k):
        raise AssertionError("an LLM was called while scoring a job")

    monkeypatch.setattr("backend.services.llm.get_llm_service", refuse)
    monkeypatch.setattr("backend.services.openai_service.OpenAIService._generate", refuse, raising=False)
    monkeypatch.setattr("backend.services.usage_limiter.enforce_llm_limits", refuse)


def _user(db, user_id=1, verified=True):
    user = User(id=user_id, email=f"u{user_id}@example.com", email_verified=verified, first_name="Jordan")
    db.add(user)
    db.commit()
    return user


def _resume(db, user_id=1, raw_text=RAW_TEXT, doc=DOC):
    profile = ResumeProfileDB(user_id=user_id, name="Main CV", raw_text=raw_text, location=doc.get("location"),
                              summary=doc.get("summary"), experience=doc.get("experience", []),
                              education=doc.get("education", []), projects=doc.get("projects", []),
                              skills=doc.get("skills", []), technologies={}, custom_sections=[])
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


_n = 0


def _job(db, title="Software Engineer, New Grad", description=SOFTWARE, **kw):
    global _n
    _n += 1
    job = ScrapedJob(title=title, company=kw.pop("company", f"Company {_n}"), url=f"https://example.com/fit/{_n}",
                     description=description, role_category=kw.pop("role_category", "Software Engineering"),
                     locations_json=kw.pop("locations_json", TORONTO), country=kw.pop("country", "CA"),
                     work_type="onsite", employment_type="full_time", experience_level="new_grad",
                     listing_status=kw.pop("listing_status", "active"),
                     scraped_at=kw.pop("scraped_at", dt.datetime.utcnow() - dt.timedelta(days=1)), **kw)
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _remember(db, profile, fingerprint=FIT_FP, user_id=1):
    """What bank_scores leaves behind: the fingerprint for these inputs."""
    key = fit_reads.input_key(profile, fit_reads.settings_for(db, user_id))
    db.merge(MatchFingerprint(user_id=user_id, input_key=key, fingerprint=fingerprint))
    db.commit()


def _bank(db, job, *, score=None, band=None, outcome="SCORED", fingerprint=FIT_FP, user_id=1, raw=None):
    db.add(JobMatchScore(user_id=user_id, job_id=job.id, score=score, band=band, outcome=outcome,
                         resume_fingerprint=fingerprint, raw_score=raw,
                         scored_at=dt.datetime.utcnow()))
    db.commit()


def _ai_fp(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _local_fp(text):
    return ("local1:" + _ai_fp(text))[:64]


@pytest.fixture
def app_client(db_session, monkeypatch):
    from backend.routers.ai import router as ai_router
    from backend.routers.apply import router as apply_router
    from backend.routers.jobs import router as jobs_router
    from backend.routers.resumes import router as resumes_router

    app = FastAPI()
    app.include_router(ai_router, prefix="/ai")
    app.include_router(jobs_router, prefix="/jobs")
    app.include_router(apply_router, prefix="/apply")
    app.include_router(resumes_router, prefix="/resumes")

    def _db():
        yield db_session

    async def _uid():
        return 1

    for dep in (get_current_user_id, get_verified_user_id, get_optional_user_id):
        app.dependency_overrides[dep] = _uid
    app.dependency_overrides[get_db] = _db
    with TestClient(app) as client:
        yield client


@pytest.fixture
def sent(monkeypatch):
    """Alert emails, captured: one list of job dicts per digest."""
    digests: list[list[dict]] = []
    monkeypatch.setattr(match_notifier.email_service, "send_job_match_alert",
                        lambda to, jobs, name=None, **_kw: digests.append(jobs) or True)
    return digests


# ─── Old rows are never shown ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_fingerprints_are_never_served(db_session, app_client, cold, sent, monkeypatch):
    """AI (sha256) and "local1:" rows, even ones claiming SCORED Strong 95, reach no reader."""
    _user(db_session)
    profile = _resume(db_session)
    _remember(db_session, profile)
    # Every job also carries the AI-era per-job values the deleted AI paths wrote on scraped_jobs.
    ai_era = dict(match_score=95, experience_score=80, skill_score=70, industry_score=60,
                  match_label="STRONG MATCH", match_summary="An AI summary of the fit.",
                  requirements_met=3, requirements_total=4,
                  requirements_detail=[{"requirement": "Python", "met": True}])
    ai_job, local_job, fit_job, old_fit_job = (_job(db_session, title=t, **ai_era) for t in
                                               ("AI row", "Local row", "Fit row", "Old fit row"))
    _bank(db_session, ai_job, score=95, band="Strong match", fingerprint=_ai_fp(RAW_TEXT))
    _bank(db_session, local_job, score=95, band="Strong match", fingerprint=_local_fp(RAW_TEXT))
    _bank(db_session, fit_job, score=88, band="Strong match")
    _bank(db_session, old_fit_job, score=95, band="Strong match", fingerprint=OTHER_FIT_FP)

    feed = {j["title"]: j for j in app_client.get("/jobs").json()}
    assert feed["Fit row"]["match_score"] == 88
    assert feed["Fit row"]["match_band"] == "Strong match"
    assert feed["Fit row"]["match_outcome"] == "SCORED"
    for title in ("AI row", "Local row", "Old fit row"):
        assert feed[title]["match_score"] == 0, title
        assert feed[title]["match_band"] is None and feed[title]["match_outcome"] is None, title
    for title, card in feed.items():
        assert card["experience_score"] == 0 and card["skill_score"] == 0, title
        assert card["industry_score"] == 0, title
        assert card["requirements_met"] == 0 and card["requirements_total"] == 0, title
        assert card["requirements_detail"] == [] and card["match_summary"] == "", title
        assert card["match_label"] == (card["match_band"] or ""), title
        assert "STRONG MATCH" not in str(card), title

    # min_score filters on the user's own engine score (SCORED only), never the AI column.
    assert [j["title"] for j in app_client.get("/jobs", params={"min_score": 80}).json()] == ["Fit row"]
    assert app_client.get("/jobs", params={"min_score": 90}).json() == []
    # The stats average is the user's SCORED engine scores.
    assert app_client.get("/jobs/stats").json()["avg_match_score"] == 88
    # The extension's apply session carries the user's engine score, never the AI column.
    for job, expected in ((ai_job, 0), (fit_job, 88)):
        session = app_client.post("/apply/initiate", json={"job_id": job.id}).json()
        assert session["match_score"] == expected, job.title

    assert [j["title"] for j in app_client.get("/jobs", params={"strong": 1}).json()] == ["Fit row"]
    assert app_client.get("/jobs", params={"sort": "match"}).json()[0]["title"] == "Fit row"

    sidebar = app_client.get("/jobs/sidebar").json()
    assert sidebar["resume"]["scored_jobs"] == 1
    assert sidebar["resume"]["strong_matches"] == 1
    assert sidebar["feed"]["strong_matches"] == 1

    # Alerts: scoring is stubbed out, so only banked rows could be sent; only the fit one is.
    from backend.services import fit_engine

    monkeypatch.setattr(fit_engine, "bank_scores", lambda *a, **k: {})
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    await match_notifier.sweep_match_alerts(db_session)
    assert [[j["title"] for j in digest] for digest in sent] == [["Fit row"]]


def test_the_breakdown_never_reads_a_stale_row(db_session, app_client, no_llm):
    """The breakdown is computed by the engine; a banked AI 95 never shows through it."""
    _user(db_session)
    _resume(db_session)
    job = _job(db_session)
    _bank(db_session, job, score=95, band="Strong match", fingerprint=_ai_fp(RAW_TEXT))
    body = app_client.post(f"/ai/match-breakdown/{job.id}").json()
    from backend.routers.ai import breakdown_from_result
    from backend.services import fit_engine

    expected = breakdown_from_result(fit_engine.explain_job(db_session, 1, db_session.get(ScrapedJob, job.id)))
    assert body == expected.model_dump(mode="json")
    assert body["outcome"] == "SCORED"


# ─── Request paths never build the engine ────────────────────────────────────────────────────


def test_the_feed_and_sidebar_never_build_the_engine(db_session, app_client, cold):
    _user(db_session)
    profile = _resume(db_session)
    _remember(db_session, profile)
    banked, unbanked = _job(db_session, title="Banked"), _job(db_session, title="Unbanked")
    _bank(db_session, banked, score=72, band="Good match")

    for params in ({}, {"sort": "match"}, {"strong": 1}):
        assert app_client.get("/jobs", params=params).status_code == 200
    assert app_client.get("/jobs/sidebar").status_code == 200
    feed = {j["title"]: j for j in app_client.get("/jobs").json()}
    assert feed["Banked"]["match_score"] == 72 and feed["Banked"]["match_band"] == "Good match"
    # Not scored yet: no number, no band, and nothing banked on the request path.
    assert feed["Unbanked"]["match_score"] == 0 and feed["Unbanked"]["match_band"] is None
    assert db_session.query(JobMatchScore).filter_by(job_id=unbanked.id).count() == 0


def test_an_edited_resume_serves_nothing_until_it_is_scored_again(db_session, app_client, cold):
    _user(db_session)
    profile = _resume(db_session)
    _remember(db_session, profile)
    job = _job(db_session, title="Banked")
    _bank(db_session, job, score=90, band="Strong match")
    assert app_client.get("/jobs").json()[0]["match_score"] == 90

    profile.raw_text = RAW_TEXT + "\nAlso: Kubernetes"
    db_session.commit()
    assert app_client.get("/jobs").json()[0]["match_score"] == 0
    assert app_client.get("/jobs", params={"strong": 1}).json() == []


def test_a_ready_engine_banks_the_page_and_remembers_the_fingerprint(db_session, app_client, no_llm):
    from backend.services import fit_engine

    fit_engine.get_engine()  # this process has built it (the sweep, an upload or a breakdown did)
    _user(db_session)
    profile = _resume(db_session)
    jobs = [_job(db_session) for _ in range(3)]

    feed = app_client.get("/jobs").json()
    rows = db_session.query(JobMatchScore).filter_by(user_id=1).all()
    assert {r.job_id for r in rows} == {j.id for j in jobs}
    assert all(r.resume_fingerprint.startswith("fit") and r.outcome for r in rows)
    assert db_session.query(MatchTrace).count() == 3
    memo = db_session.get(MatchFingerprint, 1)
    assert memo.fingerprint == rows[0].resume_fingerprint
    assert memo.input_key == fit_reads.input_key(profile, None)
    by_id = {j["id"]: j for j in feed}
    for row in rows:
        assert by_id[row.job_id]["match_outcome"] == row.outcome
        assert by_id[row.job_id]["match_score"] == (row.score or 0)


# ─── Bands, not numbers (D-85, D-86) ─────────────────────────────────────────────────────────


def test_strong_is_a_scored_strong_match_and_null_scores_sort_last(db_session, app_client, cold):
    _user(db_session)
    profile = _resume(db_session)
    _remember(db_session, profile)
    strong = _job(db_session, title="Strong")
    good = _job(db_session, title="Good")
    low_conf = _job(db_session, title="Low confidence")
    gate = _job(db_session, title="Gate fail")
    newest_unscored = _job(db_session, title="Unscored")
    _bank(db_session, strong, score=90, band="Strong match")
    _bank(db_session, good, score=75, band="Good match")
    _bank(db_session, low_conf, score=None, band="Strong match", outcome="SCORED_LOW_CONFIDENCE")
    _bank(db_session, gate, score=None, band=None, outcome="GATE_FAIL")

    assert [j["title"] for j in app_client.get("/jobs", params={"strong": 1}).json()] == ["Strong"]
    ordered = [j["title"] for j in app_client.get("/jobs", params={"sort": "match"}).json()]
    assert ordered[:2] == ["Strong", "Good"]
    assert set(ordered[2:]) == {"Low confidence", "Gate fail", "Unscored"}
    feed = {j["title"]: j for j in app_client.get("/jobs").json()}
    assert feed["Low confidence"]["match_score"] == 0
    assert feed["Low confidence"]["match_band"] == "Strong match"
    assert feed["Low confidence"]["match_outcome"] == "SCORED_LOW_CONFIDENCE"
    assert feed["Gate fail"]["match_outcome"] == "GATE_FAIL"
    assert newest_unscored.id  # unscored jobs are still listed, never hidden


def test_the_sidebar_counts_bands_and_pools_good_or_better(db_session, cold):
    from backend.services.jobs_sidebar import build_sidebar

    _user(db_session)
    profile = _resume(db_session)
    _remember(db_session, profile)
    for value, band, outcome, skills in [
        (90, "Strong match", "SCORED", ["python", "aws", "terraform"]),
        (75, "Good match", "SCORED", ["python", "aws", "terraform"]),
        (60, "Partial match", "SCORED", ["rust", "haskell"]),
        (None, "Strong match", "SCORED_LOW_CONFIDENCE", ["rust", "haskell"]),
        (None, None, "NEEDS_REVIEW", ["rust", "haskell"]),
    ]:
        _bank(db_session, _job(db_session, skills=skills, company="Acme"), score=value, band=band, outcome=outcome)
    data = build_sidebar(db_session, 1)
    assert data["resume"]["scored_jobs"] == 3          # SCORED rows only
    assert data["resume"]["avg_match"] == 75           # mean of 90, 75, 60; NULLs never averaged
    assert data["resume"]["strong_matches"] == 1
    assert data["resume"]["gap_pool_size"] == 2        # Strong + Good only
    assert {g["skill"] for g in data["skill_gaps"]} == {"aws", "terraform"}  # rust/haskell: not in the pool


# ─── The breakdown (D-83) ────────────────────────────────────────────────────────────────────


def test_the_breakdown_is_the_engine_explanation_and_costs_no_ai(db_session, app_client, no_llm):
    _user(db_session)
    _resume(db_session)
    job = _job(db_session)
    r = app_client.post(f"/ai/match-breakdown/{job.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "SCORED"
    assert body["display_score"] == body["overall_score"] and body["overall_score"] > 0
    assert body["band"] == body["match_label"]
    assert body["headline"].startswith("Fit score")
    assert body["basis"]
    items = body["strength_items"] + body["gaps"]
    assert items and all({"label", "impact", "note"} <= set(item) for item in items)
    assert body["strengths"] == [item["label"] for item in body["strength_items"]]
    assert body["weaknesses"] == [item["label"] for item in body["gaps"]]
    assert isinstance(body["to_improve"], list) and isinstance(body["notes"], list)
    assert body["interval"] is None or {"low", "high", "display"} <= set(body["interval"])
    assert "% match" not in str(body)  # the basis may say "better fit than 64% of ...", never "% match"


def test_an_unscored_breakdown_says_we_couldnt_score_this(db_session, app_client, no_llm):
    _user(db_session)
    _resume(db_session)
    job = _job(db_session, description="")
    body = app_client.post(f"/ai/match-breakdown/{job.id}").json()
    assert body["outcome"] == "NEEDS_REVIEW"
    assert body["display_score"] is None and body["overall_score"] == 0
    assert body["match_label"] == "We couldn't score this"
    assert "% match" not in str(body)


def test_the_breakdown_needs_a_resume(db_session, app_client, no_llm):
    _user(db_session)
    job = _job(db_session)
    assert app_client.post(f"/ai/match-breakdown/{job.id}").status_code == 400


def test_the_ai_scoring_endpoints_are_gone(db_session, app_client):
    _user(db_session)
    _resume(db_session)
    job = _job(db_session)
    assert app_client.post("/ai/batch-score").status_code == 404
    assert app_client.post(f"/ai/analyze-fit/{job.id}").status_code == 404


# ─── The cron sweep ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sweep_scores_with_the_engine_and_never_calls_an_llm(db_session, no_llm, sent):
    _user(db_session)
    _resume(db_session)
    jobs = [_job(db_session) for _ in range(3)]
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert summary["status"] == "completed"
    assert summary["scoring"] == "fit-engine"
    assert summary["jobs_scored"] == 3
    assert summary["fit_trace_salt_missing"] is False
    rows = db_session.query(JobMatchScore).filter_by(user_id=1).all()
    assert {r.job_id for r in rows} == {j.id for j in jobs}
    assert all(r.resume_fingerprint.startswith("fit") for r in rows)
    assert db_session.query(MatchTrace).count() == 3
    for gone in ("threshold", "ai_confirmed", "scoring_mode"):
        assert gone not in summary
    # A second run finds every score current.
    assert (await match_notifier.sweep_match_alerts(db_session))["jobs_scored"] == 0


@pytest.fixture
def fake_bank(monkeypatch):
    """bank_scores replaced: records each chunk; returns no new results."""
    from backend.services import fit_engine

    chunks: list[list[int]] = []

    def bank(db, user_id, profile, job_ids, **_k):
        chunks.append(list(job_ids))
        return {}

    monkeypatch.setattr(fit_engine, "bank_scores", bank)
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {"version": "stub"})
    return chunks


@pytest.mark.asyncio
async def test_the_sweep_alerts_only_for_scored_strong_matches(db_session, cold, fake_bank, sent, monkeypatch):
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    _user(db_session)
    profile = _resume(db_session)
    _remember(db_session, profile)
    strong, good, low_conf = (_job(db_session, title=t) for t in ("Strong", "Good", "Low confidence"))
    _bank(db_session, strong, score=90, band="Strong match")
    _bank(db_session, good, score=84, band="Good match")
    _bank(db_session, low_conf, score=None, band="Strong match", outcome="SCORED_LOW_CONFIDENCE")
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert [[j["title"] for j in digest] for digest in sent] == [["Strong"]]
    assert sent[0][0]["match_score"] == 90
    assert summary["jobs_notified"] == 1
    assert [n.job_id for n in db_session.query(JobMatchNotification).all()] == [strong.id]


@pytest.mark.asyncio
async def test_the_sweep_banks_in_chunks_of_at_most_500(db_session, cold, fake_bank, monkeypatch):
    assert match_notifier.SWEEP_CHUNK == 500
    monkeypatch.setattr(match_notifier, "SWEEP_CHUNK", 2)
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    _user(db_session)
    _resume(db_session)
    jobs = [_job(db_session) for _ in range(5)]
    await match_notifier.sweep_match_alerts(db_session)
    assert [len(c) for c in fake_bank] == [2, 2, 1]
    assert sorted(i for c in fake_bank for i in c) == sorted(j.id for j in jobs)


@pytest.mark.asyncio
async def test_a_concurrent_sweep_conflict_skips_that_chunk_only(db_session, cold, monkeypatch, caplog):
    from backend.services import fit_engine

    monkeypatch.setattr(match_notifier, "SWEEP_CHUNK", 2)
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    calls: list[list[int]] = []
    rollbacks: list[int] = []
    real_rollback = db_session.rollback

    def bank(db, user_id, profile, job_ids, **_k):
        calls.append(list(job_ids))
        if len(calls) == 1:
            raise IntegrityError("INSERT", {}, Exception("duplicate key"))
        return {}

    monkeypatch.setattr(fit_engine, "bank_scores", bank)
    monkeypatch.setattr(db_session, "rollback", lambda: rollbacks.append(1) or real_rollback())
    _user(db_session)
    _resume(db_session)
    for _ in range(4):
        _job(db_session)
    with caplog.at_level(logging.WARNING):
        summary = await match_notifier.sweep_match_alerts(db_session)
    assert len(calls) == 2  # the second chunk still ran
    assert rollbacks
    assert summary["chunks_skipped"] == 1
    assert any("IntegrityError" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_a_missing_trace_salt_stops_scoring_and_says_so(db_session, no_llm, monkeypatch, caplog):
    from backend.services import fit_engine

    monkeypatch.delenv("FIT_TRACE_SALT")
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    _user(db_session)
    _resume(db_session)
    _user(db_session, user_id=2)
    _resume(db_session, user_id=2)
    _job(db_session)
    calls = []
    real = fit_engine.bank_scores
    monkeypatch.setattr(fit_engine, "bank_scores", lambda *a, **k: calls.append(1) or real(*a, **k))
    with caplog.at_level(logging.ERROR):
        summary = await match_notifier.sweep_match_alerts(db_session)
    assert summary["fit_trace_salt_missing"] is True
    assert summary["status"] == "fit_trace_salt_missing"
    assert len(calls) == 1  # stopped after the first refusal: no second user, no fallback
    assert db_session.query(JobMatchScore).count() == 0
    assert any(rec.levelno == logging.ERROR and "FIT_TRACE_SALT" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_the_sweep_refreshes_calibration_once_and_reports_drift(db_session, cold, fake_bank, monkeypatch):
    from backend.services import fit_engine

    refreshes = []
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: refreshes.append(1) or {"version": "v"})
    monkeypatch.setattr(match_notifier, "_engine_hash", lambda: "hash")
    for user_id in (1, 2):
        _user(db_session, user_id=user_id)
        _resume(db_session, user_id=user_id)
    _job(db_session)
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert refreshes == [1]
    assert summary["calibration"] == {"version": "v"}
    drift = summary["drift"]
    assert drift["psi_reference_n"] == 0 and drift["psi_current_n"] == 0
    skipped = {s["code"]: s for s in drift["skipped"]}
    assert skipped["SCORE_DRIFT"]["reason"] == "insufficient sample"
    assert drift["alarms"] == []
    assert set(drift["not_measured"]) >= {"coverage", "four_fifths", "p95_latency"}


def test_the_drift_report_passes_both_psi_sample_sizes(db_session, monkeypatch):
    """Below 200 on either side the PSI is skipped and listed; at 200 on both it is evaluated."""
    monkeypatch.setattr(match_notifier, "_engine_hash", lambda: "hash")
    _user(db_session)
    job = _job(db_session)
    started = dt.datetime.utcnow()
    for i in range(250):  # 250 people's rows on one job (SQLite does not enforce the user FK)
        db_session.add(JobMatchScore(user_id=1000 + i, job_id=job.id, score=50, raw_score=0.5 + (i % 10) / 100,
                                     band="Limited match", outcome="SCORED", resume_fingerprint=FIT_FP,
                                     config_hash="hash", scored_at=started - dt.timedelta(days=1)))
    db_session.commit()
    few = match_notifier._drift_report(db_session, started, [0.5] * 150, {"SCORED": 150})
    assert few["psi_reference_n"] == 250 and few["psi_current_n"] == 150
    assert [s["code"] for s in few["skipped"]] == ["SCORE_DRIFT"]
    enough = match_notifier._drift_report(db_session, started, [0.9] * 250, {"SCORED": 250})
    assert enough["skipped"] == []
    assert [a["code"] for a in enough["alarms"]] == ["SCORE_DRIFT"]


@pytest.mark.asyncio
async def test_the_sweep_keeps_its_time_budget(db_session, cold, fake_bank, monkeypatch):
    monkeypatch.setattr(match_notifier, "SWEEP_CHUNK", 1)
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    monkeypatch.setenv("CRON_MATCH_BUDGET_S", "100")
    clock = iter([0.0, 0.0, 150.0, 150.0, 150.0, 150.0, 150.0, 150.0])
    monkeypatch.setattr(match_notifier, "_now", lambda: next(clock, 150.0))
    _user(db_session)
    _resume(db_session)
    for _ in range(3):
        _job(db_session)
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert len(fake_bank) == 1
    assert summary["scoring_budget_spent"] is True


# ─── Upload pre-score ────────────────────────────────────────────────────────────────────────


def test_the_upload_prescore_banks_the_newest_300_visible_jobs(db_session, monkeypatch):
    from backend.routers import resumes
    from backend.services import fit_engine

    seen = []
    monkeypatch.setattr(fit_engine, "bank_scores", lambda db, uid, profile, ids, **k: seen.append(list(ids)) or {})
    _user(db_session)
    profile = _resume(db_session)
    hidden = _job(db_session, listing_status="removed")
    jobs = [_job(db_session) for _ in range(305)]
    newest_hidden = _job(db_session, listing_status="removed")
    resumes.prescore_upload(db_session, 1, profile)
    assert resumes.UPLOAD_PRESCORE_JOBS == 300
    assert seen == [[j.id for j in reversed(jobs)][:300]]
    assert hidden.id not in seen[0] and newest_hidden.id not in seen[0]


# ─── No scoring path calls an LLM ────────────────────────────────────────────────────────────


def _backend_sources() -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in BACKEND.rglob("*.py")
            if "tests" not in path.parts and "__pycache__" not in path.parts}


def test_no_scoring_module_calls_openai():
    sources = _backend_sources()
    for path, text in sources.items():
        assert 'op="match.score"' not in text, path
        assert '"match.fit"' not in text, path
        for setting in ("MATCH_SCORING", "MATCH_AI_DAILY_PER_USER", "MATCH_AI_CONFIRM_MIN"):
            assert setting not in text, (path, setting)
    assert not (BACKEND / "services" / "local_match.py").exists()
    assert not (REPO / "prompts" / "match_job.txt").exists()
    llm_import = re.compile(r"\b(openai|openai_service|llm|get_llm_service|MatchEngine)\b")
    for module in ("fit_engine.py", "fit_reads.py", "jobs_sidebar.py", "match_notifier.py"):
        text = sources[BACKEND / "services" / module]
        imports = "\n".join(line for line in text.splitlines() if line.lstrip().startswith(("import ", "from ")))
        assert not llm_import.search(imports), module

    from backend.services.match_engine import MatchEngine
    from backend.services.openai_service import OpenAIService

    for gone in ("compute_breakdown", "analyze_fit", "queue_analysis"):
        assert not hasattr(MatchEngine, gone), gone
    for gone in ("match_job", "extract_experience_years"):
        assert not hasattr(OpenAIService, gone), gone


def test_no_user_facing_percent_match_copy_in_the_backend():
    for path, text in _backend_sources().items():
        assert "% match" not in text, path


# ─── Fix round 1 ─────────────────────────────────────────────────────────────────────────────


def test_the_sort_match_fallback_without_a_resume_is_by_date(db_session, app_client, cold):
    _user(db_session)
    old = _job(db_session, title="Old AI 95", match_score=95,
               scraped_at=dt.datetime.utcnow() - dt.timedelta(days=5))
    new = _job(db_session, title="New", scraped_at=dt.datetime.utcnow() - dt.timedelta(hours=1))
    titles = [j["title"] for j in app_client.get("/jobs", params={"sort": "match"}).json()]
    assert titles == [new.title, old.title]
    assert app_client.get("/jobs/stats").json()["avg_match_score"] is None


def test_upload_schedules_the_prescore(db_session, app_client, monkeypatch):
    from backend.routers import resumes

    calls = []

    async def no_ai(*_a, **_k):
        raise RuntimeError("no AI in this test")

    async def no_blob(*_a, **_k):
        return None

    class _LLM:
        analyze_resume = staticmethod(no_ai)

    monkeypatch.setattr(resumes, "extract_text", lambda content, name: RAW_TEXT)
    monkeypatch.setattr(resumes, "get_llm_service", lambda: _LLM())
    monkeypatch.setattr(resumes.blob_storage, "upload_resume", no_blob)
    monkeypatch.setattr(resumes, "_prescore_in_background", lambda uid, pid: calls.append((uid, pid)))
    _user(db_session)
    r = app_client.post("/resumes/upload", files={"file": ("cv.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert r.status_code == 200, r.text
    assert calls == [(1, r.json()["id"])]


def test_a_warm_feed_whose_banking_fails_still_answers_with_banked_rows(db_session, app_client, monkeypatch):
    from backend.services import fit_engine

    monkeypatch.setattr(fit_reads, "engine_ready", lambda: True)
    monkeypatch.setattr(fit_engine, "current_fingerprints", lambda profile, settings=None, **k: [FIT_FP])

    def broken(*_a, **_k):
        raise fit_engine.FitTraceSaltError("FIT_TRACE_SALT must be set")

    monkeypatch.setattr(fit_engine, "bank_scores", broken)
    _user(db_session)
    _resume(db_session)
    banked, _unbanked = _job(db_session, title="Banked"), _job(db_session, title="Unbanked")
    _bank(db_session, banked, score=77, band="Good match")
    r = app_client.get("/jobs")
    assert r.status_code == 200
    feed = {j["title"]: j for j in r.json()}
    assert feed["Banked"]["match_score"] == 77 and feed["Unbanked"]["match_outcome"] is None


def test_inline_feed_banking_is_capped_at_50_on_its_own_session(db_session, app_client, monkeypatch):
    from backend.services import fit_engine, jobs_sidebar

    assert jobs_sidebar.INLINE_BANK_LIMIT == 50
    monkeypatch.setattr(fit_reads, "engine_ready", lambda: True)
    monkeypatch.setattr(fit_engine, "current_fingerprints", lambda profile, settings=None, **k: [FIT_FP])
    seen = []

    def bank(db, user_id, profile, job_ids, **_k):
        seen.append((db is db_session, list(job_ids)))
        return {}

    monkeypatch.setattr(fit_engine, "bank_scores", bank)
    _user(db_session)
    _resume(db_session)
    for _ in range(60):
        _job(db_session)
    assert app_client.get("/jobs", params={"page_size": 60}).status_code == 200
    assert len(seen) == 1
    same_session, ids = seen[0]
    assert same_session is False  # the request's loaded rows are never expired by its commit
    assert len(ids) == 50


@pytest.mark.asyncio
async def test_an_engine_that_cannot_be_built_reports_engine_unavailable(db_session, cold, monkeypatch, caplog):
    from backend.services import fit_engine

    def unbuildable(*_a, **_k):
        raise fit_engine.FitEngineError("panel sha256 does not match MANIFEST")

    monkeypatch.setattr(fit_engine, "bank_scores", unbuildable)
    refreshes = []
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: refreshes.append(1) or {})
    _user(db_session)
    _resume(db_session)
    _job(db_session)
    with caplog.at_level(logging.ERROR):
        summary = await match_notifier.sweep_match_alerts(db_session)
    assert summary["status"] == "engine_unavailable"
    assert summary["error"] == "FitEngineError"
    assert refreshes == []


@pytest.mark.asyncio
async def test_a_salt_missing_sweep_that_scored_nothing_skips_calibration_and_drift(db_session, monkeypatch):
    from backend.services import fit_engine

    monkeypatch.delenv("FIT_TRACE_SALT")

    def forbidden(*_a, **_k):
        raise AssertionError("the engine was built for a sweep that scored nothing")

    monkeypatch.setattr(fit_engine, "refresh_calibration", forbidden)
    monkeypatch.setattr(match_notifier, "_drift_report", forbidden)
    monkeypatch.setattr(fit_reads, "engine_ready", lambda: False)
    _user(db_session)
    _resume(db_session)
    _job(db_session)
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert summary["fit_trace_salt_missing"] is True
    assert summary["calibration"] == "skipped: no scores"
    assert summary["drift"] == "skipped: no scores"


def test_a_remembered_fingerprint_from_another_month_is_not_served(db_session, app_client, cold):
    _user(db_session)
    profile = _resume(db_session)
    _remember(db_session, profile)
    job = _job(db_session, title="Banked")
    _bank(db_session, job, score=90, band="Strong match")
    this_month = fit_reads.current_as_of()
    memo = db_session.get(MatchFingerprint, 1)

    memo.as_of = this_month
    db_session.commit()
    assert app_client.get("/jobs").json()[0]["match_score"] == 90

    memo.as_of = (this_month - dt.timedelta(days=1)).replace(day=1)
    db_session.commit()
    assert app_client.get("/jobs").json()[0]["match_score"] == 0


def test_banking_remembers_the_month_it_scored_for(db_session, no_llm):
    from backend.services import fit_engine

    _user(db_session)
    profile = _resume(db_session)
    job = _job(db_session)
    fit_engine.bank_scores(db_session, 1, profile, [job.id])
    assert db_session.get(MatchFingerprint, 1).as_of == fit_engine.fit_as_of()


def test_the_migration_adds_the_fingerprint_month_to_an_existing_table():
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.pool import StaticPool

    from backend.migrations import add_fit_engine

    engine = create_engine("sqlite://", poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE match_fingerprints (user_id INTEGER PRIMARY KEY, "
                          "input_key VARCHAR(64) NOT NULL, fingerprint VARCHAR(64) NOT NULL, "
                          "updated_at DATETIME)"))
        conn.execute(text("INSERT INTO match_fingerprints VALUES (1, 'k', 'fit1:x', NULL)"))
    add_fit_engine.run_migration(engine)
    add_fit_engine.run_migration(engine)  # idempotent
    assert "as_of" in {c["name"] for c in inspect(engine).get_columns("match_fingerprints")}
    with engine.connect() as conn:
        assert conn.execute(text("SELECT fingerprint, as_of FROM match_fingerprints")).one() == ("fit1:x", None)
