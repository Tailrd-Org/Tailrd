"""
Everything the Jobs page's right-hand rail shows, in one read-only pass.

No LLM calls: match numbers come from the fit engine's banked rows in ``job_match_scores``
(services/fit_engine.py), read through ``fit_reads`` so that no request here imports or builds
the engine (DECISIONS D-107). Bands, not numbers, decide what counts (D-85): a "strong match" is
a SCORED row in the Strong band, the gap pool is SCORED Good or better, and a row with no score
(NULL: low confidence, gate fail, needs review) is never averaged, counted or treated as low
(D-86). Skill gaps compare the curated taxonomy (structured_extraction.extract_skills) on both
sides, the job's stored tags against tags extracted from the résumé text.

Scores are only trusted while the résumé they were computed from is unchanged (the fit
fingerprint), so an edited résumé never shows a stale number.
"""

import datetime
import logging
from collections import Counter
from typing import Any, Iterable, Optional

from sqlalchemy import String, cast, func, or_
from sqlalchemy.orm import Session

from backend.db.models import (
    ApplicationRecord,
    ApplicationStatus,
    AutofillReport,
    JobMatchScore,
    ResumeProfileDB,
    ScrapedJob,
    UserSavedJob,
    UserSettings,
)
from backend.services import fit_reads
from backend.services.listing_freshness import HIDDEN_LISTING_STATUSES
from backend.services.structured_extraction import _SKILL_SYNONYMS, extract_skills

logger = logging.getLogger(__name__)

# A saved job this old is worth applying to before it goes away. Matches the
# aggregator expiry window used by listing freshness (21 days).
CLOSING_AGE_DAYS = 21
MAX_GAPS = 3
MAX_CLOSING = 3
MAX_COMPANIES = 6
# Tags too generic to tell someone to "add to your resume".
_GAP_SKIP = {"agile", "scrum", "kanban", "git", "rest", "ui", "ux", "qa", "excel", "lean"}

# Knowing the left implies the right, so a résumé that lists PostgreSQL is
# never told it is "missing" SQL. (Moved here from the retired local scorer.)
IMPLIES: dict[str, tuple[str, ...]] = {
    "postgresql": ("sql",), "mysql": ("sql",), "sqlite": ("sql",), "oracle": ("sql",),
    "snowflake": ("sql",), "bigquery": ("sql",),
    "typescript": ("javascript",), "react": ("javascript",), "vue": ("javascript",),
    "angular": ("javascript",), "svelte": ("javascript",), "node.js": ("javascript",),
    "next.js": ("react", "javascript"), "react native": ("react", "javascript"),
    "express": ("node.js", "javascript"),
    "django": ("python",), "flask": ("python",), "fastapi": ("python",),
    "pandas": ("python",), "numpy": ("python",), "pytorch": ("python",),
    "scikit-learn": ("python",), "spring": ("java",), "rails": ("ruby",),
    "kubernetes": ("docker",), "helm": ("kubernetes",),
}

# Display names for taxonomy tags whose title case would be wrong. Twin of the
# web app's frontend/src/lib/skillLabel.ts.
_DISPLAY = {
    "sql": "SQL", "html": "HTML", "css": "CSS", "aws": "AWS", "gcp": "GCP", "php": "PHP",
    "c++": "C++", "c#": "C#", ".net": ".NET", "ci/cd": "CI/CD", "tcp/ip": "TCP/IP",
    "nlp": "NLP", "llm": "LLMs", "etl": "ETL", "dbt": "dbt", "ios": "iOS", "ui": "UI",
    "ux": "UX", "qa": "QA", "seo": "SEO", "sap": "SAP", "cad": "CAD", "plc": "PLC",
    "fpga": "FPGA", "vhdl": "VHDL", "grpc": "gRPC", "rest": "REST", "graphql": "GraphQL",
    "matlab": "MATLAB", "mysql": "MySQL", "postgresql": "PostgreSQL", "mongodb": "MongoDB",
    "dynamodb": "DynamoDB", "bigquery": "BigQuery", "javascript": "JavaScript",
    "typescript": "TypeScript", "node.js": "Node.js", "next.js": "Next.js",
    "pytorch": "PyTorch", "tensorflow": "TensorFlow", "scikit-learn": "scikit-learn",
    "numpy": "NumPy", "power bi": "Power BI", "autocad": "AutoCAD", "solidworks": "SolidWorks",
    "objective-c": "Objective-C", "cloudformation": "CloudFormation", "rabbitmq": "RabbitMQ",
    "fastapi": "FastAPI", "r": "R", "go": "Go",
}


