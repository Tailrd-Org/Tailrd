"""GET /jobs/levels and the viewer's levels on GET /jobs/stats (scraper v2 final review: C1, S4).

The Jobs page and onboarding offer only the Experience Level options that have live jobs
behind them: production keeps storing only internship and new grad rows until
CRON_ATS_V2_CATALOGUE is on, and an option with no rows is a silent dead end. The stats
count the levels the viewer's feed shows, not the whole catalogue.

Uses the shared conftest client (SQLite, authenticated as user 1).
"""

import pytest

from backend.db.models import JobStatus, ScrapedJob, User, UserSavedJob
from backend.routers import jobs as jobs_router
from backend.services.listing_freshness import HIDDEN_LISTING_STATUSES

STUDENT_VIEW = "intern_new_grad,entry"
ALL_SIX = "intern_new_grad,entry,mid,senior,lead,director"

_n = 0


def _job(db, level, **kw):
    global _n
    _n += 1
    row = ScrapedJob(
        title=kw.pop("title", f"Role {_n}"),
        company=kw.pop("company", "Acme"),
        url=f"https://example.com/levels/{_n}",
        experience_level=level,
        listing_status=kw.pop("listing_status", "active"),
        country=kw.pop("country", "CA"),
        **kw,
    )
    db.add(row)
    db.commit()
    return row


@pytest.fixture(autouse=True)
def fresh_levels_cache(monkeypatch):
    """The endpoint keeps its answer for minutes, so every test starts without one."""
    monkeypatch.setattr(jobs_router, "_levels_cache", None)


def _levels(client) -> list[str]:
    response = client.get("/jobs/levels")
    assert response.status_code == 200
    return response.json()["levels"]


# ─── GET /jobs/levels ────────────────────────────────────────────────────────────────────────

def test_the_student_catalogue_offers_only_intern_new_grad(client, db_session):
    """Production today: internship and new grad rows, so Senior and the rest are not offered."""
    for level in ("internship", "new_grad", "new_grad"):
        _job(db_session, level)
    assert _levels(client) == ["intern_new_grad"]


def test_every_option_with_rows_is_offered_in_the_filters_order(client, db_session):
    # Inserted out of order, and with executive standing in for the Director/Executive option.
    for level in ("executive", "lead", "senior", "mid", "entry", "internship"):
        _job(db_session, level)
    assert _levels(client) == ["intern_new_grad", "entry", "mid", "senior", "lead", "director"]


def test_an_option_needs_only_one_of_the_levels_it_covers(client, db_session):
    _job(db_session, "executive")
    assert _levels(client) == ["director"]


def test_an_option_with_no_rows_is_never_offered(client, db_session):
    _job(db_session, "new_grad")
    _job(db_session, "senior")
    assert _levels(client) == ["intern_new_grad", "senior"]


def test_only_rows_the_feed_would_show_back_an_option(client, db_session):
    live = _job(db_session, "new_grad")
    # Closed, stale and expired listings, and every other status the feed hides.
    for status in sorted(HIDDEN_LISTING_STATUSES):
        _job(db_session, "senior", listing_status=status)
    _job(db_session, "director", duplicate_of=live.id)   # a hidden cross-source twin
    _job(db_session, "executive", company="")            # no employer name
    _job(db_session, "entry", company="Unknown")
    _job(db_session, "")                                 # untagged: no filter option reaches it
    _job(db_session, None)
    assert _levels(client) == ["intern_new_grad"]


def test_an_empty_catalogue_offers_nothing(client):
    assert _levels(client) == []


def test_the_answer_is_kept_for_a_few_minutes_and_shared_with_the_cdn(client, db_session, monkeypatch):
    _job(db_session, "new_grad")
    first = client.get("/jobs/levels")
    assert first.json() == {"levels": ["intern_new_grad"]}
    # Public (the same for everyone), so a CDN may keep it for as long as an instance does.
    assert first.headers["cache-control"] == "public, max-age=300, s-maxage=300"

    _job(db_session, "senior")
    # A warm instance does not ask the database again within the window.
    assert _levels(client) == ["intern_new_grad"]

    # Once the window is over, the next call reads the catalogue again.
    kept = jobs_router._levels_cache
    monkeypatch.setattr(jobs_router, "_levels_cache", (0.0, kept[1]))
    assert _levels(client) == ["intern_new_grad", "senior"]


