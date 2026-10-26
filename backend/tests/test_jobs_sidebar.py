"""
Tests for GET /jobs/sidebar and the per-user score overlay / strong filter on
GET /jobs. Isolated SQLite app (never the Neon lifespan), safe standalone.

Scores are fit-engine rows (Task 9.2): seeded under a remembered fit fingerprint
(match_fingerprints, D-107) with the band the engine gives that number (85/70/55),
and read the way every request reads them, without building the engine.
"""

import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.auth.dependencies import get_optional_user_id
from backend.db.database import Base, get_db
from backend.db.models import (
    ApplicationRecord,
    ApplicationStatus,
    AutofillReport,
    JobMatchScore,
    MatchFingerprint,
    ResumeProfileDB,
    ScrapedJob,
    User,
    UserSavedJob,
    UserSettings,
)
from backend.routers.jobs import router as jobs_router
from backend.services import fit_reads
from backend.services.jobs_sidebar import build_sidebar, display_skill

TEST_DATABASE_URL = "sqlite:///./test_jobs_sidebar.db"
engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

USER = 1
RESUME_TEXT = "Software engineer. Python, React, SQL, Docker. Built REST APIs."
NOW = datetime.datetime(2026, 10, 8, 12, 0, 0)  # a Thursday

app = FastAPI()
app.include_router(jobs_router, prefix="/jobs")


FIT_FP = "fit1:" + "c" * 59
OLD_FIT_FP = "fit1:" + "d" * 59


@pytest.fixture(autouse=True)
def cold_engine(monkeypatch):
    """Request paths read banked rows only; nothing here builds or calls the engine."""
    monkeypatch.setattr(fit_reads, "engine_ready", lambda: False)


@pytest.fixture(autouse=True)
def setup_db():
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(db):
    def _db():
        yield db

    async def _user():
        return USER

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_optional_user_id] = _user
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


_n = 0


def job(db, **kw):
    global _n
    _n += 1
    row = ScrapedJob(
        title=kw.pop("title", f"Engineer {_n}"),
        company=kw.pop("company", "Acme"),
        url=f"https://example.com/{_n}",
        scraped_at=kw.pop("scraped_at", NOW - datetime.timedelta(days=2)),
        listing_status=kw.pop("listing_status", "active"),
        # A level the feed's default covers, since the rail counts the viewer's levels.
        experience_level=kw.pop("experience_level", "new_grad"),
        **kw,
    )
    db.add(row)
    db.commit()
    return row


def resume(db, text=RESUME_TEXT):
    db.add(User(id=USER, email="u@example.com"))
    r = ResumeProfileDB(user_id=USER, name="Main CV", raw_text=text, skills=["Kubernetes"])
    db.add(r)
    db.commit()
    # What bank_scores leaves behind: the fit fingerprint for these exact inputs.
    db.add(MatchFingerprint(user_id=USER, input_key=fit_reads.input_key(r, None), fingerprint=FIT_FP))
    db.commit()
    return r


def _band(value):
    for floor, band in ((85, "Strong match"), (70, "Good match"), (55, "Partial match")):
        if value >= floor:
            return band
    return "Limited match"


def score(db, job_row, value, current=True):
    """A SCORED fit row; ``current=False`` banks it under another (stale) fingerprint."""
    db.add(JobMatchScore(
        user_id=USER, job_id=job_row.id, score=value, band=_band(value), outcome="SCORED",
        resume_fingerprint=FIT_FP if current else OLD_FIT_FP,
    ))
    db.commit()


def test_anonymous_gets_feed_and_companies_only(db):
    job(db, company="Acme")
    job(db, company="Acme", work_type="remote")
    job(db, company="Beta")
    data = build_sidebar(db, None, now=NOW)
    assert data["resume"] is None and data["progress"] is None
    assert data["feed"]["total"] == 3
    assert data["feed"]["remote"] == 1
    assert data["feed"]["strong_matches"] is None
    assert data["top_companies"][0] == {
        "company": "Acme", "count": 2, "company_logo": "", "company_domain": "", "company_url": "",
    }
    assert data["top_companies_basis"] == "open"