def display_skill(tag: str) -> str:
    """A taxonomy tag as a person reads it ("postgresql" -> "PostgreSQL")."""
    key = (tag or "").strip().lower()
    return _DISPLAY.get(key) or " ".join(w[:1].upper() + w[1:] for w in key.split(" "))


def skill_tags(raw_text: str, declared: Iterable[Any] = ()) -> list[str]:
    """Canonical taxonomy tags a résumé shows, implied skills included."""
    tags = extract_skills("", raw_text or "")
    seen = set(tags)
    for raw in declared or ():
        if not isinstance(raw, str):
            continue
        key = raw.strip().lower()
        if key in _SKILL_SYNONYMS and key not in seen:
            seen.add(key)
            tags.append(key)
    for tag in list(tags):
        for implied in IMPLIES.get(tag, ()):
            if implied not in seen:
                seen.add(implied)
                tags.append(implied)
    return tags


def visible_job_filter():
    """The feed's own visibility rules: named company, not a hidden twin, not dead."""
    return (
        ScrapedJob.company.isnot(None)
        & (func.trim(ScrapedJob.company) != "")
        & (ScrapedJob.company != "Unknown")
        & ScrapedJob.duplicate_of.is_(None)
        & or_(
            ScrapedJob.listing_status.is_(None),
            ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES),
        )
    )


def _has_skill_tags():
    """skills holds a non-empty list (JSON null and [] both mean "untagged")."""
    return func.coalesce(cast(ScrapedJob.skills, String), "").notin_(["", "null", "[]"])


def scoring_resume(db: Session, user_id: int) -> Optional[ResumeProfileDB]:
    """The resume the match sweep scores against: the newest one with text.

    Must stay in step with match_notifier's choice, or the rail would label
    scores with a resume they were never computed from.
    """
    return (
        db.query(ResumeProfileDB)
        .filter(
            ResumeProfileDB.user_id == user_id,
            ResumeProfileDB.raw_text.isnot(None),
            ResumeProfileDB.raw_text != "",
        )
        .order_by(ResumeProfileDB.created_at.desc())
        .first()
    )


def current_scores_query(db: Session, user_id: int, fingerprints: list[str]):
    """Rows current for this résumé: fit-engine fingerprints only (never AI or "local1:")."""
    return db.query(JobMatchScore).filter(
        JobMatchScore.user_id == user_id,
        JobMatchScore.resume_fingerprint.in_(fingerprints),
    )


def serving_fingerprints(db: Session, user_id: int, profile: Optional[ResumeProfileDB]) -> list[str]:
    """The fit fingerprints current for this user's résumé (fit_reads; never builds the engine)."""
    if profile is None:
        return []
    return fit_reads.serving_fingerprints(db, user_id, profile)


