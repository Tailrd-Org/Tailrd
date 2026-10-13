"""
Adapters between the stored resume profile and the structured ``ResumeDocument``.

``db_record_to_document`` builds a document from a ``ResumeProfileDB`` row using
the structured columns that were already parsed at upload time (no re-parse, no
LLM call). ``document_to_profile`` is its inverse, so a rewritten document can be
folded back into the profile columns. ``document_to_text`` flattens a document to
plain text for the "Copy" button and for diffing/match-scoring.

Section and item ids are *semantic and stable* ("experience", "experience-0",
"custom-a1b2"). That is what lets ``merge_rewrite`` re-impose the original facts
onto an LLM rewrite, and what lets ``document_to_profile`` route each section back
to the column it came from.
"""

from __future__ import annotations

from typing import Any

from backend.schemas.resume import ResumeProfile
from backend.schemas.resume_document import (
    ResumeDocument,
    ResumeHeader,
    Section,
    SectionItem,
)

# Prefix marking a section id that came from a ResumeProfile.custom_sections entry.
CUSTOM_PREFIX = "custom-"

_COURSEWORK_PREFIX = "Relevant coursework: "
_GPA_PREFIX = "GPA: "


def _get(obj: Any, key: str, default: Any = "") -> Any:
    """Read ``key`` from a dict or a pydantic/ORM object, tolerating either."""
    if isinstance(obj, dict):
        val = obj.get(key, default)
    else:
        val = getattr(obj, key, default)
    return default if val is None else val


def _str_list(value: Any) -> list[str]:
    if not value:
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _experience_section(experience: list[Any]) -> Section:
    return Section(
        id="experience",
        type="experience",
        title="WORK EXPERIENCE",
        items=[
            SectionItem(
                id=f"experience-{i}",
                title=_get(e, "title"),
                subtitle=_get(e, "company"),
                location=_get(e, "location"),
                start_date=_get(e, "start_date"),
                end_date=_get(e, "end_date"),
                bullets=_str_list(_get(e, "bullets", [])),
            )
            for i, e in enumerate(experience)
        ],
    )


def _education_section(education: list[Any]) -> Section:
    items: list[SectionItem] = []
    for i, ed in enumerate(education):
        gpa = _get(ed, "gpa")
        coursework = _str_list(_get(ed, "coursework", []))
        bullets = _str_list(_get(ed, "achievements", []))
        if coursework:
            bullets = bullets + [f"{_COURSEWORK_PREFIX}{', '.join(coursework)}"]
        items.append(
            SectionItem(
                id=f"education-{i}",
                title=_get(ed, "degree"),
                subtitle=_get(ed, "school"),
                location=_get(ed, "location"),
                start_date=_get(ed, "start_date"),
                end_date=_get(ed, "end_date"),
                detail=f"{_GPA_PREFIX}{gpa}" if gpa else "",
                bullets=bullets,
            )
        )
    return Section(id="education", type="education", title="EDUCATION", items=items)


def _projects_section(projects: list[Any]) -> Section:
    return Section(
        id="projects",
        type="projects",
        title="PROJECTS",
        items=[
            SectionItem(
                id=f"projects-{i}",
                title=_get(p, "name"),
                subtitle=_get(p, "organization"),
                location=_get(p, "location"),
                start_date=_get(p, "start_date"),
                end_date=_get(p, "end_date"),
                link=_get(p, "link"),
                bullets=_str_list(_get(p, "bullets", [])),
            )
            for i, p in enumerate(projects)
        ],
    )


