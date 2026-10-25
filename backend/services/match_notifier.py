"""Strong-match job alert notifier and the cron scoring sweep.

Every score comes from the deterministic fit engine (services/fit_engine.py, DECISIONS D-83):
the sweep calls no LLM of any kind. An alert goes out only for a confident Strong match, an
outcome SCORED in the Strong band (D-85); a result with no number (low confidence, gate fail,
needs review) is never a match (D-86). Dedupes so a user is never alerted twice about the same
job. Used by the recurring cron sweep (``sweep_match_alerts``) and callable directly
(``notify_high_matches``).
"""

import asyncio
import datetime
import logging
import os
import time
from collections import Counter
from typing import Any, Optional

from sqlalchemy import exists, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.models import (
    JobMatchNotification,
    JobMatchScore,
    ScrapedJob,
    User,
    UserSettings,
)
from backend.services.alert_unsubscribe import unsubscribe_url
from backend.services.email_service import clean_company_name, email_service
from backend.services import fit_reads
from backend.services.job_filters import user_experience_levels
from backend.services.listing_freshness import LISTING_ACTIVE
from backend.services.logo_cache import LOGO_PATH_PREFIX, logo_quality

logger = logging.getLogger(__name__)

# Free-tier guard rails (Resend free plan = 100 emails/day, 3,000/month).
# A user gets at most one digest per cooldown window, and we never send more
# than the daily budget across all users, leaving headroom for verification
# emails. Both are env-overridable.
DEFAULT_COOLDOWN_HOURS = 24
DEFAULT_DAILY_BUDGET = 80
# Wall-clock box (seconds) for the sweep's scoring; see sweep_match_alerts.
DEFAULT_SCORING_BUDGET_S = 120
# Jobs per bank_scores call. Each chunk commits on its own, so a run cut short
# keeps what it scored, and a conflict with a concurrent sweep costs one chunk.
SWEEP_CHUNK = 500
# Trace retention (D-153): traces (and their score rows) pruned per sweep at most.
TRACE_PRUNE_LIMIT = 5000
# The drift report's reference window (EVAL section 6: the 30-day baseline) and
# how many of its raw scores it reads at most.
DRIFT_WINDOW_DAYS = 30
DRIFT_REFERENCE_LIMIT = 5000

# Every value scraped_jobs.country takes: services/country_filter.py files each
# ingested row under US or CA, or drops it.
_ALERT_COUNTRIES = frozenset({"US", "CA"})
# Sorts a never-scored account ahead of every scored one.
_NEVER_SCORED = datetime.datetime(1970, 1, 1)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_cooldown_hours() -> int:
    """Min hours between alert emails to the same user (0 disables)."""
    return _env_int("MATCH_NOTIFY_COOLDOWN_HOURS", DEFAULT_COOLDOWN_HOURS)


def get_daily_budget() -> int:
    """Max alert emails to send across all users per UTC day (0 disables sending)."""
    return _env_int("MATCH_NOTIFY_DAILY_BUDGET", DEFAULT_DAILY_BUDGET)


def alerts_enabled() -> bool:
    """Master switch for the recurring sweep.

    The sweep runs on the scrape cron and spends CPU and database writes
    whether or not a single person opens the app, so it needs an off-switch
    that costs nothing but an env var.
    """
    return _env_flag("MATCH_ALERTS_ENABLED", True)


def _now() -> float:
    """Monotonic clock for the sweep's scoring budget (a seam for tests: the
    event loop reads time.monotonic too, so that must never be patched)."""
    return time.monotonic()


def _alert_regions(raw: Optional[str]) -> list[str]:
    """The countries a user's alerts are limited to, from user_settings.regions.

    Onboarding stores the country the user picked ("CA"), the same value that
    seeds the feed's first-load country filter, so alerts honour it the way the
    feed does: before this, 33 of the 85 alerts (39%) sent to Canada-only users
    were US jobs. Empty, or only values no job carries, means no limit: an
    unrecognized setting must never silence a user.
    """
    picked = {r.strip().upper() for r in (raw or "").split(",") if r.strip()}
    return sorted(picked & _ALERT_COUNTRIES)


def _alert_levels(raw: Optional[str]) -> list[str]:
    """The stored experience levels a user's alerts cover, from
    user_settings.experience_levels (which onboarding writes), expanded by
    job_filters.EXPERIENCE_FILTER_MAP. No choice, or none recognized, means
    Intern/New Grad and Entry Level: a student is never alerted about a
    Director role because a setting was blank."""
    return user_experience_levels(raw)


