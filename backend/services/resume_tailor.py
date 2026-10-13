"""
ResumeTailor, generates tailored resume versions for specific job postings.

Uses Claude to rewrite/reorder resume content to emphasize skills and experience
relevant to the target job. Stores tailored versions linked to specific jobs.
The LLM writes the rewrite text only: every before/after score is the deterministic match
engine's (``tailor_document``, Task 9.3, D-83).
"""

import difflib
import logging
from dataclasses import dataclass
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from backend.db.models import TailoredResume, ScrapedJob, ResumeProfileDB
from backend.schemas.ai import JobAnalysisOut, TailoredResumeOut
from backend.schemas.resume_document import ResumeDocument
from backend.services.fabrication_check import find_unsupported_figures
from backend.services.llm import get_llm_service
from backend.services.resume_document import document_to_text, describe_changes

logger = logging.getLogger(__name__)

#: Leads ``reasons`` when the original was SCORED and the rewrite is SCORED_LOW_CONFIDENCE: the
#: usual cause is a skill the rewrite only listed, with no example of its use (D-123).
LISTED_ONLY_SENTENCE = (
    "Skills listed without an example where you used them make us less sure, so we show the band "
    "instead of a number. Add a bullet that shows the skill to get a score."
)


class ResumeTailor:
    """Generates tailored resume versions for specific job postings."""

    def __init__(self, db: Session):
        self.db = db
        self._llm = None

    @property
    def llm(self):
        """Lazily construct the LLM service so pure helpers (and tests that
        monkeypatch the methods) don't need an OPENAI_API_KEY at import time."""
        if self._llm is None:
            self._llm = get_llm_service()
        return self._llm

    async def tailor_resume(
        self, resume_text: str, job_description: str, job_id: int, user_id: str | None = None
    ) -> TailoredResume:
        """Generate a tailored resume for a specific job.

        Uses Claude to rewrite the resume emphasizing relevant skills/experience.
        Stores the result in the TailoredResume table.

        Returns the TailoredResume database record.
        """
        # Generate tailored version via Claude
        tailored_text = await self.llm.tailor_resume(resume_text, job_description)

        # Compute diff summary
        diff_summary = self.compute_diff(resume_text, tailored_text)

        # Store in database
        tailored = TailoredResume(
            user_id=user_id,
            job_id=job_id,
            original_text=resume_text,
            tailored_text=tailored_text,
            diff_summary=diff_summary,
            status="draft",
        )
        self.db.add(tailored)
        self.db.commit()
        self.db.refresh(tailored)

        return tailored

    def compute_diff(self, original: str, tailored: str) -> str:
        """Produce a human-readable diff summary between original and tailored resume.

        Uses unified diff format to show what changed.
        """
        original_lines = original.splitlines(keepends=True)
        tailored_lines = tailored.splitlines(keepends=True)

        diff = difflib.unified_diff(
            original_lines,
            tailored_lines,
            fromfile="Original Resume",
            tofile="Tailored Resume",
            lineterm="",
        )

        return "".join(diff)

    async def get_or_create(
        self, job_id: int
    ) -> TailoredResume | None:
        """Get existing tailored resume for a job, or create one.

        Returns None if no resume profile is available.
        """
        # Check for existing tailored resume
        existing = (
            self.db.query(TailoredResume)
            .filter(TailoredResume.job_id == job_id)
            .order_by(TailoredResume.created_at.desc())
            .first()
        )
        if existing:
            return existing

        # Get job and resume profile
        job = self.db.query(ScrapedJob).filter(ScrapedJob.id == job_id).first()
        if not job:
            return None

        profile = self.db.query(ResumeProfileDB).order_by(
            ResumeProfileDB.created_at.desc()
        ).first()
        if not profile or not profile.raw_text:
            return None

        return await self.tailor_resume(profile.raw_text, job.description, job_id)