def _custom_section(raw: Any) -> Section | None:
    """A stored ``CustomSection`` → a renderable ``Section``.

    Flat ``bullets`` become a single untitled item so the renderer, the PDF, and
    the rewriter all see one uniform shape.
    """
    cid = _get(raw, "id")
    title = _get(raw, "title")
    text = _get(raw, "text")
    bullets = _str_list(_get(raw, "bullets", []))
    raw_items = _get(raw, "items", []) or []

    items = [
        SectionItem(
            id=f"{CUSTOM_PREFIX}{cid}-{i}",
            title=_get(it, "title"),
            subtitle=_get(it, "subtitle"),
            location=_get(it, "location"),
            start_date=_get(it, "start_date"),
            end_date=_get(it, "end_date"),
            detail=_get(it, "detail"),
            link=_get(it, "link"),
            bullets=_str_list(_get(it, "bullets", [])),
        )
        for i, it in enumerate(raw_items)
    ]
    if bullets:
        items.append(SectionItem(id=f"{CUSTOM_PREFIX}{cid}-bullets", bullets=bullets))

    if not (text or items):
        return None

    kind = _get(raw, "kind") or "custom"
    return Section(
        id=f"{CUSTOM_PREFIX}{cid}",
        type="certifications" if kind == "certifications" else "custom",
        title=title or "ADDITIONAL",
        text=text,
        items=items,
    )


def db_record_to_document(record: Any) -> ResumeDocument:
    """Build a ``ResumeDocument`` from a ``ResumeProfileDB`` row or ResumeProfile.

    Only sections with content are emitted. They follow the record's own
    ``section_order`` when it has one, that is the order the sections appeared in
    the file the user uploaded, and a conventional order otherwise. Any section
    with content that ``section_order`` omits is still appended, so nothing is
    lost to a bad or stale ordering.
    """
    header = ResumeHeader(
        name=_get(record, "profile_name") or _get(record, "name") or "",
        email=_get(record, "email"),
        phone=_get(record, "phone"),
        location=_get(record, "location"),
        linkedin_url=_get(record, "linkedin_url"),
        github_url=_get(record, "github_url"),
        other_link=_get(record, "other_link"),
    )

    by_key: dict[str, Section] = {}

    summary = _get(record, "summary")
    if summary:
        by_key["summary"] = Section(
            id="summary",
            type="summary",
            title=_get(record, "summary_title") or "PROFESSIONAL SUMMARY",
            text=summary,
        )

    experience = _get(record, "experience", []) or []
    if experience:
        by_key["experience"] = _experience_section(experience)

    education = _get(record, "education", []) or []
    if education:
        by_key["education"] = _education_section(education)

    projects = _get(record, "projects", []) or []
    if projects:
        by_key["projects"] = _projects_section(projects)

    skills = _str_list(_get(record, "skills", []))
    if skills:
        by_key["skills"] = Section(id="skills", type="skills", title="SKILLS", skills=skills)

    technologies = _get(record, "technologies", {}) or {}
    if isinstance(technologies, dict) and technologies:
        groups = {
            str(cat): _str_list(items)
            for cat, items in technologies.items()
            if _str_list(items)
        }
        if groups:
            by_key["technologies"] = Section(
                id="technologies", type="technologies", title="TECHNOLOGIES", groups=groups
            )

    for raw in _get(record, "custom_sections", []) or []:
        section = _custom_section(raw)
        if section is not None:
            by_key[f"custom:{_get(raw, 'id')}"] = section

    default_order = [
        "summary", "experience", "education", "projects", "skills", "technologies",
        *(k for k in by_key if k.startswith("custom:")),
    ]
    requested = _str_list(_get(record, "section_order", []))
    ordered_keys = [k for k in requested if k in by_key]
    ordered_keys += [k for k in default_order if k in by_key and k not in ordered_keys]

    return ResumeDocument(header=header, sections=[by_key[k] for k in ordered_keys])


