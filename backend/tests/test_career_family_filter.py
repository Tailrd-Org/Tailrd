"""GET /jobs career_family filter, stale visibility and /jobs/stats breakdowns (scraper v2).

Isolated in-memory SQLite app, like test_job_filters_api.py; never the Neon lifespan.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.db.database import Base, get_db
from backend.db.models import ScrapedJob
from backend.main import app
from backend.services.listing_freshness import HIDDEN_LISTING_STATUSES, LISTING_STALE


@pytest.fixture
def make_client():
    opened = []

    def make(jobs):
        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        session = sessionmaker(bind=engine)()
        session.add_all(jobs)
        session.commit()

        def override_get_db():
            yield session

        app.dependency_overrides[get_db] = override_get_db
        opened.append((session, engine))
        return TestClient(app)

    yield make
    app.dependency_overrides.clear()
    for session, engine in opened:
        session.close()
        engine.dispose()


def _job(title, *, family=None, secondary=None, category="", level="new_grad",
         status="active", city="ottawa"):
    return ScrapedJob(
        title=title,
        company="Acme",
        location="Ottawa, ON",
        url=f"https://example.com/{title.replace(' ', '-')}",
        role_category=category,
        career_family=family,
        career_family_secondary=secondary,
        experience_level=level,
        listing_status=status,
        country="CA",
        city=city,
        location_search=f"|{city}|",
    )


def _titles(client, **params) -> list[str]:
    response = client.get("/jobs", params=params)
    assert response.status_code == 200
    return sorted(j["title"] for j in response.json())


# ─── career_family filter ────────────────────────────────────────────────────

def test_career_family_filter_matches_tagged_rows(make_client):
    client = make_client([
        _job("Backend Dev", family="software_engineering", category="Software Engineering"),
        _job("Paralegal", family="legal", category="Legal and Compliance"),
        _job("Nurse", family="healthcare_clinical", category="Other"),
    ])
    assert _titles(client, career_family="software_engineering") == ["Backend Dev"]
    assert _titles(client, career_family="legal,healthcare_clinical") == ["Nurse", "Paralegal"]
    # Slugs are matched case- and space-tolerantly.
    assert _titles(client, career_family=" Legal ") == ["Paralegal"]


def test_untagged_rows_answer_through_their_old_job_function(make_client):
    """Rows from before scraper v2 carry only role_category; a family filter must
    still find them, through taxonomy.LEGACY_CATEGORY_TO_FAMILIES."""
    client = make_client([
        _job("Data Analyst", category="Data Analysis"),
        _job("ML Intern", category="Machine Learning/AI"),  # a legacy spelling
        _job("Account Exec", category="Sales"),
        _job("Blank Category"),
        # A tagged row is judged by its family, never by its old job function.
        _job("Sales Engineer", family="sales_bizdev", category="Software Engineering"),
    ])
    assert _titles(client, career_family="data_ai_analytics") == ["Data Analyst", "ML Intern"]
    assert _titles(client, career_family="sales_bizdev") == ["Account Exec", "Sales Engineer"]
    assert _titles(client, career_family="software_engineering") == []


def test_a_family_with_no_old_job_function_matches_tagged_rows_only(make_client):
    client = make_client([
        _job("Nurse", family="healthcare_clinical"),
        _job("Untagged Other", category="Other"),
    ])
    assert _titles(client, career_family="healthcare_clinical") == ["Nurse"]
    assert _titles(client, career_family="other") == ["Untagged Other"]


def test_unknown_family_matches_nothing(make_client):
    client = make_client([_job("Backend Dev", family="software_engineering")])
    assert _titles(client, career_family="Software Engineering") == []
    assert _titles(client, career_family="astronaut") == []


def test_role_category_param_keeps_working_for_old_clients(make_client):
    client = make_client([
        _job("Backend Dev", family="software_engineering", category="Software Engineering"),
        _job("Old Dev", category="Software Engineering"),
        _job("Paralegal", family="legal", category="Legal and Compliance"),
    ])
    assert _titles(client, role_category="Software Engineering") == ["Backend Dev", "Old Dev"]


def test_career_family_and_experience_filters_combine(make_client):
    client = make_client([
        _job("Grad Dev", family="software_engineering", level="new_grad"),
        _job("Staff Dev", family="software_engineering", level="lead"),
        _job("Grad Lawyer", family="legal", level="new_grad"),
    ])
    assert _titles(client, career_family="software_engineering",
                   experience_level="intern_new_grad,entry") == ["Grad Dev"]


def test_job_responses_carry_the_career_families(make_client):
    client = make_client([
        _job("Data Engineer", family="data_ai_analytics", secondary="software_engineering"),
    ])
    listed = client.get("/jobs").json()[0]
    assert listed["career_family"] == "data_ai_analytics"
    assert listed["career_family_secondary"] == "software_engineering"
    detail = client.get(f"/jobs/{listed['id']}").json()
    assert detail["career_family"] == "data_ai_analytics"


# ─── stale is hidden ─────────────────────────────────────────────────────────

def test_stale_is_a_hidden_status():
    assert LISTING_STALE in HIDDEN_LISTING_STATUSES


def test_stale_rows_leave_the_feed_the_stats_and_the_city_list(make_client):
    client = make_client([
        _job("Live Role"),
        _job("Stale Role", status="stale"),
        _job("Removed Role", status="removed"),
    ])
    assert _titles(client) == ["Live Role"]
    stats = client.get("/jobs/stats").json()
    assert stats["total"] == 1
    cities = client.get("/jobs/cities", params={"country": "CA"}).json()
    assert cities == [{"city": "Ottawa", "count": 1}]


# ─── /jobs/stats breakdowns ──────────────────────────────────────────────────

def test_stats_breakdowns_count_visible_rows_by_family_and_level(make_client):
    client = make_client([
        _job("Grad Dev", family="software_engineering", category="Software Engineering",
             level="new_grad"),
        _job("Intern Dev", family="software_engineering", category="Software Engineering",
             level="internship"),
        _job("Nurse", family="healthcare_clinical", category="Other", level="mid"),
        _job("Old Analyst", category="Data Analysis", level="new_grad"),
        _job("Dead Director", family="legal", category="Legal and Compliance",
             level="director", status="removed"),
        _job("Stale Lead", family="legal", level="lead", status="stale"),
    ])
    # Every level asked for: the six filter options.
    stats = client.get(
        "/jobs/stats",
        params={"experience_level": "intern_new_grad,entry,mid,senior,lead,director"},
    ).json()
    assert stats["total"] == 4
    assert stats["by_career_family"] == {"software_engineering": 2, "healthcare_clinical": 1}
    assert stats["by_experience_level"] == {"new_grad": 2, "internship": 1, "mid": 1}
    assert stats["by_role_category"] == {"Software Engineering": 2, "Other": 1, "Data Analysis": 1}
    assert stats["by_country"] == {"CA": 4}

    # No levels given: the feed's default, so the mid-level nurse is not counted, but
    # the level breakdown still describes every live level (it is the filter's options).
    default = client.get("/jobs/stats").json()
    assert default["total"] == 3
    assert default["by_career_family"] == {"software_engineering": 2}
    assert default["by_role_category"] == {"Software Engineering": 2, "Data Analysis": 1}
    assert default["by_experience_level"] == {"new_grad": 2, "internship": 1, "mid": 1}
