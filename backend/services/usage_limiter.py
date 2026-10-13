"""
Database-backed usage limiter.

The legacy ``rate_limiter`` keeps counters in process memory, which is
unreliable on Vercel serverless: each function instance has its own dict, so a
per-IP limit is really "per-IP, per-instance" and barely holds. This limiter
stores counters in Postgres instead, so a limit is enforced consistently no
matter which instance serves the request, and a daily quota actually survives
cold starts.

Two things are enforced on the expensive LLM endpoints:
  * a short per-minute burst limit (cheap DoS guard), and
  * a per-user daily quota (the real cost-abuse protection, one beta user, or
    a leaked token, cannot drain the OpenAI budget).

Design notes:
  * Fixed-window counters. Simple, atomic enough, and good enough for abuse
    control (we are not billing off these numbers).
  * Fails OPEN: if the counter table is unreachable, requests are allowed. A
    limiter outage must never take the whole app down.
  * Can be disabled wholesale with RATE_LIMIT_ENABLED=false (used in tests).
"""

import os
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.db.database import get_db
from backend.db.models import RateCounter
from backend.auth.dependencies import get_verified_user_id
from backend.services.llm_cost import set_cost_user

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.getenv("RATE_LIMIT_ENABLED", "true").strip().lower() not in ("0", "false", "no")


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# Defaults are deliberately generous for a beta and fully env-overridable so we
# can tighten them from Vercel without a redeploy of code.
LLM_PER_MINUTE = _int_env("LLM_PER_MINUTE", 12)
LLM_DAILY_QUOTA = _int_env("LLM_DAILY_QUOTA", 150)
# The tailoring analysis runs the match engine, not an LLM (D-123): free, so it never touches the
# AI quota above, but it is CPU per request, so it has its own cheap per-user buckets.
FIT_ANALYSIS_PER_MINUTE = _int_env("FIT_ANALYSIS_PER_MINUTE", 20)
FIT_ANALYSIS_PER_DAY = _int_env("FIT_ANALYSIS_PER_DAY", 1000)


def client_identity(request: Optional[Request], user_id: Optional[int]) -> str:
    """Prefer the authenticated user; fall back to client IP for anonymous calls."""
    if user_id is not None:
        return f"user:{user_id}"
    ip = "unknown"
    if request is not None:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        elif request.client:
            ip = request.client.host
    return f"ip:{ip}"


def _hit(db: Session, name: str, identity: str, max_requests: int, window_seconds: int) -> Optional[int]:
    """Increment the fixed-window counter for (name, identity).

    Returns ``None`` if the request is within the limit, otherwise the number of
    seconds the caller should wait. Fails OPEN (returns ``None``) on any error.
    """
    now = time.time()
    bucket = int(now // window_seconds)
    key = f"{name}:{identity}:{bucket}"
    window_end = (bucket + 1) * window_seconds

    try:
        row = (
            db.query(RateCounter)
            .filter(RateCounter.bucket_key == key)
            .with_for_update()
            .first()
        )
        if row is None:
            # Opportunistic purge: expired buckets are dead weight and nothing
            # else deletes them. Piggybacking on new-bucket creation keeps the
            # table bounded (~active identities × 2 windows) with no cron.
            db.query(RateCounter).filter(
                RateCounter.expires_at < datetime.now(timezone.utc)
            ).delete(synchronize_session=False)
            row = RateCounter(
                bucket_key=key,
                count=0,
                expires_at=datetime.now(timezone.utc) + timedelta(seconds=window_seconds * 2),
            )
            db.add(row)
            try:
                db.flush()
            except IntegrityError:
                # Another instance created the same bucket concurrently, reload it.
                db.rollback()
                row = (
                    db.query(RateCounter)
                    .filter(RateCounter.bucket_key == key)
                    .with_for_update()
                    .first()
                )
        if row is None:
            return None

        if row.count >= max_requests:
            db.commit()
            return max(int(window_end - now) + 1, 1)

        row.count += 1
        db.commit()
        return None
    except Exception as e:  # pragma: no cover - defensive; never block on limiter failure
        logger.warning("usage limiter error (failing open): %s", e)
        try:
            db.rollback()
        except Exception:
            pass
        return None


def enforce_llm_limits(db: Session, request: Optional[Request], user_id: Optional[int]) -> None:
    """Guard an LLM-backed endpoint: per-minute burst + per-user daily quota.

    Raises HTTP 429 with a ``Retry-After`` header when a limit is exceeded.
    """
    if not _enabled():
        return

    identity = client_identity(request, user_id)

    retry_after = _hit(db, "llm_min", identity, LLM_PER_MINUTE, 60)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="You're sending AI requests too quickly. Please slow down and try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )

    retry_after = _hit(db, "llm_day", identity, LLM_DAILY_QUOTA, 86_400)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="You've reached today's AI usage limit. It resets in 24 hours.",
            headers={"Retry-After": str(retry_after)},
        )


async def llm_guard(
    request: Request,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
) -> int:
    """Drop-in replacement for ``get_verified_user_id`` on LLM endpoints.

    Authenticates + verifies the user (same as before), then enforces the
    per-minute and daily AI limits before the handler runs. Returns the user id
    so handlers that did ``Depends(get_verified_user_id)`` keep working verbatim.

    Also stamps the user onto the cost-logging context, so every LLM call this
    request makes is attributed without threading a user id through the service
    layer.
    """
    enforce_llm_limits(db, request, user_id)
    set_cost_user(user_id)
    return user_id


def fit_analysis_guard(
    request: Request,
    user_id: int = Depends(get_verified_user_id),
    db: Session = Depends(get_db),
) -> int:
    """Drop-in replacement for ``get_verified_user_id`` on the match-engine analysis routes
    (Task 9.3 fix round 1, D-123): per-user minute and day buckets, shared by every analysis route,
    on the same database-backed counters. Never charges the daily AI quota: no AI is called."""
    if not _enabled():
        return user_id
    identity = client_identity(request, user_id)
    for name, limit, window in (
        ("fit_analysis_min", FIT_ANALYSIS_PER_MINUTE, 60),
        ("fit_analysis_day", FIT_ANALYSIS_PER_DAY, 86_400),
    ):
        retry_after = _hit(db, name, identity, limit, window)
        if retry_after is not None:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
                headers={"Retry-After": str(retry_after)},
            )
    return user_id