def document_to_profile(doc: ResumeDocument, base: ResumeProfile) -> ResumeProfile:
    """Fold a (possibly rewritten) document back into a ResumeProfile.

    ``base`` supplies every factual field; the document supplies the *content* the
    AI is allowed to touch, summary text, skills, technology groups, and bullets,
    plus the new section order. Sections and items are matched by their semantic
    ids, so an item can never land under the wrong employer.
    """
    profile = base.model_copy(deep=True)
    sections = {s.id: s for s in doc.sections}

    summary_section = sections.get("summary")
    if summary_section is not None and summary_section.text.strip():
        profile.summary = summary_section.text.strip()
        profile.summary_title = summary_section.title or profile.summary_title or "PROFESSIONAL SUMMARY"

    def bullets_for(section_id: str, index: int) -> list[str] | None:
        section = sections.get(section_id)
        if section is None:
            return None
        item = next((i for i in section.items if i.id == f"{section_id}-{index}"), None)
        if item is None or not item.bullets:
            return None
        return [b.strip() for b in item.bullets if b.strip()]

    for i, entry in enumerate(profile.experience):
        updated = bullets_for("experience", i)
        if updated is not None:
            entry.bullets = updated

    for i, entry in enumerate(profile.projects):
        updated = bullets_for("projects", i)
        if updated is not None:
            entry.bullets = updated

    # Education bullets carry a synthesized coursework line; strip it back out.
    for i, entry in enumerate(profile.education):
        updated = bullets_for("education", i)
        if updated is None:
            continue
        achievements = [b for b in updated if not b.startswith(_COURSEWORK_PREFIX)]
        coursework_line = next((b for b in updated if b.startswith(_COURSEWORK_PREFIX)), "")
        entry.achievements = achievements
        if coursework_line:
            entry.coursework = [
                c.strip()
                for c in coursework_line[len(_COURSEWORK_PREFIX):].split(",")
                if c.strip()
            ]

    skills_section = sections.get("skills")
    if skills_section is not None and skills_section.skills:
        profile.skills = [s.strip() for s in skills_section.skills if s.strip()]

    tech_section = sections.get("technologies")
    if tech_section is not None and tech_section.groups:
        profile.technologies = {
            k: [v.strip() for v in vals if v.strip()]
            for k, vals in tech_section.groups.items()
        }

    for custom in profile.custom_sections:
        section = sections.get(f"{CUSTOM_PREFIX}{custom.id}")
        if section is None:
            continue
        if section.text.strip():
            custom.text = section.text.strip()
        by_id = {i.id: i for i in section.items}
        flat = by_id.get(f"{CUSTOM_PREFIX}{custom.id}-bullets")
        if flat is not None and flat.bullets:
            custom.bullets = [b.strip() for b in flat.bullets if b.strip()]
        for i, item in enumerate(custom.items):
            edited = by_id.get(f"{CUSTOM_PREFIX}{custom.id}-{i}")
            if edited is not None and edited.bullets:
                item.bullets = [b.strip() for b in edited.bullets if b.strip()]

    profile.section_order = [
        s.id.replace(CUSTOM_PREFIX, "custom:", 1) if s.id.startswith(CUSTOM_PREFIX) else s.id
        for s in doc.sections
    ]
    return profile


def merge_rewrite(
    original: ResumeDocument,
    edited: ResumeDocument,
    section_order: list[str] | None = None,
    new_summary: dict | None = None,
) -> ResumeDocument:
    """Fold an LLM rewrite into the original document, structurally.

    Only *content* is taken from ``edited``, section summary text, the skills
    list, technology groups, and item bullets. Every factual field
    (header/contact, section type + title, item title/company/dates/detail/link,
    and all ids) is taken from ``original``, so the model can never invent
    employers/dates or drop a section.

    An item never comes back with more bullets than it started with: the model may
    rewrite each bullet, but a bullet it *adds* is a claim the candidate never made.

    Two structural moves ARE allowed, both safe:

    - ``section_order``, reorder the (existing) sections by id. Ids not listed
      are appended in their original relative order, so nothing is ever lost;
      unknown ids are ignored.
    - ``new_summary``, ``{"title", "text"}`` prepended as a ``summary`` section,
      but only when the original has no ``summary``/``custom`` section (the one
      place new prose is allowed: a summary of the candidate's real content).
    """
    edited_sections_by_id = {s.id: s for s in edited.sections}

    merged_sections: list[Section] = []
    for i, orig_sec in enumerate(original.sections):
        ed_sec = edited_sections_by_id.get(orig_sec.id)
        if ed_sec is None and i < len(edited.sections):
            ed_sec = edited.sections[i]

        new_sec = orig_sec.model_copy(deep=True)
        if ed_sec is not None:
            if ed_sec.text.strip():
                new_sec.text = ed_sec.text
            if ed_sec.skills:
                new_sec.skills = [s for s in ed_sec.skills if str(s).strip()]
            if ed_sec.groups:
                new_sec.groups = {
                    k: [v for v in vals if str(v).strip()]
                    for k, vals in ed_sec.groups.items()
                }

            ed_items_by_id = {it.id: it for it in ed_sec.items}
            for j, orig_item in enumerate(new_sec.items):
                ed_item = ed_items_by_id.get(orig_item.id)
                if ed_item is None and j < len(ed_sec.items):
                    ed_item = ed_sec.items[j]
                if ed_item is not None and ed_item.bullets:
                    # Rewording a bullet is allowed; ADDING one is not. A bullet the
                    # source never had is a claim the candidate never made, asked to
                    # evidence a listed skill, a model will happily write "Used React
                    # on this project" about a project that never used React. Extra
                    # bullets are dropped here, so an entry that needs more comes back
                    # short and is reported as a gap for the candidate to fill.
                    kept = [b for b in ed_item.bullets if str(b).strip()]
                    orig_item.bullets = kept[: len(orig_item.bullets)]
        merged_sections.append(new_sec)

    # Reorder (never drop): listed ids first in the given order, remainder in
    # original relative order.
    if section_order:
        listed = set(section_order)
        by_id = {s.id: s for s in merged_sections}
        ordered = [by_id[sid] for sid in section_order if sid in by_id]
        ordered += [s for s in merged_sections if s.id not in listed]
        merged_sections = ordered

    # Add a summary only when the original truly has none.
    has_summary = any(s.type in ("summary", "custom") for s in original.sections)
    if new_summary and not has_summary:
        text = str(new_summary.get("text", "")).strip()
        if text:
            merged_sections = [
                Section(
                    id="summary",
                    type="summary",
                    title=str(new_summary.get("title") or "PROFESSIONAL SUMMARY"),
                    text=text,
                )
            ] + merged_sections

    # Header and theme are never AI-editable in the rewrite path.
    return ResumeDocument(
        header=original.header.model_copy(deep=True),
        sections=merged_sections,
        theme=original.theme.model_copy(deep=True),
    )


