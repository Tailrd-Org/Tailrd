"""Per-call token accounting for every OpenAI request.

Until this existed the app could not answer "what does one résumé rewrite cost?"
from its own logs, ``_generate`` read ``choices`` off the response and dropped
``usage`` on the floor. Every call now emits one structured line:

    llm_cost op=fill.batch.short model=gpt-4o-mini in=1512 cached=1024 out=585 usd=0.000579 user=412

which is greppable in Vercel logs and can be piped somewhere durable later
without touching call sites again.

The user id rides a contextvar rather than a parameter: attribution is set once
by the request guard, so a service three layers down does not need to know who
it is working for.
"""

import logging
import os
import sys
from contextvars import ContextVar

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Make the app's own INFO logs actually reach stdout.

    Neither uvicorn nor Vercel configures the root logger, uvicorn's default
    dictConfig names only its own three loggers, so an unconfigured
    ``logger.info`` from application code is dropped at the default WARNING
    threshold. Cost lines that nobody can read are worse than no cost lines, so
    the ``backend`` namespace gets its own handler here.

    Scoped to ``backend`` rather than root on purpose: turning on root at INFO
    also turns on httpx, sqlalchemy, and every other library, which buries the
    signal it was meant to surface. Idempotent, so repeated calls (serverless
    cold starts, test collection) don't stack duplicate handlers.

    The level is the part that actually matters, a record below the effective
    threshold is never created, so no downstream handler can rescue it. The
    stdout handler is only installed when nothing upstream would print the
    record anyway; when root is already configured (pytest's caplog, or a host
    that sets up its own logging) propagation delivers it and adding our own
    would duplicate every line.
    """
    app_logger = logging.getLogger("backend")
    app_logger.setLevel(os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO")
    already_ours = any(getattr(h, "_tailrd_stdout", False) for h in app_logger.handlers)
    if not already_ours and not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s:%(name)s:%(message)s"))
        handler._tailrd_stdout = True  # type: ignore[attr-defined]
        app_logger.addHandler(handler)

# USD per 1,000,000 tokens: (input, cached input, output).
#
# Keep this table current: it is the single place the app's cost math lives,
# and a stale row silently misreports the unit economics rather than failing.
# Last checked against OpenAI list pricing: 2026-10-24 (short-context rates).
MODEL_PRICES: dict[str, tuple[float, float, float]] = {
    "gpt-6-luna":   (0.20, 0.02,   1.00),
    "gpt-4o":       (2.50, 1.25,  10.00),
    "gpt-4o-mini":  (0.15, 0.075,  0.60),
    "gpt-4.1":      (2.00, 0.50,   8.00),
    "gpt-4.1-mini": (0.40, 0.10,   1.60),
    "gpt-4.1-nano": (0.10, 0.025,  0.40),
}

# Unknown models are priced at the flagship rate rather than zero: an
# unrecognised model should overstate spend and get noticed, not vanish.
_FALLBACK = MODEL_PRICES["gpt-4o"]

_current_user: ContextVar[int | None] = ContextVar("llm_cost_user", default=None)


def set_cost_user(user_id: int | None) -> None:
    """Attribute subsequent LLM calls on this request to ``user_id``."""
    _current_user.set(user_id)


def price_of(model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float:
    """USD for one call. ``cached_tokens`` is the discounted slice of the input."""
    rate_in, rate_cached, rate_out = MODEL_PRICES.get(model, _FALLBACK)
    fresh = max(0, prompt_tokens - cached_tokens)
    return (fresh * rate_in + cached_tokens * rate_cached + completion_tokens * rate_out) / 1_000_000


def record(op: str, model: str, usage: dict | None) -> float:
    """Log one call's token spend. Returns the USD figure (0.0 if unusable).

    Never raises: a malformed ``usage`` block must not fail a request that has
    already produced a good answer for the user.
    """
    if os.getenv("LLM_COST_LOGGING", "true").strip().lower() in ("0", "false", "no"):
        return 0.0
    if not isinstance(usage, dict):
        return 0.0
    try:
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens", 0) or 0) if isinstance(details, dict) else 0
        usd = price_of(model, prompt_tokens, completion_tokens, cached)
        logger.info(
            "llm_cost op=%s model=%s in=%d cached=%d out=%d usd=%.6f user=%s",
            op, model, prompt_tokens, cached, completion_tokens, usd,
            _current_user.get() if _current_user.get() is not None else "-",
        )
        return usd
    except Exception as e:  # noqa: BLE001
        logger.debug("llm_cost accounting failed for op=%s: %s", op, e)
        return 0.0
