"""
Reading banked fit scores on a request path, without importing or building the engine.

The fit engine (services/fit_engine.py) takes about three seconds to import and about seven more
to build. The feed, the sidebar and the job list must never pay that, so everything they need to
read ``job_match_scores`` lives here and imports nothing from ``matcher``:

    ``serving_fingerprints``   the fit fingerprints whose rows are current for a user right now
                               (DECISIONS D-107): the one remembered in ``match_fingerprints``
                               while the résumé and saved answers it came from are unchanged,
                               plus, when this process has already built the engine, the exact
                               one. Old AI (sha256) and "local1:" rows are never in it.
    ``engine_ready``           True once this process has built the engine (a request path may
                               then call ``fit_engine.bank_scores``; otherwise it serves what is
                               banked and leaves the rest "not scored yet").
    ``score_order``            the dialect-portable "NULL scores last" ordering (D-86).
    ``strong_match`` /
    ``good_or_better``         the band filters (D-85): only SCORED rows ever count as a match.
    ``profile_fields`` and
    ``input_key``              the saved-answer reading the engine uses (D-88), and a digest of
                               every stored input a fingerprint depends on.

Bands are the engine's display bands (match-engine config/bands.yaml, ``matcher.models.Band``):
Strong from 85, Good from 70, Partial from 55, else Limited.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from typing import Any, Optional

from sqlalchemy import case

#: ``matcher.models.Band`` values, as ``job_match_scores.band`` stores them.
BAND_STRONG = "Strong match"
BAND_GOOD = "Good match"
BAND_PARTIAL = "Partial match"
BAND_LIMITED = "Limited match"
GOOD_OR_BETTER = (BAND_STRONG, BAND_GOOD)
#: ``matcher.models.Outcome`` values.
OUTCOME_SCORED = "SCORED"
OUTCOME_LOW_CONFIDENCE = "SCORED_LOW_CONFIDENCE"
OUTCOME_NEEDS_REVIEW = "NEEDS_REVIEW"
OUTCOME_GATE_FAIL = "GATE_FAIL"
#: What a person reads for a result with no score (compliance.md section 2).
UNSCORED_TEXT = "We couldn't score this"
#: Every fit fingerprint starts "fit<major>:" (D-82).
FIT_PREFIX = "fit"

#: Every profile text field is cut to this many characters before profile_facts reads it: the
#: port is linear, but profile text has no upstream length limit and the sweep reads it per user.
PROFILE_FIELD_LIMIT = 2000
#: ``UserSettings.location`` is created with this value (the LinkedIn bot's search region), so it
#: says nothing about where a person lives and is never read as a residence.
DEFAULT_SETTINGS_LOCATION = "united states"

_DOCUMENT_FIELDS = ("location", "summary", "experience", "education", "projects", "skills", "technologies",
                    "custom_sections")
_UNSET = object()


def engine_ready() -> bool:
    """True when this process has already built the fit engine. Never imports or builds it."""
    module = sys.modules.get("backend.services.fit_engine")
    return module is not None and getattr(module, "_ENGINE", None) is not None


def score_order(column) -> tuple:
    """ORDER BY clauses for a match sort: scored rows by score, highest first, then every NULL
    score (not scored, D-86). ``CASE`` rather than NULLS LAST, which SQLite and Postgres spell
    differently and order by default in opposite directions."""
    return (case((column.is_(None), 1), else_=0), column.desc())


def strong_match():
    """SQL filter: a confident Strong match (SCORED, band Strong, a number). The alert bar."""
    from backend.db.models import JobMatchScore

    return ((JobMatchScore.outcome == OUTCOME_SCORED) & (JobMatchScore.band == BAND_STRONG)
            & JobMatchScore.score.isnot(None))


def good_or_better():
    """SQL filter: a confident Good or Strong match (the sidebar's gap pool and companies)."""
    from backend.db.models import JobMatchScore

    return ((JobMatchScore.outcome == OUTCOME_SCORED) & JobMatchScore.band.in_(GOOD_OR_BETTER)
            & JobMatchScore.score.isnot(None))


def is_strong_match(outcome: Optional[str], band: Optional[str], score: Optional[int]) -> bool:
    """The Python twin of ``strong_match`` (D-85: alerts only for SCORED Strong matches)."""
    return outcome == OUTCOME_SCORED and band == BAND_STRONG and score is not None


# ─── Saved answers (D-88) ────────────────────────────────────────────────────────────────────


def document_of(profile: Any) -> dict[str, Any]:
    return {name: getattr(profile, name, None) for name in _DOCUMENT_FIELDS}


def _first_non_empty(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _column(settings: Any, name: str) -> str:
    value = getattr(settings, name, "") if settings is not None else ""
    return value if isinstance(value, str) else ""


def screening(settings: Any) -> tuple[str, str, dict[str, str]]:
    """(work authorisation, sponsorship, the exact-key screening answers), read the way the
    application profile reads them (routers/profile.py), so the extension and the engine agree."""
    from backend.routers.profile import _mine_screening, _stored_screening

    prefilled = getattr(settings, "prefilled_answers", None) if settings is not None else None
    prefilled = prefilled if isinstance(prefilled, dict) else None
    work_authorization, requires_sponsorship, _ = _mine_screening(prefilled)
    return work_authorization, requires_sponsorship, _stored_screening(prefilled)


def profile_fields(profile: Any, settings: Any) -> dict[str, str]:
    """The ten profile_facts fields for this résumé and settings row, each cut to 2,000 characters.

    Mirrors ``build_application_profile`` except that a ``settings.location`` equal to the column
    default "United States" is dropped: it is a search region nobody chose, not a residence.
    """
    work_authorization, requires_sponsorship, answers = screening(settings)
    search_region = _column(settings, "location")
    if search_region.strip().lower() == DEFAULT_SETTINGS_LOCATION:
        search_region = ""
    fields = {
        "location": _first_non_empty(_column(settings, "city"), _column(settings, "address_city"), search_region,
                                     getattr(profile, "location", None)),
        "addressStreet": _first_non_empty(_column(settings, "street_address")),
        "addressCity": _first_non_empty(_column(settings, "address_city"), _column(settings, "city")),
        "addressState": _first_non_empty(_column(settings, "address_state")),
        "postalCode": _first_non_empty(_column(settings, "postal_code")),
        "country": _first_non_empty(_column(settings, "country")),
        "workAuthorization": work_authorization or "",
        "requiresSponsorship": requires_sponsorship or "",
        "authorizedUS": answers.get("authorizedUS", ""),
        "authorizedCanada": answers.get("authorizedCanada", ""),
    }
    return {name: value[:PROFILE_FIELD_LIMIT] for name, value in fields.items()}


def clearance_answer(settings: Any) -> str:
    return screening(settings)[2].get("securityClearance", "")[:PROFILE_FIELD_LIMIT]


def digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def input_key(profile: Any, settings: Any) -> str:
    """sha256 of every stored input a fingerprint depends on: the résumé document, its raw text,
    the ten profile fields and the clearance answer. Computed without the engine."""
    return digest([document_of(profile), getattr(profile, "raw_text", None) or "", profile_fields(profile, settings),
                   clearance_answer(settings)])


def settings_for(db, user_id: int):
    from backend.db.models import UserSettings

    return db.query(UserSettings).filter(UserSettings.user_id == user_id).first()


# ─── Which banked rows are current (D-107) ───────────────────────────────────────────────────


def remember_fingerprint(db, user_id: int, key: str, fingerprint: str) -> None:
    """Record that ``fingerprint`` is the newest one banked for these inputs. Adds to the
    caller's session; the caller commits (bank_scores commits it with the scores)."""
    from backend.db.models import MatchFingerprint

    row = db.get(MatchFingerprint, user_id)
    if row is None:
        db.add(MatchFingerprint(user_id=user_id, input_key=key, fingerprint=fingerprint,
                                updated_at=dt.datetime.utcnow()))
    elif row.input_key != key or row.fingerprint != fingerprint:
        row.input_key, row.fingerprint, row.updated_at = key, fingerprint, dt.datetime.utcnow()


def serving_fingerprints(db, user_id: int, profile: Any, settings: Any = _UNSET) -> list[str]:
    """The fit fingerprints whose banked rows are current for this user and résumé.

    * The remembered one (``match_fingerprints``), while its ``input_key`` matches the stored
      inputs today: an edited résumé or answer serves nothing until it is scored again.
    * The exact one (``fit_engine.current_fingerprints``), only when this process has already
      built the engine. A request path never builds it.

    Rows are one per (user, job), so serving two fingerprints never counts a job twice. Only
    "fit" fingerprints are ever returned: an AI or "local1:" row is never current (D-82).
    """
    from backend.db.models import MatchFingerprint

    if profile is None:
        return []
    if settings is _UNSET:
        settings = settings_for(db, user_id)
    fingerprints: list[str] = []
    row = db.get(MatchFingerprint, user_id)
    if row is not None and row.input_key == input_key(profile, settings):
        fingerprints.append(row.fingerprint)
    if engine_ready():
        from backend.services import fit_engine

        for fingerprint in fit_engine.current_fingerprints(profile, settings):
            if fingerprint not in fingerprints:
                fingerprints.append(fingerprint)
    return [fp for fp in fingerprints if isinstance(fp, str) and fp.startswith(FIT_PREFIX)]


__all__ = [
    "BAND_GOOD",
    "BAND_LIMITED",
    "BAND_PARTIAL",
    "BAND_STRONG",
    "GOOD_OR_BETTER",
    "OUTCOME_GATE_FAIL",
    "OUTCOME_LOW_CONFIDENCE",
    "OUTCOME_NEEDS_REVIEW",
    "OUTCOME_SCORED",
    "UNSCORED_TEXT",
    "clearance_answer",
    "engine_ready",
    "good_or_better",
    "input_key",
    "is_strong_match",
    "profile_fields",
    "remember_fingerprint",
    "score_order",
    "serving_fingerprints",
    "settings_for",
    "strong_match",
]