def test_resume_snapshot_uses_only_current_fingerprint(db):
    resume(db)
    a, b, c = job(db), job(db), job(db)
    score(db, a, 90)
    score(db, b, 70)
    score(db, c, 99, current=False)  # stale score must not count
    data = build_sidebar(db, USER, now=NOW)
    assert data["resume"]["name"] == "Main CV"
    assert data["resume"]["scored_jobs"] == 2
    assert data["resume"]["avg_match"] == 80
    assert data["resume"]["strong_matches"] == 1
    assert data["feed"]["strong_matches"] == 1
    # Taxonomy tags from the text, plus a declared skill that is in the taxonomy.
    assert {"python", "react", "sql", "docker", "kubernetes"} <= set(data["resume_skills"])


def test_skill_gaps_come_from_good_fit_jobs_and_skip_owned_skills(db):
    resume(db)
    for _ in range(3):
        j = job(db, skills=["python", "aws", "terraform"])
        score(db, j, 85)
    j = job(db, skills=["python", "aws", "go"])
    score(db, j, 75)
    j = job(db, skills=["rust", "rust"])
    score(db, j, 40)  # below the pool bar
    gaps = build_sidebar(db, USER, now=NOW)["skill_gaps"]
    assert [g["skill"] for g in gaps][:2] == ["aws", "terraform"]
    assert gaps[0]["job_count"] == 4
    assert all(g["skill"] not in {"python", "rust"} for g in gaps)
    assert all(g["job_count"] >= 2 for g in gaps)


def test_implied_skills_are_never_reported_missing(db):
    resume(db, text="Built services in PostgreSQL and Next.js.")
    for _ in range(3):
        score(db, job(db, skills=["sql", "javascript", "react", "aws"]), 90)
    data = build_sidebar(db, USER, now=NOW)
    assert {"sql", "javascript", "react"} <= set(data["resume_skills"])
    assert [g["skill"] for g in data["skill_gaps"]] == ["aws"]


def test_skill_gap_fallback_skips_untagged_rows(db):
    resume(db)
    for _ in range(3):
        job(db, skills=["python", "react", "graphql"])
    for _ in range(5):
        job(db, skills=[])  # untagged rows must not crowd out the scan
    gaps = build_sidebar(db, USER, now=NOW)["skill_gaps"]
    assert [g["skill"] for g in gaps] == ["graphql"]


def test_skill_gap_fallback_pool_for_unscored_user(db):
    resume(db)
    for _ in range(3):
        job(db, skills=["python", "react", "graphql"])
    job(db, skills=["haskell"])  # shares nothing with the resume
    gaps = build_sidebar(db, USER, now=NOW)["skill_gaps"]
    assert [g["skill"] for g in gaps] == ["graphql"]


def test_progress_counts_this_week_only(db):
    resume(db)
    monday = datetime.datetime(2026, 10, 5, 9, 0)
    db.add_all([
        ApplicationRecord(user_id=USER, company="A", role="r", applied_at=monday),
        ApplicationRecord(user_id=USER, company="B", role="r", applied_at=NOW),
        ApplicationRecord(user_id=USER, company="C", role="r", applied_at=monday - datetime.timedelta(days=1)),
        ApplicationRecord(user_id=USER, company="D", role="r", applied_at=NOW, status=ApplicationStatus.FAILED),
        ApplicationRecord(user_id=USER, company="E", role="r", applied_at=monday - datetime.timedelta(days=9),
                          status=ApplicationStatus.INTERVIEWING),
    ])
    db.commit()
    p = build_sidebar(db, USER, now=NOW)["progress"]
    assert p["applied_week"] == 2
    assert p["applied_total"] == 4
    assert p["interviews"] == 1
    assert p["week_start"] == "2026-10-05T00:00:00Z"


