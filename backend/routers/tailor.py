"""
Extension résumé-tailoring endpoints (mounted at /api).

POST /api/tailor-resume, tailor a résumé to a *scraped* job (no job_id),
                          reusing the same services as the web Custom Resume
                          flow. Returns the structured document + before/after
                          scores + the candidate keyword set (from `before`,
                          so the overlay's chips stay stable across regenerates).
                          Every score and keyword is the deterministic match
                          engine's (Task 9.3, D-83); the LLM only rewrites.
POST /api/render-resume, render a structured document to a PDF (base64 JSON).

Used by the Chrome extension on live application pages, where there is no
ScrapedJob row to key off (unlike the web /ai/custom-resume/{job_id} flow).
"""
import base64
import logging
import re

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_verified_user_id
from backend.services.usage_limiter import fit_analysis_guard, llm_guard
from backend.db.database import get_db
from backend.db.models import ResumeVersion
from backend.routers.ai import LLM_503_DETAIL, _resolve_resume
from backend.schemas.ai import JobAnalysisOut, RewriteOut
from backend.schemas.tailor import (
    CustomResumeAnalysisIn, CustomResumeIn,
    RenderResumeIn, RenderResumeOut, TailorResumeIn, TailorResumeOut,
)
from backend.services.match_engine import MatchEngine
from backend.services.resume_document import db_record_to_document, document_to_text
from backend.services.resume_pdf import render_resume_pdf
from backend.services.resume_tailor import tailor_document

logger = logging.getLogger(__name__)
router = APIRouter()


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "resume"


@router.post("/tailor-resume", response_model=TailorResumeOut)
async def tailor_resume_endpoint(
    body: TailorResumeIn,
    user_id: int = Depends(llm_guard),
    db: Session = Depends(get_db),
):
    """Tailor the caller's résumé to a scraped job description."""
    resume = _resolve_resume(db, user_id, body.resume_id)  # 400 if none on file
    original_document = db_record_to_document(resume)
    try:
        result = await tailor_document(
            db, original_document, body.job_title, body.company,
            body.job_description, body.sections, body.add_keywords,
            user_id=user_id, raw_text=resume.raw_text,
        )
    except (ConnectionError, httpx.HTTPError):  # RewriteFailedError is a ConnectionError
        raise HTTPException(status_code=503, detail=LLM_503_DETAIL)

    return TailorResumeOut(
        document=result.document,
        **result.score_fields(),
        matched_keywords=result.before.matched_keywords,
        missing_keywords=result.before.missing_keywords,
        diff_summary=result.diff_summary,
        changes=result.changes,
        gaps=result.gaps,
        figures_to_verify=result.figures_to_verify,
    )


@router.post("/render-resume", response_model=RenderResumeOut)
def render_resume_endpoint(
    body: RenderResumeIn,
    user_id: int = Depends(get_verified_user_id),
):
    """Render a structured résumé document to a PDF, returned as base64."""
    try:
        pdf = render_resume_pdf(body.document)
    except Exception as e:
        logger.warning("Resume PDF render failed: %s", e)
        raise HTTPException(status_code=422, detail="Could not render this résumé document.")
    base = body.filename or "resume"
    if base.lower().endswith(".pdf"):
        base = base[:-4]
    name = f"{_slug(base)}.pdf"
    return RenderResumeOut(
        data_base64=base64.b64encode(pdf).decode("ascii"),
        name=name,
    )


@router.post("/custom-resume-analysis", response_model=JobAnalysisOut)
def custom_resume_analysis_endpoint(
    body: CustomResumeAnalysisIn,
    user_id: int = Depends(fit_analysis_guard),
    db: Session = Depends(get_db),
):
    """Step 1 'See Your Difference' for a scraped job (no job_id).

    Extension analog of the web ``/ai/custom-resume-analysis/{job_id}``, accepts
    raw job context since there is no ScrapedJob row on a live application page.
    The deterministic match engine's analysis (Task 9.3, D-83): no AI is called, so it
    does not count against the daily AI limit; it has its own per-user rate limit
    (fit_analysis_guard). A plain (sync) route, so the engine
    (about ten seconds to build cold) runs in a worker thread.
    """
    resume = _resolve_resume(db, user_id, body.resume_id)  # 400 if none on file
    return MatchEngine(db).analyze_job(
        db_record_to_document(resume), user_id=user_id, job_title=body.job_title,
        company=body.company, job_description=body.job_description, raw_text=resume.raw_text,
    )


@router.post("/custom-resume", response_model=RewriteOut)
async def custom_resume_endpoint(
    body: CustomResumeIn,
    user_id: int = Depends(llm_guard),
    db: Session = Depends(get_db),
):
    """Step 3 'Review' for a scraped job: tailor + before/after + save a version.

    Extension analog of the web ``/ai/custom-resume/{job_id}``. The saved
    ``ResumeVersion`` has ``job_id=None`` (no ScrapedJob to key off).
    """
    resume = _resolve_resume(db, user_id, body.resume_id)  # 400 if none on file
    original_document = db_record_to_document(resume)
    original_text = document_to_text(original_document)
    try:
        result = await tailor_document(
            db, original_document, body.job_title, body.company,
            body.job_description, body.sections, body.add_keywords,
            user_id=user_id, raw_text=resume.raw_text,
        )
    except (ConnectionError, httpx.HTTPError):  # RewriteFailedError is a ConnectionError
        raise HTTPException(status_code=503, detail=LLM_503_DETAIL)

    version = ResumeVersion(
        user_id=user_id,
        resume_id=resume.id,
        job_id=None,
        label=(f"Suggestions · {body.job_title}"[:120] if body.job_title else "Suggestions · Custom résumé"),
        source="ai",
        document_json=result.document.model_dump(),
    )
    db.add(version)
    db.commit()
    db.refresh(version)

    return RewriteOut(
        document=result.document,
        original_document=original_document,
        tailored_text=result.tailored_text,
        original_text=original_text,
        diff_summary=result.diff_summary,
        **result.score_fields(),
        version_id=version.id,
        changes=result.changes,
        gaps=result.gaps,
        figures_to_verify=result.figures_to_verify,
    )
