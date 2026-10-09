"""Parse-quality helpers: the hidden-text predicates the PDF and DOCX readers share (stuffing,
SPEC §14.1) and the résumé-likelihood classifier ("wrong document", SPEC §14.1; brief row 11).

resume_likelihood(text) is a logistic score over evidence counted in the normalised text:

    z = -1.0                                    prior: an unknown document leans "not a résumé"
        + 1.0 per section heading category      experience, education, skills, projects,
              (at most 4)                       certifications, summary; English and French
        + 0.8 per date range (at most 3)        "Jan 2020 - Present", "2014 - 2018", "juin 2018 - déc. 2019"
        + 1.0 x min(1, bullet share / 0.10)     lines starting "- " (normalisation made them so)
        + 0.5 if contact details appear         an e-mail address or a phone number
        + 0.5 if a skills-style list appears    three or more short comma-separated items
        - 1.5 per cover-letter marker (max 4)   "Dear", "Sincerely", "I am writing to", "Hiring Manager", ...
        - 1.5 per transcript marker (max 4)     "Transcript", "Course Code", "Credits", "Term GPA", "Cumulative", ...
    likelihood = 1 / (1 + e^-z), rounded to 4 decimals

The weights are heuristics fixed here rather than in config (only the threshold,
`ingest.resume_likelihood_threshold`, is configuration). Below the threshold a document is
NEEDS_REVIEW: NOT_A_RESUME; a two-line résumé with a dated role and contact details clears it.
"""

from __future__ import annotations

import math
import re
from typing import Optional, Sequence

# ─── Hidden text (stuffing) ──────────────────────────────────────────────────────────────────

NEAR_WHITE = 0.95  # brief: every channel at least 0.95 -> invisible on a white page
MIN_VISIBLE_SIZE_PT = 2.0  # brief: a font size below 2 pt is unreadable


def to_rgb(components: object) -> Optional[tuple[float, float, float]]:
    """An RGB triple in [0, 1] from 1 (grey), 3 (RGB) or 4 (CMYK) components; None otherwise."""
    if not isinstance(components, (tuple, list)):
        return None
    try:
        values = [float(c) for c in components]
    except (TypeError, ValueError):
        return None
    if not values or not all(math.isfinite(v) for v in values):
        return None
    values = [min(1.0, max(0.0, v)) for v in values]
    if len(values) == 1:
        return values[0], values[0], values[0]
    if len(values) == 3:
        return values[0], values[1], values[2]
    if len(values) == 4:
        c, m, y, k = values
        return (1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k)
    return None


def is_near_white(rgb: Optional[Sequence[float]]) -> bool:
    return rgb is not None and len(rgb) > 0 and all(channel >= NEAR_WHITE for channel in rgb)


_DIGITS = re.compile(r"\d+")
_PAGE_NUMBER = re.compile(
    r"^[-\u2013\u2014 ]*(?:(?:page|p\.|pg\.?|seite|pagina|página)\s*)?#(?:\s*(?:/|of|de|sur|von|di)\s*#)?"
    r"[-\u2013\u2014 ]*$"
)


def digit_masked(text: str) -> str:
    """Lowercase, spaces collapsed, every digit run replaced by '#' ('Page 2 of 3' -> 'page # of #')."""
    return _DIGITS.sub("#", " ".join(text.lower().split()))


def is_page_number(text: str) -> bool:
    """A line that is only a page number: 'Page 2 of 3', '2', '- 2 -', '2/3'."""
    return bool(_PAGE_NUMBER.match(digit_masked(text)))


def hex_to_rgb(value: Optional[str]) -> Optional[tuple[float, float, float]]:
    """'FFFFFF' -> (1.0, 1.0, 1.0); 'auto', malformed or missing -> None."""
    if not value or len(value) != 6:
        return None
    try:
        return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


# ─── Résumé likelihood ──────────────────────────────────────────────────────────────────────

_SECTIONS: dict[str, tuple[str, ...]] = {
    "experience": ("experience", "work experience", "professional experience", "relevant experience",
                   "employment", "employment history", "work history", "career history",
                   "expérience", "expériences", "expérience professionnelle", "expériences professionnelles",
                   "parcours professionnel", "emplois"),
    "education": ("education", "academic background", "education and training", "formation", "formations",
                  "éducation", "études", "diplômes", "scolarité"),
    "skills": ("skills", "technical skills", "core skills", "key skills", "skills and tools", "competencies",
               "compétences", "compétences techniques", "aptitudes"),
    "projects": ("projects", "personal projects", "academic projects", "selected projects", "projets",
                 "projets personnels", "réalisations"),
    "certifications": ("certifications", "certificates", "licenses", "licences", "licenses and certifications",
                       "certificats"),
    "summary": ("summary", "professional summary", "profile", "professional profile", "objective",
                "career objective", "about me", "profil", "profil professionnel", "résumé", "sommaire",
                "à propos", "objectif"),
}
_HEADING_TO_SECTION = {term: section for section, terms in _SECTIONS.items() for term in terms}

