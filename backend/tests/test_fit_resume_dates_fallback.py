"""The raw-text fallback for a stored document whose roles lost their dates (D-80 amended, D-152).

Résumé rows extracted before 2026-07-09 (commit 1062972) hold documents whose experience dates are
empty although the raw text has them. When every experience role of the document is undated and
the raw text parses (engine text path) to at least one dated role, the raw-text parse is scored
instead. Pinned here:
1. an undated document plus dated raw text comes out SCORED through the fallback;
2. an undated document with no raw text, or with undated raw text, stays on the document path;
3. a dated document never takes the fallback;
4. the fingerprint changes only in the affected case.

Every résumé and posting here is invented.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services import fit_engine

RAW_DATED = """Alex Sample
Toronto, ON

EDUCATION
BSc Computer Science, Example University        2021 - Present

WORK EXPERIENCE
Software Developer Intern                        05/2024 - 08/2024
Example Systems Inc.
- Built REST APIs in Python and Django for an internal billing service
- Wrote SQL queries and PostgreSQL migrations for reporting tables
- Containerised services with Docker and deployed them to Kubernetes
- Added unit tests with pytest and set up CI on GitHub Actions
- Built React components in TypeScript for an admin dashboard

PROJECTS
Chat App
- Real-time chat built with Node.js and PostgreSQL

TECHNICAL SKILLS
Python, Django, SQL, PostgreSQL, Docker, Kubernetes, React, TypeScript, Git
"""

RAW_UNDATED = RAW_DATED.replace("        2021 - Present", "").replace("                        05/2024 - 08/2024", "")

BULLETS = [line[2:] for line in RAW_DATED.splitlines() if line.startswith("- ")][:5]


def _doc(start: str = "", end: str = "") -> dict:
    """The legacy shape: the job's dates empty, and a project filed as an experience role."""
    return {
        "location": "Toronto, ON",
        "summary": "",
        "experience": [
            {"title": "Software Developer Intern", "company": "Example Systems Inc.", "location": "",
             "start_date": start, "end_date": end, "bullets": BULLETS},
            {"title": "Chat App", "company": "Personal project", "location": "", "start_date": "",
             "end_date": "", "bullets": ["Real-time chat built with Node.js and PostgreSQL"]},
        ],
        "education": [{"school": "Example University", "degree": "BSc Computer Science", "start_date": "",
                       "end_date": ""}],
        "projects": [],
        "skills": ["Python", "Django", "SQL", "PostgreSQL", "Docker", "Kubernetes", "React", "TypeScript", "Git"],
        "technologies": {},
        "custom_sections": [],
    }


def _profile(doc: dict, raw_text):
    return SimpleNamespace(raw_text=raw_text, **doc)


POSTING_TEXT = """About the role
Join our platform team building internal services.

Requirements
- Experience with Python and Django
- SQL and PostgreSQL
- Docker and Kubernetes
- Git and CI pipelines
- Unit testing with pytest

Nice to have
- React and TypeScript
"""


@pytest.fixture(scope="module")
def engine():
    return fit_engine.get_engine()


def _parse(engine, profile):
    return fit_engine.resume_for_profile(profile, None, engine=engine)


def _dated_roles(parsed) -> int:
    return sum(1 for role in parsed.resume.roles if role.dated)


def _score(engine, parsed):
    from matcher.calibrate.percentile import PercentileCalibrator

    day = fit_engine.fit_as_of()
    posting = fit_engine.posting_from_text("Software Engineer, New Grad", "Example Co", POSTING_TEXT,
                                           engine=engine, as_of=day)
    calibrator = PercentileCalibrator(list(engine.panel), min_reference_n=engine.min_reference_n)
    return fit_engine._score_pair(parsed, posting, calibrator, engine, day, fit_engine._BULK_OPTIONS, "test")


def test_an_undated_document_with_dated_raw_text_is_scored_through_the_fallback(engine):
    without_text = _parse(engine, _profile(_doc(), None))
    assert _dated_roles(without_text) == 0
    assert _score(engine, without_text).outcome.value == "NEEDS_REVIEW"  # the legacy row today

    parsed = _parse(engine, _profile(_doc(), RAW_DATED))
    assert _dated_roles(parsed) >= 1
    assert _score(engine, parsed).outcome.value == "SCORED"


@pytest.mark.parametrize("raw_text", [None, "", "   ", RAW_UNDATED])
def test_an_undated_document_without_dated_raw_text_stays_on_the_document_path(engine, raw_text):
    doc_only = _parse(engine, _profile(_doc(), None))
    parsed = _parse(engine, _profile(_doc(), raw_text))
    assert parsed == doc_only
    assert _dated_roles(parsed) == 0


def test_a_dated_document_never_takes_the_fallback(engine, monkeypatch):
    dated = _doc("May 2024", "Aug 2024")
    calls = []
    real = fit_engine.resume_from_document

    def spy(doc, **kwargs):
        calls.append(dict(doc))
        return real(doc, **kwargs)

    monkeypatch.setattr(fit_engine, "resume_from_document", spy)
    fit_engine._PARSE_CACHE.clear()
    parsed = _parse(engine, _profile(dated, RAW_DATED))
    assert len(calls) == 1 and calls[0]["experience"]  # the document path only, no text parse
    assert parsed == _parse(engine, _profile(dated, None))


def test_the_fingerprint_changes_only_in_the_affected_case(engine):
    def fingerprint(profile):
        return fit_engine.fingerprint_for(_parse(engine, profile), engine=engine)

    # Affected: undated document, dated raw text.
    assert fingerprint(_profile(_doc(), RAW_DATED)) != fingerprint(_profile(_doc(), None))
    # Not affected: the raw text makes no difference to the fingerprint.
    assert fingerprint(_profile(_doc(), RAW_UNDATED)) == fingerprint(_profile(_doc(), None))
    dated = _doc("May 2024", "Aug 2024")
    assert fingerprint(_profile(dated, RAW_DATED)) == fingerprint(_profile(dated, None))
    # Deterministic: the same inputs give the same fingerprint after the cache is gone.
    first = fingerprint(_profile(_doc(), RAW_DATED))
    fit_engine._PARSE_CACHE.clear()
    assert fingerprint(_profile(_doc(), RAW_DATED)) == first