# ─── GET /jobs/stats counts the viewer's levels ──────────────────────────────────────────────

def _catalogue(db):
    """Three student-feed rows (one remote) and five above them, one of them applied."""
    _job(db, "internship", work_type="remote", career_family="software_engineering")
    _job(db, "new_grad", career_family="software_engineering")
    _job(db, "entry", career_family="legal")
    for level in ("mid", "senior", "lead", "director", "executive"):
        _job(db, level, career_family="legal")
    _job(db, "senior", status=JobStatus.APPLIED)


def test_stats_default_to_the_feeds_default_levels(client, db_session):
    _catalogue(db_session)
    stats = client.get("/jobs/stats").json()
    assert stats["total"] == 3
    assert stats["new"] == 3
    assert stats["applied"] == 0
    assert stats["by_work_type"] == {"remote": 1, "onsite": 2}
    assert stats["by_career_family"] == {"software_engineering": 2, "legal": 1}


def test_stats_follow_the_levels_passed(client, db_session):
    _catalogue(db_session)
    senior = client.get("/jobs/stats", params={"experience_level": "senior,lead"}).json()
    assert senior["total"] == 3          # two senior rows and a lead
    assert senior["applied"] == 1
    # The Director/Executive option covers both stored levels.
    assert client.get("/jobs/stats", params={"experience_level": "director"}).json()["total"] == 2
    # Intern/New Grad is internship plus new grad.
    assert client.get("/jobs/stats", params={"experience_level": "intern_new_grad"}).json()["total"] == 2
    assert client.get("/jobs/stats", params={"experience_level": ALL_SIX}).json()["total"] == 9


def test_stats_with_nothing_recognized_use_the_default_not_an_empty_count(client, db_session):
    _catalogue(db_session)
    assert client.get("/jobs/stats", params={"experience_level": "astronaut"}).json()["total"] == 3
    assert client.get("/jobs/stats", params={"experience_level": ""}).json()["total"] == 3


def test_the_level_breakdown_spans_every_level_and_the_others_follow_the_viewer(client, db_session):
    _job(db_session, "new_grad", career_family="software_engineering")
    _job(db_session, "senior", career_family="software_engineering")
    _job(db_session, "senior", career_family="legal")
    stats = client.get("/jobs/stats").json()
    # by_experience_level is the filter's own options, so it ignores the levels asked for.
    assert stats["by_experience_level"] == {"new_grad": 1, "senior": 2}
    assert stats["by_career_family"] == {"software_engineering": 1}
    senior = client.get("/jobs/stats", params={"experience_level": "senior"}).json()
    assert senior["by_experience_level"] == {"new_grad": 1, "senior": 2}
    assert senior["by_career_family"] == {"software_engineering": 1, "legal": 1}


def test_saved_count_is_everything_the_user_bookmarked(client, db_session):
    db_session.merge(User(id=1, email="u@example.com", hashed_password="x"))
    db_session.commit()
    senior = _job(db_session, "senior")
    _job(db_session, "new_grad")
    db_session.add(UserSavedJob(user_id=1, job_id=senior.id))
    db_session.commit()
    # The Liked tab badge is not narrowed by levels, as it is not by country or city.
    assert client.get("/jobs/stats").json()["saved_count"] == 1


@pytest.mark.parametrize("keys", [None, "intern_new_grad", "entry", "mid,senior", "director", ALL_SIX])
def test_the_total_is_the_size_of_the_feed_for_the_same_levels(client, db_session, keys):
    """The count and the list it describes use one rule."""
    _catalogue(db_session)
    stats = client.get("/jobs/stats", params={"experience_level": keys} if keys else None).json()
    feed = client.get(
        "/jobs", params={"experience_level": keys or STUDENT_VIEW, "page_size": 200}
    ).json()
    assert stats["total"] == len(feed)
