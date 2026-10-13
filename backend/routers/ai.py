"""
Endpoints for the job match breakdown, resume tailoring, and cover letter generation.

POST /ai/match-breakdown/{job_id}  → MatchBreakdown (the deterministic fit engine; no AI, free)
POST /ai/tailor-resume/{job_id}    → TailoredResumeOut
POST /ai/cover-letter/{job_id}     → CoverLetterOut

/ai/analyze-fit and /ai/batch-score were removed with every AI scoring path (DECISIONS D-83).
"""

import datetime
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from backend.db.database import get_db
from backend.db.models import ScrapedJob, ResumeProfileDB, ResumeVersion, CoverLetter
from backend.auth.dependencies import get_verified_user_id, verify_cron_secret
from backend.services.usage_limiter import llm_guard
from backend.services.match_notifier import sweep_match_alerts
from backend.schemas.match import MatchBreakdown, MatchInterval, MatchItem
from backend.services import fit_reads
from backend.schemas.ai import (
    TailoredResumeOut,
    CoverLetterOut,
    JobAnalysisOut,
    RewriteIn,
    RewriteOut,
    CoverLetterIn,
    CoverLetterRecordOut,
    CoverLetterSaveIn,
    ResumeVersionIn,
    ResumeVersionOut,
    SnippetEditIn,
    SnippetEditOut,
)
from backend.services.profile_version import bump_profile_version
from backend.schemas.resume_document import ResumeDocument
from backend.services.match_engine import MatchEngine
from backend.services.resume_tailor import ResumeTailor
from backend.services.cover_letter import CoverLetterGenerator
from backend.services.resume_document import db_record_to_document, document_to_text
from backend.services.llm import get_llm_service

logger = logging.getLogger(__name__)
router = APIRouter()

LLM_503_DETAIL = "AI suggestions are unavailable right now. Please try again in a moment."


def _resolve_resume(
    db: Session, user_id: int, resume_id: int | None = None
) -> ResumeProfileDB:
    """Resolve which resume to use: explicit resume_id → primary → most recent."""
    profile = None
    if resume_id is not None:
        profile = (
            db.query(ResumeProfileDB)
            .filter(ResumeProfileDB.id == resume_id, ResumeProfileDB.user_id == user_id)
            .first()
        )
        if not profile:
            raise HTTPException(status_code=404, detail="Resume not found.")

    if profile is None:
        profile = (
            db.query(ResumeProfileDB)
            .filter(ResumeProfileDB.user_id == user_id, ResumeProfileDB.is_primary == 1)
            .first()
            or db.query(ResumeProfileDB)
            .filter(ResumeProfileDB.user_id == user_id)
            .order_by(ResumeProfileDB.created_at.desc())
            .first()
        )

    if not profile or not profile.raw_text:
        raise HTTPException(
            status_code=400,
            detail="No resume profile found. Please upload a resume first.",
        )
    return profile


def _get_resume_text(db: Session, user_id: int) -> str:
    """Get the user's resume text from their primary or most recent profile."""
    return _resolve_resume(db, user_id).raw_text


