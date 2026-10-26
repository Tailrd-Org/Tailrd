"""backend/services/fit_engine.py: the Tailrd adapter for the deterministic match engine (Task 9.1).

Runs the REAL engine (match-engine/src, the committed taxonomy and panel) against the SQLite test
database. The engine is built once per process (about ten seconds cold), so these tests share it.
Résumés and postings here are invented; none is user data or scraped text.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from backend.db.models import (
    JobMatchScore,
    MatchCalibration,
    MatchTrace,
    ResumeProfileDB,
    ScrapedJob,
    User,
    UserSettings,
)
from backend.services import fit_engine
from backend.services.fit_engine import (
    bank_scores,
    current_fingerprints,
    delete_user_traces,
    explain_job,
    fingerprint_for,
    get_engine,
    live_references,
    postings_for,
    profile_fields,
    refresh_calibration,
    resume_for_profile,
    score_order,
)

from matcher.audit import opaque_id, trace_digest  # importable once fit_engine is imported (D-01)

SALT = "a-test-salt-of-at-least-16-bytes"
AS_OF = dt.date(2026, 10, 1)
USER = 7

DOC = {
    "location": "Toronto, ON",
    "summary": "Computer science student who builds web services.",
    "experience": [{
        "title": "Software Engineering Intern",
        "company": "Example Corp",
        "start_date": "May 2025",
        "end_date": "Aug 2025",
        "bullets": [
            "Built REST APIs in Python and Django serving 2M requests a day",
            "Wrote React components in TypeScript",
        ],
    }],
    "education": [{"school": "Example University", "degree": "BSc Computer Science",
                   "start_date": "2022", "end_date": "2026"}],
    "projects": [{"name": "Chat app", "bullets": ["Built with Node.js and PostgreSQL"]}],
    "skills": ["Python", "SQL", "Docker", "Kubernetes"],
}

RAW_TEXT = """Jordan Example
Toronto, ON

EXPERIENCE
Software Engineering Intern, Example Corp
May 2025 - Aug 2025
- Built REST APIs in Python and Django serving 2M requests a day

