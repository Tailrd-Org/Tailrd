"""Render a candidate profile as a résumé: 8 templates x 6 formats (EVAL.md section 2.1).

    render(profile, template, fmt, vocab, seed=0) -> bytes

A Jinja2 template (templates/<name>.j2) turns the profile into the neutral intermediate document
of `doc.py`; `formats.py` lays that document out as txt, docx or PDF. The same profile therefore
carries the same facts through every template and format - exactly what format invariance tests.

Presentation details that are not part of the ground truth (the school and graduation year, a
federal supervisor, publications, a military occupation) are derived from the profile id alone, so
they are identical in every rendering of one person. Style choices (date format, colours, scan
noise) come from `seed`, the template and the format, never from the clock or global randomness.
"""

from __future__ import annotations

import hashlib
import random
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

from eval.generate import formats, lexicon
from eval.generate.doc import Document, clean_field, parse_markup
from eval.generate.lexicon import family_data
from eval.generate.profiles import GENERATION_AS_OF, CandidateProfile, functional_skills
from eval.generate.vocabulary import Vocabulary, resolve_form, vocabulary_index
from matcher.models import DegreeLevel

TEMPLATES = (
    "chronological",
    "functional",
    "two_column",
    "academic_cv",
    "ats_plain",
    "designer_heavy",
    "federal_long",
    "military_transition",
)
FORMATS = ("pdf_latex", "pdf_word", "pdf_canva_like", "docx", "txt", "pdf_scanned_300dpi")
FORMAT_EXTENSIONS = {
    "pdf_latex": "pdf",
    "pdf_word": "pdf",
    "pdf_canva_like": "pdf",
    "docx": "docx",
    "txt": "txt",
    "pdf_scanned_300dpi": "pdf",
}
TEMPLATE_DIR = Path(__file__).parent / "templates"

# (month style, range separator, word for a running role) a template may pick from, per seed.
TEMPLATE_DATE_STYLES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "chronological": (("mon", " \u2013 ", "Present"), ("full", " \u2013 ", "Present")),
    "functional": (("mon", " \u2013 ", "Present"),),
    "two_column": (("mon", " - ", "Present"), ("mon", " - ", "Current")),
    "academic_cv": (("full", " \u2013 ", "Present"),),
    "ats_plain": (("num", " - ", "Present"),),
    "designer_heavy": (("mon", " \u2013 ", "Present"), ("mon", " \u2013 ", "Ongoing")),
    "federal_long": (("num", " to ", "Present"),),
    "military_transition": (("mon", " \u2013 ", "Present"), ("full", " to ", "Current")),
}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FULL_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
ICONS = {"email": "\u2709", "phone": "\u2706", "place": "\u25c6", "section": "\u2605"}


def _stable(*parts: object) -> random.Random:
    """A generator seeded by a string (hashed by SHA-512, so never by the process hash seed)."""
    return random.Random(":".join(str(p) for p in parts))


def _month_text(d: date, style: str) -> str:
    if style == "full":
        return f"{_FULL_MONTHS[d.month - 1]} {d.year}"
    if style == "num":
        return f"{d.month:02d}/{d.year}"
    return f"{_MONTHS[d.month - 1]} {d.year}"


def _degree(education, rng: random.Random) -> str:
    field = education.field_raw
    arts = field in lexicon.ARTS_FIELDS
    level = education.level
    if level is DegreeLevel.HIGH_SCHOOL:
        return "High School Diploma"
    if not field:  # a vocabulary without fields of study: the level alone
        return {
            DegreeLevel.ASSOCIATE: "Associate Degree",
            DegreeLevel.BACHELOR: "Bachelor's Degree",
            DegreeLevel.MASTER: "Master's Degree",
        }.get(level, "Doctorate")
    if level is DegreeLevel.ASSOCIATE:
        return rng.choice((f"Associate Degree in {field}", f"Diploma in {field}"))
    if level is DegreeLevel.BACHELOR:
        full, short = ("Bachelor of Arts", "B.A.") if arts else ("Bachelor of Science", "B.Sc.")
        return rng.choice((f"{full} in {field}", f"{short} {field}"))
    if level is DegreeLevel.MASTER:
        full, short = ("Master of Arts", "M.A.") if arts else ("Master of Science", "M.Sc.")
        return rng.choice((f"{full} in {field}", f"{short} {field}"))
    return f"Ph.D. in {field}"