def test_closing_soon_saved_stale_or_old_and_not_applied(db):
    resume(db)
    stale = job(db, title="Stale", listing_status="stale")
    old = job(db, title="Old", posted_date=NOW - datetime.timedelta(days=30))
    fresh = job(db, title="Fresh", posted_date=NOW - datetime.timedelta(days=3))
    dead = job(db, title="Dead", listing_status="removed", posted_date=NOW - datetime.timedelta(days=40))
    applied = job(db, title="Applied", listing_status="stale")
    for j in (stale, old, fresh, dead, applied):
        db.add(UserSavedJob(user_id=USER, job_id=j.id))
    db.add(ApplicationRecord(user_id=USER, company="x", role="y", job_id=applied.id))
    db.commit()
    closing = build_sidebar(db, USER, now=NOW)["closing_soon"]
    assert [c["title"] for c in closing] == ["Stale", "Old"]
    assert closing[0]["reason"] == "stale"
    assert closing[1]["reason"] == "old" and closing[1]["age_days"] == 30


def test_new_since_window_is_clamped_to_two_weeks(db):
    job(db, scraped_at=NOW - datetime.timedelta(hours=3))
    job(db, scraped_at=NOW - datetime.timedelta(days=5))
    job(db, scraped_at=NOW - datetime.timedelta(days=30))
    assert build_sidebar(db, None, since=NOW - datetime.timedelta(hours=6), now=NOW)["feed"]["new_since"] == 1
    assert build_sidebar(db, None, since=NOW - datetime.timedelta(days=365), now=NOW)["feed"]["new_since"] == 2
    # No since: the last 24 hours.
    assert build_sidebar(db, None, now=NOW)["feed"]["new_since"] == 1


def test_autofill_totals_and_alert_setting(db):
    resume(db)
    db.add_all([
        AutofillReport(user_id=USER, host="a", filled=12),
        AutofillReport(user_id=USER, host="b", filled=7),
        AutofillReport(user_id=2, host="c", filled=99),
    ])
    db.commit()
    data = build_sidebar(db, USER, now=NOW)
    assert data["autofill"] == {"fields_filled": 19, "passes": 2}
    assert data["alerts_enabled"] is True  # no settings row = default on
    db.add(UserSettings(user_id=USER, match_alerts_enabled=False))
    db.commit()
    assert build_sidebar(db, USER, now=NOW)["alerts_enabled"] is False


def test_top_companies_by_good_fit_roles_when_scored(db):
    resume(db)
    for company, scores in [("Big", [50, 50, 50, 50]), ("Fit", [90, 80]), ("Mid", [75]), ("Low", [72])]:
        for v in scores:
            score(db, job(db, company=company), v)
    data = build_sidebar(db, USER, now=NOW)
    assert data["top_companies_basis"] == "matches"
    assert [c["company"] for c in data["top_companies"]] == ["Fit", "Low", "Mid"]


def test_endpoint_and_list_overlay_and_strong_filter(client, db):
    resume(db)
    a = job(db, title="Great")
    b = job(db, title="Okay")
    job(db, title="Unscored")
    score(db, a, 88)
    score(db, b, 60)

    r = client.get("/jobs/sidebar")
    assert r.status_code == 200, r.text
    assert r.json()["resume"]["strong_matches"] == 1

    listed = {j["title"]: j for j in client.get("/jobs").json()}
    assert listed["Great"]["match_score"] == 88 and listed["Okay"]["match_score"] == 60
    assert listed["Great"]["match_band"] == "Strong match" and listed["Okay"]["match_band"] == "Partial match"
    # Not reached by the sweep yet: shown as not scored (no number, no band), and the
    # feed never builds the engine to score it on the spot.
    assert listed["Unscored"]["match_score"] == 0
    assert listed["Unscored"]["match_band"] is None and listed["Unscored"]["match_outcome"] is None

    strong = client.get("/jobs", params={"strong": 1}).json()
    assert [j["title"] for j in strong] == ["Great"]


def test_sort_match_orders_by_the_users_own_score(client, db):
    resume(db)
    # Inserted oldest-to-newest so date order (Unscored, Okay, Great) is the
    # exact reverse of score order.
    great = job(db, title="Great")
    okay = job(db, title="Okay")
    job(db, title="Unscored")
    score(db, okay, 60)
    score(db, great, 88)
    titles = [j["title"] for j in client.get("/jobs", params={"sort": "match"}).json()]
    assert titles == ["Great", "Okay", "Unscored"]


