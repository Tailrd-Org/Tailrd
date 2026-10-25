"""Cost-control tests for the recurring match-alert sweep.

The sweep runs on the scrape cron whether or not anyone opens the app. Since Task 9.2 every score
is the deterministic fit engine's (no LLM, D-83), so the bill it can run up is CPU, database writes
and egress, not model calls. These tests pin the guards that keep it bounded: an explicit kill
switch, memoisation so a given (user, job, résumé) triple is scored once, a wall-clock box, the
opt-out and region filters applied before any scoring, and a fair user cap.

The real engine runs here (built once per process, about ten seconds); ``bank_calls`` only counts
what the sweep hands it. Résumés and postings are invented.
"""

import datetime

import pytest
from sqlalchemy import event

from backend.db.models import (
    JobMatchNotification,
    JobMatchScore,
    MatchFingerprint,
    ResumeProfileDB,
    ScrapedJob,
    User,
    UserSettings,
)
from backend.services import fit_engine, fit_reads, match_notifier

FIT_FP = "fit1:" + "f" * 59


@pytest.fixture(autouse=True)
def _engine_env(monkeypatch):
    """A trace salt, and no LLM anywhere: any attempt to build or call one fails the test."""
    monkeypatch.setenv("FIT_TRACE_SALT", "a-test-salt-of-at-least-16-bytes")

    def refuse(*_a, **_k):
        raise AssertionError("the sweep called an LLM")

    monkeypatch.setattr("backend.services.llm.get_llm_service", refuse)
    monkeypatch.setattr("backend.services.openai_service.OpenAIService._generate", refuse, raising=False)
    monkeypatch.setattr(match_notifier.email_service, "send_job_match_alert",
                        lambda to, jobs, name=None, **_kw: True)