def describe_changes(original: ResumeDocument, final: ResumeDocument) -> list[str]:
    """Human-readable, deterministic 'what changed' list.

    Ids are stable across ``merge_rewrite``, so we align sections/items by id and
    report only what actually differs. This is the source of truth for the UI's
    "See what's changed" list, the model is never asked to self-report.
    """
    changes: list[str] = []
    orig_ids = [s.id for s in original.sections]
    orig_id_set = set(orig_ids)

    # Reordering (compare the relative order of the sections that existed before).
    kept = [s.id for s in final.sections if s.id in orig_id_set]
    if kept != [i for i in orig_ids if i in set(kept)]:
        changes.append("Reordered sections to lead with the most relevant experience")

    # New sections (e.g. an added summary).
    for s in final.sections:
        if s.id not in orig_id_set and (s.text.strip() or s.items or s.skills):
            changes.append(f"Added a {(s.title or s.type).title()} section")

    # Reworded entries (match items by id).
    orig_bullets = {
        it.id: [b.strip() for b in it.bullets]
        for sec in original.sections
        for it in sec.items
    }
    reworded = 0
    for sec in final.sections:
        for it in sec.items:
            before = orig_bullets.get(it.id)
            if before is not None and [b.strip() for b in it.bullets] != before:
                reworded += 1
    if reworded:
        changes.append(
            f"Rewrote {reworded} entr{'y' if reworded == 1 else 'ies'} for stronger impact"
        )

    # Skills added.
    orig_skills = {s.strip().lower() for sec in original.sections for s in sec.skills}
    added = [
        s
        for sec in final.sections
        for s in sec.skills
        if s.strip().lower() not in orig_skills
    ]
    if added:
        preview = ", ".join(added[:4])
        changes.append(
            f"Added {len(added)} skill{'s' if len(added) > 1 else ''}: {preview}"
            f"{'…' if len(added) > 4 else ''}"
        )

    return changes