def _level_filter(levels: list[str]):
    """SQL: the job's experience level is one of ``levels``. A row with no
    level stays in: nothing in today's visible feed is untagged and every
    ingest path tags one, so the rule only ever drops rows tagged with a
    level the user did not choose."""
    return or_(
        ScrapedJob.experience_level.in_(levels),
        ScrapedJob.experience_level.is_(None),
        ScrapedJob.experience_level == "",
    )


def _alert_prefs(db: Session, user_id: int) -> tuple[bool, list[str], list[str]]:
    """(alerts on?, region limit, experience levels) for one user. Columns
    only. No settings row, or a NULL flag, is opted in: that has been
    everyone's default."""
    row = (
        db.query(UserSettings.match_alerts_enabled, UserSettings.regions,
                 UserSettings.experience_levels)
        .filter(UserSettings.user_id == user_id)
        .first()
    )
    if row is None:
        return True, [], _alert_levels(None)
    return row[0] is not False, _alert_regions(row[1]), _alert_levels(row[2])


def _alertable() -> tuple:
    """SQL filters for rows worth an alert: exactly what the feed shows. A
    hidden cross-source duplicate or a closed listing would deep-link the
    user to a job they can't apply to (26 of the first 91 alerts pointed at
    rows since hidden as duplicates). A ``stale`` listing is hidden from the
    feed too, until a crawl or a check vouches for it again."""
    return (
        ScrapedJob.duplicate_of.is_(None),
        or_(
            ScrapedJob.listing_status.is_(None),
            ScrapedJob.listing_status == LISTING_ACTIVE,
        ),
        func.trim(func.coalesce(ScrapedJob.company, "")) != "",
        ScrapedJob.company != "Unknown",
    )


def _recently_notified(db: Session, user_id: int, hours: int) -> bool:
    """True if this user was alerted within the cooldown window."""
    if hours <= 0:
        return False
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    row = (
        db.query(JobMatchNotification.id)
        .filter(
            JobMatchNotification.user_id == user_id,
            JobMatchNotification.sent_at >= cutoff,
        )
        .first()
    )
    return row is not None


def _emails_sent_today(db: Session) -> int:
    """Distinct users alerted since UTC midnight.

    With the per-user cooldown (>=24h) each user receives at most one digest a
    day, so distinct-users-today equals emails-sent-today, a cheap, reliable
    proxy for the daily budget without a separate counter table.
    """
    start = datetime.datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (
        db.query(func.count(func.distinct(JobMatchNotification.user_id)))
        .filter(JobMatchNotification.sent_at >= start)
        .scalar()
        or 0
    )


def _frontend_base() -> str:
    return (os.getenv("FRONTEND_URL") or "").rstrip("/")


def _relative_time(when: Optional[datetime.datetime]) -> str:
    """Render a coarse 'N minutes/hours/days ago' string, or '' if unknown."""
    if not when:
        return ""
    try:
        delta = datetime.datetime.utcnow() - when
    except TypeError:
        return ""
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return ""
    minutes = seconds // 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"


def _resolve_logo_url(job: ScrapedJob) -> str:
    """Best company logo URL for the email.

    Priority: our self-hosted logo > a real stored hotlink > the favicon of
    the backend-resolved domain (website URL / name heuristic). Returns ""
    when nothing resolves so the email falls back to a letter avatar.

    A self-hosted logo is a relative '/jobs/logo/<sha>.png' path, served by
    the API behind the app's own origin (the /jobs/* rewrite), so an email
    gets it as an absolute FRONTEND_URL link. SVG ones fall through to the
    favicon: Gmail and Outlook don't render SVG images.
    """
    from backend.services.logo_resolver import logo_url_for_domain, resolve_domain

    stored = (job.company_logo or "").strip()
    if stored.startswith(LOGO_PATH_PREFIX):
        base = _frontend_base()
        if base and stored.lower().endswith(".png"):
            return f"{base}{stored}"
    elif logo_quality(stored) > 0:
        return stored

    domain = (job.company_domain or "").strip()
    if not domain:
        domain = resolve_domain(job.company, job.company_url) or ""
    return logo_url_for_domain(domain) if domain else ""


def _job_to_alert_dict(job: ScrapedJob, score: int) -> dict:
    """Shape a ScrapedJob into the dict the email template expects.

    The 'APPLY NOW' button deep-links into the Tailrd dashboard so users tailor
    and apply with our tools; falls back to the raw job URL if FRONTEND_URL is
    unset.
    """
    base = _frontend_base()
    apply_url = f"{base}/app?job={job.id}" if base else (job.url or "#")
    return {
        "title": job.title or "",
        "company": clean_company_name(job.company),
        "match_score": int(score or 0),
        "location": job.location or "",
        "salary": job.salary_range or "",
        "posted": _relative_time(job.posted_date),
        "apply_url": apply_url,
        "logo_url": _resolve_logo_url(job),
    }


