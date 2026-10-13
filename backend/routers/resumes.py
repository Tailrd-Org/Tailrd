"""
Resume upload and parsing endpoints.

POST /resumes/upload, accepts PDF or DOCX, extracts text, analyzes via Claude,
stores profile in DB, returns typed ResumeProfile.
GET /resumes, list all resumes for the current user.
GET /resumes/{id}, get full resume detail including profile and analysis report.
"""

import datetime
import logging
from fastapi import APIRouter, BackgroundTasks, UploadFile, File, HTTPException, Depends, Response
from sqlalchemy.orm import Session

from backend.db.database import get_db
from backend.db.models import ResumeProfileDB, ScrapedJob
from backend.auth.dependencies import get_verified_user_id
from backend.schemas.resume import (
    ResumeProfile,
    ResumeUploadResponse,
    ResumeListItem,
    ResumeDetailResponse,
    ResumeImproveResponse,
    ResumeUpdateRequest,
    AnalysisReport,
)
from backend.services.resume_parser import extract_text
from backend.services.llm import get_llm_service
from backend.services import blob_storage
from backend.services.profile_version import bump_profile_version
from backend.services.resume_document import (
    db_record_to_document,
    describe_changes,
    document_to_profile,
)
from backend.services.resume_metrics import build_digest, render_digest
from backend.services.resume_pdf import render_resume_pdf

logger = logging.getLogger(__name__)
router = APIRouter()

#: How many of the newest visible jobs an upload pre-scores (Task 9.2).
UPLOAD_PRESCORE_JOBS = 300


def prescore_upload(db: Session, user_id: int, profile: ResumeProfileDB) -> int:
    """Bank fit scores for the newest UPLOAD_PRESCORE_JOBS visible jobs against a new résumé.

    Deterministic, no AI (DECISIONS D-83). It may build the engine (about ten seconds cold),
    so it runs after the response, in a worker thread (BackgroundTasks). Returns how many
    jobs were newly scored; never raises (the upload already succeeded)."""
    from backend.services import fit_engine
    from backend.services.jobs_sidebar import visible_job_filter

    ids = [
        row[0]
        for row in db.query(ScrapedJob.id)
        .filter(visible_job_filter())
        .order_by(ScrapedJob.id.desc())
        .limit(UPLOAD_PRESCORE_JOBS)
        .all()
    ]
    try:
        return len(fit_engine.bank_scores(db, user_id, profile, ids))
    except fit_engine.FitTraceSaltError:
        db.rollback()
        logger.error("Résumé upload pre-score skipped: FIT_TRACE_SALT is missing or too short.")
    except Exception as exc:
        db.rollback()
        logger.error("Résumé upload pre-score failed: %s", type(exc).__name__)
    return 0


def _prescore_in_background(user_id: int, profile_id: int) -> None:
    """``prescore_upload`` on its own session (the request's is closed by then)."""
    from backend.db.database import SessionLocal

    db = SessionLocal()
    try:
        profile = db.get(ResumeProfileDB, profile_id)
        if profile is not None and profile.user_id == user_id:
            prescore_upload(db, user_id, profile)
    except Exception as exc:
        logger.error("Résumé upload pre-score could not start: %s", type(exc).__name__)
    finally:
        db.close()


def _record_to_profile(record: ResumeProfileDB) -> ResumeProfile:
    """The one place a DB row becomes a ResumeProfile.

    Every section the parser can extract has to be listed here, a field missed
    here is a section the user silently loses.
    """
    return ResumeProfile(
        name=record.profile_name or "",
        email=record.email or "",
        phone=record.phone or "",
        location=record.location or "",
        linkedin_url=record.linkedin_url or "",
        github_url=record.github_url or "",
        other_link=record.other_link or "",
        summary=record.summary or "",
        summary_title=record.summary_title or "",
        skills=record.skills or [],
        experience=record.experience or [],
        education=record.education or [],
        projects=record.projects or [],
        technologies=record.technologies or {},
        custom_sections=record.custom_sections or [],
        section_order=record.section_order or [],
    )


