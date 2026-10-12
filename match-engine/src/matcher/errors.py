"""Reason codes and flags (SPEC §13, §14, §17).

One enum carries every code the engine can attach to a result: why a pair was not scored
(NEEDS_REVIEW), why a gate failed or warned, why confidence dropped, and informational flags
the explanation surfaces. Each code has one plain-language sentence in FLAG_TEXT, written for
the candidate (SPEC §17.2: "Surface the reason, always and specifically").
"""

from __future__ import annotations

from enum import Enum


class Flag(str, Enum):
    # --- Ingest: NEEDS_REVIEW reasons (SPEC §14.1) ---------------------------------------
    ENCRYPTED = "ENCRYPTED"
    UNREADABLE = "UNREADABLE"
    CORRUPT = "CORRUPT"
    NOT_A_RESUME = "NOT_A_RESUME"
    UNSUPPORTED_LANGUAGE = "UNSUPPORTED_LANGUAGE"
    UNSUPPORTED_CALENDAR = "UNSUPPORTED_CALENDAR"

    # --- Ingest / parse: informational -----------------------------------------------
    TRUNCATED = "TRUNCATED"
    STUFFING_SUSPECTED = "STUFFING_SUSPECTED"
    OCR_USED = "OCR_USED"
    COLUMN_ORDER_UNCERTAIN = "COLUMN_ORDER_UNCERTAIN"
    NO_SECTIONS = "NO_SECTIONS"
    FUNCTIONAL_RESUME = "FUNCTIONAL_RESUME"
    MIXED_LANGUAGE = "MIXED_LANGUAGE"

    # --- Dates (SPEC §14.2) -------------------------------------------------------------
    IMPRECISE_DATE = "IMPRECISE_DATE"
    DATE_RANGE_SWAPPED = "DATE_RANGE_SWAPPED"
    DATE_RANGE_DROPPED = "DATE_RANGE_DROPPED"
    FUTURE_END_CLAMPED = "FUTURE_END_CLAMPED"
    FUTURE_START = "FUTURE_START"
    TWO_DIGIT_YEAR = "TWO_DIGIT_YEAR"
    NON_GREGORIAN_CONVERTED = "NON_GREGORIAN_CONVERTED"

    # --- Postings (SPEC §14.4) ------------------------------------------------------------
    POSTING_UNSCORABLE = "POSTING_UNSCORABLE"
    UNREALISTIC_REQUIREMENT = "UNREALISTIC_REQUIREMENT"
    CONTRADICTORY_REQUIREMENTS = "CONTRADICTORY_REQUIREMENTS"
    REQUIREMENTS_TRUNCATED = "REQUIREMENTS_TRUNCATED"
    BOILERPLATE_ONLY = "BOILERPLATE_ONLY"

    # --- Scoring: NEEDS_REVIEW reasons (SPEC §13, §14.5) -----------------------------------
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_AS_OF = "INVALID_AS_OF"
    NUMERIC_FAULT = "NUMERIC_FAULT"
    NO_REQUIREMENTS_RESOLVED = "NO_REQUIREMENTS_RESOLVED"
    NOTHING_SCORABLE = "NOTHING_SCORABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # --- Scoring: dimension abstentions and caps --------------------------------------------
    NO_REQUIRED_REQUIREMENTS = "NO_REQUIRED_REQUIREMENTS"
    NO_PREFERRED_REQUIREMENTS = "NO_PREFERRED_REQUIREMENTS"
    NO_IMPLIED_REQUIREMENTS = "NO_IMPLIED_REQUIREMENTS"
    DATES_MISSING = "DATES_MISSING"
    NO_EXPERIENCE_REQUIREMENT = "NO_EXPERIENCE_REQUIREMENT"
    SENIORITY_UNKNOWN = "SENIORITY_UNKNOWN"
    INDUSTRY_UNKNOWN = "INDUSTRY_UNKNOWN"
    CRITICAL_MISS = "CRITICAL_MISS"

    # --- Scoring: informational ---------------------------------------------------------------
    MIN_YEARS_DEFAULTED = "MIN_YEARS_DEFAULTED"
    OVERQUALIFIED = "OVERQUALIFIED"
    NO_WORK_EXPERIENCE = "NO_WORK_EXPERIENCE"

    # --- Confidence (SPEC §17) ----------------------------------------------------------------
    LOW_PARSE_QUALITY = "LOW_PARSE_QUALITY"
    LOW_TAXONOMY_COVERAGE = "LOW_TAXONOMY_COVERAGE"
    LOW_EVIDENCE_DENSITY = "LOW_EVIDENCE_DENSITY"
    LOW_TEMPORAL_COMPLETENESS = "LOW_TEMPORAL_COMPLETENESS"
    FEW_REQUIREMENTS = "FEW_REQUIREMENTS"
    LOW_DIMENSION_COVERAGE = "LOW_DIMENSION_COVERAGE"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"

    # --- Gates (SPEC §4, DECISIONS D-60) ---------------------------------------------------------
    WORK_AUTH_MISMATCH = "WORK_AUTH_MISMATCH"
    SPONSORSHIP_UNAVAILABLE = "SPONSORSHIP_UNAVAILABLE"
    CITIZENSHIP_REQUIRED = "CITIZENSHIP_REQUIRED"
    WORK_AUTH_UNKNOWN = "WORK_AUTH_UNKNOWN"
    LICENSE_MISSING = "LICENSE_MISSING"
    LICENSE_UNVERIFIED = "LICENSE_UNVERIFIED"
    CLEARANCE_MISSING = "CLEARANCE_MISSING"
    CLEARANCE_OBTAINABLE = "CLEARANCE_OBTAINABLE"
    CLEARANCE_UNVERIFIED = "CLEARANCE_UNVERIFIED"
    LOCATION_MISMATCH = "LOCATION_MISMATCH"

    # --- Calibration (SPEC §7) ---------------------------------------------------------------------
    UNCALIBRATED = "UNCALIBRATED"
    CALIBRATION_FALLBACK = "CALIBRATION_FALLBACK"