def _education_lines(profile: CandidateProfile, extras: random.Random) -> list[dict[str, str]]:
    """Degree, school and a graduation year. The engine never sees a school or a year (SPEC 9,
    DECISIONS D-14); they are on the page so a parser has to ignore them."""
    first_job = min((r.start.year for r in profile.roles), default=GENERATION_AS_OF.year - 2)
    year = first_job - (1 if extras.random() < 0.5 else 0)
    lines = []
    for education in profile.education:
        school = f"{extras.choice(lexicon.SCHOOL_PREFIXES)} {extras.choice(lexicon.SCHOOL_KINDS)}"
        when = f"Expected {GENERATION_AS_OF.year + 1}" if education.in_progress else str(year)
        lines.append({"degree": _degree(education, extras), "school": school, "year": when})
        year -= extras.randint(2, 3)
    return lines


def _publications(profile: CandidateProfile, extras: random.Random) -> list[str]:
    initial = profile.name.split(" ")[0][0]
    surname = profile.name.split(" ")[1]
    out = []
    for number, year in enumerate(
        sorted(extras.sample(range(2012, 2026), extras.randint(3, 6)), reverse=True), 1
    ):
        title = (
            f"{extras.choice(lexicon.PUB_ADJECTIVES)} {extras.choice(lexicon.PUB_NOUNS)} "
            f"for {extras.choice(lexicon.PUB_TOPICS)}"
        )
        venue = extras.choice(lexicon.PUB_VENUES)
        out.append(f"[{number}] {initial}. {surname} and J. Sample. {year}. {title}. {venue}.")
    return out


