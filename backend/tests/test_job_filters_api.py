"""API tests for date_posted and experience filters."""

import datetime

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.database import Base, get_db
from backend.db.models import ScrapedJob
from backend.main import app


def _client_with_jobs(jobs):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    for job in jobs:
        session.add(job)
    session.commit()

    def override_get_db():
        try:
            yield session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    return client, session, engine


def test_date_posted_24h_filter():
    now = datetime.datetime.utcnow()
    client, session, engine = _client_with_jobs(
        [
            ScrapedJob(
                title="Fresh",
                company="Acme",
                location="Remote",
                url="https://example.com/fresh",
                posted_date=now - datetime.timedelta(hours=2),
                scraped_at=now,
                experience_level="new_grad",
                country="US",
            ),
            ScrapedJob(
                title="Old",
                company="Acme",
                location="Remote",
                url="https://example.com/old",
                posted_date=now - datetime.timedelta(days=30),
                scraped_at=now - datetime.timedelta(days=30),
                experience_level="new_grad",
                country="US",
            ),
        ]
    )
    try:
        response = client.get("/jobs", params={"date_posted": "24h"})
        assert response.status_code == 200
        titles = [j["title"] for j in response.json()]
        assert titles == ["Fresh"]
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


# One row per stored experience level (scraper v2 stores all eight), titled by level.
_LEVEL_TITLES = {
    "internship": "Intern", "new_grad": "Grad", "entry": "Entry", "mid": "Mid",
    "senior": "Senior", "lead": "Lead", "director": "Director", "executive": "Chief",
}


def _catalogue():
    return [
        ScrapedJob(
            title=title,
            company="Acme",
            location="Remote",
            url=f"https://example.com/{level}",
            experience_level=level,
            country="US",
        )
        for level, title in _LEVEL_TITLES.items()
    ]


def _titles_for(experience_level: str) -> list[str]:
    client, session, engine = _client_with_jobs(_catalogue())
    try:
        response = client.get("/jobs", params={"experience_level": experience_level})
        assert response.status_code == 200
        return sorted(j["title"] for j in response.json())
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def test_experience_filter_six_options():
    """Jobright's six options, as the Jobs page sends them."""
    assert _titles_for("intern_new_grad") == ["Grad", "Intern"]
    assert _titles_for("entry") == ["Entry"]
    assert _titles_for("mid") == ["Mid"]
    assert _titles_for("senior") == ["Senior"]
    assert _titles_for("lead") == ["Lead"]
    assert _titles_for("director") == ["Chief", "Director"]
    # The default for a person who never chose: Intern/New Grad plus Entry Level.
    assert _titles_for("intern_new_grad,entry") == ["Entry", "Grad", "Intern"]


def test_experience_filter_legacy_values_still_resolve():
    """Stale clients, open tabs, and bookmarked URLs keep working."""
    assert _titles_for("internship") == ["Intern"]
    # The old "New Grad / Entry Level" option.
    assert _titles_for("new_grad") == ["Entry", "Grad"]
    assert _titles_for("internship,new_grad") == ["Entry", "Grad", "Intern"]
    assert _titles_for("executive") == ["Chief"]


def test_experience_filter_unknown_values_never_widen():
    """A value no option or legacy key knows matches nothing rather than everything."""
    assert _titles_for("expert") == []