_COVER_LETTER = ("dear", "sincerely", "i am writing to", "hiring manager", "to whom it may concern",
                 "yours faithfully", "yours sincerely", "cover letter", "madame, monsieur", "je vous prie",
                 "lettre de motivation", "veuillez agréer")
_TRANSCRIPT = ("transcript", "course code", "credits", "term gpa", "cumulative", "course title",
               "academic record", "relevé de notes", "crédits", "moyenne cumulative")


def _marker_pattern(markers: Sequence[str]) -> re.Pattern[str]:
    return re.compile(r"(?<!\w)(" + "|".join(re.escape(m) for m in markers) + r")(?!\w)")


_COVER_RE = _marker_pattern(_COVER_LETTER)
_TRANSCRIPT_RE = _marker_pattern(_TRANSCRIPT)

_YEAR = r"(?:19|20)\d{2}"
_MONTH = r"(?:[^\W\d_]{3,10}\.?|\d{1,2}[/.-])"
_RANGE_SEPARATOR = r"(?:-|\u2010|\u2011|\u2012|\u2013|\u2014|\u2015|to|until|till|through|à|au|jusqu'à|jusqu'au)"
_RANGE_END = (rf"(?:(?:{_MONTH}\s*)?{_YEAR}|present|current|now|today|ongoing|date|présent|aujourd'hui|"
              rf"actuel|actuellement|en cours|maintenant)")
_DATE_RANGE = re.compile(rf"(?<!\d)(?:{_MONTH}\s*)?{_YEAR}\s*{_RANGE_SEPARATOR}\s*{_RANGE_END}(?!\w)",
                         re.IGNORECASE)
# Checked per whitespace token with fullmatch and bounded parts, so a hostile 100 KB token costs
# linear time (an unanchored `[\w.+-]+@...` search retries from every position: quadratic).
_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,8}")
_EMAIL_SEPARATORS = re.compile(r"[:;,|<>()\[\]\"'/]")
MAX_EMAIL_LENGTH = 254


def _has_email(text: str) -> bool:
    for token in text.split():
        if "@" not in token:
            continue
        for piece in _EMAIL_SEPARATORS.split(token):
            piece = piece.rstrip(".")
            if "@" in piece and len(piece) <= MAX_EMAIL_LENGTH and _EMAIL.fullmatch(piece):
                return True
    return False


# "555-0100", "(416) 555-0100", "+1 416 555 0100"; never a year range such as "2014-2018".
_PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[ .-]?)?(?:\(?\d{2,4}\)?[ .-]?)?\d{3}[ .-]\d{4}(?!\d)")
_HEADING_PUNCT = re.compile(r"[^\w\s'&/-]")

PRIOR = -1.0
W_SECTION, MAX_SECTIONS = 1.0, 4
W_DATE_RANGE, MAX_DATE_RANGES = 0.8, 3
W_BULLETS, BULLET_SHARE_FULL = 1.0, 0.10
W_CONTACT = 0.5
W_LIST = 0.5
W_MARKER, MAX_MARKERS = 1.5, 4


def _section_of(line: str) -> Optional[str]:
    """The section category when the line is a heading ("EXPERIENCE", "Skills:" ...), else None."""
    text = line.strip().lower()
    if text.startswith("- "):
        text = text[2:]
    if not text or len(text) > 60:
        return None
    head = text.split(":", 1)[0]
    head = " ".join(_HEADING_PUNCT.sub(" ", head).split())
    if not head or len(head.split()) > 4:
        return None
    return _HEADING_TO_SECTION.get(head)


def _is_list_line(line: str) -> bool:
    tail = line.rsplit(":", 1)[-1]
    items = [item.strip() for item in tail.split(",")]
    return len(items) >= 3 and all(item and len(item.split()) <= 3 for item in items)


def resume_likelihood(text: str) -> float:
    """How much `text` (normalised) looks like a résumé, in [0, 1] (see the module docstring)."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    lowered = text.lower()
    sections = {s for s in map(_section_of, lines) if s is not None}
    date_ranges = len(_DATE_RANGE.findall(text))
    bullet_share = sum(1 for line in lines if line.startswith("- ")) / len(lines) if lines else 0.0
    contact = _has_email(text) or _PHONE.search(text) is not None
    has_list = any(_is_list_line(line) for line in lines)
    cover = len(set(_COVER_RE.findall(lowered)))
    transcript = len(set(_TRANSCRIPT_RE.findall(lowered)))
    z = (PRIOR
         + W_SECTION * min(len(sections), MAX_SECTIONS)
         + W_DATE_RANGE * min(date_ranges, MAX_DATE_RANGES)
         + W_BULLETS * min(1.0, bullet_share / BULLET_SHARE_FULL)
         + (W_CONTACT if contact else 0.0)
         + (W_LIST if has_list else 0.0)
         - W_MARKER * min(cover, MAX_MARKERS)
         - W_MARKER * min(transcript, MAX_MARKERS))
    return round(1.0 / (1.0 + math.exp(-z)), 4)
