"""GitHub-list rows and ingest-batch (LinkedIn/Indeed) rows carry the scraper v2
career family tags, and keep the experience level their own path decides: the
lists are student lists, so taxonomy.classify_experience does not apply."""

import pytest

import backend.auth.dependencies as auth_deps
from backend.db.models import GitHubSource, ScrapedJob
from backend.ingest import taxonomy
from backend.services.aggregator import AggregatorService
from backend.services.markdown_parser import ParsedJob

SECRET = "test-cron-secret"


def _source(db, repo="New-Grad-2027"):
    source = GitHubSource(repo_url=f"https://github.com/vanshb03/{repo}", repo_owner="vanshb03",
                          repo_name=repo, file_path="README.md", role_category="Software Engineering",
                          experience_level="new_grad", status="active")
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _store(db, source, title, n):
    url = f"https://jobs.lever.co/acme/{n}"
    job = ParsedJob(title=title, company="Acme", location="Toronto, ON, Canada", url=url)
    assert AggregatorService(db)._classify_and_store(job, source) is True
    return db.query(ScrapedJob).filter_by(url=url).one()


# ─── GitHub lists ────────────────────────────────────────────────────────────

def test_a_list_row_gets_the_family_tags(db_session):
    row = _store(db_session, _source(db_session), "Software Engineer (Backend) - Machine Learning Platform", 1)

    assert (row.career_family, row.career_family_secondary) == ("software_engineering", "data_ai_analytics")
    assert row.enrichment_version == taxonomy.ENRICHMENT_VERSION


def test_a_list_row_without_a_secondary_stores_none(db_session):
    row = _store(db_session, _source(db_session), "Registered Nurse Intern", 2)

    assert row.career_family == "healthcare_clinical" and row.career_family_secondary is None
    assert row.enrichment_version == taxonomy.ENRICHMENT_VERSION


def test_a_list_row_keeps_the_lists_own_level(db_session):
    # taxonomy would call a bare "Analyst" entry; the list says new_grad, and
    # an internship repo says internship whatever the title.
    assert taxonomy.classify_experience("Analyst") == "entry"
    new_grad = _store(db_session, _source(db_session), "Analyst", 3)
    internship = _store(db_session, _source(db_session, "Summer2027-Internships"), "Analyst", 4)

    assert new_grad.experience_level == "new_grad"
    assert internship.experience_level == "internship"
    assert new_grad.career_family == internship.career_family == "data_ai_analytics"


def test_a_remembered_dead_list_row_is_tagged_too(db_session):
    source = _source(db_session)
    url = "https://jobs.lever.co/acme/5"
    job = ParsedJob(title="Paralegal", company="Acme", location="Toronto, ON, Canada", url=url)

    assert AggregatorService(db_session)._classify_and_store(job, source, dead_urls={url}) is False
    row = db_session.query(ScrapedJob).filter_by(url=url).one()
    assert row.listing_status == "removed" and row.career_family == "legal"


# ─── ingest-batch (LinkedIn / Indeed) ────────────────────────────────────────

def _job(title, n, platform="linkedin", level="new_grad"):
    return {"title": title, "company": "Clinic", "location": "Toronto, ON, CA",
            "url": f"https://example.com/jobs/{n}", "source_platform": platform,
            "work_type": "onsite", "country": "CA", "experience_level": level}


def _ingest(client, db_session, monkeypatch, jobs):
    monkeypatch.setattr(auth_deps, "CRON_SECRET", SECRET)
    res = client.post("/jobs/ingest-batch", json={"jobs": jobs}, headers={"x-cron-secret": SECRET})
    assert res.status_code == 200, res.text
    return res.json(), {row.url: row for row in db_session.query(ScrapedJob).all()}


def test_ingest_batch_rows_get_the_family_tags(client, db_session, monkeypatch):
    body, rows = _ingest(client, db_session, monkeypatch, [
        _job("Registered Nurse", 1),
        _job("Software Engineer (Backend) - Machine Learning Platform", 2, platform="indeed"),
    ])

    assert body["created"] == 2
    nurse, engineer = rows["https://example.com/jobs/1"], rows["https://example.com/jobs/2"]
    assert (nurse.career_family, nurse.career_family_secondary) == ("healthcare_clinical", None)
    assert (engineer.career_family, engineer.career_family_secondary) == (
        "software_engineering", "data_ai_analytics")
    assert nurse.enrichment_version == engineer.enrichment_version == taxonomy.ENRICHMENT_VERSION


def test_ingest_batch_keeps_the_level_the_scraper_sent(client, db_session, monkeypatch):
    # taxonomy would call a Registered Nurse mid; the scraper's search says
    # entry level, so the sent value stands.
    assert taxonomy.classify_experience("Registered Nurse") == "mid"
    _body, rows = _ingest(client, db_session, monkeypatch, [
        _job("Registered Nurse", 3, level="new_grad"),
        _job("Registered Nurse", 4, level="internship"),
    ])

    assert rows["https://example.com/jobs/3"].experience_level == "new_grad"
    assert rows["https://example.com/jobs/4"].experience_level == "internship"


@pytest.mark.parametrize("title", ["Associate Dean", "Dean of Students"])
def test_ingest_batch_skips_a_dean_as_senior(client, db_session, monkeypatch, title):
    body, rows = _ingest(client, db_session, monkeypatch, [_job(title, 5)])

    assert (body["created"], body["senior_skipped"]) == (0, 1) and not rows