def notify_high_matches(
    db: Session,
    user_id: int,
    scored: list[tuple[ScrapedJob, Optional[int], Optional[str], Optional[str]]],
) -> int:
    """Email a user about their confident Strong matches, deduping prior alerts.

    Args:
        db: Active DB session.
        user_id: Recipient user id.
        scored: (ScrapedJob, score, band, outcome) for this user's current résumé, as banked
            by the fit engine. Only SCORED rows in the Strong band are ever sent (D-85); a
            row with no score is never a match (D-86).

    Returns:
        Number of jobs included in the sent alert (0 if nothing was sent).
    """
    candidates = [(job, int(score)) for job, score, band, outcome in scored
                  if fit_reads.is_strong_match(outcome, band, score)]
    if not candidates:
        return 0

    # Only email verified accounts with a real address.
    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.email or not user.email_verified:
        logger.info(
            "Skipping match alert for user %s (missing/unverified email).", user_id
        )
        return 0

    # The recipient's own choices. Checked here as well as in the sweep's user
    # query because a caller may call this directly, and a user can
    # unsubscribe while a sweep is running.
    enabled, regions, levels = _alert_prefs(db, user_id)
    if not enabled:
        logger.info("Skipping match alert for user %s (opted out).", user_id)
        return 0

    # Free-tier guard: at most one digest per user per cooldown window. Matches
    # found in the meantime stay un-recorded and roll into the next eligible run.
    cooldown = get_cooldown_hours()
    if _recently_notified(db, user_id, cooldown):
        logger.info(
            "Skipping match alert for user %s (within %dh cooldown).",
            user_id, cooldown,
        )
        return 0

    # Free-tier guard: stop once the daily send budget is exhausted.
    budget = get_daily_budget()
    if _emails_sent_today(db) >= budget:
        logger.info(
            "Daily match-alert budget (%d) reached; skipping user %s.",
            budget, user_id,
        )
        return 0

    # Only rows the feed still shows (a caller may hand over whatever it
    # scored, hidden or closed rows included), in the user's region if they
    # picked one and at the experience levels they chose. Ids only, never
    # whole rows.
    job_ids = [job.id for job, _ in candidates]
    region_filter = [ScrapedJob.country.in_(regions)] if regions else []
    alertable = {
        row.id
        for row in db.query(ScrapedJob.id)
        .filter(ScrapedJob.id.in_(job_ids), *_alertable(), *region_filter,
                _level_filter(levels))
        .all()
    }
    candidates = [(job, score) for job, score in candidates if job.id in alertable]
    if not candidates:
        return 0

    # Drop jobs this user was already alerted about.
    job_ids = [job.id for job, _ in candidates]
    already = {
        row.job_id
        for row in db.query(JobMatchNotification.job_id)
        .filter(
            JobMatchNotification.user_id == user_id,
            JobMatchNotification.job_id.in_(job_ids),
        )
        .all()
    }
    fresh = [(job, score) for job, score in candidates if job.id not in already]
    if not fresh:
        return 0

    fresh.sort(key=lambda pair: pair[1], reverse=True)
    payload = [_job_to_alert_dict(job, score) for job, score in fresh]
    recipient_name = (user.first_name or "").strip() or None
    # CASL: every alert carries a working opt-out. It can only be missing where
    # FRONTEND_URL is unset (local dev); prod needs it for the verification
    # email and the APPLY links anyway.
    opt_out = unsubscribe_url(user_id)
    if not opt_out:
        logger.warning(
            "Match alert for user %s has no unsubscribe link: FRONTEND_URL is unset.",
            user_id,
        )

    sent = email_service.send_job_match_alert(
        user.email, payload, recipient_name, unsubscribe_url=opt_out
    )
    if not sent:
        # Leave un-recorded so the next sweep retries (e.g. transient Resend
        # error or email not yet configured).
        return 0

    for job, score in fresh:
        db.add(
            JobMatchNotification(user_id=user_id, job_id=job.id, match_score=score)
        )
    db.commit()
    logger.info("Recorded %d match notifications for user %s.", len(fresh), user_id)
    return len(fresh)


def _engine_hash() -> str:
    """The combined config hash of the engine scores are banked with (builds it if needed)."""
    from backend.services import fit_engine

    return fit_engine.get_engine().combined_hash