def _get_job(job_id: int, db: Session) -> ScrapedJob:
    """Get a job by ID or raise 404."""
    job = db.query(ScrapedJob).filter(ScrapedJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


def _persist_active_cover_letter(
    db: Session, user_id: int, job: ScrapedJob, text: str, tone: str | None
) -> None:
    """Upsert a per-job cover letter, mark it active, and bump the sync version
    so it shows up in the web app and the extension's cover-letter fields."""
    db.query(CoverLetter).filter(CoverLetter.user_id == user_id).update(
        {CoverLetter.is_active: 0}, synchronize_session=False
    )
    row = (
        db.query(CoverLetter)
        .filter(CoverLetter.user_id == user_id, CoverLetter.job_id == job.id)
        .first()
    )
    if row is None:
        row = CoverLetter(user_id=user_id, job_id=job.id)
        db.add(row)
    row.company = job.company or ""
    row.job_title = job.title or ""
    row.job_url = job.url or ""
    row.text = text
    row.tone = tone or ""
    row.source = "generated"
    row.is_active = 1
    db.commit()
    bump_profile_version(db, user_id)


def _items(raw) -> list[MatchItem]:
    out = []
    for item in raw or ():
        if isinstance(item, dict) and item.get("label"):
            out.append(MatchItem(label=str(item["label"]), impact=str(item.get("impact") or ""),
                                 note=str(item.get("note") or ""), code=str(item.get("code") or "")))
    return out


def breakdown_from_result(detail: dict) -> MatchBreakdown:
    """The breakdown for one engine result (``fit_engine.explain_job`` JSON).

    Only a SCORED result carries numbers (compliance.md section 2, D-86): every other outcome
    gets 0 in the legacy number fields and no ``display_score``. ``match_label`` is the band; a
    gate failure reads "Not eligible" and a review "We couldn't score this"."""
    outcome = str(detail.get("outcome") or fit_reads.OUTCOME_NEEDS_REVIEW)
    scored = outcome == fit_reads.OUTCOME_SCORED
    explanation = detail.get("explanation") or {}
    band = detail.get("band")
    display = detail.get("display_score") if scored else None
    dimensions = detail.get("dimensions") or {}

    def dimension(name: str) -> int:
        value = (dimensions.get(name) or {}).get("value")
        return max(0, min(100, round(100 * value))) if scored and isinstance(value, (int, float)) else 0

    if band:
        label = band
    elif outcome == fit_reads.OUTCOME_GATE_FAIL:
        label = "Not eligible"
    else:
        label = fit_reads.UNSCORED_TEXT
    strengths, gaps = _items(explanation.get("strengths")), _items(explanation.get("gaps"))
    interval = detail.get("interval")
    return MatchBreakdown(
        overall_score=display if isinstance(display, int) else 0,
        experience_score=dimension("experience"),
        skill_score=dimension("required_skills"),
        industry_score=dimension("domain"),
        match_label=label,
        strengths=[item.label for item in strengths],
        weaknesses=[item.label for item in gaps],
        outcome=outcome,
        band=band,
        display_score=display if isinstance(display, int) else None,
        headline=str(explanation.get("headline") or ""),
        basis=str(explanation.get("basis") or ""),
        strength_items=strengths,
        gaps=gaps,
        to_improve=[str(text) for text in explanation.get("to_improve") or ()],
        notes=[str(text) for text in explanation.get("notes") or ()],
        interval=MatchInterval(**interval) if isinstance(interval, dict) else None,
    )


@router.post("/match-breakdown/{job_id}", response_model=MatchBreakdown)
def match_breakdown(
    job_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """The engine's explanation of how the user's résumé fits a job (DECISIONS D-83).

    Deterministic and free: no AI is called, so it never counts against the daily AI limit.
    A plain (sync) route, so FastAPI runs it in a worker thread: the first breakdown in a
    process builds the engine (about ten seconds) without blocking the event loop. The result
    is also banked, so the job's card shows the same result as the panel."""
    from backend.services import fit_engine
    from backend.services.jobs_sidebar import scoring_resume

    job = _get_job(job_id, db)
    detail = fit_engine.explain_job(db, user_id, job)
    if detail is None:
        raise HTTPException(status_code=400, detail="No resume found. Upload one first.")
    profile = scoring_resume(db, user_id)
    if profile is not None:
        try:
            fit_engine.bank_scores(db, user_id, profile, [job.id])
        except Exception as exc:  # banking is a nicety here; the breakdown stands on its own
            db.rollback()
            logger.error("match-breakdown: banking job %s failed: %s", job_id, type(exc).__name__)
    return breakdown_from_result(detail)


@router.post("/tailor-resume/{job_id}", response_model=TailoredResumeOut)
async def tailor_resume(
    job_id: int,
    user_id: int = Depends(llm_guard),
    db: Session = Depends(get_db),
):
    """Generate a tailored resume for a job."""
    job = _get_job(job_id, db)
    resume_text = _get_resume_text(db, user_id)

    try:
        tailor = ResumeTailor(db)
        result = await tailor.tailor_resume(resume_text, job.description, job_id, user_id=user_id)
        return result
    except (ConnectionError, httpx.ConnectError):
        raise HTTPException(status_code=503, detail=LLM_503_DETAIL)


@router.post("/cover-letter/{job_id}", response_model=CoverLetterOut)
async def cover_letter(
    job_id: int,
    body: CoverLetterIn | None = None,
    user_id: int = Depends(llm_guard),
    db: Session = Depends(get_db),
):
    """Generate (or regenerate in a tone) a cover letter for a job.

    The body is optional, existing callers that POST with no body still get a
    fresh letter from the primary resume.
    """
    job = _get_job(job_id, db)
    resume = _resolve_resume(db, user_id, body.resume_id if body else None)

    try:
        generator = CoverLetterGenerator()
        text = await generator.generate(
            resume.raw_text,
            job.description,
            job.company,
            tone=body.tone if body else None,
            base_text=body.base_text if body else None,
            name=resume.profile_name,
            email=resume.email,
            phone=resume.phone,
            location=resume.location,
            linkedin=resume.linkedin_url,
        )
        # Persist + sync so it's accessible from the web app and extension.
        _persist_active_cover_letter(db, user_id, job, text, body.tone if body else None)
        return CoverLetterOut(
            text=text,
            job_id=job_id,
            generated_at=datetime.datetime.utcnow(),
        )
    except (ConnectionError, httpx.ConnectError):
        raise HTTPException(status_code=503, detail=LLM_503_DETAIL)


# ---------------------------------------------------------------------------
# Saved cover letters (CRUD): synced to web app + extension
# ---------------------------------------------------------------------------


@router.get("/cover-letters", response_model=list[CoverLetterRecordOut])
def list_cover_letters(
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """List the user's saved cover letters, newest first."""
    return (
        db.query(CoverLetter)
        .filter(CoverLetter.user_id == user_id)
        .order_by(CoverLetter.updated_at.desc(), CoverLetter.id.desc())
        .limit(50)
        .all()
    )


@router.post("/cover-letters", response_model=CoverLetterRecordOut)
def save_cover_letter(
    body: CoverLetterSaveIn,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Save a user-written/edited cover letter (optionally as the active one)."""
    if body.set_active:
        db.query(CoverLetter).filter(CoverLetter.user_id == user_id).update(
            {CoverLetter.is_active: 0}, synchronize_session=False
        )
    row = CoverLetter(
        user_id=user_id,
        job_id=body.job_id,
        company=body.company,
        job_title=body.job_title,
        job_url=body.job_url,
        text=body.text,
        tone=body.tone,
        source="user",
        is_active=1 if body.set_active else 0,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    bump_profile_version(db, user_id)
    return row


@router.put("/cover-letters/{cover_letter_id}/active", response_model=CoverLetterRecordOut)
def set_active_cover_letter(
    cover_letter_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Mark a cover letter active (the default used for autofill)."""
    row = (
        db.query(CoverLetter)
        .filter(CoverLetter.id == cover_letter_id, CoverLetter.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Cover letter not found.")
    db.query(CoverLetter).filter(CoverLetter.user_id == user_id).update(
        {CoverLetter.is_active: 0}, synchronize_session=False
    )
    row.is_active = 1
    db.commit()
    db.refresh(row)
    bump_profile_version(db, user_id)
    return row


@router.delete("/cover-letters/{cover_letter_id}", status_code=204)
def delete_cover_letter(
    cover_letter_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    row = (
        db.query(CoverLetter)
        .filter(CoverLetter.id == cover_letter_id, CoverLetter.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Cover letter not found.")
    db.delete(row)
    db.commit()
    bump_profile_version(db, user_id)


# ---------------------------------------------------------------------------
# Web "Generate Custom Resume" flow (See Difference → Align → Review)
#
# These power the per-job-card modal. Nothing is persisted (download/copy only).
# ---------------------------------------------------------------------------


@router.post("/job-analysis/{job_id}", response_model=JobAnalysisOut)
@router.post("/custom-resume-analysis/{job_id}", response_model=JobAnalysisOut)
def job_analysis(
    job_id: int,
    body: RewriteIn | None = None,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Step 1 'See Your Difference': the engine's score, band and matched/missing requirements.

    The deterministic match engine (Task 9.3, D-83), on the job's cached posting (D-84): no AI
    is called, so it does not count against the daily AI limit. A plain (sync) route, so the
    engine (about ten seconds to build cold) runs in a worker thread."""
    job = _get_job(job_id, db)
    resume = _resolve_resume(db, user_id, body.resume_id if body else None)
    return MatchEngine(db).analyze_job(
        db_record_to_document(resume), user_id=user_id, job=job, raw_text=resume.raw_text,
    )


@router.post("/rewrite/{job_id}", response_model=RewriteOut)
@router.post("/custom-resume/{job_id}", response_model=RewriteOut)
async def rewrite_resume(
    job_id: int,
    body: RewriteIn | None = None,
    user_id: int = Depends(llm_guard),
    db: Session = Depends(get_db),
):
    """Step 3 'Review': tailor the resume (structured) with the chosen
    sections/keywords, report before/after scores, and save the version.

    The scores are the match engine's, of the original and the tailored document on the
    job's cached posting (Task 9.3, D-83); the LLM writes the rewrite text only.

    Returns a structured ``ResumeDocument``, the single source the renderer,
    PDF, and DOCX all consume, so the download always matches the preview.
    """
    job = _get_job(job_id, db)
    opts = body or RewriteIn()
    resume = _resolve_resume(db, user_id, opts.resume_id)

    original_document = db_record_to_document(resume)
    original_text = document_to_text(original_document)

    try:
        from backend.services.resume_tailor import tailor_document
        result = await tailor_document(
            db, original_document, job.title, job.company, job.description,
            opts.sections, opts.add_keywords,
            user_id=user_id, job=job, raw_text=resume.raw_text,
        )
    except (ConnectionError, httpx.ConnectError):
        raise HTTPException(status_code=503, detail=LLM_503_DETAIL)

    document = result.document
    tailored_text = result.tailored_text
    diff_summary = result.diff_summary

    version = ResumeVersion(
        user_id=user_id,
        resume_id=resume.id,
        job_id=job_id,
        label=f"Suggestions · {job.title}"[:120],
        source="ai",
        document_json=document.model_dump(),
    )
    db.add(version)
    db.commit()
    db.refresh(version)

    return RewriteOut(
        document=document,
        original_document=original_document,
        tailored_text=tailored_text,
        original_text=original_text,
        diff_summary=diff_summary,
        **result.score_fields(),
        version_id=version.id,
        changes=result.changes,
        gaps=result.gaps,
        figures_to_verify=result.figures_to_verify,
    )


# ---------------------------------------------------------------------------
# Resume version history (Phase 4) + inline edit assistant (Phase 5)
# ---------------------------------------------------------------------------


def _version_out(row: ResumeVersion) -> ResumeVersionOut:
    return ResumeVersionOut(
        id=row.id,
        resume_id=row.resume_id,
        job_id=row.job_id,
        label=row.label or "",
        source=row.source or "user",
        document=ResumeDocument(**(row.document_json or {})),
        created_at=row.created_at,
    )


@router.get("/resume-versions", response_model=list[ResumeVersionOut])
def list_resume_versions(
    resume_id: int | None = None,
    job_id: int | None = None,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """List the current user's saved resume versions, newest first.

    Optionally scoped to a resume and/or a job (job-specific versions).
    """
    q = db.query(ResumeVersion).filter(ResumeVersion.user_id == user_id)
    if resume_id is not None:
        q = q.filter(ResumeVersion.resume_id == resume_id)
    if job_id is not None:
        q = q.filter(ResumeVersion.job_id == job_id)
    rows = q.order_by(ResumeVersion.created_at.desc(), ResumeVersion.id.desc()).limit(50).all()
    return [_version_out(r) for r in rows]


@router.post("/resume-versions", response_model=ResumeVersionOut)
def save_resume_version(
    body: ResumeVersionIn,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Persist a structured resume document as a new version."""
    row = ResumeVersion(
        user_id=user_id,
        resume_id=body.resume_id,
        job_id=body.job_id,
        label=body.label or "Saved version",
        source=body.source or "user",
        document_json=body.document.model_dump(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Custom resumes are a sync target, bump so the extension picks it up.
    bump_profile_version(db, user_id)
    return _version_out(row)


@router.get("/resume-versions/{version_id}", response_model=ResumeVersionOut)
def get_resume_version(
    version_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    row = (
        db.query(ResumeVersion)
        .filter(ResumeVersion.id == version_id, ResumeVersion.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Version not found.")
    return _version_out(row)


@router.delete("/resume-versions/{version_id}", status_code=204)
def delete_resume_version(
    version_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    row = (
        db.query(ResumeVersion)
        .filter(ResumeVersion.id == version_id, ResumeVersion.user_id == user_id)
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Version not found.")
    db.delete(row)
    db.commit()


@router.post("/edit-snippet", response_model=SnippetEditOut)
async def edit_snippet(
    body: SnippetEditIn,
    user_id: int = Depends(llm_guard),
    db: Session = Depends(get_db),
):
    """Apply a single AI editing action to a selected snippet of resume text.

    Only the provided text is transformed and returned, never the whole resume.
    """
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="No text selected.")

    job_description = ""
    if body.job_id:
        job = db.query(ScrapedJob).filter(ScrapedJob.id == body.job_id).first()
        if job:
            job_description = job.description or ""

    try:
        llm = get_llm_service()
        edited = await llm.edit_snippet(text, body.action, job_description)
        return SnippetEditOut(text=edited or text)
    except (ConnectionError, httpx.ConnectError):
        raise HTTPException(status_code=503, detail=LLM_503_DETAIL)


@router.post("/cron-match-alerts")
async def cron_match_alerts(
    _cron: None = Depends(verify_cron_secret),
    db: Session = Depends(get_db),
):
    """Manually trigger the match-alert sweep (also runs inside cron-poll).

    For every verified user with a resume, score the newest jobs they haven't
    been alerted about yet with the fit engine, then send a single deduped
    digest of their confident Strong matches. Kept as a standalone endpoint for manual runs / testing;
    the scheduled trigger is folded into /github-sources/cron-poll so the app
    stays within Vercel's 2-cron Hobby limit. Authenticated via the cron secret.
    """
    return await sweep_match_alerts(db)