# Codes that, when present on a result, mean "not scorable; route to a person".
NEEDS_REVIEW_REASONS: frozenset[Flag] = frozenset(
    {
        Flag.ENCRYPTED,
        Flag.UNREADABLE,
        Flag.CORRUPT,
        Flag.NOT_A_RESUME,
        Flag.UNSUPPORTED_LANGUAGE,
        Flag.UNSUPPORTED_CALENDAR,
        Flag.POSTING_UNSCORABLE,
        Flag.INVALID_INPUT,
        Flag.INVALID_AS_OF,
        Flag.NUMERIC_FAULT,
        Flag.NO_REQUIREMENTS_RESOLVED,
        Flag.NOTHING_SCORABLE,
        Flag.INTERNAL_ERROR,
        Flag.LOW_CONFIDENCE,
    }
)


FLAG_TEXT: dict[Flag, str] = {
    Flag.ENCRYPTED: "This file is password-protected, so we couldn't read it.",
    Flag.UNREADABLE: "We couldn't read the text in this file (it may be a scanned image).",
    Flag.CORRUPT: "This file appears to be damaged or incomplete.",
    Flag.NOT_A_RESUME: "This document doesn't look like a résumé.",
    Flag.UNSUPPORTED_LANGUAGE: "This document is in a language we can't score yet.",
    Flag.UNSUPPORTED_CALENDAR: "Some dates use a calendar we couldn't convert.",
    Flag.TRUNCATED: "This document was too long or was cut short, so only its first part was read.",
    Flag.STUFFING_SUSPECTED: "Hidden or invisible text was found and ignored.",
    Flag.OCR_USED: "The text was read from an image, so some details may be missed.",
    Flag.COLUMN_ORDER_UNCERTAIN: "The page layout made the reading order uncertain.",
    Flag.NO_SECTIONS: "We couldn't find section headings, so sections were inferred.",
    Flag.FUNCTIONAL_RESUME: "This résumé lists skills without a timeline, so experience depth isn't reflected in this score.",
    Flag.MIXED_LANGUAGE: "This document mixes languages.",
    Flag.IMPRECISE_DATE: "Some dates are approximate (a season or a year), so durations are estimates.",
    Flag.DATE_RANGE_SWAPPED: "A date range ended before it started; we swapped it.",
    Flag.DATE_RANGE_DROPPED: "A date range couldn't be made sense of and was left out.",
    Flag.FUTURE_END_CLAMPED: "An end date in the future was treated as today.",
    Flag.FUTURE_START: "A role that starts in the future was left out.",
    Flag.TWO_DIGIT_YEAR: "Some years were written with two digits and were interpreted.",
    Flag.NON_GREGORIAN_CONVERTED: "Some dates were converted from another calendar.",
    Flag.POSTING_UNSCORABLE: "We couldn't find any requirements in this job posting.",
    Flag.UNREALISTIC_REQUIREMENT: "This posting asks for more years of a skill than the skill has existed.",
    Flag.CONTRADICTORY_REQUIREMENTS: "This posting's level and its experience requirement disagree.",
    Flag.REQUIREMENTS_TRUNCATED: "This posting lists many requirements; the most specific 25 were weighted fully.",
    Flag.BOILERPLATE_ONLY: "This posting is mostly boilerplate and says little about the role.",
    Flag.INVALID_INPUT: "The information was incomplete or malformed, so it couldn't be scored.",
    Flag.INVALID_AS_OF: "The scoring date is earlier than every date on the résumé.",
    Flag.NUMERIC_FAULT: "A calculation failed, so no score is shown.",
    Flag.NO_REQUIREMENTS_RESOLVED: "None of this posting's requirements are skills we recognise yet.",
    Flag.NOTHING_SCORABLE: "There wasn't enough information on either side to score this match.",
    Flag.INTERNAL_ERROR: "Something went wrong while scoring, so no score is shown.",
    Flag.NO_REQUIRED_REQUIREMENTS: "This posting lists no required skills.",
    Flag.NO_PREFERRED_REQUIREMENTS: "This posting lists no preferred skills.",
    Flag.NO_IMPLIED_REQUIREMENTS: "This posting mentions no other tools or keywords.",
    Flag.DATES_MISSING: "We couldn't read the dates on this résumé, so experience depth isn't reflected in this score.",
    Flag.NO_EXPERIENCE_REQUIREMENT: "This posting states no experience requirement.",
    Flag.SENIORITY_UNKNOWN: "We couldn't tell the level of this role or of your recent roles.",
    Flag.INDUSTRY_UNKNOWN: "We couldn't tell the industry of this role or of your past roles.",
    Flag.CRITICAL_MISS: "A skill this role centres on is missing, which limits the score.",
    Flag.MIN_YEARS_DEFAULTED: "The posting states no experience requirement; typical expectations for this level were used.",
    Flag.OVERQUALIFIED: "Your experience is above this role's level. This is noted, never deducted.",
    Flag.NO_WORK_EXPERIENCE: "No work experience is listed on this résumé.",
    Flag.LOW_PARSE_QUALITY: "Parts of the résumé were hard to read, or the job posting was cut short.",
    Flag.LOW_TAXONOMY_COVERAGE: "Some of this posting's requirements aren't skills we recognise yet.",
    Flag.LOW_EVIDENCE_DENSITY: "Most matching skills are only listed, not shown in your experience.",
    Flag.LOW_TEMPORAL_COMPLETENESS: "Several roles are missing dates.",
    Flag.FEW_REQUIREMENTS: "This posting lists very few requirements, so the score rests on little.",
    Flag.LOW_DIMENSION_COVERAGE: "Several parts of the match couldn't be assessed.",
    Flag.LOW_CONFIDENCE: "There isn't enough reliable information to show a score.",
    Flag.WORK_AUTH_MISMATCH: "This role requires work authorization you haven't indicated.",
    Flag.SPONSORSHIP_UNAVAILABLE: "This role doesn't sponsor work visas, and you indicated you need sponsorship.",
    Flag.CITIZENSHIP_REQUIRED: "This role requires citizenship.",
    Flag.WORK_AUTH_UNKNOWN: "This role has work-authorization requirements; check you're eligible.",
    Flag.LICENSE_MISSING: "This role requires a professional licence that isn't on your résumé.",
    Flag.LICENSE_UNVERIFIED: "This role asks for a licence or certification that isn't on your résumé.",
    Flag.CLEARANCE_MISSING: "This role requires an active security clearance that isn't on your résumé.",
    Flag.CLEARANCE_OBTAINABLE: "This role requires being able to obtain a security clearance.",
    Flag.CLEARANCE_UNVERIFIED: "This role requires a specific security clearance; check that yours meets it.",
    Flag.LOCATION_MISMATCH: "This role is on-site somewhere other than where you're based.",
    Flag.UNCALIBRATED: "No reference group was available, so no percentage is shown.",
    Flag.CALIBRATION_FALLBACK: "Too few candidates were scored for this posting; similar roles were used for comparison.",
}


class EngineFault(Exception):
    """A defect inside the engine (e.g. a NaN reaching the aggregator).

    Raised only in strict mode (tests). In production the entry point converts it into
    NEEDS_REVIEW with Flag.NUMERIC_FAULT or Flag.INTERNAL_ERROR (SPEC §13).
    """

    def __init__(self, flag: Flag, message: str = "") -> None:
        super().__init__(f"{flag.value}: {message}" if message else flag.value)
        self.flag = flag