def _drift_report(
    db: Session, started_at: datetime.datetime, current_raws: list[float], current_outcomes: dict[str, int]
) -> dict:
    """The EVAL section 6 drift report for this sweep (``matcher.audit.evaluate_drift``).

    Reference: the 30-day baseline, rows banked under the current config before this sweep
    started (at most DRIFT_REFERENCE_LIMIT raw scores, newest first). Current: what this sweep
    scored. Both PSI sample sizes are passed, so below 200 scores on either side the PSI is not
    evaluated and appears in ``skipped`` ("insufficient sample"), never silently dropped. The
    checks the sweep cannot measure (taxonomy coverage, four-fifths, latency) are listed in
    ``not_measured``. Abstention is NEEDS_REVIEW plus SCORED_LOW_CONFIDENCE over all pairs (D-95).
    """
    from backend.services import fit_engine  # noqa: F401  (puts matcher on sys.path, D-01)
    from matcher.audit import evaluate_drift, psi

    config_hash = _engine_hash()
    since = started_at - datetime.timedelta(days=DRIFT_WINDOW_DAYS)
    in_window = (
        JobMatchScore.config_hash == config_hash,
        JobMatchScore.resume_fingerprint.like(fit_reads.FIT_PREFIX + "%"),
        JobMatchScore.scored_at >= since,
        JobMatchScore.scored_at < started_at,
    )
    reference = [
        float(row[0])
        for row in db.query(JobMatchScore.raw_score)
        .filter(*in_window, JobMatchScore.raw_score.isnot(None))
        .order_by(JobMatchScore.scored_at.desc(), JobMatchScore.id.desc())
        .limit(DRIFT_REFERENCE_LIMIT)
        .all()
    ]
    baseline_counts = dict(
        db.query(JobMatchScore.outcome, func.count(JobMatchScore.id))
        .filter(*in_window)
        .group_by(JobMatchScore.outcome)
        .all()
    )
    abstaining = (fit_reads.OUTCOME_NEEDS_REVIEW, fit_reads.OUTCOME_LOW_CONFIDENCE)

    def rate(counts: dict) -> Optional[float]:
        total = sum(int(n) for n in counts.values())
        return sum(int(counts.get(o, 0)) for o in abstaining) / total if total else None

    current = [float(raw) for raw in current_raws]
    # psi() refuses an empty sample; an empty side is below the minimum anyway, so the
    # placeholder 0.0 is never evaluated, only reported as skipped.
    psi_value = psi(reference, current) if reference and current else 0.0
    abstention_rate, abstention_baseline = rate(current_outcomes), rate(baseline_counts)
    report = evaluate_drift(
        coverage=None,
        abstention_rate=abstention_rate,
        abstention_baseline=abstention_baseline,
        psi_value=psi_value,
        min_fairness_ratio=None,
        p95_latency_ms=None,
        psi_reference_n=len(reference),
        psi_current_n=len(current),
    )
    not_measured = ["coverage", "four_fifths", "p95_latency"]
    if abstention_rate is None:
        not_measured.append("abstention")
    return {
        "alarms": [alarm.model_dump() for alarm in report.alarms],
        "skipped": [check.model_dump() for check in report.skipped],
        "psi_reference_n": len(reference),
        "psi_current_n": len(current),
        "abstention_rate": abstention_rate,
        "abstention_baseline": abstention_baseline,
        "not_measured": not_measured,
    }


def _strong_matches(
    db: Session, user_id: int, profile, window_ids: list[int]
) -> list[tuple[ScrapedJob, Optional[int], Optional[str], Optional[str]]]:
    """This user's confident Strong matches in the window, current for their résumé."""
    fingerprints = fit_reads.serving_fingerprints(db, user_id, profile)
    if not fingerprints or not window_ids:
        return []
    rows = (
        db.query(ScrapedJob, JobMatchScore.score, JobMatchScore.band, JobMatchScore.outcome)
        .join(JobMatchScore, JobMatchScore.job_id == ScrapedJob.id)
        .filter(
            JobMatchScore.user_id == user_id,
            JobMatchScore.job_id.in_(window_ids),
            JobMatchScore.resume_fingerprint.in_(fingerprints),
            fit_reads.strong_match(),
        )
        .all()
    )
    return [(job, score, band, outcome) for job, score, band, outcome in rows]


