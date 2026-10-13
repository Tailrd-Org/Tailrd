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


# ─── Fix round 1: scope (I1), guards (I2), trigger edges (M5) ────────────────────────────────


def _profile_edited(doc: dict, raw_text):
    import datetime as dt

    return SimpleNamespace(raw_text=raw_text, content_updated_at=dt.datetime(2026, 8, 1), **doc)


def test_the_original_legacy_profile_still_falls_back(engine):
    """Never edited (content_updated_at unset), and the text covers the document's skills."""
    profile = _profile(_doc(), RAW_DATED)
    profile.content_updated_at = None
    parsed = _parse(engine, profile)
    assert _dated_roles(parsed) >= 1
    assert parsed != _parse(engine, _profile(_doc(), None))


def test_an_edited_document_never_takes_the_fallback(engine):
    """A document edited after upload is what the user sees; raw_text is never updated on edit."""
    edited = _doc()
    edited["skills"] = edited["skills"] + ["Terraform", "Go"]
    parsed = _parse(engine, _profile_edited(edited, RAW_DATED))
    assert parsed == _parse(engine, _profile_edited(edited, None))
    assert _dated_roles(parsed) == 0
    assert "terraform" in str(parsed.resume.model_dump()).lower()


THIN_RAW = "Alex Sample\n\nEXPERIENCE\nIntern   05/2024 - 08/2024\nSome Co\n- Did things\n"


def test_a_thin_raw_text_never_replaces_a_richer_document(engine):
    """Review probe P4: 4 undated roles and full skills against a 1-role dated text with no skills."""
    rich = _doc()
    rich["experience"] = [dict(rich["experience"][0], title=f"Engineer {i}", company=f"Co {i}") for i in range(4)]
    parsed = _parse(engine, _profile(rich, THIN_RAW))
    assert parsed == _parse(engine, _profile(rich, None))
    assert _dated_roles(parsed) == 0


def test_the_text_parse_must_cover_80_percent_of_the_document_skills(engine):
    doc_ids = fit_engine._skill_ids(_parse(engine, _profile(_doc(), None)))
    text_ids = fit_engine._skill_ids(fit_engine.resume_from_document(
        {}, as_of=fit_engine.fit_as_of(), resolver=engine.resolver, config=engine.config,
        parse_config=engine.parse_config, raw_text=RAW_DATED, resources=engine.resume_resources))
    assert doc_ids and len(doc_ids & text_ids) / len(doc_ids) >= 0.8  # the legacy case clears the bar
    # Ten document skills the raw text never names pull the coverage under 80 %: no fallback.
    wide = _doc()
    wide["skills"] = wide["skills"] + ["Terraform", "Go", "Rust", "Scala", "Kafka", "Redis", "GraphQL",
                                       "Ansible", "Jenkins", "Elasticsearch"]
    parsed = _parse(engine, _profile(wide, RAW_DATED))
    assert parsed == _parse(engine, _profile(wide, None))


def test_a_document_without_experience_roles_never_takes_the_fallback(engine):
    skills_only = _doc()
    skills_only["experience"] = []
    assert not fit_engine._document_roles_undated(skills_only)
    assert _parse(engine, _profile(skills_only, RAW_DATED)) == _parse(engine, _profile(skills_only, None))


@pytest.mark.parametrize("blank", [" ", "\t", "  \n "])
def test_whitespace_only_dates_are_undated(engine, blank):
    doc = _doc(blank, blank)
    assert fit_engine._document_roles_undated(doc)
    assert _dated_roles(_parse(engine, _profile(doc, RAW_DATED))) >= 1


def test_tailoring_never_takes_the_fallback_so_a_rewrite_scores_differently(engine):
    """resume_for_document (the tailoring flow) reads the documents themselves, never raw_text's
    dated parse: otherwise the original and the rewrite would both be the same upload-time text."""
    original = _doc()
    tailored = _doc()
    tailored["skills"] = original["skills"] + ["Go", "gRPC", "Terraform"]
    tailored["experience"][0]["bullets"] = original["experience"][0]["bullets"] + [
        "Wrote Go services with gRPC and Terraform"]
    before = fit_engine.resume_for_document(original, None, raw_text=RAW_DATED, engine=engine)
    after = fit_engine.resume_for_document(tailored, None, raw_text=RAW_DATED, engine=engine)
    assert before == fit_engine.resume_for_document(original, None, raw_text=None, engine=engine)
    assert before != after
    assert _dated_roles(before) == 0
