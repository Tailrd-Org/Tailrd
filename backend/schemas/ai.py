"""
Pydantic schemas for AI-generated content (tailored resumes, cover letters).
"""

import datetime
from pydantic import BaseModel

from backend.schemas.resume_document import ResumeDocument


class TailoredResumeOut(BaseModel):
    """A tailored resume returned to the frontend."""
    id: int
    job_id: int
    tailored_text: str
    diff_summary: str
    status: str
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class CoverLetterOut(BaseModel):
    """A generated cover letter returned to the frontend."""
    text: str
    job_id: int
    generated_at: datetime.datetime


# ---------------------------------------------------------------------------
# Web "Generate Custom Resume" flow (See Difference → Align → Review)
# ---------------------------------------------------------------------------


class JobAnalysisOut(BaseModel):
    """Resume↔job analysis for the rewrite flow's Step 1, from the match engine (Task 9.3, D-83).

    Scores are None unless the engine outcome is SCORED (SCORED_LOW_CONFIDENCE shows the band
    only). ``ats_score`` is the same engine number as ``overall_score`` (D-123). See
    services/match_engine.py for every field's source.
    """
    overall_score: int | None = None  # 0-100, the engine's displayed score
    ats_score: int | None = None  # = overall_score (D-123)
    match_label: str  # the band, "We couldn't score this" or "Not eligible"
    keyword_coverage: int | None = None  # 0-100, required + preferred requirement coverage
    matched_keywords: list[str] = []
    missing_keywords: list[str] = []
    strengths: list[str] = []
    weaknesses: list[str] = []
    suggestions: list[str] = []
    outcome: str | None = None  # SCORED / SCORED_LOW_CONFIDENCE / NEEDS_REVIEW / GATE_FAIL
    reasons: list[str] = []  # why the result is not SCORED, as sentences


class RewriteIn(BaseModel):
    """Options chosen in the 'Align' step (all optional)."""
    resume_id: int | None = None
    sections: list[str] = []  # e.g. ["Skills", "Work Experience", "Projects"]
    add_keywords: list[str] = []


class RewriteOut(BaseModel):
    """Result of the tailoring pass, with before/after scores for Step 3.

    ``document`` is the structured, rewritten resume that the renderer/export
    consume (the single source of truth). ``tailored_text``/``original_text``
    are flattened plain text kept for the Copy button and diffing.
    """
    document: ResumeDocument
    original_document: ResumeDocument
    tailored_text: str
    original_text: str
    diff_summary: str
    # The engine's scores of the original and the tailored document (Task 9.3, D-83, D-123):
    # None unless that side's outcome is SCORED.
    original_overall_score: int | None = None
    new_overall_score: int | None = None
    new_ats_score: int | None = None
    new_keyword_coverage: int | None = None
    original_outcome: str | None = None
    outcome: str | None = None
    original_match_label: str = ""
    match_label: str = ""
    version_id: int | None = None
    changes: list[str] = []
    gaps: list[str] = []
    figures_to_verify: list[str] = []


class CoverLetterIn(BaseModel):
    """Optional body for /ai/cover-letter/{job_id} (resume choice + tone)."""
    resume_id: int | None = None
    # professional | formal | enthusiastic | concise | technical
    tone: str | None = None
    # When regenerating in a new tone, the existing letter to rewrite.
    base_text: str | None = None


class CoverLetterRecordOut(BaseModel):
    """A persisted cover letter, synced to the web app and extension."""
    id: int
    job_id: int | None = None
    company: str = ""
    job_title: str = ""
    job_url: str = ""
    text: str = ""
    tone: str = ""
    source: str = "generated"
    is_active: bool = False
    created_at: datetime.datetime

    model_config = {"from_attributes": True}


class CoverLetterSaveIn(BaseModel):
    """Save (or edit) a cover letter from the web app or extension."""
    job_id: int | None = None
    company: str = ""
    job_title: str = ""
    job_url: str = ""
    text: str
    tone: str = ""
    set_active: bool = True


# ---------------------------------------------------------------------------
# Version history (Phase 4) + inline edit assistant (Phase 5)
# ---------------------------------------------------------------------------


class ResumeVersionIn(BaseModel):
    """Save a structured resume document as a version."""
    resume_id: int | None = None
    job_id: int | None = None
    label: str = ""
    source: str = "user"  # original | ai | user
    document: ResumeDocument


class ResumeVersionOut(BaseModel):
    """A stored resume version returned to the frontend."""
    id: int
    resume_id: int | None
    job_id: int | None
    label: str
    source: str
    document: ResumeDocument
    created_at: datetime.datetime


class SnippetEditIn(BaseModel):
    """Apply an AI action to a selected snippet of resume text."""
    text: str
    # rewrite | shorten | expand | professional | ats | impact | grammar
    action: str
    job_id: int | None = None


class SnippetEditOut(BaseModel):
    text: str