def user_match_scores(
    db: Session, user_id: Optional[int], job_ids: list[int]
) -> dict[int, tuple[Optional[int], Optional[str], Optional[str]]]:
    """{job_id: (score, band, outcome)} for the user's current résumé, to overlay on cards.

    ``score`` is NULL unless the outcome is SCORED (D-86). Jobs nobody has scored yet are
    banked on the spot only when this process has already built the engine (the sweep, an
    upload or a breakdown built it); otherwise they show as not scored yet. A feed request
    never builds the engine (about ten seconds cold).
    """
    if user_id is None or not job_ids:
        return {}
    profile = scoring_resume(db, user_id)
    if profile is None:
        return {}
    fingerprints = serving_fingerprints(db, user_id, profile)
    rows = (
        current_scores_query(db, user_id, fingerprints)
        .with_entities(JobMatchScore.job_id, JobMatchScore.score, JobMatchScore.band, JobMatchScore.outcome)
        .filter(JobMatchScore.job_id.in_(job_ids))
        .all()
    ) if fingerprints else []
    scores = {job_id: (score, band, outcome) for job_id, score, band, outcome in rows}
    missing = [j for j in job_ids if j not in scores]
    if missing and fit_reads.engine_ready():
        from backend.services import fit_engine

        try:
            for job_id, result in fit_engine.bank_scores(db, user_id, profile, missing).items():
                scores[job_id] = (fit_engine.stored_score(result),
                                  result.band.value if result.band is not None else None,
                                  result.outcome.value)
        except Exception as exc:
            # Badges are a nicety; a scoring hiccup must never break the feed.
            db.rollback()
            logger.error("fit scoring on the feed failed for user %s: %s", user_id, type(exc).__name__)
    return scores


def resume_skill_tags(profile: ResumeProfileDB) -> list[str]:
    """Canonical taxonomy tags the resume shows, in the jobs' vocabulary."""
    return skill_tags(profile.raw_text or "", profile.skills or [])


def _start_of_week(now: datetime.datetime) -> datetime.datetime:
    """Monday 00:00 UTC of the current week."""
    monday = now - datetime.timedelta(days=now.weekday())
    return monday.replace(hour=0, minute=0, second=0, microsecond=0)


def _job_age_days(job: ScrapedJob, now: datetime.datetime) -> Optional[int]:
    seen = job.posted_date or job.first_seen_at or job.scraped_at
    if seen is None:
        return None
    return max(0, (now - seen).days)


def _resume_section(
    db: Session, user_id: int, profile: Optional[ResumeProfileDB]
) -> tuple[Optional[dict], list[dict], list[str]]:
    if profile is None:
        return None, [], []

    fingerprints = serving_fingerprints(db, user_id, profile)
    visible = visible_job_filter()
    # Only SCORED rows carry a number: a NULL is "not scored", never averaged or counted (D-86).
    scored = (
        current_scores_query(db, user_id, fingerprints)
        .join(ScrapedJob, ScrapedJob.id == JobMatchScore.job_id)
        .filter(visible, JobMatchScore.outcome == fit_reads.OUTCOME_SCORED, JobMatchScore.score.isnot(None))
    )
    count, avg = scored.with_entities(
        func.count(JobMatchScore.id), func.avg(JobMatchScore.score)
    ).one()
    strong = scored.filter(fit_reads.strong_match()).count()

    skills = resume_skill_tags(profile)
    have = set(skills)

    # Gap pool: jobs the engine banked as Good or Strong matches (SCORED). A
    # user the sweep has barely reached is topped up with recent jobs that share
    # at least two of their skills, so the card is useful from the first visit.
    pool = dict(
        db.query(ScrapedJob.id, ScrapedJob.skills)
        .join(JobMatchScore, JobMatchScore.job_id == ScrapedJob.id)
        .filter(
            JobMatchScore.user_id == user_id,
            JobMatchScore.resume_fingerprint.in_(fingerprints),
            fit_reads.good_or_better(),
            visible,
            ScrapedJob.skills.isnot(None),
        )
        .limit(300)
        .all()
    )
    if len(pool) < 5 and have:
        # About half the catalogue has no tags yet; skip those rows in SQL so
        # the 400 we scan all carry skills.
        recent = (
            db.query(ScrapedJob.id, ScrapedJob.skills)
            .filter(visible, _has_skill_tags())
            .order_by(ScrapedJob.id.desc())
            .limit(400)
            .all()
        )
        for job_id, tags in recent:
            if tags and len(have.intersection(tags)) >= 2:
                pool.setdefault(job_id, tags)
    pool_skills = [tags or [] for tags in pool.values()]

    counter: Counter = Counter()
    for tags in pool_skills:
        for tag in set(tags):
            if tag not in have and tag not in _GAP_SKIP:
                counter[tag] += 1
    gaps = [
        {"skill": skill, "job_count": n}
        for skill, n in counter.most_common(MAX_GAPS)
        # One job asking for it is noise, not a pattern.
        if n >= 2
    ]

    resume = {
        "id": profile.id,
        "name": profile.name or "Untitled Resume",
        "scored_jobs": int(count or 0),
        "avg_match": round(avg) if avg is not None else None,
        "strong_matches": strong,
        "gap_pool_size": len(pool_skills),
    }
    return resume, gaps, skills


