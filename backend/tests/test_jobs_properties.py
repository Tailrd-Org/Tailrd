# Feature: job-dashboard-apply, Property 13, 14, 15: Jobs Router Properties
"""
Property-based tests for the Jobs router endpoints.

Property 13: Aggregate Stats Computation
Property 14: Multi-Filter Intersection
Property 15: Pagination Correctness

**Validates: Requirements 9.4, 9.5, 9.7**
"""

import math

import pytest
from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.db.database import Base, get_db
from backend.db.models import ScrapedJob, JobStatus
from backend.routers.jobs import router as jobs_router

# Create a test-specific app with only the jobs router
TEST_DATABASE_URL = "sqlite:///./test_jobs_properties.db"
test_engine = create_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

jobs_app = FastAPI()
jobs_app.include_router(jobs_router, prefix="/jobs", tags=["jobs"])


@pytest.fixture
def setup_test_db():
    """Create all tables before each test, drop after."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session(setup_test_db):
    """Yield a test DB session."""
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(db_session):
    """FastAPI test client with overridden DB dependency."""
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    jobs_app.dependency_overrides[get_db] = _override_get_db
    with TestClient(jobs_app) as c:
        yield c
    jobs_app.dependency_overrides.clear()


# --- Strategies ---

job_status_strategy = st.sampled_from([JobStatus.NEW, JobStatus.APPLIED, JobStatus.FAILED, JobStatus.SKIPPED])
source_platform_strategy = st.sampled_from(["linkedin", "github", "other"])
location_strategy = st.sampled_from(["New York", "San Francisco", "Remote", "London", "Ottawa"])
match_score_strategy = st.integers(min_value=0, max_value=100)
# The levels GET /jobs/stats counts when none are passed: the feed's default, Intern/New Grad
# (internship, new_grad) plus Entry Level. The rest are in the catalogue but not in that feed.
FEED_LEVELS = {"internship", "new_grad", "entry"}
level_strategy = st.sampled_from(["internship", "new_grad", "entry", "mid", "senior", "director"])


def job_strategy():
    """Strategy to generate job data dicts."""
    return st.fixed_dictionaries({
        "title": st.text(min_size=3, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))).filter(lambda s: s.strip() != ""),
        "company": st.text(min_size=2, max_size=30, alphabet=st.characters(whitelist_categories=("L", "N", "Z"))).filter(lambda s: s.strip() != ""),
        "status": job_status_strategy,
        "source_platform": source_platform_strategy,
        "location": location_strategy,
        "match_score": match_score_strategy,
        "experience_level": level_strategy,
    })


# --- Property 13: Aggregate Stats Computation ---

@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(jobs_data=st.lists(job_strategy(), min_size=0, max_size=15))
def test_aggregate_stats_computation(jobs_data, db_session, client):
    """
    Property 13: Aggregate Stats Computation

    For any list of jobs, the aggregate stats shall satisfy, over the jobs at the feed's
    default levels (the viewer's levels; the rest are not in their feed):
    - total == len(those jobs)
    - applied == count(status=="applied")
    - new == count(status=="new")
    - avg_match_score is the requesting user's own SCORED fit-engine average; with no
      resume it is None, whatever AI-era value scraped_jobs.match_score holds (Task 9.2, D-109)

    **Validates: Requirements 9.4**
    """
    # Insert jobs into the database
    for i, jd in enumerate(jobs_data):
        job = ScrapedJob(
            title=jd["title"],
            company=jd["company"],
            url=f"https://example.com/job/{i}/{id(jd['title'])}",
            description="Test job description",
            status=jd["status"],
            source_platform=jd["source_platform"],
            location=jd["location"],
            match_score=jd["match_score"],
            experience_level=jd["experience_level"],
        )
        db_session.add(job)
    db_session.commit()

    # Call the stats endpoint
    response = client.get("/jobs/stats")
    assert response.status_code == 200
    stats = response.json()

    # Compute expected values
    in_feed = [j for j in jobs_data if j["experience_level"] in FEED_LEVELS]
    expected_total = len(in_feed)
    expected_applied = sum(1 for j in in_feed if j["status"] == JobStatus.APPLIED)
    expected_new = sum(1 for j in in_feed if j["status"] == JobStatus.NEW)

    # The test user has no resume, so no engine score: the AI-era column is never averaged.
    expected_avg = None

    # Assert properties
    assert stats["total"] == expected_total
    assert stats["applied"] == expected_applied
    assert stats["new"] == expected_new
    assert stats["avg_match_score"] == expected_avg

    # Cleanup for next hypothesis iteration
    db_session.query(ScrapedJob).delete()
    db_session.commit()


# --- Property 14: Multi-Filter Intersection ---

@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    jobs_data=st.lists(job_strategy(), min_size=1, max_size=15),
    filter_source=st.one_of(st.none(), source_platform_strategy),
    filter_min_score=st.integers(min_value=0, max_value=100),
    filter_location=st.one_of(st.none(), location_strategy),
)
def test_multi_filter_intersection(
    jobs_data, filter_source, filter_min_score, filter_location, db_session, client
):
    """
    Property 14: Multi-Filter Intersection

    For any list of jobs and any combination of active filters (source platform,
    minimum match score, location), the filtered result shall contain exactly those
    jobs that satisfy ALL active filter conditions simultaneously.

    **Validates: Requirements 9.5**
    """
    # Insert jobs into the database
    inserted_jobs = []
    for i, jd in enumerate(jobs_data):
        job = ScrapedJob(
            title=jd["title"],
            company=jd["company"],
            url=f"https://example.com/job/{i}/{id(jd['title'])}",
            description="Test job description",
            status=jd["status"],
            source_platform=jd["source_platform"],
            location=jd["location"],
            match_score=jd["match_score"],
        )
        db_session.add(job)
        inserted_jobs.append(jd)
    db_session.commit()

    # Build query params
    params = {"page_size": 200}  # large enough to get all results
    if filter_source is not None:
        params["source"] = filter_source
    if filter_min_score > 0:
        params["min_score"] = filter_min_score
    if filter_location is not None:
        params["location"] = filter_location

    # Call the list endpoint with filters
    response = client.get("/jobs", params=params)
    assert response.status_code == 200
    result_jobs = response.json()

    # Compute expected filtered set
    expected = []
    for jd in inserted_jobs:
        # min_score filters on the user's own SCORED engine score (Task 9.2, D-109). The
        # test user has no resume, so any min_score > 0 keeps nothing; the AI-era
        # scraped_jobs.match_score is never read.
        if filter_min_score > 0:
            continue
        # source filter
        if filter_source is not None and jd["source_platform"] != filter_source:
            continue
        # location filter (ilike %location%)
        if filter_location is not None and filter_location.lower() not in jd["location"].lower():
            continue
        expected.append(jd)

    # Assert the count matches
    assert len(result_jobs) == len(expected)

    # Cleanup for next hypothesis iteration
    db_session.query(ScrapedJob).delete()
    db_session.commit()


# --- Property 15: Pagination Correctness ---

@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    num_jobs=st.integers(min_value=0, max_value=30),
    page_size=st.integers(min_value=1, max_value=10),
)
def test_pagination_correctness(num_jobs, page_size, db_session, client):
    """
    Property 15: Pagination Correctness

    For any total job count N and page size P (P > 0):
    - Requesting page K shall return at most P items
    - The total number of pages shall equal ceil(N/P)
    - The union of all pages shall equal the full sorted list with no duplicates or omissions

    **Validates: Requirements 9.7**
    """
    # Insert N jobs with distinct match scores for deterministic ordering
    for i in range(num_jobs):
        job = ScrapedJob(
            title=f"Job {i}",
            company=f"Company {i}",
            url=f"https://example.com/pagination/{i}",
            description="Test",
            match_score=num_jobs - i,  # descending scores: num_jobs, num_jobs-1, ..., 1
        )
        db_session.add(job)
    db_session.commit()

    # Calculate expected total pages
    if num_jobs == 0:
        expected_total_pages = 0
    else:
        expected_total_pages = math.ceil(num_jobs / page_size)

    # Fetch all pages and collect results
    all_job_ids = []
    for page_num in range(1, expected_total_pages + 1):
        response = client.get("/jobs", params={"page": page_num, "page_size": page_size})
        assert response.status_code == 200
        page_jobs = response.json()

        # Each page returns at most page_size items
        assert len(page_jobs) <= page_size

        # Collect IDs
        for j in page_jobs:
            all_job_ids.append(j["id"])

    # If no jobs, verify empty response
    if num_jobs == 0:
        response = client.get("/jobs", params={"page": 1, "page_size": page_size})
        assert response.status_code == 200
        assert response.json() == []
    else:
        # Union of all pages equals full list (no duplicates, no omissions)
        assert len(all_job_ids) == num_jobs
        assert len(set(all_job_ids)) == num_jobs  # no duplicates

    # Verify requesting a page beyond total returns empty
    if expected_total_pages > 0:
        response = client.get("/jobs", params={"page": expected_total_pages + 1, "page_size": page_size})
        assert response.status_code == 200
        assert response.json() == []

    # Cleanup for next hypothesis iteration
    db_session.query(ScrapedJob).delete()
    db_session.commit()
