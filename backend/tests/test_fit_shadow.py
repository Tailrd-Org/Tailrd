"""Shadow scoring in the cron sweep (EVAL section 6, Task 9.6, D-150).

When FIT_SHADOW_CONFIG names a config directory, the sweep scores each chunk a second time with
that config and reports the difference in ``summary["shadow"]``. Pinned here:
* only the live result is persisted: no score row or trace carries the shadow config;
* the shadow pass writes nothing (no pending change, no extra row, the posting cache untouched);
* a shadow config that fails to load is reported and the live sweep runs on;
* the shadow pass counts against the same scoring deadline;
* the summary's histograms, score delta, band agreement and Strong crossings are computed right;
* the drift report shows its alarms and its skipped checks in the summary and in the log.

Résumés and postings here are invented; none is user data or scraped text.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from backend.db.models import JobMatchScore, MatchTrace, ScrapedJob
from backend.services import fit_reads, match_notifier
from backend.tests.test_fit_scoring_paths import _job, _resume, _salt, _user, no_llm, sent  # noqa: F401

LIVE_CONFIG = Path(__file__).resolve().parents[2] / "match-engine" / "config"


def _shadow_dir(tmp_path: Path) -> Path:
    """A copy of the live config with other weights and bands (a candidate config)."""
    target = tmp_path / "candidate-config"
    shutil.copytree(LIVE_CONFIG, target)
    weights = yaml.safe_load((target / "weights.yaml").read_text(encoding="utf-8"))
    weights["dimensions"].update(required_skills=0.25, experience=0.25)
    (target / "weights.yaml").write_text(yaml.safe_dump(weights), encoding="utf-8")
    bands = yaml.safe_load((target / "bands.yaml").read_text(encoding="utf-8"))
    for rule, low in zip(bands["bands"], (3, 2, 1, 0)):
        rule["min"] = low
    (target / "bands.yaml").write_text(yaml.safe_dump(bands, sort_keys=False), encoding="utf-8")
    return target


def _result(outcome, band=None, display=None, raw=None):
    return SimpleNamespace(outcome=SimpleNamespace(value=outcome),
                           band=None if band is None else SimpleNamespace(value=band),
                           display_score=display, raw_score=raw)


# ─── The tally ───────────────────────────────────────────────────────────────────────────────


def test_the_shadow_tally_reports_histograms_delta_agreement_and_strong_crossings():
    tally = match_notifier.ShadowTally(config_hash="cand")
    strong, good, partial = fit_reads.BAND_STRONG, fit_reads.BAND_GOOD, fit_reads.BAND_PARTIAL
    tally.add(_result("SCORED", strong, 90), _result("SCORED", good, 80))       # out of Strong, -10
    tally.add(_result("SCORED", good, 80), _result("SCORED", strong, 86))       # into Strong, +6
    tally.add(_result("SCORED", partial, 60), _result("SCORED", partial, 62))   # agree, +2
    tally.add(_result("SCORED_LOW_CONFIDENCE", good, 75), _result("SCORED", good, 75))  # agree, no delta
    tally.add(_result("NEEDS_REVIEW"), _result("NEEDS_REVIEW"))
    tally.add(_result("SCORED", strong, 88), _result("GATE_FAIL"))              # out of Strong
    out = tally.summary()
    assert out["status"] == "ok"
    assert out["config_hash"] == "cand"
    assert out["jobs"] == 6
    assert out["outcomes"]["live"] == {"SCORED": 4, "SCORED_LOW_CONFIDENCE": 1, "NEEDS_REVIEW": 1}
    assert out["outcomes"]["shadow"] == {"SCORED": 4, "NEEDS_REVIEW": 1, "GATE_FAIL": 1}
    assert out["bands"]["live"] == {strong: 2, good: 2, partial: 1}
    assert out["bands"]["shadow"] == {strong: 1, good: 2, partial: 1}
    # Deltas only where both sides are SCORED: -10, +6, +2.
    assert out["score_delta"] == {"n": 3, "mean": pytest.approx(-2 / 3), "p50": 2.0}
    # Agreement over the jobs where both sides have a band: 2 of 4.
    assert out["band_agreement"] == {"n": 4, "agree": 2, "rate": 0.5}
    assert out["crossed_strong"] == {"into": 1, "out_of": 2, "total": 3}
    assert out["errors"] == 0 and out["skipped_deadline"] == 0


def test_an_empty_tally_reports_no_numbers():
    out = match_notifier.ShadowTally(config_hash=None).summary()
    assert out["jobs"] == 0
    assert out["score_delta"] == {"n": 0, "mean": None, "p50": None}
    assert out["band_agreement"] == {"n": 0, "agree": 0, "rate": None}


# ─── The sweep, with the real engine ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sweep_shadow_scores_and_persists_only_the_live_result(db_session, no_llm, sent, tmp_path,
                                                                         monkeypatch, caplog):
    from backend.services import fit_engine

    monkeypatch.setenv("FIT_SHADOW_CONFIG", str(_shadow_dir(tmp_path)))
    _user(db_session)
    _resume(db_session)
    jobs = [_job(db_session) for _ in range(3)]
    with caplog.at_level(logging.INFO):
        summary = await match_notifier.sweep_match_alerts(db_session)
    assert summary["status"] == "completed" and summary["jobs_scored"] == 3

    shadow = summary["shadow"]
    assert shadow["status"] == "ok", shadow
    live_hash = fit_engine.get_engine().combined_hash
    assert shadow["config_hash"] and shadow["config_hash"] != live_hash
    assert shadow["jobs"] == 3
    assert sum(shadow["outcomes"]["live"].values()) == 3
    assert sum(shadow["outcomes"]["shadow"].values()) == 3
    assert set(shadow) >= {"bands", "score_delta", "band_agreement", "crossed_strong", "errors",
                           "skipped_deadline", "postings"}

    # Only the live result is persisted, with the live config.
    rows = db_session.query(JobMatchScore).all()
    assert len(rows) == 3 and {r.config_hash for r in rows} == {live_hash}
    traces = db_session.query(MatchTrace).all()
    assert len(traces) == 3 and {t.config_hash for t in traces} == {live_hash}
    # The posting cache is the live one.
    version = fit_engine.get_engine().posting_version(fit_engine.fit_as_of())
    for job in jobs:
        db_session.refresh(job)
        assert db_session.get(ScrapedJob, job.id).match_terms["v"] == version
    # The diff is logged.
    assert any("match-alert sweep shadow:" in rec.getMessage() for rec in caplog.records)


def test_shadow_scores_write_nothing(db_session, tmp_path):
    from backend.services import fit_engine

    _user(db_session)
    profile = _resume(db_session)
    job = _job(db_session)
    live = fit_engine.get_engine()
    postings: dict = {}
    results = fit_engine.bank_scores(db_session, 1, profile, [job.id], postings_out=postings)
    assert set(postings) == set(results) == {job.id}
    counts = (db_session.query(JobMatchScore).count(), db_session.query(MatchTrace).count())
    terms = db_session.get(ScrapedJob, job.id).match_terms

    shadow = fit_engine.build_shadow_engine(_shadow_dir(tmp_path), live=live)
    assert shadow.combined_hash != live.combined_hash
    shadow_results = fit_engine.shadow_scores(db_session, 1, profile, postings, shadow=shadow)
    assert set(shadow_results) == {job.id}
    assert shadow_results[job.id].config_hash != results[job.id].config_hash
    assert not (db_session.new or db_session.dirty or db_session.deleted)
    assert (db_session.query(JobMatchScore).count(), db_session.query(MatchTrace).count()) == counts
    assert db_session.get(ScrapedJob, job.id).match_terms == terms


@pytest.mark.asyncio
async def test_a_shadow_config_that_fails_to_load_never_breaks_the_live_sweep(db_session, no_llm, sent,
                                                                              tmp_path, monkeypatch):
    broken = _shadow_dir(tmp_path)
    (broken / "weights.yaml").write_text("dimensions:\n  required_skills: 2.0\n", encoding="utf-8")
    monkeypatch.setenv("FIT_SHADOW_CONFIG", str(broken))
    _user(db_session)
    _resume(db_session)
    _job(db_session)
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert summary["status"] == "completed" and summary["jobs_scored"] == 1
    assert db_session.query(JobMatchScore).count() == 1
    assert summary["shadow"]["status"] == "error"
    assert summary["shadow"]["error"].startswith(("FitEngineError", "ConfigError"))
    assert summary["shadow"]["jobs"] == 0


@pytest.mark.asyncio
async def test_a_missing_shadow_directory_is_reported(db_session, no_llm, sent, tmp_path, monkeypatch):
    monkeypatch.setenv("FIT_SHADOW_CONFIG", str(tmp_path / "nope"))
    _user(db_session)
    _resume(db_session)
    _job(db_session)
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert summary["jobs_scored"] == 1
    assert summary["shadow"]["status"] == "error"


@pytest.mark.asyncio
async def test_no_shadow_key_without_the_env_var(db_session, monkeypatch):
    from backend.services import fit_engine

    monkeypatch.delenv("FIT_SHADOW_CONFIG", raising=False)
    monkeypatch.setattr(fit_engine, "bank_scores", lambda *a, **k: {})
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    _user(db_session)
    _resume(db_session)
    _job(db_session)
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert "shadow" not in summary


# ─── The deadline ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_shadow_pass_counts_against_the_same_deadline(db_session, monkeypatch, tmp_path):
    from backend.services import fit_engine

    clock = {"now": 0.0}
    monkeypatch.setattr(match_notifier, "_now", lambda: clock["now"])
    monkeypatch.setenv("CRON_MATCH_BUDGET_S", "100")
    monkeypatch.setenv("FIT_SHADOW_CONFIG", str(tmp_path))
    monkeypatch.setattr(match_notifier, "SWEEP_CHUNK", 1)
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    live_calls: list = []

    def bank(db, user_id, profile, job_ids, postings_out=None, **_k):
        live_calls.append(list(job_ids))
        if postings_out is not None:
            postings_out.update({j: object() for j in job_ids})
        return {j: _result("SCORED", fit_reads.BAND_GOOD, 80, 0.5) for j in job_ids}

    def shadow_scores(db, user_id, profile, postings, *, shadow, **_k):
        clock["now"] += 150.0  # the shadow pass alone spends the whole budget
        return {j: _result("SCORED", fit_reads.BAND_GOOD, 81, 0.5) for j in postings}

    monkeypatch.setattr(fit_engine, "bank_scores", bank)
    monkeypatch.setattr(fit_engine, "build_shadow_engine", lambda path, **k: SimpleNamespace(combined_hash="cand"))
    monkeypatch.setattr(fit_engine, "shadow_scores", shadow_scores)
    _user(db_session)
    _resume(db_session)
    for _ in range(3):
        _job(db_session)
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert len(live_calls) == 1  # the next live chunk saw the deadline the shadow pass used up
    assert summary["scoring_budget_spent"] is True
    assert summary["shadow"]["jobs"] == 1


@pytest.mark.asyncio
async def test_a_past_deadline_skips_the_shadow_pass(db_session, monkeypatch, tmp_path):
    from backend.services import fit_engine

    clock = {"now": 0.0}
    monkeypatch.setattr(match_notifier, "_now", lambda: clock["now"])
    monkeypatch.setenv("CRON_MATCH_BUDGET_S", "100")
    monkeypatch.setenv("FIT_SHADOW_CONFIG", str(tmp_path))
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})

    def bank(db, user_id, profile, job_ids, postings_out=None, **_k):
        clock["now"] += 150.0  # the live chunk itself overran
        if postings_out is not None:
            postings_out.update({j: object() for j in job_ids})
        return {j: _result("SCORED", fit_reads.BAND_GOOD, 80, 0.5) for j in job_ids}

    def forbidden(*_a, **_k):
        raise AssertionError("the shadow pass ran past the deadline")

    monkeypatch.setattr(fit_engine, "bank_scores", bank)
    monkeypatch.setattr(fit_engine, "build_shadow_engine", lambda path, **k: SimpleNamespace(combined_hash="cand"))
    monkeypatch.setattr(fit_engine, "shadow_scores", forbidden)
    _user(db_session)
    _resume(db_session)
    _job(db_session)
    _job(db_session)
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert summary["shadow"]["jobs"] == 0
    assert summary["shadow"]["skipped_deadline"] == 2


@pytest.mark.asyncio
async def test_a_shadow_scoring_fault_is_counted_and_the_live_sweep_runs_on(db_session, monkeypatch, tmp_path):
    from backend.services import fit_engine

    monkeypatch.setenv("FIT_SHADOW_CONFIG", str(tmp_path))
    monkeypatch.setattr(match_notifier, "SWEEP_CHUNK", 1)
    monkeypatch.setattr(match_notifier, "_drift_report", lambda *a, **k: {})
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})
    live_calls: list = []

    def bank(db, user_id, profile, job_ids, postings_out=None, **_k):
        live_calls.append(list(job_ids))
        if postings_out is not None:
            postings_out.update({j: object() for j in job_ids})
        return {j: _result("SCORED", fit_reads.BAND_GOOD, 80, 0.5) for j in job_ids}

    def broken(*_a, **_k):
        raise KeyError("boom")

    monkeypatch.setattr(fit_engine, "bank_scores", bank)
    monkeypatch.setattr(fit_engine, "build_shadow_engine", lambda path, **k: SimpleNamespace(combined_hash="cand"))
    monkeypatch.setattr(fit_engine, "shadow_scores", broken)
    _user(db_session)
    _resume(db_session)
    _job(db_session)
    _job(db_session)
    summary = await match_notifier.sweep_match_alerts(db_session)
    assert len(live_calls) == 2 and summary["jobs_scored"] == 2
    assert summary["shadow"]["errors"] == 2 and summary["shadow"]["jobs"] == 0


# ─── The drift report in the sweep summary ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sweep_summary_and_log_show_drift_alarms_and_skipped_checks(db_session, monkeypatch, caplog):
    """A sweep that withheld most scores alarms (abstention) AND lists the skipped PSI (too few)."""
    from backend.services import fit_engine

    monkeypatch.delenv("FIT_SHADOW_CONFIG", raising=False)
    monkeypatch.setattr(match_notifier, "_engine_hash", lambda: "hash")
    monkeypatch.setattr(fit_engine, "refresh_calibration", lambda db, **k: {})

    def bank(db, user_id, profile, job_ids, **_k):
        return {j: _result("NEEDS_REVIEW") for j in job_ids}

    monkeypatch.setattr(fit_engine, "bank_scores", bank)
    _user(db_session)
    _resume(db_session)
    for _ in range(3):
        _job(db_session)
    with caplog.at_level(logging.INFO):
        summary = await match_notifier.sweep_match_alerts(db_session)
    drift = summary["drift"]
    assert drift["abstention_rate"] == 1.0
    assert "ABSTENTION_HIGH" in [a["code"] for a in drift["alarms"]]
    assert [s["code"] for s in drift["skipped"]] == ["SCORE_DRIFT"]
    messages = [(rec.levelno, rec.getMessage()) for rec in caplog.records]
    assert any(level == logging.WARNING and "drift alarm ABSTENTION_HIGH" in m for level, m in messages)
    assert any(level == logging.INFO and "drift check skipped SCORE_DRIFT" in m for level, m in messages)
