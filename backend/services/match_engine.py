"""
MatchEngine: the LLM résumé/job analysis the tailoring flow still uses (``analyze_job``).

No job score comes from here any more. Every match score Tailrd shows is the deterministic fit
engine's (services/fit_engine.py, DECISIONS D-83); the old LLM breakdown, fit analysis and
background analysis (``compute_breakdown``, ``analyze_fit``, ``queue_analysis``) were removed.
Task 9.3 moves the tailoring flow's own scores onto the engine as well.
"""

import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Optional

from sqlalchemy.orm import Session

from backend.schemas.ai import JobAnalysisOut
from backend.services.llm import get_llm_service

logger = logging.getLogger(__name__)


DEFAULT_MATCH_MODEL = "gpt-4o-mini"


def _match_model() -> str:
    """Model used for the tailoring flow's résumé/job analysis (``analyze_job``).

    The analysis does not need the flagship model: default to the cheap sibling
    and leave the quality-sensitive rewrite flows on OPENAI_MODEL.
    """
    return os.getenv("OPENAI_MATCH_MODEL", DEFAULT_MATCH_MODEL).strip().strip("﻿") or DEFAULT_MATCH_MODEL


# ── Analysis memo ────────────────────────────────────────────────────────────
#
# One "tailor my résumé" journey asked for the SAME résumé↔job analysis up to
# three times in 34 seconds (prod, 2026-08-11): once when the user opened the
# job, once for the "See Your Difference" panel, and once more as the rewrite's
# own before-state. Identical prompt, identical answer, three round-trips the
# user waits through.
#
# Keyed on the rendered prompt, so the key IS the complete input — no chance of
# a stale hit from a résumé edit or a different job. Process-local and
# short-lived: on Fluid Compute an instance serves consecutive requests from the
# same user, which is exactly the window this needs to cover. A miss simply
# costs what it costs today, so this can never return a wrong answer, only fail
# to save one.
ANALYSIS_MEMO_TTL = float(os.getenv("MATCH_ANALYSIS_MEMO_TTL", "600"))
ANALYSIS_MEMO_MAX = 128

_analysis_memo: "OrderedDict[str, tuple[float, JobAnalysisOut]]" = OrderedDict()


def _memo_key(prompt: str, model: str) -> str:
    return hashlib.sha256(f"{model}\x00{prompt}".encode("utf-8")).hexdigest()


def _memo_get(key: str) -> Optional[JobAnalysisOut]:
    hit = _analysis_memo.get(key)
    if hit is None:
        return None
    stored_at, value = hit
    if time.time() - stored_at > ANALYSIS_MEMO_TTL:
        _analysis_memo.pop(key, None)
        return None
    _analysis_memo.move_to_end(key)
    # A copy, so a caller mutating the result cannot poison later hits.
    return value.model_copy(deep=True)


def _memo_put(key: str, value: JobAnalysisOut) -> None:
    _analysis_memo[key] = (time.time(), value.model_copy(deep=True))
    _analysis_memo.move_to_end(key)
    while len(_analysis_memo) > ANALYSIS_MEMO_MAX:
        _analysis_memo.popitem(last=False)


def reset_analysis_memo() -> None:
    """Drop every memoised analysis (tests, and anything that needs a cold read)."""
    _analysis_memo.clear()


def score_to_label(score: int) -> str:
    """Map a match score (0-100) to a human-readable label.

    >=80 → "STRONG MATCH"
    >=60 → "GOOD MATCH"
    <60  → "FAIR MATCH"
    """
    if score >= 80:
        return "STRONG MATCH"
    elif score >= 60:
        return "GOOD MATCH"
    else:
        return "FAIR MATCH"


