"""
Apply flow endpoints for managing job application sessions.

POST /apply/initiate                 → ApplySession
GET  /apply/{session_id}/profile     → FillProfile
POST /apply/{session_id}/progress    → ProgressUpdate
POST /apply/{session_id}/complete    → ApplicationRecordOut
POST /apply/{session_id}/question    → PendingQuestionOut
"""

import time
import uuid
import logging
import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.db.database import get_db
from backend.db.models import (
    ScrapedJob, JobStatus, ApplicationRecord, PendingQuestion,
    ResumeProfileDB, TailoredResume, UserSettings,
)
from backend.auth.dependencies import get_verified_user_id
from backend.schemas.apply import (
    ApplySession, FillProfile, ProgressUpdate,
    LogApplicationRequest, LoggedApplication,
)
from backend.schemas.jobs import PendingQuestionOut

logger = logging.getLogger(__name__)
router = APIRouter()

# In-memory session store with TTL and size limits
_sessions: dict[str, dict] = {}
_SESSION_TTL_SECONDS = 3600  # 1 hour
_MAX_SESSIONS = 1000


def _cleanup_expired_sessions():
    """Remove expired sessions to prevent memory exhaustion."""
    now = time.time()
    expired = [sid for sid, data in _sessions.items() if now - data.get("_created_at", 0) > _SESSION_TTL_SECONDS]
    for sid in expired:
        del _sessions[sid]


def _get_session(session_id: str) -> dict | None:
    """Get a session, returning None if expired or not found."""
    session = _sessions.get(session_id)
    if not session:
        return None
    if time.time() - session.get("_created_at", 0) > _SESSION_TTL_SECONDS:
        del _sessions[session_id]
        return None
    return session


class InitiateRequest(BaseModel):
    """Request body for initiating an apply flow."""
    job_id: int


class QuestionRequest(BaseModel):
    """Request body for submitting a question from the extension."""
    question: str
    field_type: str = "text"
    options: list[str] = []


