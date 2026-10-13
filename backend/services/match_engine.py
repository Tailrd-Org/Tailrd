"""
MatchEngine: the tailoring flow's résumé/job analysis, on the deterministic match engine (Task 9.3).

No AI is called here (DECISIONS D-83). Every score the tailoring flow shows, its band, its
matched / missing requirements and its before / after numbers come from scoring a résumé
document against one parsed posting with ``services/fit_engine.py``. The LLM writes the rewrite
text and nothing else; it is fed the engine's missing requirements (display labels).

Field mapping (``JobAnalysisOut``, D-123):
    overall_score     the engine's displayed (calibrated) score, 0-100; only for SCORED.
    ats_score         the SAME number. The engine has no separate "ATS" model, and inventing a
                      second number would be a score with no basis; the field stays for the
                      clients that read it.
    match_label       the band ("Strong match" ...) for SCORED and SCORED_LOW_CONFIDENCE;
                      "We couldn't score this" for NEEDS_REVIEW; "Not eligible" for GATE_FAIL.
    keyword_coverage  the engine's coverage over the required and preferred skill requirements
                      (idf-weighted, 0-100); None when no requirement carries weight.
    matched_keywords  requirements the résumé meets (REQ_MATCH), as display labels.
    missing_keywords  requirements partly covered or absent (REQ_PARTIAL, REQ_MISS).
    strengths /
    weaknesses        the engine explanation's strengths and gaps (labels); a failed gate's
                      reason leads ``weaknesses``.
    suggestions       the engine's counterfactual (``to_improve``) when asked for.
    outcome           the engine outcome (additive).
    reasons           why the result is not SCORED, as sentences (additive).
SCORED_LOW_CONFIDENCE shows the band only: ``overall_score`` and ``ats_score`` are None.

The engine is imported lazily (D-107): the app's other routes never pay for it. Building it takes
about ten seconds cold, which a user-initiated tailoring request may pay.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from backend.schemas.ai import JobAnalysisOut
from backend.schemas.resume_document import ResumeDocument

logger = logging.getLogger(__name__)

OUTCOME_SCORED = "SCORED"
OUTCOME_LOW_CONFIDENCE = "SCORED_LOW_CONFIDENCE"
OUTCOME_NEEDS_REVIEW = "NEEDS_REVIEW"
OUTCOME_GATE_FAIL = "GATE_FAIL"
UNSCORED_LABEL = "We couldn't score this"
NOT_ELIGIBLE_LABEL = "Not eligible"


def analysis_from_result(result: Any, *, engine: Any = None) -> JobAnalysisOut:
    """The tailoring analysis for one engine ``MatchResult`` (see the module docstring)."""
    from backend.services import fit_engine

    outcome = result.outcome.value
    band = result.band.value if result.band is not None else None
    score = result.display_score if outcome == OUTCOME_SCORED else None
    if outcome == OUTCOME_GATE_FAIL:
        label = NOT_ELIGIBLE_LABEL
    elif outcome in (OUTCOME_SCORED, OUTCOME_LOW_CONFIDENCE) and band:
        label = band
    else:
        label = UNSCORED_LABEL
    coverage = fit_engine.requirement_coverage(result, engine=engine)
    explanation = result.explanation
    strengths = [item.label for item in explanation.strengths] if explanation is not None else []
    weaknesses = [item.label for item in explanation.gaps] if explanation is not None else []
    suggestions = [str(text) for text in explanation.to_improve] if explanation is not None else []
    reasons = fit_engine.reason_sentences(result) if outcome != OUTCOME_SCORED else []
    if outcome == OUTCOME_GATE_FAIL:
        weaknesses = [*reasons, *(text for text in weaknesses if text not in reasons)]
    return JobAnalysisOut(
        overall_score=score,
        ats_score=score,
        match_label=label,
        keyword_coverage=coverage.percent,
        matched_keywords=list(coverage.matched),
        missing_keywords=list(coverage.missing),
        strengths=strengths,
        weaknesses=weaknesses,
        suggestions=suggestions,
        outcome=outcome,
        reasons=reasons,
    )


class MatchEngine:
    """The tailoring flow's analysis: one posting, any number of résumé documents scored on it."""

    def __init__(self, db: Optional[Session]):
        self.db = db

    def posting(self, *, job: Any = None, job_title: str = "", company: str = "",
                job_description: str = "") -> Any:
        """The parsed posting: a job row's cached posting (D-84, committed when refreshed), else
        the pasted text parsed by the engine."""
        from backend.services import fit_engine

        if job is not None:
            posting = fit_engine.posting_for_job(job)
            if self.db is not None:
                fit_engine._commit_if_cached(self.db, job)
            return posting
        return fit_engine.posting_from_text(job_title or "", company or "", job_description or "")

    def analyze(self, document: ResumeDocument, posting: Any, *, user_id: Optional[int] = None,
                raw_text: Optional[str] = None, with_suggestions: bool = False) -> JobAnalysisOut:
        """Score one résumé document on ``posting`` (blocking: call from a worker thread).

        ``raw_text`` is read only when the document has no content (D-80), as for a stored
        profile, so a text-only résumé is still scored."""
        from backend.services import fit_engine
        from backend.services.resume_document import document_to_record

        result = fit_engine.score_document(self.db, user_id, document_to_record(document), posting,
                                           raw_text=raw_text, with_suggestions=with_suggestions)
        return analysis_from_result(result)

    def analyze_job(self, document: ResumeDocument, *, user_id: Optional[int] = None, job: Any = None,
                    job_title: str = "", company: str = "", job_description: str = "",
                    raw_text: Optional[str] = None, with_suggestions: bool = True) -> JobAnalysisOut:
        """Step 1 "See Your Difference": the analysis of one résumé for one job (row or text),
        with the engine's suggestions."""
        posting = self.posting(job=job, job_title=job_title, company=company, job_description=job_description)
        return self.analyze(document, posting, user_id=user_id, raw_text=raw_text, with_suggestions=with_suggestions)