class ShadowTally:
    """What the shadow pass found (EVAL section 6, D-150): live and shadow side by side.

    ``add(live, shadow)`` takes the two results of one job. ``summary()`` gives the outcome and
    band histograms of each side, the display-score delta (shadow minus live, over jobs SCORED on
    both sides: n, mean, p50), band agreement (over jobs with a band on both sides) and the jobs
    that cross the alert bar (a SCORED Strong match) in either direction."""

    def __init__(self, config_hash: Optional[str]) -> None:
        self.config_hash = config_hash
        self.error: Optional[str] = None
        self.jobs = self.errors = self.skipped_deadline = 0
        self.outcomes: dict[str, Counter] = {"live": Counter(), "shadow": Counter()}
        self.bands: dict[str, Counter] = {"live": Counter(), "shadow": Counter()}
        self.deltas: list[float] = []
        self.band_pairs = self.band_agree = 0
        self.into_strong = self.out_of_strong = 0

    @staticmethod
    def _band(result) -> Optional[str]:
        band = getattr(result, "band", None)
        return None if band is None else band.value

    @classmethod
    def _strong(cls, result) -> bool:
        """The alert bar itself (``fit_reads.is_strong_match``, the twin of the SQL filter alerts use)."""
        return fit_reads.is_strong_match(result.outcome.value, cls._band(result),
                                         getattr(result, "display_score", None))

    def add(self, live, shadow) -> None:
        self.jobs += 1
        for side, result in (("live", live), ("shadow", shadow)):
            self.outcomes[side][result.outcome.value] += 1
            band = self._band(result)
            if band is not None:
                self.bands[side][band] += 1
        scored = fit_reads.OUTCOME_SCORED
        if (live.outcome.value == scored and shadow.outcome.value == scored
                and live.display_score is not None and shadow.display_score is not None):
            self.deltas.append(float(shadow.display_score) - float(live.display_score))
        live_band, shadow_band = self._band(live), self._band(shadow)
        if live_band is not None and shadow_band is not None:
            self.band_pairs += 1
            self.band_agree += int(live_band == shadow_band)
        live_strong, shadow_strong = self._strong(live), self._strong(shadow)
        self.into_strong += int(shadow_strong and not live_strong)
        self.out_of_strong += int(live_strong and not shadow_strong)

    def summary(self) -> dict[str, Any]:
        deltas = sorted(self.deltas)
        mean: Optional[float] = None
        p50: Optional[float] = None
        if deltas:
            middle = len(deltas) // 2
            p50 = deltas[middle] if len(deltas) % 2 else (deltas[middle - 1] + deltas[middle]) / 2
            mean = sum(deltas) / len(deltas)
        return {
            "status": "error" if self.error else "ok",
            "error": self.error,
            "config_hash": self.config_hash,
            "postings": "live parse",
            "jobs": self.jobs,
            "outcomes": {side: dict(counts) for side, counts in self.outcomes.items()},
            "bands": {side: dict(counts) for side, counts in self.bands.items()},
            "score_delta": {"n": len(deltas), "mean": mean, "p50": p50},
            "band_agreement": {"n": self.band_pairs, "agree": self.band_agree,
                               "rate": self.band_agree / self.band_pairs if self.band_pairs else None},
            "crossed_strong": {"into": self.into_strong, "out_of": self.out_of_strong,
                               "total": self.into_strong + self.out_of_strong},
            "errors": self.errors,
            "skipped_deadline": self.skipped_deadline,
        }


def _shadow_pass(db: Session, user_id: int, profile, results: dict, postings: dict, shadow_dir,
                 shadow_engine, tally: ShadowTally, started: float, budget_s: int):
    """Score one live chunk again with the shadow config and tally it (D-150). Returns the shadow
    engine (built here on first use). Never raises: a config that fails to load sets
    ``tally.error`` (no more shadow passes this sweep); any other fault counts as an error."""
    from backend.services import fit_engine

    if budget_s > 0 and _now() - started >= budget_s:
        tally.skipped_deadline += len(results)
        return shadow_engine
    if shadow_engine is None:
        try:
            shadow_engine = fit_engine.build_shadow_engine(shadow_dir)
        except Exception as exc:
            tally.error = f"{type(exc).__name__}: {exc}"[:500]
            logger.error("match-alert sweep: the shadow config could not be loaded: %s", tally.error)
            return None
        tally.config_hash = shadow_engine.combined_hash
    wanted = {job_id: postings[job_id] for job_id in results if job_id in postings}
    try:
        shadow_results = fit_engine.shadow_scores(db, user_id, profile, wanted, shadow=shadow_engine)
    except Exception as exc:
        db.rollback()  # the live chunk is already committed; the shadow pass holds nothing
        tally.errors += 1
        logger.error("match-alert sweep: shadow scoring a chunk failed: %s", type(exc).__name__)
        return shadow_engine
    for job_id, shadow_result in shadow_results.items():
        if job_id in results:
            tally.add(results[job_id], shadow_result)
    return shadow_engine