def _context(profile: CandidateProfile, vocab: Vocabulary, template: str, seed: int) -> dict[str, Any]:
    index = vocabulary_index(vocab)
    fam = family_data(profile.family)
    extras = _stable("extras", profile.profile_id)  # the same in every template and format
    federal = _stable("federal", profile.profile_id)  # drawn apart: only the federal template shows these
    month_style, separator, present = _stable("style", seed, template).choice(TEMPLATE_DATE_STYLES[template])

    def when(d: date | None) -> str:
        return _month_text(d, month_style) if d else present

    roles = []
    for role in profile.roles:
        city, region, _country = extras.choice(lexicon.LOCATIONS)
        supervisor = f"{federal.choice(lexicon.FIRST_NAMES)} {federal.choice(lexicon.LAST_NAMES)}"
        supervisor_phone = f"({federal.choice(lexicon.AREA_CODES)}) 555-01{federal.randint(0, 99):02d}"
        roles.append(
            {
                "title": role.title,
                "employer": role.employer,
                "dates": f"{when(role.start)}{separator}{when(role.end)}",
                "place": f"{city}, {region}",
                "bullets": [b.text for b in role.bullets],
                "hours": federal.choice((40, 40, 40, 30, 20)),
                "supervisor": f"{supervisor}, {federal.choice(lexicon.SUPERVISOR_ROLES)}",
                "supervisor_phone": supervisor_phone,
                "may_contact": federal.choice(("Yes", "No")),
                # a federal résumé is long by nature: even a one-job history runs past a page
                "duties": federal.sample(lexicon.FEDERAL_DUTIES, federal.randint(7, 9)),
            }
        )
    projects = [
        {
            "name": p.name,
            "dates": f"{_month_text(p.start, month_style)}{separator}{_month_text(p.end, month_style)}",
            "bullets": [b.text for b in p.bullets],
        }
        for p in profile.projects
    ]
    education = _education_lines(profile, extras)
    code, mos_title, translation = extras.choice(fam.mos)
    military = {
        "branch": extras.choice(lexicon.MILITARY_BRANCHES),
        "rank": extras.choice(lexicon.MILITARY_RANKS),
        "code": code,
        "title": mos_title,
        "translation": translation,
    }
    publications = _publications(profile, extras)
    teaching = extras.sample(lexicon.TEACHING, extras.randint(2, 3))
    ksa = federal.sample(lexicon.FEDERAL_KSA, len(lexicon.FEDERAL_KSA))  # all of them, in this person's order

    years = {sid: y for sid, y, _tier, _last in profile.skills}
    bars = []
    for label in profile.skills_section:
        shown = years.get(resolve_form(vocab, label) or "", 0.0)
        bars.append((label, 2 if shown == 0 else min(5, 2 + int(shown // 2))))
    grouped: dict[Any, list[str]] = {}
    for skill_id, spelling in functional_skills(profile, vocab):
        grouped.setdefault(index.by_id[skill_id].kind, []).append(spelling)
    skill_groups = [
        (lexicon.KIND_THEMES[kind], grouped[kind]) for kind in lexicon.KIND_THEMES if kind in grouped
    ]

    where = profile.locations[0] if profile.locations else None
    place = f"{where.city.title()}, {where.region}" if where and where.city else ""
    context = {
        "name": profile.name,
        "email": profile.email,
        "phone": profile.phone,
        "place": place,
        "headline": profile.roles[0].title if profile.roles else "",
        "summary": profile.summary,
        "roles": roles,
        "projects": projects,
        "education": education,
        "certifications": [index.by_id[c].label for c in profile.certifications],
        "skills": list(profile.skills_section),
        "skill_groups": skill_groups,
        "bars": bars,
        "publications": publications,
        "teaching": teaching,
        "ksa": ksa,
        "military": military,
        "icons": ICONS,
    }
    return _clean(context)


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        return clean_field(value)
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


@lru_cache(maxsize=1)
def _environment():
    import jinja2

    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined,
    )


def _check(template: str, fmt: str) -> None:
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; expected one of {TEMPLATES}")
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {FORMATS}")


def build_document(profile: CandidateProfile, template: str, vocab: Vocabulary, *, seed: int = 0) -> Document:
    """The intermediate document of one profile in one template (format independent)."""
    _check(template, "txt")
    markup = _environment().get_template(f"{template}.j2").render(p=_context(profile, vocab, template, seed))
    return parse_markup(markup)


def _number(*parts: object) -> int:
    return int.from_bytes(hashlib.sha256(":".join(str(p) for p in parts).encode()).digest()[:8], "big")


def render_document(doc: Document, fmt: str, *, seed: int, template: str, profile_id: str) -> bytes:
    """One format of an already built document."""
    _check(template, fmt)
    key = (seed, template, fmt, profile_id)
    if fmt == "txt":
        return formats.to_txt(doc)
    if fmt == "docx":
        return formats.to_docx(doc)
    if fmt == "pdf_latex":
        return formats.to_pdf(doc, "latex", title=f"Resume {profile_id}")
    if fmt == "pdf_word":
        return formats.to_pdf(doc, "word", title=f"Resume {profile_id}")
    if fmt == "pdf_canva_like":
        return formats.to_pdf(
            doc, "canva", accent_roll=_number("accent", *key) % 6, title=f"Resume {profile_id}"
        )
    # a scan is the word-processor PDF, printed and photographed
    return formats.scan(formats.to_pdf(doc, "word", title=f"Resume {profile_id}"), _number("scan", *key))


def render(profile: CandidateProfile, template: str, fmt: str, vocab: Vocabulary, *, seed: int = 0) -> bytes:
    """`profile` as a document: one of TEMPLATES in one of FORMATS. Same inputs, same bytes."""
    _check(template, fmt)
    doc = build_document(profile, template, vocab, seed=seed)
    return render_document(doc, fmt, seed=seed, template=template, profile_id=profile.profile_id)