@pytest.fixture
def swept_user(db_session):
    """A verified user with a resume, i.e. one the cron sweep will score."""
    user = User(
        email="sweep@example.com",
        first_name="Sweep",
        email_verified=True,
        auth_provider="local",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    db_session.add(ResumeProfileDB(user_id=user.id, raw_text="resume body text"))
    db_session.commit()
    return user


def _make_job(db_session, title="Engineer", company="Globex", **fields):
    # Alerts cover only jobs at the user's levels (none chosen: Intern/New Grad, Entry).
    fields.setdefault("experience_level", "new_grad")
    job = ScrapedJob(
        title=title,
        company=company,
        url=f"https://jobs.example.com/{title}-{company}".replace(" ", "-"),
        description="Requirements\n- Python\n- SQL\n" + "x" * 200,
        posted_date=datetime.datetime.utcnow(),
        **fields,
    )
    db_session.add(job)
    db_session.commit()
    db_session.refresh(job)
    return job


@pytest.fixture
def bank_calls(monkeypatch):
    """Every bank_scores call the sweep makes, as (résumé raw text, job ids, newly scored ids).
    The real engine still scores."""
    calls = []
    real = fit_engine.bank_scores

    def counting(db, user_id, profile, job_ids, **kwargs):
        results = real(db, user_id, profile, job_ids, **kwargs)
        calls.append((profile.raw_text, list(job_ids), sorted(results)))
        return results

    monkeypatch.setattr(fit_engine, "bank_scores", counting)
    return calls


def _scored(calls) -> list[int]:
    return [job_id for _text, _ids, scored in calls for job_id in scored]


@pytest.mark.asyncio
async def test_kill_switch_spends_nothing(db_session, swept_user, bank_calls, monkeypatch):
    _make_job(db_session)
    monkeypatch.setenv("MATCH_ALERTS_ENABLED", "false")

    result = await match_notifier.sweep_match_alerts(db_session)

    assert bank_calls == []
    assert result["status"] == "disabled"


@pytest.mark.asyncio
async def test_a_job_is_scored_once_not_every_run(db_session, swept_user, bank_calls):
    job = _make_job(db_session)

    first = await match_notifier.sweep_match_alerts(db_session)
    assert _scored(bank_calls) == [job.id] and first["jobs_scored"] == 1

    # Second run: neither the job nor the résumé changed, so the banked row is current.
    second = await match_notifier.sweep_match_alerts(db_session)
    assert _scored(bank_calls) == [job.id] and second["jobs_scored"] == 0

    cached = db_session.query(JobMatchScore).filter_by(user_id=swept_user.id, job_id=job.id).one()
    assert cached.resume_fingerprint.startswith("fit")
    assert db_session.query(JobMatchNotification).count() == 0


@pytest.mark.asyncio
async def test_new_resume_invalidates_the_banked_score(db_session, swept_user, bank_calls):
    _make_job(db_session)

    await match_notifier.sweep_match_alerts(db_session)
    first_fp = db_session.query(JobMatchScore).one().resume_fingerprint

    profile = db_session.query(ResumeProfileDB).filter_by(user_id=swept_user.id).one()
    profile.raw_text = "a materially different resume\nSKILLS\nGo, Rust"
    db_session.commit()

    result = await match_notifier.sweep_match_alerts(db_session)
    assert result["jobs_scored"] == 1

    # Rescored in place: one row per (user, job), not one per résumé revision.
    rows = db_session.query(JobMatchScore).filter_by(user_id=swept_user.id).all()
    assert len(rows) == 1 and rows[0].resume_fingerprint != first_fp


@pytest.mark.asyncio
async def test_the_sweep_never_loads_whole_job_rows_it_cannot_email(db_session, swept_user, bank_calls):
    """Postings are read by column (and cached in match_terms); a job with no Strong match is
    never materialised as a whole row, description and all, on any run."""
    for i in range(3):
        _make_job(db_session, title=f"Role {i}")

    db_session.expunge_all()
    loaded: list = []

    def _record(session, instance):
        if isinstance(instance, ScrapedJob):
            loaded.append(instance)

    event.listen(db_session, "loaded_as_persistent", _record)
    try:
        await match_notifier.sweep_match_alerts(db_session)
        await match_notifier.sweep_match_alerts(db_session)
    finally:
        event.remove(db_session, "loaded_as_persistent", _record)

    assert len(_scored(bank_calls)) == 3
    assert loaded == []


@pytest.fixture
def fake_bank(monkeypatch):
    """bank_scores replaced by a no-op recorder (for tests about banked rows and sends)."""
    calls: list[list[int]] = []

    def bank(db, user_id, profile, job_ids, **_k):
        calls.append(list(job_ids))
        return {}

    monkeypatch.setattr(fit_engine, "bank_scores", bank)
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    monkeypatch.setattr(fit_reads, "engine_ready", lambda: False)
    return calls


def _bank_strong(db_session, user, job, score=91):
    profile = db_session.query(ResumeProfileDB).filter_by(user_id=user.id).one()
    key = fit_reads.input_key(profile, fit_reads.settings_for(db_session, user.id))
    db_session.merge(MatchFingerprint(user_id=user.id, input_key=key, fingerprint=FIT_FP))
    db_session.add(JobMatchScore(user_id=user.id, job_id=job.id, score=score, band="Strong match",
                                 outcome="SCORED", resume_fingerprint=FIT_FP))
    db_session.commit()


@pytest.mark.asyncio
async def test_banked_strong_match_is_emailed_without_scoring_it_again(
    db_session, swept_user, fake_bank, monkeypatch
):
    """A banked Strong match must still produce the alert on a later run (the send budget was
    spent, or Resend failed): the sweep emails it from the bank."""
    job = _make_job(db_session)
    _bank_strong(db_session, swept_user, job)
    sent = []
    monkeypatch.setattr(match_notifier.email_service, "send_job_match_alert",
                        lambda to, jobs, name=None, **_kw: sent.append(jobs) or True)

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["jobs_notified"] == 1
    assert [j["title"] for j in sent[0]] == ["Engineer"]
    assert db_session.query(JobMatchNotification).filter_by(user_id=swept_user.id, job_id=job.id).count() == 1


@pytest.mark.asyncio
async def test_a_missing_salt_still_emails_banked_strong_matches(db_session, swept_user, monkeypatch):
    """Sending needs no scoring: a Strong match banked before the salt went missing still goes
    out, while nothing new is scored and the summary says why."""
    monkeypatch.delenv("FIT_TRACE_SALT")
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    monkeypatch.setattr(fit_reads, "engine_ready", lambda: False)
    _make_job(db_session, title="Unscored")
    cached = _make_job(db_session, title="Cached")
    _bank_strong(db_session, swept_user, cached)
    sent = []
    monkeypatch.setattr(match_notifier.email_service, "send_job_match_alert",
                        lambda to, jobs, name=None, **_kw: sent.append(jobs) or True)

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["fit_trace_salt_missing"] is True
    assert result["jobs_scored"] == 0
    assert result["jobs_notified"] == 1
    assert [j["title"] for j in sent[0]] == ["Cached"]


@pytest.mark.asyncio
async def test_scoring_failures_are_counted_not_silent(db_session, swept_user, monkeypatch):
    monkeypatch.setattr(match_notifier, "SWEEP_CHUNK", 1)
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    _make_job(db_session, title="A")
    _make_job(db_session, title="B")
    calls = []

    def flaky(*_a, **_k):
        calls.append(1)
        raise ValueError("a posting the engine could not take")

    monkeypatch.setattr(fit_engine, "bank_scores", flaky)

    result = await match_notifier.sweep_match_alerts(db_session)

    # A one-off failure never stops the run: every chunk still gets its call.
    assert len(calls) == 2
    assert result["status"] == "completed"
    assert result["scoring_errors"] == 2
    assert result["jobs_scored"] == 0


@pytest.mark.asyncio
async def test_summary_counts_what_was_scored(db_session, swept_user, bank_calls):
    for i in range(3):
        _make_job(db_session, title=f"Role {i}")

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["status"] == "completed"
    assert result["jobs_scored"] == 3
    assert result["scoring_errors"] == 0
    assert "error" not in result


@pytest.mark.asyncio
async def test_disabled_summary_keeps_every_counter(db_session, monkeypatch):
    monkeypatch.setenv("MATCH_ALERTS_ENABLED", "false")

    result = await match_notifier.sweep_match_alerts(db_session)

    assert result["status"] == "disabled"
    assert result["jobs_scored"] == 0 and result["scoring_errors"] == 0


# --- wall-clock box ----------------------------------------------------------


@pytest.mark.asyncio
async def test_scoring_budget_stops_scoring_but_not_banked_sends(
    db_session, swept_user, fake_bank, monkeypatch
):
    """Once CRON_MATCH_BUDGET_S is spent the sweep stops scoring (cron-poll has to finish
    inside Vercel's 300 s), but banked Strong matches still go out."""
    monkeypatch.setenv("CRON_MATCH_BUDGET_S", "100")
    monkeypatch.setattr(match_notifier, "SWEEP_CHUNK", 1)
    cached_job = _make_job(db_session, title="Cached")
    for i in range(4):
        _make_job(db_session, title=f"Role {i}")
    _bank_strong(db_session, swept_user, cached_job)

    clock = {"t": 1000.0}
    monkeypatch.setattr(match_notifier, "_now", lambda: clock["t"])

    def slow(db, user_id, profile, job_ids, **_k):
        fake_bank.append(list(job_ids))
        clock["t"] += 60  # each chunk eats a minute
        return {}

    monkeypatch.setattr(fit_engine, "bank_scores", slow)
    sent = []
    monkeypatch.setattr(match_notifier.email_service, "send_job_match_alert",
                        lambda to, jobs, name=None, **_kw: sent.append(jobs) or True)

    result = await match_notifier.sweep_match_alerts(db_session)

    # Chunks start at +0 s and +60 s; at +120 s the 100 s box is spent.
    assert len(fake_bank) == 2
    assert result["scoring_budget_spent"] is True
    assert result["status"] == "completed"
    assert [j["title"] for j in sent[0]] == ["Cached"]


@pytest.mark.asyncio
async def test_scoring_budget_zero_means_no_box(db_session, swept_user, fake_bank, monkeypatch):
    monkeypatch.setenv("CRON_MATCH_BUDGET_S", "0")
    monkeypatch.setattr(match_notifier, "SWEEP_CHUNK", 1)
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 10_000
        return clock["t"]

    monkeypatch.setattr(match_notifier, "_now", tick)
    for i in range(3):
        _make_job(db_session, title=f"Role {i}")

    result = await match_notifier.sweep_match_alerts(db_session)

    assert len(fake_bank) == 3
    assert result["scoring_budget_spent"] is False


# --- opt-out (2026-09 audit) -------------------------------------------------


@pytest.mark.asyncio
async def test_opted_out_user_costs_nothing_and_gets_nothing(db_session, swept_user, fake_bank):
    """No scoring and no email for an account that unsubscribed, not even a banked Strong
    match from before they left."""
    db_session.add(UserSettings(user_id=swept_user.id, match_alerts_enabled=False))
    cached_job = _make_job(db_session, title="Cached")
    _make_job(db_session, title="Unscored")
    db_session.commit()
    _bank_strong(db_session, swept_user, cached_job, score=95)

    result = await match_notifier.sweep_match_alerts(db_session)

    assert fake_bank == []
    assert result["users_scanned"] == 0
    assert result["jobs_notified"] == 0
    assert db_session.query(JobMatchNotification).count() == 0


@pytest.mark.asyncio
async def test_settings_row_without_a_choice_is_opted_in(db_session, swept_user, bank_calls):
    """The column defaults ON: everyone who never touched it keeps alerts."""
    db_session.add(UserSettings(user_id=swept_user.id))
    db_session.commit()
    _make_job(db_session)

    result = await match_notifier.sweep_match_alerts(db_session)

    assert len(_scored(bank_calls)) == 1
    assert result["users_scanned"] == 1


# --- region (2026-09 audit) --------------------------------------------------
# 33 of the 85 alerts sent to users who picked Canada at onboarding were US jobs.


def _swept(db_session, email, resume, **settings):
    """Another verified user with a resume (and a settings row when given)."""
    user = User(email=email, first_name="U", email_verified=True, auth_provider="local")
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    db_session.add(ResumeProfileDB(user_id=user.id, raw_text=resume))
    if settings:
        db_session.add(UserSettings(user_id=user.id, **settings))
    db_session.commit()
    return user


@pytest.mark.asyncio
async def test_alert_window_honours_the_onboarding_region(db_session, bank_calls):
    ca_only = _swept(db_session, "ca@example.com", "canadian resume", regions="CA")
    anywhere = _swept(db_session, "any@example.com", "open resume", regions="")
    _make_job(db_session, title="Toronto Role", country="CA")
    _make_job(db_session, title="Austin Role", country="US")

    await match_notifier.sweep_match_alerts(db_session)

    scored = {}
    for row in db_session.query(JobMatchScore).all():
        scored.setdefault(row.user_id, set()).add(db_session.get(ScrapedJob, row.job_id).title)
    assert scored[ca_only.id] == {"Toronto Role"}
    assert scored[anywhere.id] == {"Toronto Role", "Austin Role"}
    assert len(_scored(bank_calls)) == 3


@pytest.mark.asyncio
async def test_region_does_not_let_the_window_dig_into_the_backlog(db_session, bank_calls):
    """The region narrows the newest-N window, it doesn't widen it: with N=1 the CA user is
    scored on the newest CA job only, once."""
    _swept(db_session, "ca@example.com", "canadian resume", regions="CA")
    _make_job(db_session, title="Old CA Role", country="CA")
    _make_job(db_session, title="New CA Role", country="CA")
    _make_job(db_session, title="Newest US Role", country="US")

    await match_notifier.sweep_match_alerts(db_session, jobs_per_user=1)
    await match_notifier.sweep_match_alerts(db_session, jobs_per_user=1)

    assert len(_scored(bank_calls)) == 1
    only = db_session.query(JobMatchScore).one()
    assert db_session.get(ScrapedJob, only.job_id).title == "New CA Role"


def test_unknown_region_values_never_silence_a_user():
    assert match_notifier._alert_regions("CA") == ["CA"]
    assert match_notifier._alert_regions(" ca , US ") == ["CA", "US"]
    assert match_notifier._alert_regions("") == []
    assert match_notifier._alert_regions(None) == []
    # No job carries these, so they mean "no limit", not "match nothing".
    assert match_notifier._alert_regions("EU,remote") == []


# --- fair user cap (2026-09 audit) -------------------------------------------


@pytest.mark.asyncio
async def test_cap_counts_only_users_the_sweep_can_score(db_session, bank_calls):
    """Resume-less users used to take cap slots (the LIMIT came before the resume check), so
    with max_users=1 the only resume holder, account 2, was never swept."""
    no_resume = User(email="first@example.com", email_verified=True, auth_provider="local")
    db_session.add(no_resume)
    db_session.commit()
    holder = _swept(db_session, "second@example.com", "holder resume")
    _make_job(db_session)

    result = await match_notifier.sweep_match_alerts(db_session, max_users=1)

    assert [text for text, _ids, _scored_ids in bank_calls] == ["holder resume"]
    assert result["users_scanned"] == 1
    assert db_session.query(JobMatchScore).one().user_id == holder.id


@pytest.mark.asyncio
async def test_cap_skips_cooldown_users_before_taking_a_slot(db_session, bank_calls):
    cooling = _swept(db_session, "cooling@example.com", "cooling resume")
    _swept(db_session, "ready@example.com", "ready resume")
    old = _make_job(db_session, title="Already Sent")
    db_session.add(JobMatchNotification(user_id=cooling.id, job_id=old.id, match_score=90))
    db_session.commit()
    _make_job(db_session, title="Fresh")

    await match_notifier.sweep_match_alerts(db_session, max_users=1)

    assert {text for text, _ids, _scored_ids in bank_calls} == {"ready resume"}


@pytest.mark.asyncio
async def test_cap_rotates_least_recently_scored_first(db_session, bank_calls):
    """Ordering by id alone meant the same accounts every run. Each run scores the users it
    takes, which sends them to the back of the queue."""
    _swept(db_session, "a@example.com", "resume A")
    _swept(db_session, "b@example.com", "resume B")
    _swept(db_session, "c@example.com", "resume C")

    order = []
    for i in range(4):
        _make_job(db_session, title=f"Role {i}")
        before = len(bank_calls)
        await match_notifier.sweep_match_alerts(db_session, max_users=1)
        order.append({text for text, _ids, _scored_ids in bank_calls[before:]})

    assert order == [{"resume A"}, {"resume B"}, {"resume C"}, {"resume A"}]