@dataclass
class TailorResult:
    """Output of one tailoring pass: the rewritten doc + the engine's before/after analyses."""
    document: ResumeDocument
    original_text: str
    tailored_text: str
    before: JobAnalysisOut
    after: JobAnalysisOut
    diff_summary: str
    changes: list[str]
    gaps: list[str]
    figures_to_verify: list[str]

    def score_fields(self) -> dict:
        """The before/after fields every tailoring response carries (RewriteOut, TailorResumeOut).
        Scores are None unless that side is SCORED; ``new_ats_score`` is the same engine number
        as ``new_overall_score`` (D-123)."""
        return {
            "original_overall_score": self.before.overall_score,
            "new_overall_score": self.after.overall_score,
            "new_ats_score": self.after.ats_score,
            "new_keyword_coverage": self.after.keyword_coverage,
            "original_outcome": self.before.outcome,
            "outcome": self.after.outcome,
            "original_match_label": self.before.match_label,
            "match_label": self.after.match_label,
            "reasons": self.reasons(),
        }

    def reasons(self) -> list[str]:
        """Why the tailored side shows no number: the engine's plain sentences (compliance.md
        section 2), led by ``LISTED_ONLY_SENTENCE`` when a SCORED original became
        SCORED_LOW_CONFIDENCE. Empty when the tailored side is SCORED."""
        reasons = list(self.after.reasons)
        if self.before.outcome == "SCORED" and self.after.outcome == "SCORED_LOW_CONFIDENCE":
            reasons = [LISTED_ONLY_SENTENCE, *(text for text in reasons if text != LISTED_ONLY_SENTENCE)]
        return reasons


async def tailor_document(
    db: Session,
    original_document: ResumeDocument,
    job_title: str,
    company: str,
    job_description: str,
    sections: list[str] | None = None,
    add_keywords: list[str] | None = None,
    *,
    user_id: int | None = None,
    job: ScrapedJob | None = None,
    raw_text: str | None = None,
) -> TailorResult:
    """Tailor a structured résumé to a job and score it before/after.

    Shared by the web ``/ai/custom-resume`` flow and the extension
    ``/api/tailor-resume`` endpoint so the two cannot drift.

    Scores (Task 9.3, D-83): the original and the tailored document are scored by the
    deterministic match engine against the SAME parsed posting (the job row's cached posting
    when ``job`` is given, else the pasted ``job_description``). The LLM is called once, for the
    rewrite text, and is fed the engine's missing requirements; it is never asked for a score,
    a label or a keyword list. ``user_id`` brings in the user's saved answers (work
    authorisation, location), as the feed reads them; ``raw_text`` is read only when a document
    has no content (D-80).

    Keyword semantics: when ``add_keywords`` is None, the engine's suggested terms are woven in
    (``suggested_keywords``: the REQUIRED skills the résumé does not show at all, at most 10;
    partial matches and preferred skills stay chips the user can pick); when a list is given
    (even empty), exactly that set is used.
    """
    from backend.services.match_engine import MatchEngine  # local: avoid import cycle

    engine = MatchEngine(db)
    tailor = ResumeTailor(db)
    original_text = document_to_text(original_document)
    # The engine is CPU-bound (and builds itself on first use): keep it off the event loop.
    posting = await run_in_threadpool(
        engine.posting, job=job, job_title=job_title, company=company, job_description=job_description,
    )
    before = await run_in_threadpool(engine.analyze, original_document, posting, user_id=user_id,
                                     raw_text=raw_text)
    keywords = add_keywords if add_keywords is not None else list(before.suggested_keywords)
    structured = await tailor.llm.tailor_resume_structured(
        original_document, job_description, sections, keywords
    )
    document = structured.document
    tailored_text = document_to_text(document)
    after = await run_in_threadpool(engine.analyze, document, posting, user_id=user_id, raw_text=raw_text)
    diff_summary = tailor.compute_diff(original_text, tailored_text)
    changes = describe_changes(original_document, document)

    # Rule-based guard: flag any metric in the rewrite not grounded in the source.
    rewritten_texts: list[str] = []
    for sec in document.sections:
        if sec.text.strip():
            rewritten_texts.append(sec.text)
        for it in sec.items:
            rewritten_texts.extend(it.bullets)
    figures_to_verify = find_unsupported_figures(original_text, rewritten_texts)

    return TailorResult(
        document=document, original_text=original_text, tailored_text=tailored_text,
        before=before, after=after, diff_summary=diff_summary,
        changes=changes, gaps=structured.gaps, figures_to_verify=figures_to_verify,
    )