def document_to_text(doc: ResumeDocument) -> str:
    """Flatten a document to clean plain text (for Copy / diff / scoring)."""
    lines: list[str] = []
    h = doc.header
    if h.name:
        lines.append(h.name)
    contact = " | ".join(
        v for v in (h.location, h.email, h.phone) if v
    )
    if contact:
        lines.append(contact)
    links = " | ".join(
        v for v in (h.linkedin_url, h.github_url, h.other_link) if v
    )
    if links:
        lines.append(links)

    for section in doc.sections:
        lines.append("")
        lines.append((section.title or section.type).upper())

        if section.type == "summary" or section.type == "custom":
            if section.text:
                lines.append(section.text)

        if section.skills:
            lines.append(", ".join(section.skills))

        if section.groups:
            for category, items in section.groups.items():
                lines.append(f"{category}: {', '.join(items)}")

        for item in section.items:
            heading = ", ".join(v for v in (item.title, item.subtitle) if v)
            dates = " - ".join(v for v in (item.start_date, item.end_date) if v)
            head_line = "  ".join(v for v in (heading, dates) if v)
            if head_line:
                lines.append(head_line)
            if item.detail:
                lines.append(item.detail)
            for bullet in item.bullets:
                if bullet.strip():
                    lines.append(f"- {bullet.strip()}")

    return "\n".join(lines).strip()


def document_to_record(doc: ResumeDocument) -> dict[str, Any]:
    """A document in the stored-profile shape the match engine reads (D-80): the inverse of
    ``db_record_to_document`` for every field the engine uses.

    Keys: ``location`` (the header's), ``summary``, ``experience``, ``education``, ``projects``,
    ``skills``, ``technologies``, ``custom_sections``. Education bullets are split back into
    ``achievements`` and the synthesized coursework line, and the GPA detail into ``gpa``; a custom
    section's untitled bullets item back into its flat ``bullets``. Sections are read by type, so
    a summary the rewrite added is read like a stored one. Used to score the original and the
    tailored document the same way (Task 9.3), so the "before" equals the feed's score.
    """
    record: dict[str, Any] = {
        "location": doc.header.location, "summary": "", "experience": [], "education": [], "projects": [],
        "skills": [], "technologies": {}, "custom_sections": [],
    }
    summaries: list[str] = []
    for section in doc.sections:
        kind = section.type
        if kind == "summary":
            if section.text.strip():
                summaries.append(section.text.strip())
        elif kind == "experience":
            record["experience"] += [
                {"title": it.title, "company": it.subtitle, "location": it.location, "start_date": it.start_date,
                 "end_date": it.end_date, "bullets": list(it.bullets)}
                for it in section.items
            ]
        elif kind == "education":
            for it in section.items:
                coursework = next((b for b in it.bullets if b.startswith(_COURSEWORK_PREFIX)), "")
                record["education"].append({
                    "degree": it.title, "school": it.subtitle, "location": it.location,
                    "start_date": it.start_date, "end_date": it.end_date,
                    "gpa": it.detail[len(_GPA_PREFIX):] if it.detail.startswith(_GPA_PREFIX) else "",
                    "achievements": [b for b in it.bullets if not b.startswith(_COURSEWORK_PREFIX)],
                    "coursework": [c.strip() for c in coursework[len(_COURSEWORK_PREFIX):].split(",") if c.strip()],
                })
        elif kind == "projects":
            record["projects"] += [
                {"name": it.title, "organization": it.subtitle, "location": it.location,
                 "start_date": it.start_date, "end_date": it.end_date, "link": it.link, "bullets": list(it.bullets)}
                for it in section.items
            ]
        elif kind == "skills":
            record["skills"] += list(section.skills)
        elif kind == "technologies":
            for category, values in section.groups.items():
                record["technologies"].setdefault(category, []).extend(values)
        else:  # custom / certifications
            cid = section.id[len(CUSTOM_PREFIX):] if section.id.startswith(CUSTOM_PREFIX) else section.id
            flat_id = f"{CUSTOM_PREFIX}{cid}-bullets"
            bullets = [b for it in section.items if it.id == flat_id for b in it.bullets]
            items = [
                {"title": it.title, "subtitle": it.subtitle, "location": it.location, "start_date": it.start_date,
                 "end_date": it.end_date, "detail": it.detail, "link": it.link, "bullets": list(it.bullets)}
                for it in section.items if it.id != flat_id
            ]
            record["custom_sections"].append({
                "id": cid, "title": section.title, "kind": "certifications" if kind == "certifications" else "custom",
                "text": section.text, "bullets": bullets, "items": items,
            })
    record["summary"] = "\n\n".join(summaries)
    return record
