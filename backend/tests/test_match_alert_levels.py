"""Match alerts follow each user's experience levels (scraper v2).

The sweep's scoring window keeps only jobs at the levels in user_settings.experience_levels
(expanded by job_filters.EXPERIENCE_FILTER_MAP; no choice means Intern/New Grad and Entry
Level), and notify_high_matches applies the same rule to whatever a caller hands it. The fit
engine is replaced by a fake that banks every window job as a Strong 91 and records what it was
asked to score, as in test_match_notifier.py.
"""

import asyncio
import datetime

import pytest

from backend.db.models import (
    JobMatchNotification,
    JobMatchScore,
    ResumeProfileDB,
    ScrapedJob,
    User,
    UserSettings,
)
from backend.services import fit_reads, match_notifier

FIT_FP = "fit1:" + "f" * 59
LEVELS = ("internship", "new_grad", "entry", "mid", "senior", "lead", "director", "executive")


@pytest.fixture
def fake_engine(monkeypatch):
    from backend.services import fit_engine

    scored_titles: list[str] = []

    def bank(db, user_id, profile, job_ids, **_k):
        for job_id in job_ids:
            scored_titles.append(db.get(ScrapedJob, job_id).title)
            if db.query(JobMatchScore).filter_by(user_id=user_id, job_id=job_id).first() is None:
                db.add(JobMatchScore(user_id=user_id, job_id=job_id, score=91, band="Strong match",
                                     outcome="SCORED", resume_fingerprint=FIT_FP))
        key = fit_reads.input_key(profile, fit_reads.settings_for(db, user_id))
        fit_reads.remember_fingerprint(db, user_id, key, FIT_FP)
        db.commit()
        return {}

    monkeypatch.setenv("FIT_TRACE_SALT", "a-test-salt-of-at-least-16-bytes")
    monkeypatch.setattr(fit_reads, "engine_ready", lambda: False)
    monkeypatch.setattr(fit_engine, "bank_scores", bank)
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    return scored_titles


@pytest.fixture
def sent(monkeypatch):
    emails: list[list[dict]] = []
    monkeypatch.setattr(
        match_notifier.email_service,
        "send_job_match_alert",
        lambda to, jobs, name=None, **_kw: (emails.append(jobs) or True),
    )
    return emails


def _user(db, email, levels=None):
    user = User(email=email, first_name="Pat", email_verified=True, auth_provider="local")
    db.add(user)
    db.commit()
    db.add(ResumeProfileDB(user_id=user.id, raw_text="resume body text"))
    if levels is not None:
        db.add(UserSettings(user_id=user.id, experience_levels=levels))
    db.commit()
    return user


def _catalogue(db) -> dict[str, ScrapedJob]:
    """One visible job per stored level, plus one no tagger has reached."""
    rows = {}
    for level in LEVELS + ("",):
        title = f"{level or 'untagged'} role"
        row = ScrapedJob(
            title=title,
            company="Globex",
            url=f"https://jobs.example.com/{level or 'untagged'}",
            description=f"{title} description " + "x" * 200,
            posted_date=datetime.datetime.utcnow(),
            experience_level=level,
        )
        db.add(row)
        rows[title] = row
    db.commit()
    return rows


def _titles(levels) -> set[str]:
    return {f"{level or 'untagged'} role" for level in levels}


def _notified(db, user) -> set[str]:
    return {
        db.get(ScrapedJob, row.job_id).title
        for row in db.query(JobMatchNotification).filter_by(user_id=user.id).all()
    }


def test_the_sweep_never_scores_a_director_job_for_an_intern_new_grad_user(
        db_session, fake_engine, sent):
    user = _user(db_session, "student@example.com", levels="intern_new_grad")
    _catalogue(db_session)

    result = asyncio.run(match_notifier.sweep_match_alerts(db_session))

    expected = _titles(("internship", "new_grad", ""))
    assert set(fake_engine) == expected
    assert not {"director role", "executive role"} & set(fake_engine)
    assert result["jobs_notified"] == 3
    assert {j["title"] for j in sent[0]} == expected
    assert _notified(db_session, user) == expected


def test_a_user_with_no_saved_levels_gets_intern_new_grad_and_entry(db_session, fake_engine, sent):
    user = _user(db_session, "nochoice@example.com")  # no settings row at all
    _catalogue(db_session)

    asyncio.run(match_notifier.sweep_match_alerts(db_session))

    expected = _titles(("internship", "new_grad", "entry", ""))
    assert set(fake_engine) == expected
    assert _notified(db_session, user) == expected


def test_a_blank_or_unrecognized_choice_is_the_default_too(db_session, fake_engine, sent):
    _user(db_session, "blank@example.com", levels="")
    _user(db_session, "odd@example.com", levels="Senior Level")
    _catalogue(db_session)

    asyncio.run(match_notifier.sweep_match_alerts(db_session))

    # Two users, each scored over the same default window.
    assert sorted(fake_engine) == sorted(2 * list(_titles(("internship", "new_grad", "entry", ""))))


def test_the_sweep_follows_an_experienced_users_levels(db_session, fake_engine, sent):
    user = _user(db_session, "lead@example.com", levels="senior,director")
    _catalogue(db_session)

    asyncio.run(match_notifier.sweep_match_alerts(db_session))

    expected = _titles(("senior", "director", "executive", ""))
    assert set(fake_engine) == expected
    assert _notified(db_session, user) == expected


def test_a_legacy_new_grad_choice_keeps_entry_level(db_session, fake_engine, sent):
    """What onboarding wrote before the six options: "New Grad / Entry Level"."""
    _user(db_session, "legacy@example.com", levels="new_grad")
    _catalogue(db_session)

    asyncio.run(match_notifier.sweep_match_alerts(db_session))

    assert set(fake_engine) == _titles(("new_grad", "entry", ""))


def test_notify_drops_candidates_outside_the_users_levels(db_session, sent):
    user = _user(db_session, "direct@example.com", levels="intern_new_grad")
    rows = _catalogue(db_session)
    candidates = [(row, 95, "Strong match", "SCORED") for row in rows.values()]

    count = match_notifier.notify_high_matches(db_session, user.id, candidates)

    expected = _titles(("internship", "new_grad", ""))
    assert count == 3
    assert {j["title"] for j in sent[0]} == expected
    assert _notified(db_session, user) == expected
