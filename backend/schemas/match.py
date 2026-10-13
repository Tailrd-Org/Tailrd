"""
Pydantic schemas for the job match breakdown (POST /ai/match-breakdown).

Every value comes from the deterministic fit engine (services/fit_engine.explain_job, DECISIONS
D-83); no AI produces any of it. The first seven fields keep their old names and types so
existing clients keep working; the rest carry the engine's own result. Display rules
(match-engine/docs/compliance.md section 2): only a SCORED result shows a number; a low-confidence
result shows its band only; GATE_FAIL shows "Not eligible" with the reason; NEEDS_REVIEW shows
"We couldn't score this". Never "%" after a score.
"""

from typing import Optional

from pydantic import BaseModel


class MatchItem(BaseModel):
    """One strength or gap: what it is, its signed contribution on the raw scale ("+0.31"),
    and a plain note (e.g. which of your skills partly cover a missing one)."""
    label: str
    impact: str = ""
    note: str = ""
    code: str = ""


class MatchInterval(BaseModel):
    """The score's plausible range; shown only when ``display`` is true."""
    low: int
    high: int
    display: bool


class MatchBreakdown(BaseModel):
    """The engine's result for one (résumé, job) pair, plus the legacy fields."""
    # Legacy fields (compatibility). overall_score is the displayed score of a SCORED result
    # and 0 otherwise (read ``outcome``); match_label is the band, or the outcome's own words;
    # strengths / weaknesses are the labels of ``strength_items`` / ``gaps``. The three
    # sub-scores are the engine's skills, experience and domain dimensions on 0..100.
    overall_score: int
    experience_score: int
    skill_score: int
    industry_score: int
    match_label: str
    strengths: list[str] = []
    weaknesses: list[str] = []
    # The engine's result.
    outcome: str  # SCORED | SCORED_LOW_CONFIDENCE | NEEDS_REVIEW | GATE_FAIL
    band: Optional[str] = None  # "Strong match", "Good match", "Partial match", "Limited match"
    display_score: Optional[int] = None  # set only for SCORED
    headline: str = ""  # e.g. "Fit score 76 · Good match"
    basis: str = ""  # what the score is relative to
    strength_items: list[MatchItem] = []
    gaps: list[MatchItem] = []
    to_improve: list[str] = []
    notes: list[str] = []
    interval: Optional[MatchInterval] = None