def test_list_since_filter(client, db):
    job(db, title="New", scraped_at=NOW - datetime.timedelta(hours=1))
    job(db, title="Older", scraped_at=NOW - datetime.timedelta(days=3))
    since = (NOW - datetime.timedelta(hours=2)).isoformat() + "Z"
    titles = [j["title"] for j in client.get("/jobs", params={"since": since}).json()]
    assert titles == ["New"]


def test_display_skill_casing_moved_with_the_skill_tags():
    assert display_skill("postgresql") == "PostgreSQL"
    assert display_skill("machine learning") == "Machine Learning"
    assert display_skill("") == ""


# ─── The rail counts the viewer's levels (scraper v2 final review, S4) ───────────────────────


def _mixed_levels(db):
    """Two student-level jobs at Small, and a senior and a director crowd at Big."""
    job(db, company="Small", experience_level="internship", work_type="remote",
        scraped_at=NOW - datetime.timedelta(hours=1))
    job(db, company="Small", experience_level="entry")
    for _ in range(3):
        job(db, company="Big", experience_level="senior", work_type="remote",
            scraped_at=NOW - datetime.timedelta(hours=1))
    job(db, company="Big", experience_level="director")
    job(db, company="Big", experience_level="")  # untagged rows belong to no level


def test_the_feed_summary_and_employers_default_to_the_students_levels(db):
    _mixed_levels(db)
    data = build_sidebar(db, None, now=NOW)
    # Four senior or director roles and an untagged one are in the catalogue, not in this feed.
    assert data["feed"]["total"] == 2
    assert data["feed"]["remote"] == 1
    assert data["feed"]["new_since"] == 1
    assert [(c["company"], c["count"]) for c in data["top_companies"]] == [("Small", 2)]


def test_the_feed_summary_and_employers_follow_the_levels_passed(db):
    _mixed_levels(db)
    senior = build_sidebar(db, None, now=NOW, levels=["senior"])
    assert senior["feed"]["total"] == 3
    assert senior["feed"]["remote"] == 3
    assert [(c["company"], c["count"]) for c in senior["top_companies"]] == [("Big", 3)]
    # Several levels add up (these are stored levels; the endpoint expands filter keys).
    wide = build_sidebar(db, None, now=NOW, levels=["senior", "director", "executive", "internship"])
    assert wide["feed"]["total"] == 5
    assert [(c["company"], c["count"]) for c in wide["top_companies"]] == [("Big", 4), ("Small", 1)]


def test_strong_matches_count_only_the_levels(db):
    resume(db)
    student = job(db, company="Small", experience_level="new_grad")
    senior = job(db, company="Big", experience_level="senior")
    score(db, student, 90)
    score(db, senior, 95)
    data = build_sidebar(db, USER, now=NOW)
    assert data["feed"]["strong_matches"] == 1
    # The same list the "strong matches" tile opens: the viewer's levels only.
    senior_view = build_sidebar(db, USER, now=NOW, levels=["senior"])
    assert senior_view["feed"]["strong_matches"] == 1
    assert build_sidebar(db, USER, now=NOW, levels=["lead"])["feed"]["strong_matches"] == 0


def test_the_sidebar_endpoint_takes_the_levels_as_filter_keys(client, db):
    _mixed_levels(db)
    default = client.get("/jobs/sidebar").json()
    assert default["feed"]["total"] == 2
    assert [c["company"] for c in default["top_companies"]] == ["Small"]

    # The same option keys GET /jobs takes: intern_new_grad is internship plus new grad.
    keys = client.get("/jobs/sidebar", params={"experience_level": "intern_new_grad"}).json()
    assert keys["feed"]["total"] == 1
    senior = client.get("/jobs/sidebar", params={"experience_level": "senior,director"}).json()
    assert senior["feed"]["total"] == 4
    assert [c["company"] for c in senior["top_companies"]] == ["Big"]

    # Nothing recognized is no choice at all: the default, not an empty rail.
    junk = client.get("/jobs/sidebar", params={"experience_level": "astronaut"}).json()
    assert junk["feed"]["total"] == 2