@router.post("/initiate", response_model=ApplySession)
def initiate_apply(
    request: InitiateRequest,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Initiate an apply flow for a job."""
    job = db.query(ScrapedJob).filter(ScrapedJob.id == request.job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")

    # Update job status to applying
    job.status = JobStatus.APPLYING
    db.commit()

    # Check if tailored resume exists for this user
    tailored = (
        db.query(TailoredResume)
        .filter(
            TailoredResume.job_id == request.job_id,
            TailoredResume.user_id == user_id,
            TailoredResume.status == "accepted",
        )
        .first()
    )

    session_id = str(uuid.uuid4())
    resume_version = "tailored" if tailored else "original"

    # Cleanup expired sessions and enforce size limit
    _cleanup_expired_sessions()
    if len(_sessions) >= _MAX_SESSIONS:
        # Remove oldest sessions
        sorted_sessions = sorted(_sessions.items(), key=lambda x: x[1].get("_created_at", 0))
        for sid, _ in sorted_sessions[:100]:
            del _sessions[sid]

    # Store session with user_id and creation time
    _sessions[session_id] = {
        "job_id": request.job_id,
        "user_id": user_id,
        "resume_version": resume_version,
        "status": "initiated",
        "_created_at": time.time(),
    }

    return ApplySession(
        session_id=session_id,
        job_id=request.job_id,
        resume_version=resume_version,
        cover_letter_ready=False,
        match_score=_user_fit_score(db, user_id, job.id),
    )


def _user_fit_score(db: Session, user_id: int, job_id: int) -> int:
    """The user's own displayed fit score for this job: SCORED only, else 0 (D-86). Never the
    AI-era scraped_jobs.match_score (D-109)."""
    from backend.services.jobs_sidebar import user_match_scores

    value, _band, outcome = user_match_scores(db, user_id, [job_id]).get(job_id, (None, None, None))
    return value if outcome == "SCORED" and value is not None else 0


@router.get("/{session_id}/profile", response_model=FillProfile)
def get_fill_profile(
    session_id: str,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Get profile data for form filling by the extension."""
    session = _get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Apply session not found.")

    # Verify session belongs to this user
    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this session.")

    # Get user settings
    settings = db.query(UserSettings).filter(UserSettings.user_id == user_id).first()
    if not settings:
        raise HTTPException(status_code=400, detail="User settings not configured.")

    # Get resume text (tailored if available)
    resume_text = ""
    if session["resume_version"] == "tailored":
        tailored = (
            db.query(TailoredResume)
            .filter(
                TailoredResume.job_id == session["job_id"],
                TailoredResume.user_id == user_id,
                TailoredResume.status == "accepted",
            )
            .first()
        )
        if tailored:
            resume_text = tailored.tailored_text

    if not resume_text:
        profile = (
            db.query(ResumeProfileDB)
            .filter(ResumeProfileDB.user_id == user_id)
            .order_by(ResumeProfileDB.created_at.desc())
            .first()
        )
        resume_text = profile.raw_text if profile else ""

    # Get resume profile for structured data, prefer primary, fall back to most recent
    profile = (
        db.query(ResumeProfileDB)
        .filter(ResumeProfileDB.user_id == user_id, ResumeProfileDB.is_primary == 1)
        .first()
    )
    if not profile:
        profile = (
            db.query(ResumeProfileDB)
            .filter(ResumeProfileDB.user_id == user_id)
            .order_by(ResumeProfileDB.created_at.desc())
            .first()
        )

    # Merge all technology categories into flat skills list for backward compat
    skills = list(profile.skills or []) if profile else []
    if profile:
        technologies = profile.technologies or {}
        for category_skills in technologies.values():
            for skill in category_skills:
                if skill not in skills:
                    skills.append(skill)

    return FillProfile(
        first_name=settings.first_name,
        last_name=settings.last_name,
        email=settings.email,
        phone=settings.phone,
        location=settings.city,
        linkedin_url=settings.linkedin_url,
        website=settings.website,
        skills=skills,
        experience=profile.experience if profile else [],
        education=profile.education if profile else [],
        projects=profile.projects if profile else [],
        resume_text=resume_text,
        cover_letter="",
        prefilled_answers=settings.prefilled_answers or {},
    )


@router.post("/{session_id}/progress")
def update_progress(
    session_id: str,
    update: ProgressUpdate,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Receive progress update from the extension."""
    session = _get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Apply session not found.")

    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this session.")

    session["status"] = update.status
    session["progress"] = update.percentage

    return {"status": "ok"}


@router.post("/{session_id}/complete")
def complete_apply(
    session_id: str,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Mark application as complete."""
    session = _get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Apply session not found.")

    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this session.")

    job = db.query(ScrapedJob).filter(ScrapedJob.id == session["job_id"]).first()
    if job:
        job.status = JobStatus.APPLIED

    # Create application record linked to user
    record = ApplicationRecord(
        user_id=user_id,
        platform=job.platform if job else "linkedin",
        company=job.company if job else "",
        role=job.title if job else "",
        url=job.url if job else "",
        resume_version=session["resume_version"],
        job_id=session["job_id"],
    )
    db.add(record)
    db.commit()

    # Mark session as complete
    session["status"] = "complete"

    return {"status": "applied", "job_id": session["job_id"]}


@router.post("/{session_id}/question", response_model=PendingQuestionOut)
def submit_question(
    session_id: str,
    request: QuestionRequest,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Submit a question the extension couldn't answer."""
    session = _get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Apply session not found.")

    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Not authorized for this session.")

    question = PendingQuestion(
        user_id=user_id,
        job_id=session["job_id"],
        question=request.question,
        field_type=request.field_type,
        options=request.options,
    )
    db.add(question)
    db.commit()
    db.refresh(question)

    return question


@router.post("/log", response_model=LoggedApplication)
def log_application(
    request: LogApplicationRequest,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Record an application the extension observed the user submit.

    This is the external-page counterpart to the session `/complete` flow: the
    autofill extension deliberately never clicks Submit, but when the user does,
    it POSTs here so the job shows up on the user's Applications page, the same
    behaviour as Jobright's submit tracking. No internal job_id is required;
    ApplicationRecord.job_id and .url are nullable and the applications list
    outer-joins ScrapedJob, so an external record renders correctly.

    Deduped by (user_id, url): re-submitting the same URL refreshes applied_at
    instead of creating a duplicate row.
    """
    company = (request.company or "").strip()
    role = (request.role or "").strip()
    url = (request.url or "").strip() or None

    if not company and not role and not url:
        raise HTTPException(
            status_code=422,
            detail="Need at least a company, role, or URL to log an application.",
        )

    now = datetime.datetime.utcnow()

    # Dedupe by job_id first (matches records mark-applied creates), then by url
    # (external applies with no internal job). Either way we refresh in place.
    existing = None
    if request.job_id:
        existing = (
            db.query(ApplicationRecord)
            .filter(
                ApplicationRecord.user_id == user_id,
                ApplicationRecord.job_id == request.job_id,
            )
            .order_by(ApplicationRecord.applied_at.desc())
            .first()
        )
    if existing is None and url:
        existing = (
            db.query(ApplicationRecord)
            .filter(ApplicationRecord.user_id == user_id, ApplicationRecord.url == url)
            .order_by(ApplicationRecord.applied_at.desc())
            .first()
        )

    if existing:
        existing.applied_at = now
        if company:
            existing.company = company
        if role:
            existing.role = role
        if request.resume_version:
            existing.resume_version = request.resume_version
        record = existing
        created = False
    else:
        record = ApplicationRecord(
            user_id=user_id,
            job_id=request.job_id,
            platform=request.platform or "extension",
            company=company or (url or "Unknown"),
            role=role or "",
            url=url,
            resume_version=request.resume_version or "original",
            ats_type=request.ats_type or "",
            applied_at=now,
        )
        db.add(record)
        created = True

    # If this application maps to a job Tailrd already tracks, reflect the status.
    if request.job_id:
        job = db.query(ScrapedJob).filter(ScrapedJob.id == request.job_id).first()
        if job:
            job.status = JobStatus.APPLIED

    db.commit()
    db.refresh(record)

    status = record.status.value if hasattr(record.status, "value") else str(record.status)
    return LoggedApplication(
        id=record.id,
        company=record.company,
        role=record.role,
        url=record.url,
        status=status,
        applied_at=record.applied_at,
        job_id=record.job_id,
        created=created,
    )
