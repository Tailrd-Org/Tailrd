"""
Application profile endpoint for the Chrome extension.

GET /api/user/application-profile, the canonical, ready-to-fill profile the
extension autofills from. It merges the three places a user's data can live:

    1. ResumeProfileDB, data parsed from the uploaded resume (what the web
                          app's Profile page shows). This is the primary source.
    2. UserSettings, manually-entered form-filling fields + screening answers.
    3. User, account name/email as a final fallback.

The response uses camelCase keys so it maps 1:1 onto the extension's
`UserApplicationProfile` type (see chrome-extension/src/shared/types.ts). Before
this endpoint existed the extension fell back to GET /settings, which a resume
upload never populates, so signed-in users saw an empty profile.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.db.database import get_db
from backend.db.models import CoverLetter, ResumeProfileDB, User, UserSettings
from backend.auth.dependencies import get_verified_user
from backend.services.profile_version import bump_profile_version, get_profile_version

logger = logging.getLogger(__name__)
router = APIRouter()


# ─── Response schema (camelCase to match the extension) ──────────────────────

class EducationEntry(BaseModel):
    school: str = ""
    degree: str = ""
    graduationYear: str = ""


class ExperienceEntry(BaseModel):
    company: str = ""
    title: str = ""
    startDate: str = ""
    endDate: str = ""
    description: str = ""


class EeoOut(BaseModel):
    """EEO / demographic self-identification. camelCase mirrors the extension's
    ``EeoAnswers`` (chrome-extension/src/shared/types.ts). Only used by the
    extension when its "Fill EEO fields" setting is on."""
    gender: str = ""
    race: str = ""
    hispanicLatino: str = ""
    veteranStatus: str = ""
    disabilityStatus: str = ""
    # Second wave (2026-08-09 profile-parity contract). genderIdentity and
    # sexualOrientation were already in the extension's type with nothing behind
    # them; pronouns are new. Option vocabularies are pinned in the contract.
    genderIdentity: str = ""
    pronouns: str = ""
    sexualOrientation: str = ""


class ApplicationProfileOut(BaseModel):
    firstName: str = ""
    lastName: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    # Structured mailing address. addressCity has its own ``address_city``
    # column and falls back to ``city`` (which backs ``location``) while that
    # column is still blank, the two shared one column until 2026-08-09.
    addressStreet: str = ""
    addressCity: str = ""
    addressState: str = ""
    postalCode: str = ""
    country: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    currentCompany: str = ""
    currentTitle: str = ""
    workAuthorization: str = ""
    requiresSponsorship: str = ""
    salaryExpectation: str = ""
    # ISO "YYYY-MM-DD". The only fact on this profile that exists purely so an
    # age gate can be COMPUTED rather than recalled, see
    # backend/services/derived_facts.py. Blank means every age resolver
    # abstains, which is the honest default.
    dateOfBirth: str = ""
    # Screening answers the applicant states once and every ATS then asks for.
    # Stored in prefilled_answers under the exact keys in _SCREENING_KEYS; blank
    # means "not answered", never "no". Filling these is what lets /api/fill
    # answer the questions with no LLM call at all.
    willingToRelocate: str = ""
    workPreference: str = ""
    noticePeriod: str = ""
    earliestStartDate: str = ""
    yearsOfExperience: str = ""
    securityClearance: str = ""
    driversLicense: str = ""
    languages: str = ""
    # Added 2026-10-03, so the extension answers without an LLM the questions
    # no mapping can settle: per-country work authorization (US / Canada,
    # "Yes" | "No"), the "How did you hear about us" channel, the expected
    # graduation MONTH ("YYYY-MM", for season / month-range options) and GPA.
    # Same storage contract as the eight above.
    authorizedUS: str = ""
    authorizedCanada: str = ""
    howDidYouHear: str = ""
    expectedGraduation: str = ""
    gpa: str = ""
    education: list[EducationEntry] = []
    experience: list[ExperienceEntry] = []
    skills: list[str] = []
    coverLetter: str = ""
    eeo: EeoOut = EeoOut()
    # Sync + resume metadata for the extension.
    version: int = 1
    resumeId: int | None = None
    resumeFileName: str = ""
    hasResumeFile: bool = False


class EeoIn(BaseModel):
    """Editable EEO / demographic answers (all optional; omitted → untouched)."""
    gender: str | None = None
    race: str | None = None
    hispanicLatino: str | None = None
    veteranStatus: str | None = None
    disabilityStatus: str | None = None
    genderIdentity: str | None = None
    pronouns: str | None = None
    sexualOrientation: str | None = None


class ApplicationProfileIn(BaseModel):
    """Editable autofill fields the extension (or web app) can write back.

    Only contact + screening fields are user-editable; resume-derived sections
    (experience, education, skills) come from the parsed resume. Any provided
    field overrides the stored value; omitted fields are left untouched.
    """
    firstName: str | None = None
    lastName: str | None = None
    email: str | None = None
    phone: str | None = None
    location: str | None = None
    # Structured mailing address. addressCity has its own column now, so a PUT
    # carrying both it and ``location`` persists both (it used to drop one).
    addressStreet: str | None = None
    addressCity: str | None = None
    addressState: str | None = None
    postalCode: str | None = None
    country: str | None = None
    linkedin: str | None = None
    # Writable since 2026-08-09: github used to be read-only here, so the web
    # app wrote it to the resume row and the extension kept it device-local.
    github: str | None = None
    portfolio: str | None = None
    currentTitle: str | None = None
    workAuthorization: str | None = None
    requiresSponsorship: str | None = None
    salaryExpectation: str | None = None
    dateOfBirth: str | None = None
    # Screening answers: persisted into prefilled_answers under _SCREENING_KEYS.
    willingToRelocate: str | None = None
    workPreference: str | None = None
    noticePeriod: str | None = None
    earliestStartDate: str | None = None
    yearsOfExperience: str | None = None
    securityClearance: str | None = None
    driversLicense: str | None = None
    languages: str | None = None
    authorizedUS: str | None = None
    authorizedCanada: str | None = None
    howDidYouHear: str | None = None
    expectedGraduation: str | None = None
    gpa: str | None = None
    eeo: EeoIn | None = None


class ProfileVersionOut(BaseModel):
    version: int = 1
    updated_at: str | None = None


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _first_non_empty(*values: object) -> str:
    """First value that is a non-blank string, else ""."""
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _split_name(full_name: str) -> tuple[str, str]:
    """'Wissam Elmasry' -> ('Wissam', 'Elmasry'); single token -> (token, '')."""
    parts = (full_name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# The exact keys update_application_profile writes. A value the user or the
# extension explicitly saved must beat anything merely mined by substring.
_WORK_AUTH_KEY = "Are you authorized to work in this country?"
_SPONSOR_KEY = "Do you now or in the future require sponsorship?"
_SALARY_KEY = "Salary expectation"
# Not a screening ANSWER: a stored fact, kept in the same free-form map so it
# needs no column of its own. Never mined by substring: a date of birth is
# either the value under this exact key or absent.
_DOB_KEY = "Date of birth"

# The eight screening answers added by the 2026-08-09 profile-parity contract,
# API field → the EXACT prefilled_answers key. These keys are binding: the web
# app, the extension and this router must all agree on them byte for byte, so
# never rename one. (``driversLicense`` / "Driver's licence" mixing spellings is
# deliberate, the API key mirrors the extension's camelCase field, the storage
# key mirrors the label the user sees.)
_SCREENING_KEYS: dict[str, str] = {
    "willingToRelocate": "Willing to relocate",
    "workPreference": "Work preference",
    "noticePeriod": "Notice period",
    "earliestStartDate": "Earliest start date",
    "yearsOfExperience": "Years of experience",
    "securityClearance": "Security clearance",
    "driversLicense": "Driver's licence",
    "languages": "Languages",
    # 2026-10-03. "Authorized to work in the US" contains "authoriz": it MUST
    # stay in this exact-key set, or substring mining would serve it back as
    # the general work-authorization answer.
    "authorizedUS": "Authorized to work in the US",
    "authorizedCanada": "Authorized to work in Canada",
    "howDidYouHear": "How did you hear about us",
    "expectedGraduation": "Expected graduation",
    "gpa": "GPA",
}

# Keys read by EXACT MATCH ONLY, never mined by substring, for the reason
# _stored_dob documents at length: "Earliest start date" is one fuzzy read away
# from being served as a date of birth, and "Work preference" is one added
# keyword away from being served as a work-authorization answer. Excluding them
# from the mining loop is belt-and-braces today (none of the current keywords
# hit them) and load-bearing the moment a keyword is added.
_EXACT_ONLY_KEYS = frozenset({_DOB_KEY, *_SCREENING_KEYS.values()})

# SetupWizard (frontend/src/setup/SetupWizard.tsx) dumps its own internal filter
# state into the same map. These are enum tokens ("needs_sponsorship",
# "internship"), not answers any employer should ever see, and
# "work_authorization" happens to contain "authoriz", so substring mining used
# to serve it back as the user's work-authorization answer and shadow their
# real one.
_SETUP_KEYS = frozenset({"job_types", "work_authorization", "target_titles"})


def _stored_dob(prefilled: dict | None) -> str:
    """The stored date of birth, exact-key only.

    Substring mining is deliberately not used here. "Date of birth" is close
    enough to a dozen other question texts a form might harvest ("Date of birth
    of dependent", "Earliest start date") that a fuzzy read could hand an age
    resolver the wrong date, and an age gate answered from the wrong date is
    exactly the class of bug this whole path exists to remove.
    """
    value = (prefilled or {}).get(_DOB_KEY)
    return value.strip() if isinstance(value, str) else ""


def _stored_screening(prefilled: dict | None) -> dict[str, str]:
    """The screening answers (_SCREENING_KEYS), keyed by API field name.

    Exact-key only, for the same reason as :func:`_stored_dob`: every one of
    these has a fixed key that the PUT writes, so a fuzzy read could only ever
    hand back somebody else's answer. A missing or non-string value reads as ""
    ("not answered"), which is what makes every downstream rule abstain.
    """
    p = prefilled or {}
    out: dict[str, str] = {}
    for field, key in _SCREENING_KEYS.items():
        value = p.get(key)
        out[field] = value.strip() if isinstance(value, str) else ""
    return out


def _mine_screening(prefilled: dict | None) -> tuple[str, str, str]:
    """
    Resolve the work-authorization, sponsorship and salary answers from the
    free-form prefilled_answers question→answer map.

    Exact fixed keys win. They are what update_application_profile writes, so an
    answer the user or the extension deliberately saved is authoritative. Only a
    still-empty slot falls back to substring mining, which exists for legacy rows
    and for question text the extension harvested verbatim from a real form.
    """
    p = prefilled or {}

    def exact(key: str) -> str:
        """Exact-key lookup, honouring only a string value (the map is
        free-form JSON, so a malformed row must not reach the response model)."""
        value = p.get(key)
        return value if isinstance(value, str) else ""

    work_authorization = exact(_WORK_AUTH_KEY)
    requires_sponsorship = exact(_SPONSOR_KEY)
    salary_expectation = exact(_SALARY_KEY)

    for question, answer in p.items():
        if not isinstance(answer, str) or question in _SETUP_KEYS or question in _EXACT_ONLY_KEYS:
            continue
        q = question.lower()
        if not requires_sponsorship and "sponsor" in q:
            requires_sponsorship = answer
        elif not work_authorization and ("authoriz" in q or "eligible" in q):
            work_authorization = answer
        elif not salary_expectation and ("salary" in q or "compensation" in q):
            salary_expectation = answer

    return work_authorization, requires_sponsorship, salary_expectation


def _flatten_skills(skills: object, technologies: object) -> list[str]:
    """Combine the flat skills list with the categorized technologies, deduped."""
    out: list[str] = []
    seen: set[str] = set()

    def add(item: object) -> None:
        if isinstance(item, str):
            s = item.strip()
            key = s.lower()
            if s and key not in seen:
                seen.add(key)
                out.append(s)

    if isinstance(skills, list):
        for s in skills:
            add(s)
    if isinstance(technologies, dict):
        for values in technologies.values():
            if isinstance(values, list):
                for s in values:
                    add(s)
    return out


def _map_experience(raw: object) -> list[ExperienceEntry]:
    items: list[ExperienceEntry] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        bullets = entry.get("bullets") or []
        description = "\n".join(b for b in bullets if isinstance(b, str)) if isinstance(bullets, list) else ""
        items.append(
            ExperienceEntry(
                company=_first_non_empty(entry.get("company")),
                title=_first_non_empty(entry.get("title")),
                startDate=_first_non_empty(entry.get("start_date")),
                endDate=_first_non_empty(entry.get("end_date")),
                description=description,
            )
        )
    return items


def _map_education(raw: object) -> list[EducationEntry]:
    items: list[EducationEntry] = []
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        items.append(
            EducationEntry(
                school=_first_non_empty(entry.get("school")),
                degree=_first_non_empty(entry.get("degree")),
                # The extension models a single "graduation year"; the resume
                # stores a free-form end date, which is the closest fit.
                graduationYear=_first_non_empty(entry.get("end_date")),
            )
        )
    return items


# ─── Endpoint ────────────────────────────────────────────────────────────────

def build_application_profile(user: User, db: Session) -> tuple[ApplicationProfileOut, bool]:
    """Merge a user's resume + settings + account into a ready-to-fill profile.

    Returns ``(profile, has_data)`` where ``has_data`` is False when the user has
    neither a resume nor a settings row yet. Shared by the GET endpoint (which
    404s when ``has_data`` is False) and the extension sync snapshot (which uses
    the profile regardless, falling back to account basics).
    """
    resume = (
        db.query(ResumeProfileDB)
        .filter(ResumeProfileDB.user_id == user.id)
        .order_by(ResumeProfileDB.is_primary.desc(), ResumeProfileDB.created_at.desc())
        .first()
    )
    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    has_data = not (resume is None and settings is None)

    # Name: prefer a manual settings override, then the resume's parsed name,
    # then the account. (Settings-first so edits made in the app/extension win.)
    resume_first, resume_last = _split_name(resume.profile_name if resume else "")
    first_name = _first_non_empty(
        settings.first_name if settings else "",
        resume_first,
        user.first_name,
    )
    last_name = _first_non_empty(
        settings.last_name if settings else "",
        resume_last,
        user.last_name,
    )

    experience = _map_experience(resume.experience if resume else None)
    education = _map_education(resume.education if resume else None)
    skills = _flatten_skills(
        resume.skills if resume else None,
        resume.technologies if resume else None,
    )

    current_company = experience[0].company if experience else ""
    current_title = _first_non_empty(
        settings.job_title if settings else "",
        experience[0].title if experience else "",
    )

    prefilled = settings.prefilled_answers if settings else None
    work_authorization, requires_sponsorship, salary_expectation = _mine_screening(prefilled)
    screening = _stored_screening(prefilled)

    # Active cover letter (synced to the extension's cover-letter fields).
    cover = (
        db.query(CoverLetter)
        .filter(CoverLetter.user_id == user.id, CoverLetter.is_active == 1)
        .order_by(CoverLetter.updated_at.desc())
        .first()
    )

    version = settings.data_version if settings and settings.data_version else 1

    profile = ApplicationProfileOut(
        firstName=first_name,
        lastName=last_name,
        email=_first_non_empty(
            settings.email if settings else "",
            resume.email if resume else "",
            user.email,
        ),
        phone=_first_non_empty(
            settings.phone if settings else "",
            resume.phone if resume else "",
        ),
        location=_first_non_empty(
            settings.city if settings else "",
            # The mirror of addressCity's fallback below. ``settings.location``
            # is the LinkedIn bot's SEARCH region, and rows created before
            # 2026-10-13 hold its old default "United States" (new rows start
            # empty), so it must never outrank a city the user actually typed:
            # while the two shared a column that was impossible, and now that
            # they don't, a user who fills only City would otherwise be told
            # they live in the United States, and the LLM would be told so too.
            settings.address_city if settings else "",
            settings.location if settings else "",
            resume.location if resume else "",
        ),
        addressStreet=_first_non_empty(settings.street_address if settings else ""),
        # addressCity has its own column since the profile-parity contract. Rows
        # written before that stored it in ``city`` (which ``location`` also
        # writes), so a blank falls back there rather than losing the value.
        addressCity=_first_non_empty(
            settings.address_city if settings else "",
            settings.city if settings else "",
        ),
        addressState=_first_non_empty(settings.address_state if settings else ""),
        postalCode=_first_non_empty(settings.postal_code if settings else ""),
        country=_first_non_empty(settings.country if settings else ""),
        linkedin=_first_non_empty(
            settings.linkedin_url if settings else "",
            resume.linkedin_url if resume else "",
        ),
        # Settings first: the resume row is only the PARSED value, so an edit
        # made in the app or the extension has to be able to override it.
        github=_first_non_empty(
            settings.github_url if settings else "",
            resume.github_url if resume else "",
        ),
        portfolio=_first_non_empty(
            settings.website if settings else "",
            resume.other_link if resume else "",
        ),
        currentCompany=current_company,
        currentTitle=current_title,
        workAuthorization=work_authorization,
        requiresSponsorship=requires_sponsorship,
        salaryExpectation=salary_expectation,
        dateOfBirth=_stored_dob(prefilled),
        **screening,
        education=education,
        experience=experience,
        skills=skills,
        coverLetter=(cover.text if cover else "") or "",
        eeo=EeoOut(
            gender=_first_non_empty(settings.eeo_gender if settings else ""),
            race=_first_non_empty(settings.eeo_race if settings else ""),
            hispanicLatino=_first_non_empty(settings.eeo_hispanic if settings else ""),
            veteranStatus=_first_non_empty(settings.eeo_veteran if settings else ""),
            disabilityStatus=_first_non_empty(settings.eeo_disability if settings else ""),
            genderIdentity=_first_non_empty(settings.eeo_gender_identity if settings else ""),
            pronouns=_first_non_empty(settings.eeo_pronouns if settings else ""),
            sexualOrientation=_first_non_empty(settings.eeo_sexual_orientation if settings else ""),
        ),
        version=version,
        resumeId=resume.id if resume else None,
        resumeFileName=(resume.file_name if resume else "") or "",
        hasResumeFile=bool(resume.file_blob_url) if resume else False,
    )
    return profile, has_data


@router.get("/user/application-profile", response_model=ApplicationProfileOut)
def get_application_profile(
    user: User = Depends(get_verified_user),
    db: Session = Depends(get_db),
):
    """
    Return the current user's ready-to-fill application profile, merged from
    their resume, settings, and account. 404 only when none of those exist.
    """
    profile, has_data = build_application_profile(user, db)
    if not has_data:
        raise HTTPException(
            status_code=404,
            detail="No application profile yet. Upload a resume or fill in your settings first.",
        )
    return profile


@router.put("/user/application-profile", response_model=ProfileVersionOut)
def update_application_profile(
    body: ApplicationProfileIn,
    user: User = Depends(get_verified_user),
    db: Session = Depends(get_db),
):
    """Write editable autofill fields back to settings and bump the sync version
    so the change reflects across both the web app and the extension."""
    settings = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    if settings is None:
        settings = UserSettings(user_id=user.id)
        db.add(settings)

    field_map = {
        "firstName": "first_name",
        "lastName": "last_name",
        "email": "email",
        "phone": "phone",
        "location": "city",
        "linkedin": "linkedin_url",
        "github": "github_url",
        "portfolio": "website",
        "currentTitle": "job_title",
        # Structured address. ``addressCity`` has its own column: it shared
        # ``city`` with ``location`` until 2026-08-09, which meant dict order
        # here decided which of the two survived a PUT carrying both.
        "addressStreet": "street_address",
        "addressCity": "address_city",
        "addressState": "address_state",
        "postalCode": "postal_code",
        "country": "country",
    }
    for in_field, col in field_map.items():
        val = getattr(body, in_field)
        if val is not None:
            setattr(settings, col, val)

    # EEO self-identification (nested object mirroring the output shape).
    if body.eeo is not None:
        eeo_map = {
            "gender": "eeo_gender",
            "race": "eeo_race",
            "hispanicLatino": "eeo_hispanic",
            "veteranStatus": "eeo_veteran",
            "disabilityStatus": "eeo_disability",
            "genderIdentity": "eeo_gender_identity",
            "pronouns": "eeo_pronouns",
            "sexualOrientation": "eeo_sexual_orientation",
        }
        for in_field, col in eeo_map.items():
            val = getattr(body.eeo, in_field)
            if val is not None:
                setattr(settings, col, val)

    # Screening answers + salary live in the free-form prefilled_answers map.
    # Reassign (don't mutate in place) so SQLAlchemy detects the JSON change.
    # These are the exact keys _mine_screening reads back first, so what the user
    # saves here is authoritative over anything mined by substring.
    answers = dict(settings.prefilled_answers or {})
    if body.workAuthorization is not None:
        answers[_WORK_AUTH_KEY] = body.workAuthorization
    if body.requiresSponsorship is not None:
        answers[_SPONSOR_KEY] = body.requiresSponsorship
    if body.salaryExpectation is not None:
        answers[_SALARY_KEY] = body.salaryExpectation
    if body.dateOfBirth is not None:
        answers[_DOB_KEY] = body.dateOfBirth.strip()
    # The screening answers, each under its own exact key. _stored_screening
    # reads these back by exact match only, so nothing here can be mined into a
    # neighbouring answer and nothing here can shadow one.
    for in_field, key in _SCREENING_KEYS.items():
        val = getattr(body, in_field)
        if val is not None:
            answers[key] = val.strip()
    settings.prefilled_answers = answers

    db.commit()

    version = bump_profile_version(db, user.id)
    _, updated_at = get_profile_version(db, user.id)
    return ProfileVersionOut(
        version=version, updated_at=updated_at.isoformat() if updated_at else None
    )


@router.get("/user/profile-version", response_model=ProfileVersionOut)
def read_profile_version(
    user: User = Depends(get_verified_user),
    db: Session = Depends(get_db),
):
    """Cheap staleness check for the extension, returns the current sync
    version so it only re-downloads the full profile when something changed."""
    version, updated_at = get_profile_version(db, user.id)
    return ProfileVersionOut(
        version=version, updated_at=updated_at.isoformat() if updated_at else None
    )