def _progress_section(db: Session, user_id: int, now: datetime.datetime) -> dict:
    week_start = _start_of_week(now)
    real = ApplicationRecord.status.notin_([ApplicationStatus.FAILED, ApplicationStatus.SKIPPED])
    applied_week = (
        db.query(ApplicationRecord)
        .filter(ApplicationRecord.user_id == user_id, real, ApplicationRecord.applied_at >= week_start)
        .count()
    )
    applied_total = (
        db.query(ApplicationRecord).filter(ApplicationRecord.user_id == user_id, real).count()
    )
    interviews = (
        db.query(ApplicationRecord)
        .filter(
            ApplicationRecord.user_id == user_id,
            ApplicationRecord.status.in_([ApplicationStatus.INTERVIEWING, ApplicationStatus.OFFER]),
        )
        .count()
    )
    saved_week = (
        db.query(UserSavedJob)
        .filter(UserSavedJob.user_id == user_id, UserSavedJob.saved_at >= week_start)
        .count()
    )
    return {
        "week_start": week_start.isoformat() + "Z",
        "applied_week": applied_week,
        "applied_total": applied_total,
        "saved_week": saved_week,
        "interviews": interviews,
    }


def _closing_section(db: Session, user_id: int, now: datetime.datetime) -> list[dict]:
    """Saved, not yet applied, still open, and either flagged stale or old."""
    applied_ids = {
        row[0]
        for row in db.query(ApplicationRecord.job_id)
        .filter(ApplicationRecord.user_id == user_id, ApplicationRecord.job_id.isnot(None))
        .all()
    }
    saved = (
        db.query(ScrapedJob)
        .join(UserSavedJob, UserSavedJob.job_id == ScrapedJob.id)
        .filter(
            UserSavedJob.user_id == user_id,
            or_(
                ScrapedJob.listing_status.is_(None),
                ScrapedJob.listing_status.notin_(HIDDEN_LISTING_STATUSES),
            ),
        )
        .all()
    )
    picks = []
    for job in saved:
        if job.id in applied_ids:
            continue
        age = _job_age_days(job, now)
        stale = job.listing_status == "stale"
        if not stale and (age is None or age < CLOSING_AGE_DAYS):
            continue
        picks.append((0 if stale else 1, -(age or 0), job, age, stale))
    picks.sort(key=lambda p: (p[0], p[1]))
    return [
        {
            "id": job.id,
            "title": job.title,
            "company": job.company,
            "company_logo": job.company_logo or "",
            "company_domain": job.company_domain or "",
            "company_url": job.company_url or "",
            "age_days": age,
            "reason": "stale" if stale else "old",
        }
        for _, _, job, age, stale in picks[:MAX_CLOSING]
    ]


def _feed_section(
    db: Session, user_id: Optional[int], since: datetime.datetime, fingerprints: Optional[list[str]]
) -> dict:
    visible = visible_job_filter()
    first_seen = func.coalesce(ScrapedJob.first_seen_at, ScrapedJob.scraped_at)
    total = db.query(ScrapedJob).filter(visible).count()
    new_since = db.query(ScrapedJob).filter(visible, first_seen >= since).count()
    remote = db.query(ScrapedJob).filter(visible, ScrapedJob.work_type == "remote").count()
    strong = None
    if user_id is not None and fingerprints:
        strong = (
            current_scores_query(db, user_id, fingerprints)
            .join(ScrapedJob, ScrapedJob.id == JobMatchScore.job_id)
            .filter(visible, fit_reads.strong_match())
            .count()
        )
    return {
        "total": total,
        "new_since": new_since,
        "since": since.isoformat() + "Z",
        "remote": remote,
        "strong_matches": strong,
    }


