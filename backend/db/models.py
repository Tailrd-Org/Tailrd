"""
SQLAlchemy ORM models for the Auto Apply Bot.

Models:
    - User: authenticated user
    - ScrapedJob: jobs found by the scraper, shown on dashboard
    - ApplicationRecord: tracked job applications
    - UserSettings: all client config (creds, profile, filters, prefilled answers)
    - BotRun: persisted bot execution logs
    - PendingQuestion: questions the bot couldn't answer during apply
    - ResumeProfileDB: parsed resume data
"""

import enum
import datetime
from sqlalchemy import (
    Boolean, Column, Integer, String, Text, DateTime, Enum, JSON, Float,
    ForeignKey, LargeBinary, UniqueConstraint, func, true,
)
from sqlalchemy.orm import deferred
from backend.db.database import Base


# ─── User ────────────────────────────────────────────────────────────────────

class User(Base):
    """Authenticated user. Links all user-specific data."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, nullable=False, index=True)
    hashed_password = Column(String, nullable=True)  # nullable for OAuth-only users
    auth_provider = Column(String, default="local")  # "local" or "google"
    first_name = Column(String, default="")
    last_name = Column(String, default="")
    profile_image_url = Column(String, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)

    # Email verification fields
    email_verified = Column(Boolean, default=False, nullable=False)
    verification_token = Column(String(255), nullable=True, default=None)
    verification_token_expires_at = Column(DateTime, nullable=True, default=None)

    # Admin role
    is_admin = Column(Boolean, default=False, nullable=False)

    # Account lockout fields
    failed_login_attempts = Column(Integer, default=0, nullable=False)
    locked_until = Column(DateTime, nullable=True)
    last_failed_login_at = Column(DateTime, nullable=True)

    # --- Onboarding ---
    has_completed_onboarding = Column(Boolean, default=False, nullable=False)
    has_completed_setup = Column(Boolean, default=False, nullable=False)


# ─── Enums ───────────────────────────────────────────────────────────────────

class JobStatus(str, enum.Enum):
    """Status of a scraped job listing."""
    NEW = "new"
    APPLYING = "applying"
    WAITING_ANSWER = "waiting_answer"
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"


class ApplicationStatus(str, enum.Enum):
    """Possible states for a job application."""
    APPLIED = "applied"
    FAILED = "failed"
    SKIPPED = "skipped"
    INTERVIEWING = "interviewing"
    REJECTED = "rejected"
    OFFER = "offer"


# ─── Jobs ────────────────────────────────────────────────────────────────────

class ScrapedJob(Base):
    """A job listing found by the scraper. Shown on dashboard for user to act on."""
    __tablename__ = "scraped_jobs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    platform = Column(String, default="linkedin")
    title = Column(String, nullable=False)
    company = Column(String, nullable=False)
    location = Column(String, default="")
    url = Column(String, nullable=False, unique=True)
    description = Column(Text, default="")
    easy_apply = Column(Integer, default=1)
    status = Column(Enum(JobStatus, values_callable=lambda x: [e.value for e in x]), default=JobStatus.NEW)
    scraped_at = Column(DateTime, default=datetime.datetime.utcnow)
    posted_date = Column(DateTime, nullable=True)

    # AI match analysis
    match_score = Column(Integer, default=0)
    requirements_met = Column(Integer, default=0)
    requirements_total = Column(Integer, default=0)
    match_summary = Column(Text, default="")
    requirements_detail = Column(JSON, default=list)
    salary_range = Column(String, default="")
    company_size = Column(String, default="")
    company_description = Column(Text, default="")
    company_logo = Column(String, default="")
    ats_type = Column(String, default="")

    # Smart filter fields
    experience_years_required = Column(Integer, nullable=True)
    skip_reason = Column(String, default="")

    # Multi-source tracking & dashboard enhancements
    source_platform = Column(String, default="linkedin")
    saved = Column(Integer, default=0)
    experience_score = Column(Integer, default=0)
    skill_score = Column(Integer, default=0)
    industry_score = Column(Integer, default=0)
    match_label = Column(String, default="")
    applicant_count = Column(Integer, nullable=True)
    github_source_id = Column(Integer, nullable=True)
    last_viewed_at = Column(DateTime, nullable=True)

    # Job aggregator classification fields
    work_type = Column(String, default="onsite")
    role_category = Column(String, default="")
    country = Column(String, default="")
    experience_level = Column(String, default="")

    # Logo accuracy: the resolved company website domain (e.g. "stripe.com")
    # and the raw company website URL captured from the source listing.
    company_domain = Column(String, default="")
    company_url = Column(String, default="")

    # Structured location (services/location_parser.py) + description pipeline
    city = Column(String, default="")            # folded primary city ("ottawa")
    region = Column(String, default="")          # 2-letter code ("ON")
    locations_json = Column(JSON, default=list)  # [{city,region,region_name,country}]
    location_search = Column(Text, default="")   # "|ottawa|on|ontario|canada|"
    desc_fetch_attempts = Column(Integer, default=0)
    description_sections = Column(JSON, nullable=True)

    # Cross-source dedup: folded title for twin lookups; when this row is a
    # hidden duplicate, the id of the surviving (direct/ATS) row.
    title_norm = Column(String, default="", index=True)
    duplicate_of = Column(Integer, nullable=True)

    # --- Freshness lifecycle (services/listing_freshness.py) ---
    # `status` above is the USER's workflow state (new/applied/…); this is the
    # LISTING's lifecycle: active | stale | removed | expired. Soft states only,
    # rows are never deleted (saved-job/application records reference them).
    listing_status = Column(String, default="active", index=True)
    listing_status_changed_at = Column(DateTime, nullable=True)
    first_seen_at = Column(DateTime, nullable=True)
    # Positive evidence only: a board listed the row, or the platform's own
    # API said the posting is open.
    last_seen_at = Column(DateTime, nullable=True, index=True)
    # When the freshness verifier last checked the posting, whatever it
    # learned. Drives the least-recently-probed-first rotation; never read as
    # a confirmation.
    last_probed_at = Column(DateTime, nullable=True, index=True)
    # "{platform}:{slug}" of the board this row was scraped from. Lets a board
    # re-crawl reconcile exactly its own rows (mark-removed / revive) without
    # URL-pattern guessing. Empty for aggregator rows and legacy ATS rows until
    # the freshness cron backfills them from URL shape.
    board_key = Column(String, default="", index=True)
    # "{platform}:{slug}:{source's own id}", stable across URL format changes,
    # so a re-crawl updates in place even when the apply URL shifts shape.
    external_id = Column(String, default="", index=True)

    # Change detection: hash of (title|location|description|salary text). A
    # differing hash on re-crawl appends to change_log (capped) and bumps
    # edit_count, edit frequency and salary-removal are trust signals.
    raw_hash = Column(String, default="")
    edit_count = Column(Integer, default=0)
    change_log = Column(JSON, nullable=True)

    # Ghost-job heuristic (0-100) + the factors that produced it. Surfaced, not
    # silently filtered, the product decides whether to hide or badge.
    ghost_risk_score = Column(Integer, default=0)
    ghost_risk_factors = Column(JSON, nullable=True)

    # Source trust tier: high (employer's own board), medium (curated lists),
    # low (aggregators). Aggregator copies never become the canonical row.
    source_trust = Column(String, default="")

    # --- Structured extraction (services/structured_extraction.py) ---
    salary_min = Column(Integer, nullable=True)
    salary_max = Column(Integer, nullable=True)
    salary_currency = Column(String, default="")
    salary_period = Column(String, default="")  # year | month | week | day | hour
    employment_type = Column(String, default="")  # full_time | part_time | contract | internship
    visa_sponsorship = Column(String, default="unknown")  # yes | no | unknown
    skills = Column(JSON, nullable=True)  # extracted skill tags, capped list
    # The fit engine's posting cache (fit_engine.postings_for, D-84):
    # {"v": <version>, "dl": <description length>, "posting": {...}}. NULL or a
    # legacy local-scorer entry = parse on next read. migrations/add_job_match_terms.py
    match_terms = Column(JSON, nullable=True)


class PendingQuestion(Base):
    """A question the bot got stuck on during an application. User must answer."""
    __tablename__ = "pending_questions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    job_id = Column(Integer, nullable=False)
    task_id = Column(String, nullable=True)
    question = Column(Text, nullable=False)
    field_type = Column(String, default="text")
    options = Column(JSON, default=list)
    answer = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ResumeProfileDB(Base):
    """Stores a parsed resume profile in the database."""
    __tablename__ = "resume_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    name = Column(String, default="Untitled Resume")
    target_job_title = Column(String, nullable=True)
    is_primary = Column(Integer, default=0)
    status = Column(String, default="analyzed")

    # Personal info
    profile_name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    location = Column(String, nullable=True)
    linkedin_url = Column(String, nullable=True)
    github_url = Column(String, nullable=True)
    other_link = Column(String, nullable=True)

    # Structured sections (JSON)
    summary = Column(Text, nullable=True)
    summary_title = Column(String, nullable=True)
    skills = Column(JSON, default=list)
    experience = Column(JSON, default=list)
    education = Column(JSON, default=list)
    projects = Column(JSON, default=list)
    technologies = Column(JSON, default=dict)
    # Everything a resume can have that isn't one of the sections above:
    # certifications, awards, volunteering, publications, languages…
    custom_sections = Column(JSON, default=list)
    # The order the sections appeared in the uploaded file.
    section_order = Column(JSON, default=list)

    # Raw text and analysis
    raw_text = Column(Text, nullable=True)
    analysis_report = Column(JSON, nullable=True)

    # Original uploaded file, stored in Vercel Blob. Enables the Chrome
    # extension to auto-upload the real PDF/DOCX into ATS application forms.
    file_blob_url = Column(Text, nullable=True)
    file_name = Column(String, nullable=True)
    file_content_type = Column(String, nullable=True)
    file_size = Column(Integer, nullable=True)
    file_uploaded_at = Column(DateTime, nullable=True)
    # When the structured content was last edited AFTER upload (web-app editor).
    # Set → the stored original no longer matches what the user sees, so the
    # file download renders the current document instead of streaming the blob.
    content_updated_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)


class JobMatchNotification(Base):
    """Records that we emailed a user about a high-scoring job match.

    One row per (user, job). Used to dedupe alert emails so a user is never
    notified twice about the same job, whether the match was found during a
    resume upload or by the recurring cron sweep.
    """
    __tablename__ = "job_match_notifications"

    id = Column(Integer, primary_key=True, index=True)
    # A notification is a receipt, not a record worth keeping past the thing it
    # points at: without the cascades, deleting a user failed on this table
    # alone (every other user-owned table cascades), and so did deleting any job
    # a user had already been alerted about.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("scraped_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    match_score = Column(Integer, default=0)
    sent_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_job_match_notification_user_job"),
    )


class JobMatchScore(Base):
    """Memoizes what the LLM said about a (user, job) pair.

    The cron sweep scores jobs on a schedule, so every re-score is money spent
    with nobody using the app. A notification row only exists for jobs that beat
    the alert threshold *and* got emailed, which left every below-threshold job
    looking unscored forever, so the sweep bought the same answers again on
    every run. This table is the receipt for the score itself, whatever it was.

    ``resume_fingerprint`` hashes the resume text the score was computed from,
    so a user who uploads a new resume gets re-scored instead of served a stale
    answer. One row per (user, job): a new fingerprint overwrites in place.

    Fit engine rows (services/fit_engine.py, fingerprint "fit<major>:..."):
    ``score`` is the calibrated integer used for sorting, and it is NULL when
    the engine showed no number (GATE_FAIL, NEEDS_REVIEW, and a low-confidence
    band). A NULL score is "not scored", never a low score: match-sorted
    queries put it after every scored row (fit_engine.score_order) and no
    count, alert or "strong" filter treats it as low or as a match
    (DECISIONS D-86). The other engine columns are the audit record's
    headline; the full trace lives in match_traces, keyed by an opaque id.
    Added by migrations/add_fit_engine.py.
    """
    __tablename__ = "job_match_scores"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("scraped_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    score = Column(Integer, nullable=True)
    resume_fingerprint = Column(String(64), nullable=False)
    scored_at = Column(DateTime, default=datetime.datetime.utcnow)
    raw_score = Column(Float, nullable=True)
    band = Column(String(20), nullable=True)
    outcome = Column(String(30), nullable=True)
    confidence = Column(Float, nullable=True)
    engine_version = Column(String(20), nullable=True)
    config_hash = Column(String(20), nullable=True)
    # The posting the score was computed from: the posting cache version and the
    # description length ("pv1-...:1234"). A row is current only when this and the
    # fingerprint both match; NULL (an INTERNAL_ERROR result) is never current.
    posting_key = Column(String(40), nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "job_id", name="uq_job_match_score_user_job"),
    )


class MatchTrace(Base):
    """The audit trace of one fit-engine score (match-engine/docs/compliance.md section 3).

    Keyed by ``subject_id`` = ``matcher.audit.opaque_id(str(user_id), FIT_TRACE_SALT)``,
    NEVER by user_id: demographic data may only ever be joined on the opaque id
    (SPEC section 9, DECISIONS D-91). Written beside every banked score; never
    selected by feed queries (it is the heavy column, and nothing a person sees
    reads it). One row per (subject, job), overwritten when the score is.
    Account deletion recomputes the subject id to delete a user's traces
    (fit_engine.delete_user_traces). The trace holds no free text (D-14).
    """
    __tablename__ = "match_traces"

    id = Column(Integer, primary_key=True, index=True)
    subject_id = Column(String(24), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("scraped_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    trace = Column(JSON, nullable=False)
    trace_digest = Column(String(16), nullable=False)
    engine_version = Column(String(20), nullable=True)
    config_hash = Column(String(20), nullable=True)
    scored_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("subject_id", "job_id", name="uq_match_trace_subject_job"),
    )


class MatchCalibration(Base):
    """Live calibration references built from banked raw scores (DECISIONS D-50).

    One row per (scope, key): scope "family" (key = the job's role_category)
    or "global" (key "*"). ``n_candidates`` counts DISTINCT résumé
    fingerprints, never (candidate, posting) pairs; a group under
    ``calibration.min_reference_n`` (30) is stored but ignored by the
    calibrator, so the shipped panel stays in charge. ``version`` names the
    engine config hash the scores came from; rows of another hash are ignored.
    Rebuilt by fit_engine.refresh_calibration.
    """
    __tablename__ = "match_calibration"

    id = Column(Integer, primary_key=True, index=True)
    scope = Column(String(20), nullable=False)
    key = Column(String(200), nullable=False)
    n_candidates = Column(Integer, nullable=False, default=0)
    n_pairs = Column(Integer, nullable=False, default=0)
    samples = Column(JSON, nullable=False, default=list)
    version = Column(String(200), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("scope", "key", name="uq_match_calibration_scope_key"),
    )


class MatchFingerprint(Base):
    """The fit fingerprint a user's newest banked scores carry, keyed by the inputs it came from.

    A fit fingerprint (fit_engine.fingerprint_for) needs the built engine (about ten seconds
    cold), which the feed, the sidebar and the job list never build. They read this row instead
    (fit_reads.serving_fingerprints): when ``input_key`` (a sha256 of the stored résumé and the
    saved answers, computed without the engine) still matches the user's current inputs, the
    rows banked under ``fingerprint`` are current for them. An edited résumé or answer changes
    the key, so its old scores stop being served at once. Written by fit_engine.bank_scores.
    One row per user (DECISIONS D-107).
    """
    __tablename__ = "match_fingerprints"

    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    input_key = Column(String(64), nullable=False)
    fingerprint = Column(String(64), nullable=False)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow)


class ApplicationRecord(Base):
    """Tracks every job application the bot makes or the user logs."""
    __tablename__ = "application_records"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    platform = Column(String, nullable=False, default="linkedin")
    company = Column(String, nullable=False)
    role = Column(String, nullable=False)
    url = Column(String, nullable=True)
    status = Column(Enum(ApplicationStatus, values_callable=lambda x: [e.value for e in x]), default=ApplicationStatus.APPLIED)
    applied_at = Column(DateTime, default=datetime.datetime.utcnow)
    notes = Column(Text, nullable=True)
    resume_version = Column(String, default="original")
    job_id = Column(Integer, nullable=True)

    # Enhanced tracking fields
    screenshot_path = Column(String, default="")
    failure_screenshot_path = Column(String, default="")
    cover_letter_text = Column(Text, default="")
    questions_answered = Column(JSON, default=list)
    ats_type = Column(String, default="")


class UserSettings(Base):
    """All client-configurable settings stored in DB."""
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, unique=True, index=True)

    # LinkedIn credentials
    linkedin_email = Column(String, default="")
    linkedin_password_encrypted = Column(String, default="")

    # Personal info for form filling
    first_name = Column(String, default="")
    last_name = Column(String, default="")
    email = Column(String, default="")
    phone = Column(String, default="")
    city = Column(String, default="")
    linkedin_url = Column(String, default="")
    website = Column(String, default="")
    # GitHub profile URL. Used to live only on the resume row (parsed out of the
    # uploaded file), so an edit made in the web app or the extension had
    # nowhere to persist, see the 2026-08-09 profile-parity contract.
    github_url = Column(String, default="")

    # Structured mailing address (autofill v2.1). All optional.
    street_address = Column(String, default="")
    # Mailing-address city. ``city`` above backs the free-form ``location``
    # field; the two used to share one column, so a PUT carrying both silently
    # dropped one. Blank here falls back to ``city`` on read.
    address_city = Column(String, default="")
    address_state = Column(String, default="")
    postal_code = Column(String, default="")
    country = Column(String, default="")

    # EEO / demographic self-identification (autofill v2.1). Only filled by the
    # extension when the user explicitly enables "Fill EEO fields". Empty string
    # means "not provided" (the user's own "Prefer not to say" is stored as-is).
    eeo_gender = Column(String, default="")
    eeo_race = Column(String, default="")
    eeo_hispanic = Column(String, default="")
    eeo_veteran = Column(String, default="")
    eeo_disability = Column(String, default="")
    # Second wave of demographics (2026-08-09 profile-parity contract). The
    # first two were already declared in the extension's ``EeoAnswers`` type but
    # had no column to persist into; pronouns are new.
    eeo_gender_identity = Column(String, default="")
    eeo_pronouns = Column(String, default="")
    eeo_sexual_orientation = Column(String, default="")

    # Diagnostic autofill capture: when ON, this account's fills also store the
    # answers written and a sanitised snapshot of the employer's form markup
    # (see AutofillFieldCapture). OFF for everybody by default, which is what
    # keeps "telemetry never stores your answers" true for every other account.
    diagnostic_capture = Column(Boolean, default=False, nullable=False)

    # Match-alert emails (services/match_notifier). ON by default, which keeps
    # the behaviour every account has had since alerts shipped; switched off
    # by the unsubscribe link in each alert or the Settings toggle. An account
    # with no settings row is opted in. Added by
    # migrations/add_match_alerts_opt_out.py.
    match_alerts_enabled = Column(Boolean, default=True, nullable=False, server_default=true())

    # Resume file path
    resume_file_path = Column(String, default="")

    # LinkedIn session cookies
    linkedin_cookies = Column(Text, default="")

    # Bot preferences
    job_title = Column(String, default="Software Engineer")
    location = Column(String, default="United States")
    remote_only = Column(Integer, default=0)
    max_applications_per_run = Column(Integer, default=25)

    # Filters
    experience_levels = Column(String, default="")
    work_type = Column(String, default="")
    regions = Column(String, default="")

    # Prefilled answers
    prefilled_answers = Column(JSON, default=dict)

    # Smart filter settings
    company_blacklist = Column(JSON, default=list)
    keyword_blacklist = Column(JSON, default=list)
    min_salary = Column(Integer, nullable=True)
    max_salary = Column(Integer, nullable=True)
    min_experience_years = Column(Integer, nullable=True)
    max_experience_years = Column(Integer, nullable=True)

    # Autopilot settings
    autopilot_enabled = Column(Integer, default=0)
    daily_apply_limit = Column(Integer, default=50)
    weekly_apply_limit = Column(Integer, default=200)
    apply_delay_min = Column(Float, default=30.0)
    apply_delay_max = Column(Float, default=120.0)

    # UX toggles
    pause_before_submit = Column(Integer, default=0)
    follow_companies = Column(Integer, default=0)
    smooth_scrolling = Column(Integer, default=0)

    # HR outreach
    hr_outreach_enabled = Column(Integer, default=0)
    hr_daily_connect_limit = Column(Integer, default=10)

    # AI features
    resume_tailoring_enabled = Column(Integer, default=0)

    # Sync versioning: bumped on any profile / resume / cover-letter change so
    # the extension can cheaply detect staleness via GET /api/user/profile-version
    # and refetch only when something actually changed.
    data_version = Column(Integer, default=1, nullable=False)
    data_updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                             onupdate=datetime.datetime.utcnow)


class BotRun(Base):
    """Persisted record of a bot execution for historical review."""
    __tablename__ = "bot_runs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    task_id = Column(String, unique=True, index=True)
    status = Column(String, default="idle")
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    total_applied = Column(Integer, default=0)
    total_skipped = Column(Integer, default=0)
    total_failed = Column(Integer, default=0)
    log_lines = Column(JSON, default=list)


class ConnectionRequest(Base):
    """Tracks connection requests sent to hiring managers after applying."""
    __tablename__ = "connection_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    job_id = Column(Integer, nullable=True)
    contact_name = Column(String, nullable=False)
    contact_title = Column(String, default="")
    company = Column(String, nullable=False)
    role_applied = Column(String, default="")
    message_sent = Column(Text, default="")
    status = Column(String, default="sent")
    sent_at = Column(DateTime, default=datetime.datetime.utcnow)


class AutopilotRun(Base):
    """Tracks each autopilot session with aggregate stats."""
    __tablename__ = "autopilot_runs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    task_id = Column(String, unique=True, index=True)
    started_at = Column(DateTime, default=datetime.datetime.utcnow)
    stopped_at = Column(DateTime, nullable=True)
    total_applied = Column(Integer, default=0)
    total_skipped = Column(Integer, default=0)
    total_failed = Column(Integer, default=0)
    total_waiting = Column(Integer, default=0)
    status = Column(String, default="running")


class GitHubSource(Base):
    """A configured GitHub repository job source."""
    __tablename__ = "github_sources"

    id = Column(Integer, primary_key=True, index=True)
    repo_url = Column(String, nullable=False, unique=True)
    repo_owner = Column(String, nullable=False)
    repo_name = Column(String, nullable=False)
    file_path = Column(String, default="README.md")
    poll_interval_minutes = Column(Integer, default=60)
    last_polled_at = Column(DateTime, nullable=True)
    last_commit_sha = Column(String, nullable=True)
    status = Column(String, default="active")
    error_message = Column(String, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    # Aggregator classification defaults
    role_category = Column(String, default="")
    experience_level = Column(String, default="")


class TailoredResume(Base):
    """A resume version tailored for a specific job."""
    __tablename__ = "tailored_resumes"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    job_id = Column(Integer, nullable=False)
    original_text = Column(Text, nullable=False)
    tailored_text = Column(Text, nullable=False)
    diff_summary = Column(Text, default="")
    status = Column(String, default="draft")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class ResumeVersion(Base):
    """A saved version of a structured resume document.

    Backs version history and job-specific resume generation. ``document_json``
    is a serialized ``ResumeDocument``; ``source`` is one of original | ai | user.
    """
    __tablename__ = "resume_versions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    resume_id = Column(Integer, ForeignKey("resume_profiles.id"), nullable=True, index=True)
    job_id = Column(Integer, nullable=True, index=True)
    label = Column(String, default="")
    source = Column(String, default="ai")  # original | ai | user
    document_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class CoverLetter(Base):
    """A saved cover letter, optionally tied to a specific job.

    Generated by ``CoverLetterGenerator`` or written/edited by the user. Synced
    to the extension so it can be inserted into ATS cover-letter fields. One
    cover letter per user may be flagged ``is_active`` as the default.
    """
    __tablename__ = "cover_letters"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    resume_id = Column(Integer, nullable=True)
    job_id = Column(Integer, nullable=True, index=True)
    job_title = Column(String, default="")
    company = Column(String, default="")
    job_url = Column(String, default="")
    text = Column(Text, default="")
    tone = Column(String, default="")
    source = Column(String, default="generated")  # generated | user
    is_active = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)


class InsiderConnection(Base):
    """A connection at a target company."""
    __tablename__ = "insider_connections"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    company = Column(String, nullable=False, index=True)
    name = Column(String, nullable=False)
    title = Column(String, default="")
    linkedin_url = Column(String, default="")
    relationship_type = Column(String, default="beyond_network")
    source = Column(String, default="linkedin")
    discovered_at = Column(DateTime, default=datetime.datetime.utcnow)


# ─── Feedback ────────────────────────────────────────────────────────────────

class Feedback(Base):
    """User-submitted feedback (bug reports, feature requests, etc.)."""
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(String, default="")
    category = Column(String, default="")  # bug_report, feature_request, ux_feedback, subscription, other
    message = Column(Text, default="")
    wants_followup = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())


# ─── User Saved Jobs (per-user bookmarks) ────────────────────────────────────

class UserSavedJob(Base):
    """Per-user job bookmarks (many-to-many between users and jobs)."""
    __tablename__ = "user_saved_jobs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id = Column(Integer, ForeignKey("scraped_jobs.id"), nullable=False, index=True)
    saved_at = Column(DateTime, default=datetime.datetime.utcnow)


# ─── Revoked Tokens (for token blacklisting) ─────────────────────────────────

class RevokedToken(Base):
    """Stores revoked refresh tokens to prevent reuse after logout/password change."""
    __tablename__ = "revoked_tokens"

    id = Column(Integer, primary_key=True, index=True)
    jti = Column(String(255), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    revoked_at = Column(DateTime, default=datetime.datetime.utcnow)
    expires_at = Column(DateTime, nullable=False)


# ─── Extension Auth Codes (PKCE handshake) ──────────────────────────────────

class ExtensionAuthCode(Base):
    """Short-lived, single-use authorization code for the extension handshake.

    Issued by ``POST /auth/extension/authorize`` to an authenticated web session
    and redeemed once by ``POST /auth/extension/token`` with the matching PKCE
    verifier. Bound to a user and a SHA-256 ``code_challenge``; expires in ~60s.
    This is how the extension obtains tokens without ever handling credentials.
    """
    __tablename__ = "extension_auth_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(255), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    code_challenge = Column(String(255), nullable=False)
    redirect_uri = Column(String, nullable=False)
    used = Column(Boolean, default=False, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


# ─── Sessions (Connected Devices registry) ──────────────────────────────────

class Session(Base):
    """A long-lived auth session (one per connect / login), keyed by a stable
    ``sid`` that survives refresh-token rotation. Backs the "Connected Devices"
    dashboard and per-device revocation. ``revoked_at`` set => the session's next
    refresh is rejected. Labels/UA-parsing are intentionally omitted (YAGNI)."""
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    sid = Column(String(36), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    client = Column(String(20), nullable=False, default="web")
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.datetime.utcnow)
    revoked_at = Column(DateTime, nullable=True)
    last_ip = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)


# ─── Security Event Log ──────────────────────────────────────────────────────

class SecurityEvent(Base):
    """Structured security audit log for authentication and access events."""
    __tablename__ = "security_events"

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(100), nullable=False, index=True)
    user_id = Column(Integer, nullable=True, index=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    details = Column(JSON, nullable=True)
    success = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, server_default=func.now(), index=True)


class RateCounter(Base):
    """Fixed-window request counter, shared across serverless instances.

    Each row is one (limit-name, identity, time-bucket) tuple. Because the
    counter lives in the database rather than process memory, the limit holds
    consistently across Vercel function invocations. Rows are disposable,
    ``expires_at`` lets a periodic cleanup drop stale buckets.
    """
    __tablename__ = "rate_counters"

    bucket_key = Column(String(255), primary_key=True)
    count = Column(Integer, default=0, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)


# ─── Autofill telemetry ──────────────────────────────────────────────────────

class AutofillReport(Base):
    """One row per autofill pass the extension performs, so we can see which
    sites and fields the filler struggles with, the signal that tells us where a
    server-side override rule is worth authoring. Stores field LABELS + outcomes
    only, never the user's answer values.
    """
    __tablename__ = "autofill_reports"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    host = Column(String, nullable=False, index=True)
    ats_type = Column(String, default="")
    url = Column(String, nullable=True)
    total_fields = Column(Integer, default=0)
    filled = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    skipped = Column(Integer, default=0)
    # [{ "label": str, "category": str, "reason": str }], no user values.
    failed_fields = Column(JSON, default=list)
    # Every attempted field, SUCCESSES INCLUDED:
    #   [{label, category, tier, pass, expected_value_present,
    #     observed_value_present, outcome, reason}]
    #
    # Recording only failures made one bug class unobservable by construction:
    # a field answered wrongly but written successfully produced a `filled`
    # count and nothing else, so the record of the page said everything went
    # well. That is the class we keep hitting, and it was the one thing this
    # table could never show. Still labels, categories and booleans only,
    # never the user's answers.
    field_outcomes = Column(JSON, default=list)
    # Fields written successfully whose value the page no longer held at the
    # terminal re-scan. Counted separately from `failed` so a framework that
    # reverts values is visible as itself rather than as a write failure.
    reverted = Column(Integer, default=0)
    # Which build produced this report, so a stale local extension is visible as
    # itself instead of being investigated as a code bug (it has been, twice).
    extension_version = Column(String, default="")
    # {scan_ms, local_ms, backend_ms, reask_ms, total_ms}. "The autofill takes
    # exceptionally long" was reported on 2026-08-13 with nothing in the record
    # to support it; the cause was found in Vercel logs by reading timestamps.
    durations = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)


class AutofillFieldCapture(Base):
    """One field of one fill, in full, for accounts that opted into diagnostics.

    The deliberate opposite of AutofillReport.field_outcomes: this DOES store
    the answer and the employer's markup. It is written only when
    ``UserSettings.diagnostic_capture`` is on, so the no-values posture holds
    for every account that did not ask for this.

    It exists for one workflow: read a failed field out of here, paste `dom`
    into ``chrome-extension/test/fixtures/``, and write a regression test
    without visiting the live page. For the forms that fail most, several steps
    into a flow behind a login, there is no other way to get that markup back.
    """
    __tablename__ = "autofill_field_captures"

    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, ForeignKey("autofill_reports.id", ondelete="CASCADE"), index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    # Denormalised from the report so the common queries need no join.
    host = Column(String, index=True)
    ats_type = Column(String, default="")
    url = Column(String, default="")

    field_id = Column(String, default="")
    label = Column(Text, default="")          # full, not truncated
    category = Column(String, default="", index=True)
    confidence = Column(Float, default=0)
    control_type = Column(String, default="")
    input_type = Column(String, default="")
    help_text = Column(Text, default="")
    required = Column(Boolean, default=False)
    group_index = Column(Integer, nullable=True)
    # What the widget was really offering, read live. DetectedField.options is
    # captured at SCAN time, which for a react-select is before its list exists.
    options = Column(JSON, default=list)

    proposed_value = Column(Text, default="")
    observed_value = Column(Text, default="")
    # True when a secret (password, national ID) was replaced by a type marker,
    # so an empty-looking answer is never mistaken for a fill failure.
    redacted = Column(Boolean, default=False)

    tier = Column(String, default="")
    pass_ = Column("pass", String, default="")
    outcome = Column(String, default="", index=True)
    reason = Column(Text, default="")

    # Sanitised markup around the control: scripts, styles, SVG paths, images
    # and inline styles stripped, capped client-side. Postgres TOAST compresses
    # this, so a ~4 KB snapshot costs well under that on disk.
    dom = Column(Text, default="")
    selector = Column(String, default="")
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)


# ─── Autofill server-side overrides (hot-fix layer) ──────────────────────────

class AutofillOverride(Base):
    """A server-authored correction the extension applies at classify time, so a
    broken/new site can be hot-fixed without shipping a new extension. Matched by
    host (or "*") + a normalized field-label regex; forces a field category (and
    optional value synonyms) onto the generic adapter+AI pipeline.
    """
    __tablename__ = "autofill_overrides"

    id = Column(Integer, primary_key=True, index=True)
    # Host to match, e.g. "boards.greenhouse.io"; "*" matches any host.
    host = Column(String, nullable=False, default="*", index=True)
    # Regex tested (case-insensitive) against the normalized field label.
    label_pattern = Column(String, nullable=False)
    # Field category to force (e.g. "workAuthorization"); "" leaves it unchanged.
    category = Column(String, default="")
    # Optional { "typed value": "canonical option" } hints for choice controls.
    value_synonyms = Column(JSON, default=dict)
    enabled = Column(Boolean, default=True, nullable=False)
    note = Column(String, default="")
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


# ─── Ingestion source health (per-board circuit breaker + dead-letter log) ───

class SourceHealth(Base):
    """One row per scraped board ("greenhouse:stripe"). Records consecutive
    failures so cron-ats can skip a broken board for a cooldown instead of
    burning its request budget every hour, and keeps the last error visible so
    a source that changed its API shape gets caught instead of failing
    silently forever.
    """
    __tablename__ = "source_health"

    id = Column(Integer, primary_key=True, index=True)
    board_key = Column(String, unique=True, nullable=False, index=True)
    platform = Column(String, default="")
    slug = Column(String, default="")
    consecutive_failures = Column(Integer, default=0, nullable=False)
    total_failures = Column(Integer, default=0, nullable=False)
    last_error = Column(String, default="")
    last_success_at = Column(DateTime, nullable=True)
    last_failure_at = Column(DateTime, nullable=True)
    # Listing count seen on the last successful crawl, a sudden drop to zero
    # on a board that had jobs is itself an alert-worthy signal.
    last_job_count = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)


# ─── Self-hosted company logos (one validated image per employer) ────────────

class CompanyLogo(Base):
    """One row per employer, keyed by a normalized company name
    (services/logo_cache.company_key), so "**Tesla**", "Tesla" and "Tesla, Inc."
    share one logo. `status` is 'ok' once a real logo was harvested, normalized
    to a small square PNG (or sanitized SVG) and stored here; scraped_jobs rows
    then point at it as '/jobs/logo/<sha>.png', served from our own origin with
    an immutable cache. A 'miss' is never permanent: `next_retry_at` backs off
    with `attempts` and the cron harvester tries again once it is due.
    """
    __tablename__ = "company_logos"

    id = Column(Integer, primary_key=True, index=True)
    company_key = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, default="")
    # Registrable domain the harvester verified (homepage reachable, not
    # parked, names the company). NULL until one is proven.
    domain = Column(String, nullable=True)
    status = Column(String, default="miss", nullable=False)  # ok | miss
    # sha1 of `data`; the served URL is keyed by it, so it changes whenever
    # the image does.
    sha = Column(String, nullable=True, index=True)
    fmt = Column(String, nullable=True)  # png | svg
    # Normalized bytes, ~2-8 KB. Deferred: a whole-row load never pulls them
    # over the wire; only the serving endpoint selects this column.
    data = deferred(Column(LargeBinary, nullable=True))
    source = Column(String, default="")
    source_url = Column(String, default="")
    width = Column(Integer, nullable=True)  # original decoded size
    height = Column(Integer, nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    # Name-guessed domains proven bogus (NXDOMAIN or a parked page), so ingest
    # never plants them on this employer's rows again.
    rejected_domains = Column(JSON, nullable=True)
    # Real hotlinks (logo_quality 1: homepage/CDN/Commons images) this
    # employer's rows showed before propagation replaced them, most used
    # first. A re-harvest tries them before anything else, and demoting a
    # wrong pick (backfill_logos_v2.py --reharvest) puts the first one back.
    prior_logo_urls = Column(JSON, nullable=True)
    # sha1s of normalized images demoted as wrong picks: the harvester skips
    # a candidate that normalizes to one of them.
    blocked_shas = Column(JSON, nullable=True)
    checked_at = Column(DateTime, nullable=True)
    # status 'miss': when to try again. status 'ok': NULL once final; set when
    # the logo is provisional (the cron stored a lower-tier pick because
    # LinkedIn was rate-limited) and due for a re-check then.
    next_retry_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow,
                        onupdate=datetime.datetime.utcnow)