JOB_ANALYSIS_PROMPT = """
Analyze how well this resume matches the job posting, like an applicant tracking
system (ATS) would. Return ONLY a JSON object with these exact fields:

{{
  "overall_score": <0-100 overall fit>,
  "ats_score": <0-100 how well the resume would pass an automated ATS keyword scan>,
  "matched_keywords": ["important skills/keywords from the job that ARE in the resume"],
  "missing_keywords": ["important skills/keywords from the job that are NOT in the resume"],
  "strengths": ["short strength phrases"],
  "weaknesses": ["short gap phrases"],
  "suggestions": ["1-2 sentence actionable suggestions to improve the match for this role"]
}}

Job title: {job_title}
Company: {company}

Resume:
{resume_text}

Job Posting:
{job_description}
"""

class MatchEngine:
    """The tailoring flow's LLM résumé/job analysis (no job scoring)."""

    def __init__(self, db: Session):
        self.db = db
        self._llm = None

    @property
    def llm(self):
        """Lazily construct the LLM service.

        Deferred so that pure helpers (e.g. JSON parsing) and unit tests can
        use MatchEngine without an OPENAI_API_KEY in the environment.
        """
        if self._llm is None:
            self._llm = get_llm_service()
        return self._llm

    async def analyze_job(
        self,
        resume_text: str,
        job_title: str,
        company: str,
        job_description: str,
    ) -> JobAnalysisOut:
        """Resume↔job analysis for the rewrite flow.

        Adds an ATS score plus matched/missing keyword lists (and a derived
        keyword-coverage percentage) on top of the overall fit score.

        Memoised for ``ANALYSIS_MEMO_TTL``: the "See Your Difference" panel and
        the rewrite's own before-state ask the identical question seconds apart,
        so the second and third asks are served from memory.
        """
        prompt = JOB_ANALYSIS_PROMPT.format(
            job_title=job_title or "",
            company=company or "",
            resume_text=resume_text[:3000],
            job_description=job_description[:3000],
        )

        model = _match_model()
        key = _memo_key(prompt, model)
        cached = _memo_get(key)
        if cached is not None:
            logger.info("llm_memo op=match.analyze hit=1")
            return cached

        response = await self.llm._generate(prompt, model=model, op="match.analyze")
        data = self._parse_json_response(response)

        def _strs(key: str) -> list[str]:
            # The model sometimes returns a single string where the schema asks
            # for a list (e.g. one suggestion). Iterating a str yields its
            # characters, so coerce a lone string into a one-element list first.
            val = data.get(key, [])
            if isinstance(val, str):
                val = [val]
            elif not isinstance(val, list):
                val = []
            return [str(v).strip() for v in val if str(v).strip()]

        def _score(key: str) -> int:
            try:
                return max(0, min(100, int(data.get(key, 0) or 0)))
            except (TypeError, ValueError):
                return 0

        matched = _strs("matched_keywords")
        missing = _strs("missing_keywords")
        total = len(matched) + len(missing)
        coverage = round(100 * len(matched) / total) if total else 0
        overall = _score("overall_score")

        result = JobAnalysisOut(
            overall_score=overall,
            ats_score=_score("ats_score"),
            match_label=score_to_label(overall),
            keyword_coverage=coverage,
            matched_keywords=matched,
            missing_keywords=missing,
            strengths=_strs("strengths"),
            weaknesses=_strs("weaknesses"),
            suggestions=_strs("suggestions"),
        )
        # Only a parsed, non-empty analysis is worth remembering: banking a zero
        # born from a parse hiccup would freeze it in for the whole TTL.
        if overall or matched or missing:
            _memo_put(key, result)
        return result

    def _parse_json_response(self, response: str) -> dict:
        """Parse JSON from LLM response, handling code fences."""
        json_str = response.strip()

        # Handle markdown code fences
        if "```" in json_str:
            parts = json_str.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    json_str = part
                    break

        # Find first { and last }
        if not json_str.startswith("{"):
            start = json_str.find("{")
            end = json_str.rfind("}")
            if start >= 0 and end > start:
                json_str = json_str[start : end + 1]

        try:
            return json.loads(json_str)
        except json.JSONDecodeError:
            logger.warning("Failed to parse match response: %s", response[:300])
            return {}