def _record_to_detail(record: ResumeProfileDB) -> ResumeDetailResponse:
    analysis_report = (
        AnalysisReport(**record.analysis_report) if record.analysis_report is not None else None
    )
    return ResumeDetailResponse(
        id=record.id,
        name=record.name,
        target_job_title=record.target_job_title,
        is_primary=bool(record.is_primary),
        profile=_record_to_profile(record),
        analysis_report=analysis_report,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _owned_resume(resume_id: int, user_id: int, db: Session) -> ResumeProfileDB:
    record = (
        db.query(ResumeProfileDB)
        .filter(ResumeProfileDB.id == resume_id, ResumeProfileDB.user_id == user_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Resume not found.")
    return record


@router.get("", response_model=list[ResumeListItem])
def list_resumes(
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """List all resumes for the current user, ordered by most recently created first."""
    records = (
        db.query(ResumeProfileDB)
        .filter(ResumeProfileDB.user_id == user_id)
        .order_by(ResumeProfileDB.created_at.desc())
        .all()
    )
    return [
        ResumeListItem(
            id=r.id,
            name=r.name,
            target_job_title=r.target_job_title,
            is_primary=bool(r.is_primary),
            status=r.status,
            created_at=r.created_at,
            updated_at=r.updated_at,
            has_file=bool(r.file_blob_url),
        )
        for r in records
    ]


@router.put("/{resume_id}/primary", response_model=ResumeDetailResponse)
def set_primary_resume(
    resume_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Set a resume as primary, unsetting all others for this user."""
    record = _owned_resume(resume_id, user_id, db)

    # Unset all user's resumes as primary
    db.query(ResumeProfileDB).filter(ResumeProfileDB.user_id == user_id).update(
        {ResumeProfileDB.is_primary: 0}
    )
    # Set the target resume as primary
    record.is_primary = 1
    db.commit()
    db.refresh(record)

    # The active resume changed: sync it to the extension.
    bump_profile_version(db, user_id)

    return _record_to_detail(record)


@router.post("/{resume_id}/analyze", response_model=AnalysisReport)
async def analyze_resume(
    resume_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Run AI quality analysis on a resume and persist the report.

    The model is handed a deterministic digest of the resume alongside the raw
    text, so its counts and evidence describe what is actually on the page.
    """
    record = _owned_resume(resume_id, user_id, db)

    if not record.raw_text:
        raise HTTPException(status_code=422, detail="Resume has no extracted text to analyze.")

    digest = render_digest(build_digest(record.raw_text, _record_to_profile(record)))

    llm = get_llm_service()
    try:
        report = await llm.analyze_resume_quality(record.raw_text, digest)
    except Exception as e:
        logger.error("AI quality analysis failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Couldn't analyze this resume: {e}")

    record.analysis_report = report.model_dump(mode="json")
    record.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(record)

    return report


@router.post("/{resume_id}/improve", response_model=ResumeImproveResponse)
async def improve_resume(
    resume_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Rewrite a resume against its own analysis report.

    Returns a *preview*: the improved profile plus a deterministic list of what
    changed. Nothing is persisted, the client applies it with PUT /resumes/{id}
    once the user accepts. Every factual field is re-imposed from the stored
    record by ``merge_rewrite``, so the rewrite can reword but never invent.
    """
    record = _owned_resume(resume_id, user_id, db)

    original_profile = _record_to_profile(record)
    if not (original_profile.experience or original_profile.projects or original_profile.education):
        raise HTTPException(
            status_code=422,
            detail="This resume has no parsed content to improve. Try re-uploading it.",
        )

    report = (
        AnalysisReport(**record.analysis_report) if record.analysis_report is not None else None
    )
    original_doc = db_record_to_document(record)

    llm = get_llm_service()
    try:
        result = await llm.improve_resume_structured(original_doc, report)
    except Exception as e:
        logger.error("AI improvement failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Couldn't prepare suggestions: {e}")

    improved_profile = document_to_profile(result.document, original_profile)
    changes = describe_changes(original_doc, result.document)
    changes += [f"Needs your input: {u}" for u in result.unresolved]

    return ResumeImproveResponse(profile=improved_profile, changes=changes)


@router.get("/{resume_id}/file")
async def download_resume_file(
    resume_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Stream the original PDF/DOCX for a resume the caller owns.

    Authorization is enforced here and the bytes are proxied from Blob storage,
    so the underlying public Blob URL is never handed to the client. Powers the
    Chrome extension's auto-upload into ATS application forms.
    """
    record = (
        db.query(ResumeProfileDB)
        .filter(ResumeProfileDB.id == resume_id, ResumeProfileDB.user_id == user_id)
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="Resume not found.")

    # Edited after upload → the stored original no longer matches what the user
    # sees in the web app. Render the CURRENT document so the extension attaches
    # exactly that; the untouched original streams below as before. Render
    # failures fall back to the original rather than failing the attach.
    if record.content_updated_at is not None:
        try:
            pdf = render_resume_pdf(db_record_to_document(record))
            base = (record.file_name or record.name or "resume").rsplit(".", 1)[0] or "resume"
            return Response(
                content=pdf,
                media_type="application/pdf",
                headers={
                    "Content-Disposition": f'inline; filename="{base}.pdf"',
                    "Cache-Control": "private, no-store",
                },
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Fresh render failed for resume %s (%s), serving the stored original", resume_id, e
            )

    if not record.file_blob_url:
        raise HTTPException(
            status_code=404,
            detail="No original file stored for this resume. Re-upload it so Tailrd can attach it.",
        )

    content = await blob_storage.download(record.file_blob_url)
    if content is None:
        raise HTTPException(status_code=502, detail="Could not retrieve the stored resume file.")

    filename = record.file_name or "resume"
    media_type = record.file_content_type or "application/octet-stream"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            # `inline` lets the extension read bytes; the filename is preserved
            # so the ATS upload widget shows the real name.
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/{resume_id}", response_model=ResumeDetailResponse)
def get_resume(
    resume_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Get full resume detail by id, including profile and analysis report."""
    return _record_to_detail(_owned_resume(resume_id, user_id, db))


@router.put("/{resume_id}", response_model=ResumeDetailResponse)
def update_resume(
    resume_id: int,
    body: ResumeUpdateRequest,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Update a resume's name, target_job_title, and/or profile fields."""
    record = _owned_resume(resume_id, user_id, db)

    if body.name is not None:
        record.name = body.name
    if body.target_job_title is not None:
        record.target_job_title = body.target_job_title
    if body.profile is not None:
        profile = body.profile
        record.profile_name = profile.name
        record.email = profile.email
        record.phone = profile.phone
        record.location = profile.location
        record.linkedin_url = profile.linkedin_url
        record.github_url = profile.github_url
        record.other_link = profile.other_link
        record.skills = profile.skills
        record.experience = [exp.model_dump() for exp in profile.experience]
        record.education = [edu.model_dump() for edu in profile.education]
        record.projects = [proj.model_dump() for proj in profile.projects]
        record.technologies = profile.technologies

        # A client that predates these fields (the extension, an old tab) sends a
        # profile without them. Writing the schema default would silently delete
        # the user's summary and custom sections, so only touch what was sent.
        sent = profile.model_fields_set
        if "summary" in sent:
            record.summary = profile.summary
        if "summary_title" in sent:
            record.summary_title = profile.summary_title
        if "custom_sections" in sent:
            record.custom_sections = [cs.model_dump() for cs in profile.custom_sections]
        if "section_order" in sent:
            record.section_order = profile.section_order

        # The document's content diverged from the uploaded file, from now on
        # the file download renders the current document (see download_resume_file).
        record.content_updated_at = datetime.datetime.utcnow()

    record.updated_at = datetime.datetime.utcnow()
    db.commit()
    db.refresh(record)

    # Profile fields may have changed, sync to the extension.
    bump_profile_version(db, user_id)

    return _record_to_detail(record)


@router.delete("/{resume_id}", status_code=204)
async def delete_resume(
    resume_id: int,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Delete a resume by id. Returns 204 on success, 404 if not found."""
    record = _owned_resume(resume_id, user_id, db)
    blob_url = record.file_blob_url
    db.delete(record)
    db.commit()
    if blob_url:
        await blob_storage.delete(blob_url)
    bump_profile_version(db, user_id)


@router.post("/upload", response_model=ResumeUploadResponse)
async def upload_resume(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
):
    """Upload a PDF or DOCX resume, parse it, and store the profile."""
    if file.content_type not in (
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ):
        raise HTTPException(status_code=400, detail="Only PDF and DOCX files are accepted.")

    # File size limit: 10 MB
    MAX_FILE_SIZE = 10 * 1024 * 1024
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size is 10 MB.")
    filename = file.filename or "resume"

    # Extract raw text
    try:
        raw_text = extract_text(content, filename)
    except Exception as e:
        logger.error("Text extraction failed: %s", e)
        raise HTTPException(status_code=422, detail=f"Could not extract text: {e}")

    if not raw_text.strip():
        raise HTTPException(status_code=422, detail="Extracted text is empty.")

    # Analyze with Claude (graceful degradation if rate limited)
    llm = get_llm_service()
    profile = None
    try:
        profile = await llm.analyze_resume(raw_text)
    except Exception as e:
        logger.warning("AI analysis failed (will save raw text): %s", e)
        # Don't fail the upload: save with basic info extracted from text
        from backend.schemas.resume import ResumeProfile as RP
        profile = RP(name=file.filename.rsplit(".", 1)[0] if file.filename else "")

    # Store the original file so the extension can auto-upload it into ATS
    # forms later. Best-effort: a None result (e.g. Blob not configured) just
    # means this resume has no downloadable file, parsing still succeeds.
    blob = await blob_storage.upload_resume(content, filename, file.content_type or "", user_id)

    # Persist to DB
    db_profile = ResumeProfileDB(
        user_id=user_id,
        name=file.filename.rsplit(".", 1)[0] if file.filename else "Untitled Resume",
        profile_name=profile.name,
        email=profile.email,
        phone=profile.phone,
        location=profile.location,
        linkedin_url=profile.linkedin_url,
        github_url=profile.github_url,
        other_link=profile.other_link,
        summary=profile.summary,
        summary_title=profile.summary_title,
        skills=profile.skills,
        experience=[exp.model_dump() for exp in profile.experience],
        education=[edu.model_dump() for edu in profile.education],
        projects=[proj.model_dump() for proj in profile.projects],
        technologies=profile.technologies,
        custom_sections=[cs.model_dump() for cs in profile.custom_sections],
        section_order=profile.section_order,
        raw_text=raw_text,
        status="analyzed" if (profile.experience or profile.projects) else "uploaded",
        file_blob_url=blob["url"] if blob else None,
        file_name=blob["name"] if blob else None,
        file_content_type=blob["content_type"] if blob else None,
        file_size=blob["size"] if blob else None,
        file_uploaded_at=datetime.datetime.utcnow() if blob else None,
    )
    db.add(db_profile)
    db.commit()
    db.refresh(db_profile)

    # Bump the sync version so the extension picks up the new resume.
    bump_profile_version(db, user_id)

    # Pre-score the newest visible jobs with the fit engine after the response, in
    # the same invocation (BackgroundTasks runs a sync callable in a worker thread),
    # so the feed shows this résumé's results right away. Alerts are left to the sweep.
    background_tasks.add_task(_prescore_in_background, user_id, db_profile.id)

    return ResumeUploadResponse(id=db_profile.id, profile=profile)