SKILLS
Python, SQL, Docker
"""

SOFTWARE = ("Requirements\n- 2+ years of Python\n- SQL and Docker\n- Kubernetes\n"
            "Nice to have\n- Go, Terraform")
NO_SPONSOR = ("Requirements\n- 2+ years of Python\n- SQL and Docker\n\n"
              "We are unable to sponsor work visas for this role.")
SEATTLE = [{"city": "Seattle", "region": "WA", "region_name": "Washington", "country": "United States"}]
TORONTO = [{"city": "Toronto", "region": "ON", "region_name": "Ontario", "country": "Canada"}]


@pytest.fixture(autouse=True)
def _salt(monkeypatch):
    monkeypatch.setenv("FIT_TRACE_SALT", SALT)
    monkeypatch.setattr(fit_engine, "fit_as_of", lambda today=None: AS_OF)


def _profile(db, user_id=USER, doc=DOC, raw_text=RAW_TEXT, created=None) -> ResumeProfileDB:
    profile = ResumeProfileDB(user_id=user_id, raw_text=raw_text, location=doc.get("location"),
                              summary=doc.get("summary"), experience=doc.get("experience", []),
                              education=doc.get("education", []), projects=doc.get("projects", []),
                              skills=doc.get("skills", []), technologies={}, custom_sections=[],
                              created_at=created or dt.datetime(2026, 9, 1))
    db.add(profile)
    db.commit()
    return profile


def _settings(db, user_id=USER, prefilled=None, **columns) -> UserSettings:
    settings = UserSettings(user_id=user_id, prefilled_answers=prefilled or {}, **columns)
    db.add(settings)
    db.commit()
    return settings


def _job(db, n, description=SOFTWARE, *, title="Software Engineer, New Grad", family="Software Engineering",
         locations=TORONTO, work_type="onsite", country="CA") -> ScrapedJob:
    job = ScrapedJob(title=title, company=f"Company {n}", url=f"https://example.com/jobs/{n}",
                     description=description, role_category=family, locations_json=locations,
                     work_type=work_type, country=country, employment_type="full_time",
                     experience_level="new_grad")
    db.add(job)
    db.commit()
    return job


def _user(db, user_id=USER):
    db.add(User(id=user_id, email=f"u{user_id}@example.com"))
    db.commit()


# ─── the engine ─────────────────────────────────────────────────────────────────────────────


def test_the_engine_is_built_once_per_process_and_carries_its_hashes():
    from matcher.config import combined_hash

    engine = get_engine()
    assert get_engine() is engine
    assert engine.combined_hash == combined_hash(engine.config, engine.graph.version)
    assert engine.major == engine.config.engine_version.split(".")[0]
    assert engine.panel and {ref.scope for ref in engine.panel} == {"panel"}
    assert engine.build_seconds > 0


def _copy_calibration(tmp_path: Path) -> Path:
    src = fit_engine.MATCH_ENGINE_ROOT / "data" / "calibration"
    dst = tmp_path / "calibration"
    shutil.copytree(src, dst)
    return dst


def test_the_panel_is_verified_against_its_manifest(tmp_path):
    good = _copy_calibration(tmp_path)
    panel, version = fit_engine.load_verified_panel(good)
    assert panel and version.startswith("panel-")


def test_a_panel_whose_bytes_do_not_match_the_manifest_fails_loudly(tmp_path):
    bad = _copy_calibration(tmp_path)
    manifest = json.loads((bad / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["panel"]["sha256"] = "0" * 64
    (bad / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(fit_engine.FitEngineError, match="sha256"):
        fit_engine.load_verified_panel(bad)


def test_a_panel_of_another_version_than_the_manifest_fails_loudly(tmp_path):
    bad = _copy_calibration(tmp_path)
    manifest = json.loads((bad / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["panel"]["version"] = "panel-000000000000"
    (bad / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(fit_engine.FitEngineError, match="version"):
        fit_engine.load_verified_panel(bad)


# ─── résumé conversion (D-80) ───────────────────────────────────────────────────────────────


def test_a_tailrd_document_converts_to_an_engine_resume(db_session):
    parsed = resume_for_profile(_profile(db_session), None)
    assert parsed.resume is not None and not parsed.reasons
    assert [role.title_raw for role in parsed.resume.roles] == ["Software Engineering Intern"]
    assert parsed.resume.projects
    skill_ids = {ref.canonical_id for ref in parsed.resume.skills_blob}
    assert skill_ids  # the skills list resolved to graph ids


def test_a_document_without_sections_falls_back_to_raw_text(db_session):
    empty = {"location": "", "summary": "", "experience": [], "education": [], "projects": [], "skills": []}
    parsed = resume_for_profile(_profile(db_session, doc=empty), None)
    assert parsed.resume is not None
    assert [role.title_raw for role in parsed.resume.roles] == ["Software Engineering Intern"]


def test_an_empty_profile_is_a_review_reason_never_a_crash(db_session):
    empty = {"location": "", "summary": "", "experience": [], "education": [], "projects": [], "skills": []}
    parsed = resume_for_profile(_profile(db_session, doc=empty, raw_text=""), None)
    assert parsed.resume is None and parsed.reasons


def test_work_authorisation_and_location_come_from_the_saved_answers(db_session):
    settings = _settings(db_session, prefilled={
        "Authorized to work in Canada": "Yes",
        "Authorized to work in the US": "No",
    }, address_city="Toronto", address_state="ON", country="Canada")
    parsed = resume_for_profile(_profile(db_session), settings)
    auth = parsed.resume.work_auth
    assert auth is not None
    assert auth.authorized_countries == ("CA",)
    # D-61 amendment (decide-b round 4): "not authorised in the US" is a need scoped to the US, so
    # no global flag: a US posting warns, never fails, and the Canadian one passes.
    assert auth.needs_sponsorship is None
    assert any(place.country == "CA" and place.region == "ON" for place in parsed.resume.locations)


def test_a_general_sponsorship_answer_does_not_reach_another_country(db_session):
    # The extension's reading (needsSponsorshipIn): a status scoped to Canada plus "Yes, I need
    # sponsorship" says nothing settled about the US, so it stays unknown there (a warning, never a fail).
    settings = _settings(db_session, prefilled={
        "Authorized to work in Canada": "Yes",
        "Do you now or in the future require sponsorship?": "Yes",
    }, address_city="Toronto", address_state="ON", country="Canada")
    auth = resume_for_profile(_profile(db_session), settings).resume.work_auth
    assert auth.authorized_countries == ("CA",)
    assert auth.needs_sponsorship is None


# D-61 amendment (decide-b round 4, review3's engine table): a sponsorship need scoped to some
# countries never becomes the global flag, so it can never FAIL a country whose authorisation is
# merely unknown; and the authorised list is exactly the countries with a positive status.
_REFUSED = "refused"
_NAMED = "named"


def _gate_for(fields: dict, residence, country: str, policy: str):
    from matcher.models import JobPosting, WorkAuthGate
    from matcher.score.gates import _work_authorization

    config = get_engine().config
    work_auth = fit_engine._profile_work_auth(fields, "", residence, config)
    gate = (WorkAuthGate(countries=(country,), sponsorship_available=False) if policy == _REFUSED
            else WorkAuthGate(countries=(country,)))
    return work_auth, _work_authorization(gate, JobPosting(gates=(gate,)), work_auth, config).status.value


@pytest.mark.parametrize("policy", [_REFUSED, _NAMED])
@pytest.mark.parametrize("text,residence,expected", [
    # review3 #42, #210, #211: the citizen's (or PR's) own country passes; the US need stays scoped.
    ("Canada: citizen\nUSA: needs sponsorship", None, {"CA": {"PASS"}, "US": {"WARN"}}),
    ("Canada: Citizen\nUS: Requires sponsorship", None, {"CA": {"PASS"}, "US": {"WARN"}}),
    ("Canada: PR\nUS: need visa", None, {"CA": {"PASS"}, "US": {"WARN"}}),
    # review3 #71: a US posting passes or warns, never fails; the Canadian one is never a PASS.
    ("American living in Toronto, no Canadian work permit", "CA", {"US": {"PASS", "WARN"}, "CA": {"WARN"}}),
    ("American living in Toronto, no Canadian work permit", None, {"US": {"PASS", "WARN"}, "CA": {"WARN"}}),
    # review3's FAIL-on-unknown rows: a US No never fails a Canadian posting.
    ("Not authorized to work in the US", None, {"CA": {"WARN"}, "US": {"WARN"}}),
    ("I am not legally allowed to work in the United States", None, {"CA": {"WARN"}, "US": {"WARN"}}),
    ("Are you legally authorized to work in the US? No", None, {"CA": {"WARN"}, "US": {"WARN"}}),
])
def test_a_scoped_sponsorship_need_never_fails_another_country(text, residence, expected, policy):
    for country, allowed in expected.items():
        work_auth, status = _gate_for({"workAuthorization": text}, residence, country, policy)
        assert status in allowed, (country, work_auth, status)
        assert work_auth is None or work_auth.needs_sponsorship is not True


def test_an_unscoped_sponsorship_need_is_the_global_flag():
    # Nothing scoped to a country: "Yes, I need sponsorship" applies everywhere.
    work_auth, status = _gate_for({"workAuthorization": "", "requiresSponsorship": "Yes"}, "CA", "US", _REFUSED)
    assert work_auth.needs_sponsorship is True and status == "FAIL"


def test_the_authorised_list_is_exactly_the_countries_with_a_positive_status():
    config = get_engine().config
    work_auth = fit_engine._profile_work_auth({"workAuthorization": "Canadian citizen"}, "", None, config)
    assert work_auth.authorized_countries == ("CA",)
    # "would" and "relocate" are outside the allow-list vocabulary (D-61 amendment): no positive status
    # for any country, so not even Canada is listed.
    work_auth = fit_engine._profile_work_auth(
        {"workAuthorization": "Canadian citizen; would relocate to the US on a TN"}, "", None, config)
    assert work_auth is None or work_auth.authorized_countries == ()
    work_auth = fit_engine._profile_work_auth({"workAuthorization": "green card - pending"}, "", None, config)
    assert work_auth is None or work_auth.authorized_countries == ()


def test_unknown_work_authorisation_stays_unknown(db_session):
    parsed = resume_for_profile(_profile(db_session), _settings(db_session))
    assert parsed.resume.work_auth is None


def test_the_default_united_states_location_is_never_a_residence(db_session):
    no_place = dict(DOC, location="")
    settings = _settings(db_session, prefilled={"Are you authorized to work in this country?": "Yes"},
                         location="United States")
    fields = profile_fields(_profile(db_session, doc=no_place), settings)
    assert "United States" not in fields["location"]
    parsed = resume_for_profile(_profile(db_session, doc=no_place), settings)
    assert all(place.country != "US" for place in parsed.resume.locations)
    auth = parsed.resume.work_auth
    assert auth is None or "US" not in auth.authorized_countries


def test_an_active_clearance_is_the_configured_unknown_level_token(db_session):
    settings = _settings(db_session, prefilled={"Security clearance": "Active clearance"})
    parsed = resume_for_profile(_profile(db_session), settings)
    token = get_engine().config.gates.security_clearance.unknown_level_token
    assert parsed.resume.work_auth is not None
    assert parsed.resume.work_auth.clearances == (token,)


def test_no_clearance_answer_holds_no_clearance(db_session):
    settings = _settings(db_session, prefilled={"Security clearance": "Eligible / previously held"})
    parsed = resume_for_profile(_profile(db_session), settings)
    assert parsed.resume.work_auth is None or parsed.resume.work_auth.clearances == ()


def test_every_profile_text_field_is_cut_to_2000_characters(db_session):
    settings = _settings(db_session, city="x" * 5000, street_address="y" * 5000,
                         prefilled={"Are you authorized to work in this country?": "z" * 5000})
    fields = profile_fields(_profile(db_session), settings)
    assert set(fields) == {"location", "addressStreet", "addressCity", "addressState", "postalCode",
                           "country", "workAuthorization", "requiresSponsorship", "authorizedUS",
                           "authorizedCanada"}
    assert max(len(value) for value in fields.values()) == 2000


# ─── posting cache (D-84) ───────────────────────────────────────────────────────────────────


def _count_parses(monkeypatch):
    calls = []
    real = fit_engine.parse_posting

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(fit_engine, "parse_posting", counting)
    return calls


def test_the_posting_is_parsed_once_and_cached_in_match_terms(db_session, monkeypatch):
    job = _job(db_session, 1)
    calls = _count_parses(monkeypatch)
    first = postings_for(db_session, [job.id])[job.id]
    assert len(calls) == 1
    db_session.refresh(job)
    terms = job.match_terms
    assert terms["v"] == get_engine().posting_version(AS_OF)
    assert terms["dl"] == len(SOFTWARE)
    assert terms["posting"] == json.loads(first.model_dump_json())
    assert first.job_family == "Software Engineering"

    again = postings_for(db_session, [job.id])[job.id]
    assert len(calls) == 1  # cache hit: not parsed again
    assert again == first


def test_a_description_of_another_length_invalidates_the_cache(db_session, monkeypatch):
    job = _job(db_session, 1)
    calls = _count_parses(monkeypatch)
    postings_for(db_session, [job.id])
    job.description = SOFTWARE + "\n- Kafka"
    db_session.commit()
    posting = postings_for(db_session, [job.id])[job.id]
    assert len(calls) == 2
    assert "skill.kafka" in {r.skill_id for r in posting.requirements} or posting.requirements
    db_session.refresh(job)
    assert job.match_terms["dl"] == len(SOFTWARE) + len("\n- Kafka")


def test_another_parser_version_invalidates_the_cache(db_session, monkeypatch):
    job = _job(db_session, 1)
    calls = _count_parses(monkeypatch)
    postings_for(db_session, [job.id])
    job.match_terms = dict(job.match_terms, v="an-older-version")
    db_session.commit()
    postings_for(db_session, [job.id])
    assert len(calls) == 2


def test_legacy_local_match_terms_are_recomputed(db_session, monkeypatch):
    job = _job(db_session, 1)
    job.match_terms = {"v": 1, "top": ["python"], "tags": ["python"]}
    db_session.commit()
    calls = _count_parses(monkeypatch)
    postings_for(db_session, [job.id])
    assert len(calls) == 1


def test_posting_meta_comes_from_the_job_columns(db_session):
    job = _job(db_session, 1, NO_SPONSOR, locations=SEATTLE, country="US")
    posting = postings_for(db_session, [job.id])[job.id]
    assert posting.job_family == "Software Engineering"
    location_gates = [gate for gate in posting.gates if gate.kind == "LOCATION"]
    assert location_gates and location_gates[0].locations[0].country == "US"
    assert any(gate.kind == "WORK_AUTHORIZATION" and gate.sponsorship_available is False for gate in posting.gates)


# ─── fingerprints (D-82) ────────────────────────────────────────────────────────────────────


def test_the_fingerprint_is_fit_major_and_64_characters(db_session):
    profile = _profile(db_session)
    parsed = resume_for_profile(profile, None)
    fp = fingerprint_for(parsed)
    assert fp.startswith(f"fit{get_engine().major}:") and len(fp) == 64
    assert current_fingerprints(profile, None) == [fp]


def test_the_fingerprint_changes_when_the_resume_is_edited(db_session):
    profile = _profile(db_session)
    before = current_fingerprints(profile, None)
    profile.skills = [*DOC["skills"], "Terraform"]
    db_session.commit()
    assert current_fingerprints(profile, None) != before


def test_the_fingerprint_changes_when_the_profile_facts_change(db_session):
    profile = _profile(db_session)
    before = current_fingerprints(profile, None)
    # An unscoped need (D-61 amendment, decide-b round 4): a scoped one no longer sets the flag.
    settings = _settings(db_session, prefilled={"Do you now or in the future require sponsorship?": "Yes"})
    assert current_fingerprints(profile, settings) != before


def test_the_fingerprint_changes_when_the_config_changes(db_session):
    profile = _profile(db_session)
    parsed = resume_for_profile(profile, None)
    engine = get_engine()
    changed = dataclasses.replace(engine, combined_hash="0123456789abcdef")
    assert fingerprint_for(parsed, engine=changed) != fingerprint_for(parsed, engine=engine)


def test_the_fingerprint_changes_when_the_panel_changes(db_session):
    """A panel rebuild (D-151) recalibrates every display score, so banked rows go stale and are
    rescored, as for a config change (final review M-1)."""
    profile = _profile(db_session)
    parsed = resume_for_profile(profile, None)
    engine = get_engine()
    changed = dataclasses.replace(engine, panel_version=engine.panel_version + "-rebuilt")
    assert fingerprint_for(parsed, engine=changed) != fingerprint_for(parsed, engine=engine)


# ─── banking (scores + traces) ──────────────────────────────────────────────────────────────


def test_banking_writes_scores_and_traces(db_session):
    _user(db_session)
    profile = _profile(db_session)
    jobs = [_job(db_session, 1), _job(db_session, 2, title="Backend Developer")]
    results = bank_scores(db_session, USER, profile, [job.id for job in jobs])
    assert set(results) == {job.id for job in jobs}

    engine = get_engine()
    fp = current_fingerprints(profile, None)[0]
    subject = opaque_id(str(USER), SALT.encode("utf-8"))
    for job in jobs:
        result = results[job.id]
        row = db_session.query(JobMatchScore).filter_by(user_id=USER, job_id=job.id).one()
        assert row.resume_fingerprint == fp
        assert row.outcome == result.outcome.value
        assert row.engine_version == engine.config.engine_version
        assert row.config_hash == engine.combined_hash
        assert row.raw_score == result.raw_score
        assert result.explanation is None  # bulk sweeps never build explanations
        assert row.score == fit_engine.stored_score(result)
        trace = db_session.query(MatchTrace).filter_by(subject_id=subject, job_id=job.id).one()
        assert trace.trace_digest == trace_digest(trace.trace)
        assert trace.trace["outcome"] == result.outcome.value
        assert trace.config_hash == engine.combined_hash
    assert "user_id" not in {c.name for c in MatchTrace.__table__.columns}
    scored = [job.id for job in jobs if results[job.id].outcome.value == "SCORED"]
    assert scored  # the invented résumé and postings are meant to score
    for job_id in scored:
        row = db_session.query(JobMatchScore).filter_by(user_id=USER, job_id=job_id).one()
        assert row.score == results[job_id].display_score and row.band == results[job_id].band.value

    assert bank_scores(db_session, USER, profile, [job.id for job in jobs]) == {}  # all current


def test_a_resume_edit_rescores_and_overwrites_in_place(db_session):
    _user(db_session)
    profile = _profile(db_session)
    job = _job(db_session, 1)
    bank_scores(db_session, USER, profile, [job.id])
    profile.skills = [*DOC["skills"], "Terraform", "Go"]
    db_session.commit()
    assert set(bank_scores(db_session, USER, profile, [job.id])) == {job.id}
    assert db_session.query(JobMatchScore).filter_by(user_id=USER).count() == 1
    assert db_session.query(MatchTrace).count() == 1


def test_stale_ai_and_local_rows_are_rescored(db_session):
    _user(db_session)
    profile = _profile(db_session)
    job = _job(db_session, 1)
    db_session.add(JobMatchScore(user_id=USER, job_id=job.id, score=91, resume_fingerprint="local1:abc"))
    db_session.commit()
    assert set(bank_scores(db_session, USER, profile, [job.id])) == {job.id}
    row = db_session.query(JobMatchScore).filter_by(user_id=USER, job_id=job.id).one()
    assert row.resume_fingerprint.startswith("fit")


def test_a_gate_fail_stores_no_score(db_session):
    _user(db_session)
    # An unscoped sponsorship need is the global flag (D-61 amendment, decide-b round 4).
    _settings(db_session, prefilled={"Do you now or in the future require sponsorship?": "Yes"},
              address_city="Toronto", address_state="ON", country="Canada")
    profile = _profile(db_session)
    job = _job(db_session, 1, NO_SPONSOR, locations=SEATTLE, country="US")
    result = bank_scores(db_session, USER, profile, [job.id])[job.id]
    assert result.outcome.value == "GATE_FAIL"
    row = db_session.query(JobMatchScore).filter_by(user_id=USER, job_id=job.id).one()
    assert row.score is None and row.outcome == "GATE_FAIL" and row.raw_score is None


def test_an_empty_profile_banks_needs_review_with_no_score(db_session):
    _user(db_session)
    empty = {"location": "", "summary": "", "experience": [], "education": [], "projects": [], "skills": []}
    profile = _profile(db_session, doc=empty, raw_text="")
    job = _job(db_session, 1)
    result = bank_scores(db_session, USER, profile, [job.id])[job.id]
    assert result.outcome.value == "NEEDS_REVIEW"
    row = db_session.query(JobMatchScore).filter_by(user_id=USER, job_id=job.id).one()
    assert row.score is None and row.outcome == "NEEDS_REVIEW"
    assert row.resume_fingerprint.startswith("fit")
    assert db_session.query(MatchTrace).count() == 1
    assert bank_scores(db_session, USER, profile, [job.id]) == {}  # current: not rescored every sweep


def test_an_empty_posting_is_a_review_not_a_low_score(db_session):
    _user(db_session)
    profile = _profile(db_session)
    job = _job(db_session, 1, "")
    result = bank_scores(db_session, USER, profile, [job.id])[job.id]
    assert result.outcome.value == "NEEDS_REVIEW"
    assert db_session.query(JobMatchScore).filter_by(job_id=job.id).one().score is None


def test_banking_refuses_without_a_trace_salt(db_session, monkeypatch):
    _user(db_session)
    profile = _profile(db_session)
    job = _job(db_session, 1)
    monkeypatch.delenv("FIT_TRACE_SALT", raising=False)
    with pytest.raises(fit_engine.FitTraceSaltError):
        bank_scores(db_session, USER, profile, [job.id])
    monkeypatch.setenv("FIT_TRACE_SALT", "too-short")
    with pytest.raises(fit_engine.FitTraceSaltError):
        bank_scores(db_session, USER, profile, [job.id])
    assert db_session.query(JobMatchScore).count() == 0


def test_deleting_a_user_removes_their_traces_by_recomputing_the_subject(db_session):
    _user(db_session)
    _user(db_session, USER + 1)
    job = _job(db_session, 1)
    bank_scores(db_session, USER, _profile(db_session), [job.id])
    bank_scores(db_session, USER + 1, _profile(db_session, user_id=USER + 1), [job.id])
    assert delete_user_traces(db_session, USER) == 1
    remaining = db_session.query(MatchTrace).all()
    assert [row.subject_id for row in remaining] == [opaque_id(str(USER + 1), SALT.encode("utf-8"))]


def test_null_scores_sort_after_every_scored_row(db_session):
    _user(db_session)
    jobs = [_job(db_session, n) for n in range(4)]
    for job, value in zip(jobs, (None, 40, None, 90)):
        db_session.add(JobMatchScore(user_id=USER, job_id=job.id, score=value, resume_fingerprint="fit1:x"))
    db_session.commit()
    rows = db_session.query(JobMatchScore).order_by(*score_order(JobMatchScore.score)).all()
    assert [row.score for row in rows] == [90, 40, None, None]


# ─── calibration references (D-50) ──────────────────────────────────────────────────────────


def _bank_raw(db, job, fingerprint, raw, config_hash=None, user_id=None):
    engine = get_engine()
    db.add(JobMatchScore(user_id=user_id or int(hashlib.sha256(fingerprint.encode()).hexdigest()[:6], 16) + 1000, job_id=job.id,
                         score=None, raw_score=raw, resume_fingerprint=fingerprint,
                         config_hash=config_hash or engine.combined_hash, outcome="SCORED"))


def test_refresh_counts_distinct_people_as_candidates(db_session):
    data = [_job(db_session, n, family="Data Analysis") for n in range(3)]
    design = _job(db_session, 9, family="Design")
    for job in data:
        _bank_raw(db_session, job, "fit1:aaa", 0.5)
        _bank_raw(db_session, job, "fit1:bbb", 0.6)
    _bank_raw(db_session, design, "fit1:ccc", 0.7)
    _bank_raw(db_session, design, "fit1:old", 0.9, config_hash="ffffffffffffffff")  # another config
    db_session.add(JobMatchScore(user_id=4242, job_id=design.id, score=None, raw_score=None,
                                 resume_fingerprint="fit1:ddd", config_hash=get_engine().combined_hash,
                                 outcome="GATE_FAIL"))
    db_session.commit()

    summary = refresh_calibration(db_session)
    rows = {(row.scope, row.key): row for row in db_session.query(MatchCalibration).all()}
    data_ref = rows[("family", "Data Analysis")]
    assert (data_ref.n_candidates, data_ref.n_pairs) == (2, 6)
    assert rows[("family", "Design")].n_pairs == 1  # the other config's row and the NULL raw are left out
    pooled = rows[("global", "*")]
    assert (pooled.n_candidates, pooled.n_pairs) == (3, 7)
    assert summary["global"]["n_candidates"] == 3
    assert {ref.key for ref in live_references(db_session)} == {"Data Analysis", "Design", "*"}


def test_under_thirty_candidates_the_panel_stays_in_charge(db_session):
    _user(db_session)
    job = _job(db_session, 50)
    for n in range(5):
        _bank_raw(db_session, job, f"fit1:{n:03d}", 0.5)
    db_session.commit()
    refresh_calibration(db_session)
    result = bank_scores(db_session, USER, _profile(db_session), [job.id])[job.id]
    assert result.outcome.value == "SCORED"
    assert result.calibration.scope == "panel"  # 5 real people: the shipped panel answers


def test_thirty_distinct_candidates_put_the_family_in_charge(db_session):
    _user(db_session)
    job = _job(db_session, 50)
    for n in range(30):
        _bank_raw(db_session, job, f"fit1:{n:03d}", 0.3 + n / 100)
    db_session.commit()
    refresh_calibration(db_session)
    result = bank_scores(db_session, USER, _profile(db_session), [job.id])[job.id]
    assert result.outcome.value == "SCORED"
    assert (result.calibration.scope, result.calibration.n) == ("family", 30)


def test_a_refresh_replaces_the_previous_references(db_session):
    job = _job(db_session, 1, family="Data Analysis")
    _bank_raw(db_session, job, "fit1:aaa", 0.5)
    db_session.commit()
    refresh_calibration(db_session)
    db_session.query(JobMatchScore).delete()
    db_session.commit()
    refresh_calibration(db_session)
    assert db_session.query(MatchCalibration).count() == 0


# ─── the detail view ────────────────────────────────────────────────────────────────────────


def test_explain_job_gives_one_full_result(db_session):
    _user(db_session)
    _profile(db_session)
    job = _job(db_session, 1)
    detail = explain_job(db_session, USER, job)
    assert detail["outcome"] in ("SCORED", "SCORED_LOW_CONFIDENCE", "NEEDS_REVIEW", "GATE_FAIL")
    assert detail["explanation"]["headline"]
    if detail["outcome"] == "SCORED":
        assert detail["interval"] is not None
    assert detail["fingerprint"].startswith("fit")
    assert "improvements" in detail  # the counterfactual was asked for (a Strong result may list none)


def test_explain_job_without_a_resume_is_none(db_session):
    _user(db_session)
    assert explain_job(db_session, USER, _job(db_session, 1)) is None


# ─── the migration ──────────────────────────────────────────────────────────────────────────


def _legacy_engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE job_match_scores ("
            " id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, job_id INTEGER NOT NULL,"
            " score INTEGER NOT NULL, resume_fingerprint VARCHAR(64) NOT NULL, scored_at DATETIME,"
            " CONSTRAINT uq_job_match_score_user_job UNIQUE (user_id, job_id))"
        ))
        conn.execute(text("CREATE INDEX ix_job_match_scores_user_id ON job_match_scores (user_id)"))
        conn.execute(text("CREATE INDEX ix_job_match_scores_job_id ON job_match_scores (job_id)"))
        conn.execute(text("INSERT INTO job_match_scores (user_id, job_id, score, resume_fingerprint)"
                          " VALUES (1, 2, 77, 'local1:abc')"))
    return engine


NEW_COLUMNS = {"raw_score", "band", "outcome", "confidence", "engine_version", "config_hash",
               "posting_key"}


def test_the_migration_adds_columns_and_makes_score_nullable_keeping_rows():
    from backend.migrations.add_fit_engine import run_migration

    engine = _legacy_engine()
    run_migration(engine)
    columns = {c["name"]: c for c in inspect(engine).get_columns("job_match_scores")}
    assert NEW_COLUMNS <= set(columns)
    assert columns["score"]["nullable"] is True
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT user_id, job_id, score, resume_fingerprint FROM job_match_scores")).all()
        assert [tuple(r) for r in rows] == [(1, 2, 77, "local1:abc")]
        conn.execute(text("INSERT INTO job_match_scores (user_id, job_id, score, resume_fingerprint)"
                          " VALUES (1, 3, NULL, 'fit1:x')"))
        with pytest.raises(Exception):  # the (user, job) pair is still unique
            conn.execute(text("INSERT INTO job_match_scores (user_id, job_id, score, resume_fingerprint)"
                              " VALUES (1, 3, 5, 'fit1:y')"))
    tables = set(inspect(engine).get_table_names())
    assert {"match_traces", "match_calibration"} <= tables


def test_the_migration_is_idempotent():
    from backend.migrations.add_fit_engine import run_migration

    engine = _legacy_engine()
    run_migration(engine)
    run_migration(engine)
    columns = [c["name"] for c in inspect(engine).get_columns("job_match_scores")]
    assert len(columns) == len(set(columns))
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM job_match_scores")).scalar() == 1


def test_the_migration_skips_a_database_without_the_table():
    from backend.migrations.add_fit_engine import run_migration

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    run_migration(engine)  # create_all builds every table from the models; nothing to alter
    assert "job_match_scores" not in inspect(engine).get_table_names()


def test_the_migration_is_registered_at_startup():
    import backend.main as main

    source = Path(main.__file__).read_text(encoding="utf-8")
    assert "add_fit_engine import run_migration" in source
    assert "run_fit_engine_migration()" in source


class _RecordingPostgres:
    """Stands in for a Postgres engine (no Postgres in CI): records every statement sent."""

    class dialect:
        name = "postgresql"

    def __init__(self):
        self.sql: list[str] = []

    def begin(self):
        import contextlib

        engine = self

        class _Conn:
            def execute(self, statement, params=None):
                engine.sql.append(" ".join(str(statement).split()))

        return contextlib.nullcontext(_Conn())


def _fake_inspector(columns: dict[str, bool]):
    class _Inspector:
        def get_table_names(self):
            return ["job_match_scores"]

        def get_columns(self, table):
            return [{"name": name, "nullable": nullable} for name, nullable in columns.items()]

    return lambda _engine: _Inspector()


LEGACY = {"id": False, "user_id": False, "job_id": False, "score": False, "resume_fingerprint": False,
          "scored_at": True}


def test_the_postgres_migration_caps_lock_waits_and_tolerates_a_lost_race(monkeypatch):
    from backend.migrations import add_fit_engine

    monkeypatch.setattr(add_fit_engine, "inspect", _fake_inspector(LEGACY))
    engine = _RecordingPostgres()
    add_fit_engine.run_migration(engine)
    assert engine.sql[0] == "SET LOCAL lock_timeout = '5s'"
    assert engine.sql[1].startswith("SELECT pg_advisory_xact_lock(")
    assert engine.sql[2:] == [
        "ALTER TABLE job_match_scores ADD COLUMN IF NOT EXISTS raw_score FLOAT",
        "ALTER TABLE job_match_scores ADD COLUMN IF NOT EXISTS band VARCHAR(20)",
        "ALTER TABLE job_match_scores ADD COLUMN IF NOT EXISTS outcome VARCHAR(30)",
        "ALTER TABLE job_match_scores ADD COLUMN IF NOT EXISTS confidence FLOAT",
        "ALTER TABLE job_match_scores ADD COLUMN IF NOT EXISTS engine_version VARCHAR(20)",
        "ALTER TABLE job_match_scores ADD COLUMN IF NOT EXISTS config_hash VARCHAR(20)",
        "ALTER TABLE job_match_scores ADD COLUMN IF NOT EXISTS posting_key VARCHAR(40)",
        "ALTER TABLE job_match_scores ALTER COLUMN score DROP NOT NULL",
    ]
    assert not any(word in sql.upper() for sql in engine.sql for word in ("DROP TABLE", "DELETE", "UPDATE "))


def test_the_postgres_migration_sends_nothing_once_applied(monkeypatch):
    from backend.migrations import add_fit_engine

    applied = dict(LEGACY, score=True, **{name: True for name in NEW_COLUMNS})
    monkeypatch.setattr(add_fit_engine, "inspect", _fake_inspector(applied))
    engine = _RecordingPostgres()
    add_fit_engine.run_migration(engine)
    assert engine.sql == []


@pytest.mark.asyncio
async def test_a_failed_fit_engine_migration_fails_startup(monkeypatch):
    # Every JobMatchScore query maps the new columns: an instance without them must not serve.
    import backend.main as main
    from sqlalchemy.exc import OperationalError

    def times_out():
        raise OperationalError("ALTER TABLE ...", {}, Exception("canceling statement due to lock timeout"))

    monkeypatch.setattr(main, "run_fit_engine_migration", times_out)
    with pytest.raises(OperationalError):
        async with main.lifespan(main.app):
            pytest.fail("an instance whose schema migration failed must not serve")


def test_live_references_follow_each_refresh(db_session):
    job = _job(db_session, 1, family="Data Analysis")
    _bank_raw(db_session, job, "fit1:aaa", 0.5)
    db_session.commit()
    refresh_calibration(db_session)
    first = live_references(db_session)
    assert live_references(db_session) == first  # unchanged version: the cached references
    _bank_raw(db_session, _job(db_session, 2, family="Data Analysis"), "fit1:bbb", 0.7)
    db_session.commit()
    refresh_calibration(db_session)
    pooled = {ref.key: ref for ref in live_references(db_session)}["*"]
    assert (pooled.n_candidates, pooled.n_pairs) == (2, 2)


# ─── fix round 1 ────────────────────────────────────────────────────────────────────────────


def test_a_description_backfill_rescores_the_job(db_session):
    # I-1: about 1,100 postings a month arrive with no description; cron-backfill fills it later.
    _user(db_session)
    profile = _profile(db_session)
    job = _job(db_session, 1, "")
    assert bank_scores(db_session, USER, profile, [job.id])[job.id].outcome.value == "NEEDS_REVIEW"
    job.description = SOFTWARE
    db_session.commit()
    again = bank_scores(db_session, USER, profile, [job.id])
    assert again[job.id].outcome.value == "SCORED"
    row = db_session.query(JobMatchScore).filter_by(user_id=USER, job_id=job.id).one()
    assert row.score == again[job.id].display_score
    assert row.posting_key == f"{get_engine().posting_version(AS_OF)}:{len(SOFTWARE)}"
    assert bank_scores(db_session, USER, profile, [job.id]) == {}


def test_a_description_edit_rescores_the_job(db_session):
    _user(db_session)
    profile = _profile(db_session)
    job = _job(db_session, 1)
    bank_scores(db_session, USER, profile, [job.id])
    job.description = SOFTWARE + "\n- Kafka"
    db_session.commit()
    assert set(bank_scores(db_session, USER, profile, [job.id])) == {job.id}


def test_a_parse_config_change_rescores_the_job(db_session):
    _user(db_session)
    profile = _profile(db_session)
    job = _job(db_session, 1)
    bank_scores(db_session, USER, profile, [job.id])
    engine = get_engine()
    changed = dataclasses.replace(engine, parse_config=engine.parse_config.model_copy(update={"version": "other"}))
    assert set(bank_scores(db_session, USER, profile, [job.id], engine=changed)) == {job.id}


def _internal_error_score(monkeypatch, secret="secret posting text"):
    from matcher.errors import Flag
    from matcher.score.engine import review_result

    calls = []

    def fake(resume, posting, *, as_of, config, graph, calibration, options):
        calls.append(options.strict)
        if options.strict:
            raise KeyError(secret)
        return review_result([Flag.INTERNAL_ERROR], config=config, graph=graph, as_of=as_of)

    monkeypatch.setattr(fit_engine, "score", fake)
    return calls


def test_an_internal_error_row_is_retried(db_session, monkeypatch):
    _user(db_session)
    profile = _profile(db_session)
    job = _job(db_session, 1)
    real = fit_engine.score
    _internal_error_score(monkeypatch)
    assert bank_scores(db_session, USER, profile, [job.id])[job.id].outcome.value == "NEEDS_REVIEW"
    assert db_session.query(JobMatchScore).filter_by(job_id=job.id).one().posting_key is None
    monkeypatch.setattr(fit_engine, "score", real)
    assert bank_scores(db_session, USER, profile, [job.id])[job.id].outcome.value == "SCORED"


def test_an_internal_error_is_replayed_strictly_and_logs_only_the_class(db_session, monkeypatch, caplog):
    import logging

    _user(db_session)
    profile = _profile(db_session)
    job = _job(db_session, 1)
    calls = _internal_error_score(monkeypatch)
    with caplog.at_level(logging.ERROR, logger="backend.services.fit_engine"):
        bank_scores(db_session, USER, profile, [job.id])
    assert calls == [False, True]
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert "KeyError" in errors[0].getMessage()
    assert "secret posting text" not in errors[0].getMessage()


def test_a_low_confidence_result_stores_no_score():
    from matcher.models import Band, CalibrationInfo, GateStatus, MatchResult, Outcome

    result = MatchResult(outcome=Outcome.SCORED_LOW_CONFIDENCE, gate_status=GateStatus.PASS, raw_score=0.6,
                         calibrated_score=72, display_score=None, band=Band.GOOD,
                         calibration=CalibrationInfo(method="percentile", scope="panel", n=480, version="panel-x"),
                         engine_version="1.0.0", config_hash="abc", as_of=AS_OF)
    assert fit_engine.stored_score(result) is None


def test_one_person_with_two_resumes_is_one_candidate_sampled_on_the_newest(db_session):
    # I-2: candidates are people; only the user's newest fingerprint's scores are sampled.
    jobs = [_job(db_session, n, family="Data Analysis") for n in range(3)]
    old, new = dt.datetime(2026, 9, 1), dt.datetime(2026, 10, 2)
    current = get_engine().combined_hash
    for job, raw, fp, when in ((jobs[0], 0.2, "fit1:old", old), (jobs[1], 0.8, "fit1:new", new),
                               (jobs[2], 0.7, "fit1:new", new)):
        db_session.add(JobMatchScore(user_id=11, job_id=job.id, raw_score=raw, resume_fingerprint=fp,
                                     config_hash=current, outcome="SCORED", scored_at=when))
    db_session.commit()
    refresh_calibration(db_session)
    rows = {(row.scope, row.key): row for row in db_session.query(MatchCalibration).all()}
    ref = rows[("family", "Data Analysis")]
    assert (ref.n_candidates, ref.n_pairs) == (1, 2)
    assert sorted(ref.samples) == [0.7, 0.8]


def test_inactive_jobs_are_left_out_of_the_references(db_session):
    live = _job(db_session, 1, family="Data Analysis")
    gone = _job(db_session, 2, family="Data Analysis")
    gone.listing_status = "removed"
    _bank_raw(db_session, live, "fit1:aaa", 0.5)
    _bank_raw(db_session, gone, "fit1:aaa", 0.9)
    db_session.commit()
    refresh_calibration(db_session)
    rows = {(row.scope, row.key): row for row in db_session.query(MatchCalibration).all()}
    assert rows[("family", "Data Analysis")].samples == [0.5]


def test_a_stale_job_stays_in_the_references(db_session):
    """stale is hidden from the feed since scraper v2, but it is no closure: most stale jobs
    come back, so their scores stay samples (D-153)."""
    jobs = {status: _job(db_session, n, family="Data Analysis")
            for n, status in enumerate(("stale", "off_target", "expired"))}
    for raw, (status, job) in zip((0.4, 0.6, 0.8), jobs.items()):
        job.listing_status = status
        _bank_raw(db_session, job, "fit1:aaa", raw)
    db_session.commit()
    refresh_calibration(db_session)
    rows = {(row.scope, row.key): row for row in db_session.query(MatchCalibration).all()}
    assert rows[("family", "Data Analysis")].samples == [0.4]


def test_a_failing_sqlite_rebuild_leaves_the_original_table_intact():
    from backend.migrations.add_fit_engine import run_migration

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:  # no UNIQUE (user_id, job_id): the duplicate below breaks the copy
        conn.execute(text("CREATE TABLE job_match_scores (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                          " job_id INTEGER NOT NULL, score INTEGER NOT NULL,"
                          " resume_fingerprint VARCHAR(64) NOT NULL, scored_at DATETIME)"))
        conn.execute(text("CREATE INDEX ix_job_match_scores_user_id ON job_match_scores (user_id)"))
        for n in (1, 2):
            conn.execute(text("INSERT INTO job_match_scores (user_id, job_id, score, resume_fingerprint)"
                              f" VALUES (1, 2, {n}, 'local1:x')"))
    with pytest.raises(Exception):
        run_migration(engine)
    assert set(inspect(engine).get_table_names()) == {"job_match_scores"}
    columns = {c["name"]: c for c in inspect(engine).get_columns("job_match_scores")}
    assert columns["score"]["nullable"] is False and "raw_score" not in columns
    assert "ix_job_match_scores_user_id" in {ix["name"] for ix in inspect(engine).get_indexes("job_match_scores")}
    with engine.begin() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM job_match_scores")).scalar() == 2


def test_two_people_with_the_same_resume_are_two_candidates(db_session):
    # Identical résumé and profile facts give one fingerprint; they are still two people.
    job = _job(db_session, 1, family="Data Analysis")
    for user_id, raw in ((21, 0.4), (22, 0.6)):
        db_session.add(JobMatchScore(user_id=user_id, job_id=job.id, raw_score=raw, resume_fingerprint="fit1:same",
                                     config_hash=get_engine().combined_hash, outcome="SCORED"))
    db_session.commit()
    refresh_calibration(db_session)
    rows = {(row.scope, row.key): row for row in db_session.query(MatchCalibration).all()}
    assert (rows[("family", "Data Analysis")].n_candidates, rows[("family", "Data Analysis")].n_pairs) == (2, 2)