def _companies_section(
    db: Session, user_id: Optional[int], fingerprints: Optional[list[str]]
) -> tuple[list[dict], str]:
    """Companies with the most open roles for this user.

    With scores: count Good or Strong matches (SCORED). Without: count all open roles,
    which is still "who is hiring the most right now".
    """
    visible = visible_job_filter()
    q = db.query(ScrapedJob.company, func.count(ScrapedJob.id).label("n")).filter(visible)
    if user_id is not None and fingerprints:
        scored = (
            q.join(JobMatchScore, JobMatchScore.job_id == ScrapedJob.id)
            .filter(
                JobMatchScore.user_id == user_id,
                JobMatchScore.resume_fingerprint.in_(fingerprints),
                fit_reads.good_or_better(),
            )
            .group_by(ScrapedJob.company)
            .order_by(func.count(ScrapedJob.id).desc(), ScrapedJob.company)
            .limit(MAX_COMPANIES)
            .all()
        )
        if len(scored) >= 3:
            rows, basis = scored, "matches"
        else:
            rows, basis = None, "open"
    else:
        rows, basis = None, "open"
    if rows is None:
        rows = (
            q.group_by(ScrapedJob.company)
            .order_by(func.count(ScrapedJob.id).desc(), ScrapedJob.company)
            .limit(MAX_COMPANIES)
            .all()
        )

    out = []
    for company, n in rows:
        # Any one visible row carries the company's branding fields.
        sample = (
            db.query(ScrapedJob.company_logo, ScrapedJob.company_domain, ScrapedJob.company_url)
            .filter(visible, ScrapedJob.company == company)
            .order_by((ScrapedJob.company_logo == "").asc(), ScrapedJob.id.desc())
            .first()
        )
        logo, domain, url = sample or ("", "", "")
        out.append({
            "company": company,
            "count": n,
            "company_logo": logo or "",
            "company_domain": domain or "",
            "company_url": url or "",
        })
    return out, basis


def _autofill_section(db: Session, user_id: int) -> dict:
    fields, passes = (
        db.query(func.coalesce(func.sum(AutofillReport.filled), 0), func.count(AutofillReport.id))
        .filter(AutofillReport.user_id == user_id)
        .one()
    )
    return {"fields_filled": int(fields or 0), "passes": int(passes or 0)}


def _alerts_enabled(db: Session, user_id: int) -> bool:
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    # No row yet means the column default: on.
    return True if settings is None else bool(settings.match_alerts_enabled)


def build_sidebar(
    db: Session,
    user_id: Optional[int],
    since: Optional[datetime.datetime] = None,
    now: Optional[datetime.datetime] = None,
) -> dict:
    now = now or datetime.datetime.utcnow()
    if since is None or since > now:
        since = now - datetime.timedelta(days=1)
    # Never count further back than two weeks: "new since your last visit"
    # three months ago is the whole catalogue, which says nothing.
    since = max(since, now - datetime.timedelta(days=14))

    resume = gaps = None
    skills: list[str] = []
    fingerprints: Optional[list[str]] = None
    progress = closing = autofill = None
    alerts = None
    if user_id is not None:
        profile = scoring_resume(db, user_id)
        resume, gaps, skills = _resume_section(db, user_id, profile)
        fingerprints = serving_fingerprints(db, user_id, profile) if profile else None
        progress = _progress_section(db, user_id, now)
        closing = _closing_section(db, user_id, now)
        autofill = _autofill_section(db, user_id)
        alerts = _alerts_enabled(db, user_id)

    companies, basis = _companies_section(db, user_id, fingerprints)
    return {
        "resume": resume,
        "resume_skills": skills,
        "skill_gaps": gaps or [],
        "progress": progress,
        "closing_soon": closing or [],
        "feed": _feed_section(db, user_id, since, fingerprints),
        "top_companies": companies,
        "top_companies_basis": basis,
        "autofill": autofill,
        "alerts_enabled": alerts,
    }
