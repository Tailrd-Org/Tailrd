"""Schemas for the extension résumé-tailoring endpoints (mounted at /api)."""
from pydantic import BaseModel

from backend.schemas.resume_document import ResumeDocument


class TailorResumeIn(BaseModel):
    """Tailor a résumé to a scraped job (no job_id)."""
    resume_id: int | None = None
    job_description: str = ""
    job_title: str = ""
    company: str = ""
    sections: list[str] | None = None
    # None -> weave all missing keywords; a list (even []) -> use exactly that.
    add_keywords: list[str] | None = None


class TailorResumeOut(BaseModel):
    """Tailored document + before/after scores + the stable candidate keyword set."""
    document: ResumeDocument
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
    matched_keywords: list[str] = []
    missing_keywords: list[str] = []
    diff_summary: str = ""
    changes: list[str] = []
    gaps: list[str] = []
    figures_to_verify: list[str] = []


class CustomResumeAnalysisIn(BaseModel):
    """Analyze a résumé against a scraped job (no job_id)."""
    resume_id: int | None = None
    job_title: str = ""
    company: str = ""
    job_description: str = ""


class CustomResumeIn(CustomResumeAnalysisIn):
    """Analysis inputs + the Align-step choices."""
    sections: list[str] | None = None
    add_keywords: list[str] | None = None


class RenderResumeIn(BaseModel):
    document: ResumeDocument
    filename: str | None = None


class RenderResumeOut(BaseModel):
    data_base64: str
    name: str
    content_type: str = "application/pdf"
