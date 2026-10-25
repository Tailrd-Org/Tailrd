"""
HTTP for every v2 source: one identity, per-host pacing, retries, robots.txt.

The spec's scraping conduct, in one place so no adapter can forget a part:

- Every request names us (``USER_AGENT``, with a contact URL), so a site that
  objects knows whom to tell. It is set on each request, not only on clients
  from ``make_client``, because callers such as cron-ats pass their own client.
- A host gets at most one request per ``rate_limit`` seconds from this
  process, however many boards are crawled at once: the pacing is keyed by
  host and shared by every client and adapter.
- A 429, a 5xx or a timeout is retried after an exponentially growing pause
  with jitter, so concurrent crawls don't retry in lockstep, or after the
  host's own Retry-After when it sends one. The pause holds back every
  request to that host, not only the retry, and holds even when the request
  gives up: a host that says it is overloaded is overloaded for all of them.
- robots.txt is read once per host per process.

Nothing here logs in, solves or dodges a bot check, or drives a browser: a
source that blocks us is disabled and recorded, never worked around.

Tests replace ``_sleep`` (and ``_now``) to skip or record waits; pacing sleeps
once per request, so a sleep that returns at once never spins. They reset
``_next_slot`` and ``_robots`` to start from a clean process.
"""

from __future__ import annotations

import asyncio
import datetime
import email.utils
import logging
import random
import time
import urllib.robotparser
from typing import Optional
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "TailrdBot/2.0 (+https://www.tailrd.ca)"
TIMEOUT_SECONDS = 30.0

# The first retry waits about BACKOFF_BASE seconds and each later one twice
# the last, up to BACKOFF_CAP; jitter puts each pause between half and all of
# that.
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
# A Retry-After longer than this is not waited out inside one request: the
# request fails at once, so a crawl moves on instead of stalling and the
# board's health record says why.
MAX_RETRY_AFTER = 60.0

# host -> time.monotonic() before which no request to it may start.
_next_slot: dict[str, float] = {}
# One lock per host (and per host's robots.txt). asyncio locks bind to the
# loop that first waits on them, so the table starts over when the loop changes.
_locks: dict[str, asyncio.Lock] = {}
_locks_loop: Optional[asyncio.AbstractEventLoop] = None
# origin -> parsed robots.txt, or None when it could not be fetched (allow).
_robots: dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}


def _now() -> float:
    return time.monotonic()


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def make_client(**kwargs) -> httpx.AsyncClient:
    """An httpx.AsyncClient that identifies as USER_AGENT, follows redirects
    and gives a request 30 seconds. Keyword args go to httpx.AsyncClient and
    win (tests pass ``transport=``), except the user agent: caller headers are
    kept, the User-Agent is always ours."""
    headers = httpx.Headers(kwargs.pop("headers", None))
    headers["User-Agent"] = USER_AGENT
    options = {"timeout": TIMEOUT_SECONDS, "follow_redirects": True}
    options.update(kwargs)
    return httpx.AsyncClient(headers=headers, **options)


async def request(client: httpx.AsyncClient, method: str, url, *, rate_limit: float = 1.0,
                  retries: int = 3, **kwargs) -> httpx.Response:
    """Send one request the polite way and return the response.

    ``rate_limit`` is the seconds between requests to the URL's host (an
    adapter passes its RATE_LIMIT). A 429, a 5xx or a timeout is retried up to
    ``retries`` more times; any other status (2xx, 3xx, a 404) comes back as it
    is, since only the caller knows what it means. When the last try still
    fails, or the host asks for a Retry-After longer than MAX_RETRY_AFTER, this
    raises what it got: httpx.HTTPStatusError for a status, the timeout itself
    for a timeout. Other errors (a refused connection, a bad URL) raise at
    once. ``kwargs`` go to ``client.request`` (params, json, headers, ...); the
    User-Agent is always ours.
    """
    host = _host(url)
    headers = httpx.Headers(kwargs.pop("headers", None))
    headers["User-Agent"] = USER_AGENT
    attempt = 0
    while True:
        await _pace(host, rate_limit)
        failure: httpx.Response | httpx.TimeoutException
        try:
            response = await client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            failure, delay = exc, _backoff(attempt)
        else:
            if not _retryable(response.status_code):
                return response
            failure = response
            delay = max(_backoff(attempt), _retry_after(response) or 0.0)
        # The host is held back whether or not this request tries again: the
        # next board on the same API host would only meet the same overload.
        _defer(host, min(delay, MAX_RETRY_AFTER))
        if attempt >= retries or delay > MAX_RETRY_AFTER:
            if isinstance(failure, httpx.Response):
                failure.raise_for_status()
            raise failure
        attempt += 1
        logger.info("Retrying %s %s after %s in %.1f s (retry %d of %d)", method, url,
                    f"HTTP {failure.status_code}" if isinstance(failure, httpx.Response)
                    else type(failure).__name__, delay, attempt, retries)


async def robots_allowed(client: httpx.AsyncClient, url) -> bool:
    """Whether the site's robots.txt lets USER_AGENT fetch ``url``.

    Read once per host per process; concurrent first asks share one fetch. A
    robots.txt that cannot be fetched (an error status, a timeout, a network
    failure) allows: only rules a site actually publishes restrict us."""
    parsed = urlparse(str(url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return True
    origin = f"{parsed.scheme}://{parsed.netloc.lower()}"
    if origin not in _robots:
        async with _lock(f"robots {origin}"):
            if origin not in _robots:
                _robots[origin] = await _read_robots(client, origin)
    rules = _robots[origin]
    return rules is None or rules.can_fetch(USER_AGENT, str(url))


async def _read_robots(client: httpx.AsyncClient,
                       origin: str) -> Optional[urllib.robotparser.RobotFileParser]:
    try:
        response = await request(client, "GET", f"{origin}/robots.txt", retries=1)
    except Exception as exc:  # whatever stopped the fetch, the rule is "allow"
        logger.info("robots.txt unreadable for %s (%s); allowing", origin, exc)
        return None
    if response.status_code != 200:
        return None
    rules = urllib.robotparser.RobotFileParser()
    rules.parse(response.text.splitlines())
    return rules


def _host(url) -> str:
    try:
        return (urlparse(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def _lock(key: str) -> asyncio.Lock:
    global _locks_loop
    loop = asyncio.get_running_loop()
    if loop is not _locks_loop:
        _locks.clear()
        _locks_loop = loop
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


async def _pace(host: str, interval: float) -> None:
    """Wait for the host's next free slot, take it, and book the one after it
    ``interval`` seconds later. A backoff another request set while we waited
    (``_defer``) moves the slot, and is waited out too."""
    if not host:
        return
    async with _lock(host):
        while True:
            slot = _next_slot.get(host, 0.0)
            wait = slot - _now()
            if wait <= 0:
                break
            await _sleep(wait)
            if _next_slot.get(host, 0.0) <= slot:
                break  # nobody deferred the host while we waited
        _next_slot[host] = max(_next_slot.get(host, 0.0), _now() + max(interval, 0.0))


def _defer(host: str, delay: float) -> None:
    """Hold every request to ``host`` back for ``delay`` seconds from now."""
    if host:
        _next_slot[host] = max(_next_slot.get(host, 0.0), _now() + delay)


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= status <= 599


def _backoff(attempt: int) -> float:
    """The pause before retry ``attempt + 1``: exponential, capped, jittered."""
    ceiling = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


def _retry_after(response: httpx.Response) -> Optional[float]:
    """The seconds a Retry-After header asks for (a number or an HTTP date),
    or None without a usable one."""
    value = (response.headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
