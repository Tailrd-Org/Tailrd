"""
Pydantic schemas for scraped jobs and pending questions.
"""

import datetime
from pydantic import BaseModel, Field
from typing import Optional
from backend.db.models import JobStatus


class ScrapedJobOut(BaseModel):
    """A scraped job listing returned to the frontend."""
    id: int
    platform: str
    title: str
    company: str
    location: str
    url: str
    description: str
    easy_apply: bool
    status: JobStatus
    scraped_at: datetime.datetime
    # The requesting user's fit score, overlaid per user (services/jobs_sidebar.user_match_scores).
    # It is the engine's displayed integer only when match_outcome is "SCORED"; every other
    # outcome shows no number and leaves it 0 (DECISIONS D-86). Read match_outcome first.
    match_score: int = 0
    # The engine's band ("Strong match", "Good match", "Partial match", "Limited match"), also
    # present for SCORED_LOW_CONFIDENCE (band only, no number). None when not scored yet.
    match_band: Optional[str] = None
    # SCORED, SCORED_LOW_CONFIDENCE, GATE_FAIL or NEEDS_REVIEW ("We couldn't score this");
    # None when this job has not been scored for the user's current résumé yet.
    match_outcome: Optional[str] = None
    requirements_met: int = 0
    requirements_total: int = 0
    match_summary: str = ""
    requirements_detail: list[dict] = []
    salary_range: str = ""
    company_size: str = ""
    company_description: str = ""
    company_logo: str = ""
    company_domain: str = ""
    company_url: str = ""
    city: str = ""
    region: str = ""
    # None until the one-time backfill touches pre-existing rows.
    locations_json: Optional[list] = None
    ats_type: str = ""

    # Multi-source and match breakdown fields
    source_platform: str = "linkedin"
    saved: int = 0
    experience_score: int = 0
    skill_score: int = 0
    industry_score: int = 0
    match_label: str = ""
    applicant_count: Optional[int] = None

    # Job aggregator classification fields
    work_type: str = "onsite"
    role_category: str = ""
    country: str = ""
    experience_level: str = ""
    posted_date: Optional[datetime.datetime] = None

    # Freshness lifecycle + trust (None on rows the migration hasn't touched)
    listing_status: Optional[str] = "active"
    first_seen_at: Optional[datetime.datetime] = None
    last_seen_at: Optional[datetime.datetime] = None
    ghost_risk_score: Optional[int] = 0
    source_trust: Optional[str] = ""

    # Structured extraction
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: Optional[str] = ""
    salary_period: Optional[str] = ""
    employment_type: Optional[str] = ""
    visa_sponsorship: Optional[str] = "unknown"
    skills: Optional[list[str]] = None

    model_config = {"from_attributes": True}


class IngestJobIn(BaseModel):
    """One scraped job pushed by a scraper script (POST /jobs/ingest-batch)."""
    title: str
    company: str
    url: str = ""
    location: str = ""
    source_platform: str = "linkedin"
    experience_level: str = "new_grad"
    work_type: str = "onsite"
    country: str = "CA"
    posted_date: Optional[str] = None  # ISO date; unparseable values are dropped
    # A real logo the scraper captured from the source (e.g. LinkedIn's
    # media.licdn.com company image). Preferred over a name-guessed favicon.
    company_logo: str = ""
    company_domain: str = ""
    # The employer's own website when the source lists one (JobSpy's
    # company_url_direct, Indeed's corporateWebsite). Its domain beats the
    # name guess; LinkedIn company pages and ATS links are ignored.
    company_url: str = ""


class IngestBatchIn(BaseModel):
    """A batch of scraped jobs. Sized so one request stays cheap to dedupe."""
    jobs: list[IngestJobIn] = Field(default_factory=list, max_length=500)


class PendingQuestionOut(BaseModel):
    """A question the bot needs the user to answer."""
    id: int
    job_id: int
    question: str
    field_type: str
    options: list[str]
    answer: Optional[str] = None
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class AnswerSubmit(BaseModel):
    """User submitting an answer to a pending question."""
    answer: str