def _prune_traces(db: Session) -> dict[str, Any]:
    """One bounded retention pass (D-153) for the sweep summary: ``traces_pruned``, plus
    ``traces_prune_unmatched`` (traces kept because their score row was not found, logged as a
    warning: a changed FIT_TRACE_SALT looks like this), ``traces_prune_skipped`` or
    ``traces_prune_error``. Never raises; a failure is rolled back and retried next run."""
    from backend.services import fit_engine

    try:
        result = fit_engine.prune_traces(db, limit=TRACE_PRUNE_LIMIT)
    except fit_engine.FitTraceSaltError:
        db.rollback()
        return {"traces_pruned": 0, "traces_prune_skipped": "fit_trace_salt_missing"}
    except Exception as exc:
        db.rollback()
        logger.error("match-alert sweep: trace pruning failed: %s", type(exc).__name__)
        return {"traces_pruned": 0, "traces_prune_error": type(exc).__name__}
    summary: dict[str, Any] = {"traces_pruned": result.pruned}
    if result.unmatched:
        summary["traces_prune_unmatched"] = result.unmatched
        logger.warning("match-alert sweep: %d closed-job traces kept because their score row was not "
                       "found (was FIT_TRACE_SALT changed?)", result.unmatched)
    return summary


async def sweep_match_alerts(
    db: Session,
    max_users: Optional[int] = None,
    jobs_per_user: Optional[int] = None,
) -> dict:
    """Score recent jobs for each verified user with the fit engine and email new Strong matches.

    Shared by the standalone cron endpoint and the github-sources cron-poll run (so the whole
    product fits inside Vercel's 2-cron Hobby limit). No LLM is called, waited on or paid for.

    Per user, the window's jobs (the newest CRON_MATCH_JOBS_PER_USER the feed shows) go to
    ``fit_engine.bank_scores`` in chunks of at most SWEEP_CHUNK; each chunk commits on its own.
    A chunk that collides with a concurrent sweep for the same user (IntegrityError) is rolled
    back, logged and skipped; the next run scores it. A missing or short FIT_TRACE_SALT stops
    scoring for the rest of the sweep (``fit_trace_salt_missing``: no score is banked without
    its trace, and there is no fallback scorer). CRON_MATCH_BUDGET_S boxes the scoring; between
    chunks the sweep yields to the event loop, so the cron-poll wall clock can cut it there.
    Alerts are read from the banked rows: SCORED Strong matches only. Then, once per sweep,
    the calibration references are refreshed and the drift report is computed.

    Users are capped per run (CRON_MATCH_MAX_USERS); any truncation is logged.

    Trace retention (D-153): after scoring, while the deadline has time left, the sweep deletes
    at most TRACE_PRUNE_LIMIT traces (each with its score row) of closed jobs scored more than 90
    days ago (``fit_engine.prune_traces``, its own transaction), reported as ``traces_pruned``. It
    runs when alerts are disabled too.

    Shadow scoring (EVAL section 6, D-150): when FIT_SHADOW_CONFIG names a config directory, each
    scored chunk is scored again with that config, in memory (``fit_engine.shadow_scores``), and
    ``summary["shadow"]`` reports the difference (``ShadowTally``). Only the live result is
    persisted. The shadow pass runs inside the same scoring deadline (it is skipped, and counted,
    once the deadline has passed). A shadow config that fails to load, or a shadow fault, is
    reported in the summary and never stops the live sweep.
    """
    from backend.db.models import ResumeProfileDB
    from backend.services import fit_engine

    if not alerts_enabled():
        # Retention does not depend on alerts (review M-3): turning alerts off during an email
        # incident must not let trace storage grow again.
        logger.info("Match-alert sweep disabled (MATCH_ALERTS_ENABLED); skipping scoring and alerts.")
        return {
            "status": "disabled",
            "users_scanned": 0,
            "users_notified": 0,
            "jobs_notified": 0,
            "jobs_scored": 0,
            "scoring_errors": 0,
            **_prune_traces(db),
        }

    if max_users is None:
        max_users = _env_int("CRON_MATCH_MAX_USERS", 25)
    if jobs_per_user is None:
        # 2,000 covers the whole live catalogue (~5k visible, newest first) in a few runs;
        # steady state is only the new arrivals, since current scores are skipped and each
        # posting is parsed once per parser version (D-84).
        jobs_per_user = _env_int("CRON_MATCH_JOBS_PER_USER", 2000)
    # Wall-clock box for scoring. cron-poll runs this sweep last, after polling and
    # enrichment, under Vercel's 300 s ceiling. Checked before each chunk, so the sweep
    # overruns it by at most one chunk. Unscored jobs are simply scored next run. 0 disables.
    scoring_budget_s = _env_int("CRON_MATCH_BUDGET_S", DEFAULT_SCORING_BUDGET_S)
    started = _now()
    started_at = datetime.datetime.utcnow()

    budget = get_daily_budget()
    cooldown = get_cooldown_hours()

    # Everyone the sweep can do anything for, filtered in SQL BEFORE the cap:
    # verified, holding a resume, not opted out, not in cooldown. The cap used
    # to take the first 25 verified users by id and drop the resume-less ones
    # afterwards, so they held slots, and with no rotation account 26 onward
    # would never have been swept at all.
    cooldown_filter = []
    if cooldown > 0:
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=cooldown)
        cooldown_filter.append(
            ~exists().where(
                JobMatchNotification.user_id == User.id,
                JobMatchNotification.sent_at >= cutoff,
            )
        )
    eligible = (
        User.email_verified == True,  # noqa: E712
        exists().where(
            ResumeProfileDB.user_id == User.id,
            ResumeProfileDB.raw_text != None,  # noqa: E711
            ResumeProfileDB.raw_text != "",
        ),
        # Outer-joined below: no settings row, or a NULL flag, is opted in.
        or_(
            UserSettings.match_alerts_enabled == None,  # noqa: E711
            UserSettings.match_alerts_enabled == True,  # noqa: E712
        ),
        *cooldown_filter,
    )
    # Fair rotation under the cap: least recently scored first (never-scored
    # accounts ahead of everyone), id as the tiebreak. A run that banks a user a
    # score sends them to the back of the next run's queue.
    last_scored = (
        db.query(func.max(JobMatchScore.scored_at))
        .filter(JobMatchScore.user_id == User.id)
        .correlate(User)
        .scalar_subquery()
    )
    users = (
        db.query(User, UserSettings.regions, UserSettings.experience_levels)
        .outerjoin(UserSettings, UserSettings.user_id == User.id)
        .filter(*eligible)
        .order_by(func.coalesce(last_scored, _NEVER_SCORED).asc(), User.id.asc())
        .limit(max_users)
        .all()
    )
    if max_users and len(users) >= max_users:
        total_eligible = (
            db.query(func.count(User.id))
            .outerjoin(UserSettings, UserSettings.user_id == User.id)
            .filter(*eligible)
            .scalar()
        ) or 0
        if total_eligible > max_users:
            logger.info(
                "match-alert sweep: processing %d of %d eligible users this run "
                "(capped; least recently scored first).",
                max_users, total_eligible,
            )

    users_scanned = users_notified = jobs_notified = jobs_scored = 0
    scoring_errors = chunks_skipped = 0
    budget_spent = False
    salt_missing = False
    engine_failed: Optional[str] = None
    current_raws: list[float] = []
    current_outcomes: Counter = Counter()
    shadow_dir = fit_engine.shadow_config_dir()
    shadow_tally: Optional[ShadowTally] = ShadowTally(config_hash=None) if shadow_dir else None
    shadow_engine = None

    for user, regions_raw, levels_raw in users:
        # Stop once the day's email budget is gone.
        if _emails_sent_today(db) >= budget:
            logger.info("Daily match-alert budget (%d) reached; ending sweep early.", budget)
            break

        profile = (
            db.query(ResumeProfileDB)
            .filter(
                ResumeProfileDB.user_id == user.id,
                ResumeProfileDB.raw_text != None,  # noqa: E711
                ResumeProfileDB.raw_text != "",
            )
            .order_by(ResumeProfileDB.created_at.desc())
            .first()
        )
        if not profile or not profile.raw_text:
            continue

        # Cooldown and opt-out were applied in the users query, before any scoring.
        users_scanned += 1
        regions = _alert_regions(regions_raw)
        notified_subq = (
            db.query(JobMatchNotification.job_id)
            .filter(JobMatchNotification.user_id == user.id)
            .subquery()
        )
        # The scoring window: the newest N jobs the feed shows that this user hasn't been
        # alerted about, in their region if they picked one and at the experience levels
        # they chose (Intern/New Grad and Entry Level when they chose none). Ids only, and
        # anchored to "newest N": filtering by score state before the LIMIT would make each
        # run dig further into the backlog.
        region_filter = [ScrapedJob.country.in_(regions)] if regions else []
        window_ids = [
            row[0]
            for row in db.query(ScrapedJob.id)
            .filter(
                *_alertable(),
                *region_filter,
                _level_filter(_alert_levels(levels_raw)),
                ScrapedJob.description != "",
                ScrapedJob.description != None,  # noqa: E711
                func.length(ScrapedJob.description) > 50,
                ~ScrapedJob.id.in_(db.query(notified_subq.c.job_id)),
            )
            .order_by(ScrapedJob.id.desc())
            .limit(jobs_per_user)
            .all()
        ]

        for start in range(0, len(window_ids), SWEEP_CHUNK):
            if salt_missing or engine_failed or budget_spent:
                break
            if scoring_budget_s > 0 and _now() - started >= scoring_budget_s:
                budget_spent = True
                logger.warning("match-alert sweep: %ds scoring budget spent; no more scoring this run.",
                               scoring_budget_s)
                break
            chunk = window_ids[start:start + SWEEP_CHUNK]
            shadow_on = shadow_tally is not None and shadow_tally.error is None
            used_postings: dict = {}
            try:
                if shadow_on:
                    results = fit_engine.bank_scores(db, user.id, profile, chunk, postings_out=used_postings)
                else:
                    results = fit_engine.bank_scores(db, user.id, profile, chunk)
            except fit_engine.FitTraceSaltError:
                db.rollback()
                salt_missing = True
                logger.error("match-alert sweep: FIT_TRACE_SALT is missing or shorter than 16 bytes; "
                             "no score can be banked without its trace, so scoring stops for this sweep.")
                break
            except IntegrityError as exc:
                # A concurrent sweep (or upload) banked the same (user, job) first.
                db.rollback()
                chunks_skipped += 1
                logger.warning("match-alert sweep: %s on a chunk of %d jobs for user %s; skipped, "
                               "the next run scores it.", type(exc).__name__, len(chunk), user.id)
                continue
            except fit_engine.FitEngineError as exc:
                db.rollback()
                engine_failed = type(exc).__name__
                logger.error("match-alert sweep: the fit engine could not be built: %s", exc)
                break
            except Exception as exc:
                db.rollback()
                scoring_errors += 1
                logger.error("match-alert sweep: scoring a chunk for user %s failed: %s",
                             user.id, type(exc).__name__)
                continue
            jobs_scored += len(results)
            for result in results.values():
                current_outcomes[result.outcome.value] += 1
                if result.raw_score is not None:
                    current_raws.append(float(result.raw_score))
            if shadow_on and results:
                shadow_engine = _shadow_pass(db, user.id, profile, results, used_postings, shadow_dir,
                                             shadow_engine, shadow_tally, started, scoring_budget_s)
            # Yield between chunks: the cron-poll wall clock can only cut the sweep at an await.
            await asyncio.sleep(0)

        sent = notify_high_matches(db, user.id, _strong_matches(db, user.id, profile, window_ids))
        if sent:
            users_notified += 1
            jobs_notified += sent

    # Trace retention (D-153), only while the sweep's deadline has time left.
    if scoring_budget_s > 0 and _now() - started >= scoring_budget_s:
        prune_summary: dict[str, Any] = {"traces_pruned": 0, "traces_prune_skipped": "deadline"}
    elif salt_missing:
        prune_summary = {"traces_pruned": 0, "traces_prune_skipped": "fit_trace_salt_missing"}
    else:
        prune_summary = _prune_traces(db)
        await asyncio.sleep(0)

    if salt_missing:
        status = "fit_trace_salt_missing"
    elif engine_failed:
        status = "engine_unavailable"
    else:
        status = "completed"
    summary: dict[str, Any] = {
        "status": status,
        "scoring": "fit-engine",
        "alert_band": fit_reads.BAND_STRONG,
        "users_scanned": users_scanned,
        "users_notified": users_notified,
        "jobs_notified": jobs_notified,
        "jobs_scored": jobs_scored,
        "scoring_errors": scoring_errors,
        "chunks_skipped": chunks_skipped,
        "scoring_budget_spent": budget_spent,
        "fit_trace_salt_missing": salt_missing,
        **prune_summary,
    }
    if engine_failed:
        summary["error"] = engine_failed
    if shadow_tally is not None:
        summary["shadow"] = shadow_tally.summary()
        logger.info("match-alert sweep shadow: %s", summary["shadow"])

    # Once per sweep: the live calibration references, then the drift report. A sweep
    # that could not score anything (no salt) skips both rather than build the engine
    # (about 10 s of the cron's 300) for nothing, and says so.
    if salt_missing and jobs_scored == 0:
        summary["calibration"] = summary["drift"] = "skipped: no scores"
    elif users_scanned and not engine_failed:
        try:
            summary["calibration"] = fit_engine.refresh_calibration(db)
        except Exception as exc:
            db.rollback()
            logger.error("match-alert sweep: calibration refresh failed: %s", type(exc).__name__)
            summary["calibration"] = {"error": type(exc).__name__}
        try:
            summary["drift"] = _drift_report(db, started_at, current_raws, dict(current_outcomes))
        except Exception as exc:
            db.rollback()
            logger.error("match-alert sweep: drift report failed: %s", type(exc).__name__)
            summary["drift"] = {"error": type(exc).__name__}
        drift = summary["drift"] if isinstance(summary.get("drift"), dict) else {}
        for alarm in drift.get("alarms", []):
            logger.warning("match-alert sweep drift alarm %s: %s", alarm["code"], alarm["message"])
        for check in drift.get("skipped", []):
            logger.info("match-alert sweep drift check skipped %s: %s", check["code"], check["message"])

    logger.info("match-alert sweep: %s", summary)
    return summary
