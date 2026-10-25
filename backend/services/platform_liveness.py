"""
Platform-aware listing liveness: is this posting still open?

A plain GET can't answer that for most of the catalogue. Workday serves the
same ~6.5 KB app shell (HTTP 200) for live, closed and made-up job ids;
SmartRecruiters' careers links 302 to the company careers home either way;
Ashby and Oracle HCM pages are SPAs. So every known ATS is asked through its
own public API, which answers honestly both ways (a platform a scraper v2
adapter owns, through the adapter's check_alive), and only unknown hosts fall
back to the page itself, where a death signal (404/410, a redirect to an
error page, a "no longer available" message in the VISIBLE text) is trusted
but a bare 200 proves nothing.

Three verdicts:
  - ``dead``: the platform (or an honest page) says the posting is gone
  - ``alive``: the posting is open. ``authoritative`` only when the
    platform's own API/board said so, the one case allowed to revive a row
  - ``unknown``: bot walls (401/403/429/999), network noise, SPA shells,
    anything inconclusive. Never evidence either way.

The one 403 read as death is Workday's CXS JSON ``errorCode: "S22"``, an
API-level "posting unpublished" signal (85/85 agreement with a board search
by requisition id); a bot wall never answers with that JSON.

A result that learned nothing because no answer came back (the host was
skipped, the run's budget for it was spent, the check never got its turn,
or the host rate-limited us) says so in its reason (``DEFERRED_REASONS``),
so callers can leave the row's place in line alone instead of recording a
probe that never happened.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import logging
import re
import socket
import time
from collections import defaultdict
from typing import Mapping, NamedTuple
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from backend.services.description_extractor import BROWSER_HEADERS, workday_cxs_url

logger = logging.getLogger(__name__)

ALIVE = "alive"
DEAD = "dead"
UNKNOWN = "unknown"


class LivenessResult(NamedTuple):
    verdict: str  # 'alive' | 'dead' | 'unknown'
    reason: str  # short machine-ish reason, e.g. 'workday_cxs_S22', 'http_404'
    authoritative: bool = False  # the platform's own API/board said so


JSON_HEADERS = dict(BROWSER_HEADERS, Accept="application/json, text/plain, */*")

# Per request. The per-request wall clock below also bounds slow-drip bodies,
# which a read timeout alone doesn't (it resets on every chunk). It starts
# once the request holds its host's gate: time spent waiting for a turn is
# bounded separately and never reads as the host being slow.
REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_PER_REQUEST_SECONDS = 25.0
# The longest a request waits for its turn at a host's gate (and never past
# the run's deadline). Waiting that long means the check never ran.
_GATE_QUEUE_SECONDS = 30.0
# A backstop around one whole check (at most two requests, each bounded
# above); nothing inside should ever reach it.
_CHECK_SECONDS = 120.0

_PER_HOST_CONCURRENCY = 2
# Consecutive network errors / rate limits before a host is skipped for the
# rest of the run, one hanging career site must not eat the time box.
_HOST_FAILURE_LIMIT = 3

# LinkedIn walls bursts (429 after about ten quick requests per host) but
# lets a paced client through: every *.linkedin.com host shares ONE gate,
# one request at a time with a gap after each, and a run makes at most
# LINKEDIN_RUN_CAP of them. The rows past the cap are deferred, not probed.
_LINKEDIN_GATE = "linkedin.com"
_LINKEDIN_MIN_INTERVAL = 1.5
LINKEDIN_RUN_CAP = 40

_MAX_HTML_BYTES = 600_000  # LinkedIn guest pages run ~350 KB
_MAX_JSON_BYTES = 12_000_000
# An Ashby board embeds every description (OpenAI's runs ~14 MB). A board
# cut off by the cap is never parsed: a missing id would read as closed.
_MAX_ASHBY_BOARD_BYTES = 40_000_000

# Pages answering these are bot walls or rate limits, a real browser usually
# gets through, so they are never evidence of death.
BOT_WALL_STATUSES = (401, 403, 429, 999)
_RATE_LIMIT_STATUSES = (429, 999)
DEAD_HTTP_STATUSES = (404, 410)

# Phrases that appear only on a dead posting, matched against VISIBLE text
# (script/style/noscript/template stripped first, so an SPA bundle's i18n
# strings or a Next.js "This page could not be found" template can't trip
# them). Kept specific so a live page's boilerplate never matches: no bare
# "not found" / "page not found" (live Roblox and Lucid pages carry those),
# and "the job you're looking for" only with a gone-verb after it (career
# footers say "can't find the job you're looking for?").
DEAD_BODY_RE = re.compile(
    r"no longer accepting applications"
    r"|this (?:job|position|posting|role|requisition) is no longer (?:available|active|open)"
    r"|job (?:posting )?(?:is )?no longer (?:available|active|open)"
    # "not available" only when nothing qualifies it: a live posting may say
    # "this position is not available for relocation / to remote candidates".
    r"|this (?:job|position|requisition) (?:is )?(?:no longer|not) (?:available|accepting)"
    r"(?! (?:for|to|in|on|at|as|with|outside|remotely|remote)\b)"
    r"|(?:job|position|posting) (?:has been|has) (?:filled|closed)"
    r"|(?:job|posting) is closed"
    r"|position (?:has been )?(?:filled|closed)"
    r"|the job you(?:'re| are| were)? (?:looking for|requested) (?:is no longer|no longer exists"
    r"|has (?:been )?(?:expired|removed|closed|filled)|could not be found|was not found|does not exist)"
    r"|this posting has (?:closed|expired|been removed)"
    r"|this job has expired"
    r"|(?:job|posting|position) has expired",
    re.IGNORECASE,
)

# Taleo's "Career Section Unavailable ... The system may be under
# maintenance" page is about the whole career section (it answers the same
# for live job ids, the section's own search page and made-up sections), so
# it says nothing about the posting.
_SECTION_UNAVAILABLE_RE = re.compile(r"career section unavailable", re.IGNORECASE)

# Live descriptions say "open until the position is filled"; strip those
# before the dead-phrase scan so they can't read as "position filled".
_BENIGN_FILLED_RE = re.compile(
    r"(?:until|till|once|after|before|when|unless)\s+(?:the\s+|this\s+)?"
    r"(?:position|job|role|posting|vacancy|opening)s?\s+"
    r"(?:is\s+|are\s+|has\s+been\s+|have\s+been\s+|gets\s+|has\s+)?(?:filled|closed)",
    re.IGNORECASE,
)

# LinkedIn's own closed markers: the "no longer accepting" figcaption class,
# and the trk token an expired job's redirect stamps into the nav links.
_LINKEDIN_DEAD_MARKERS = ("expired_jd_redirect", "closed-job__flavor--closed")
# A live guest page carries the apply CTA; a closed one keeps the top card
# but drops the button.
_LINKEDIN_LIVE_MARKERS = ("apply-button", "sign-up-modal__outlet")

# A redirect landing here means the posting was taken down.
_ERROR_URL_RE = re.compile(
    r"[?&](?:error|err)=(?:404|true)\b|[?&]errortype=404\b|/errorpage\b|/404(?:[/?#.]|$)",
    re.IGNORECASE,
)

_INVISIBLE_BLOCK_RE = re.compile(
    r"<(script|style|noscript|template)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL,
)
# A block the byte cap cut off mid-way has no closing tag; drop it to the end.
_UNCLOSED_BLOCK_RE = re.compile(
    r"<(?:script|style|noscript|template)\b.*\Z", re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_SR_PATH_RE = re.compile(
    r"^/([^/]+)/(\d{6,}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
_GH_BOARD_PATH_RE = re.compile(r"^/([^/]+)/jobs/(\d+)")
_LEVER_PATH_RE = re.compile(r"^/([^/]+)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                            re.IGNORECASE)
_ASHBY_PATH_RE = re.compile(r"^/([^/]+)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                            re.IGNORECASE)
_ORACLE_PATH_RE = re.compile(
    r"/hcmUI/CandidateExperience/(?:[^/]+/)*?sites/([^/?#]+)/"
    r"(?:job|jobs/job|jobs/preview|requisitions/preview)/(\d+)",
    re.IGNORECASE,
)
_WORKDAY_SITE_RE = re.compile(
    r"^https?://([^/]+\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Za-z]{2}/)?recruiting/([^/]+)/([^/]+)(/job/.+)$",
)
# A Workday link to the posting's apply step ('/job/<slug>/apply',
# '/job/<slug>/apply/applyManually'): the posting is '/job/<slug>'.
_WORKDAY_APPLY_RE = re.compile(r"(/job/[^?#]+?)/apply(?:/[^?#]*)?(?=$|[?#])")
# Oracle HCM Candidate Experience on the employer's own domain
# (jobs.nokia.com/en/sites/CX_1/job/38158).
_ORACLE_VANITY_PATH_RE = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?sites/([^/?#]+)/job/(\d+)/?$",
                                    re.IGNORECASE)
# The Oracle pod such a page loads its data from.
_ORACLE_POD_RE = re.compile(r"[a-z0-9-]+\.fa(?:\.[a-z0-9-]+)*\.oraclecloud\.com", re.IGNORECASE)
_BAMBOOHR_PATH_RE = re.compile(r"^/careers/(\d+)/?$")
_RECRUITEE_PATH_RE = re.compile(r"^/o/([^/?#]+)/?$")

# Hosts whose job pages sit on a career-site pattern: only for these does a
# redirect to another company's careers home read as "posting gone".
_CAREER_HOST_LABEL_RE = re.compile(r"^(?:careers?|jobs?|apply|join|talent|recruiting|hiring)\b",
                                   re.IGNORECASE)
_CAREER_PLATFORM_HOSTS = (
    "smartrecruiters.com", "bamboohr.com", "jobvite.com", "breezy.hr", "recruitee.com",
    "workable.com", "icims.com", "taleo.net", "successfactors.com", "successfactors.eu",
    "jazzhr.com", "applytojob.com", "teamtailor.com", "pinpointhq.com", "rippling-ats.com",
)
_HOME_SEGMENTS = {"careers", "career", "jobs", "join-us", "joinus", "opportunities",
                  "openings", "search", "job-search", "search-results"}
_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$", re.IGNORECASE)
_MULTI_PART_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.au", "co.jp", "co.in", "com.br",
                        "co.nz", "com.mx", "co.za", "com.sg", "com.cn", "com.hk", "co.kr"}
_JOB_QUERY_KEYS = ("gh_jid", "jobid", "job_id", "job", "jk", "id", "pid", "req", "reqid",
                   "requisitionid", "jobreqid")


# ─── HTTP plumbing ───────────────────────────────────────────────────────────

class UnsafeAddressError(httpx.TransportError):
    """A request (the first hop or any redirect) aimed at a loopback,
    private, link-local, shared (CGNAT) or otherwise non-public address."""


def _is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    """Every address ``host`` resolves to (a seam for tests)."""
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


async def _refuse_internal_hosts(request: httpx.Request) -> None:
    """Request hook: httpx runs it for every hop, redirects included, so a
    posting URL can't bounce the probe into the function's own network. The
    host (an IP literal, or every address its name resolves to) must be
    public. A name that doesn't resolve is left to the connect to fail."""
    host = request.url.host
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            addresses = await _resolve(host, port)
        except OSError:
            return
    if not addresses or not all(_is_public_address(address) for address in addresses):
        raise UnsafeAddressError(f"refusing a non-public address for {host}", request=request)


def make_client(*, ssrf_guard: bool | None = None, **kw) -> httpx.AsyncClient:
    """An AsyncClient set up for liveness probing: browser-like headers,
    redirects followed, bounded timeouts, and every hop refused unless its
    host is a public address. Keyword args override (tests pass
    ``transport=``). The address guard is on by default; a caller-supplied
    transport (a test's MockTransport, which never touches the network)
    turns it off unless ``ssrf_guard=True`` asks for it."""
    guard = ("transport" not in kw) if ssrf_guard is None else ssrf_guard
    options = {
        "headers": dict(BROWSER_HEADERS),
        "follow_redirects": True,
        "timeout": REQUEST_TIMEOUT,
        "limits": httpx.Limits(max_connections=32, max_keepalive_connections=16),
    }
    if guard:
        options["event_hooks"] = {"request": [_refuse_internal_hosts]}
    options.update(kw)
    return httpx.AsyncClient(**options)


class _Page(NamedTuple):
    status: int
    url: str  # final URL after redirects
    text: str
    redirected: bool
    truncated: bool = False  # the body ran past the byte cap


def _gate_key(host: str) -> str:
    """Hosts that share one politeness gate: every *.linkedin.com host is one
    LinkedIn (its rate limit follows the client, not the subdomain)."""
    return _LINKEDIN_GATE if _host_is(host, "linkedin.com") else host


class _RunState:
    """Per-run politeness and memo: at most _PER_HOST_CONCURRENCY requests in
    flight per host (LinkedIn: one, paced, capped per run), hosts that keep
    failing get skipped, an Ashby board is fetched once per org and an
    Oracle vanity host's pod looked up once, however many rows are checked.

    ``single`` marks a one-off check (no shared cache): Ashby is then asked
    about the one posting instead of downloading the org's whole board."""

    def __init__(self, *, linkedin_cap: int | None = LINKEDIN_RUN_CAP, single: bool = False):
        self.gates: dict[str, asyncio.Semaphore] = {}
        self.failures: dict[str, int] = defaultdict(int)
        self.sent: dict[str, int] = defaultdict(int)
        self.last_done: dict[str, float] = {}
        self.caps: dict[str, int] = {} if linkedin_cap is None else {_LINKEDIN_GATE: linkedin_cap}
        self.single = single
        self.deadline: float | None = None
        self.ashby_boards: dict[str, asyncio.Task] = {}
        self.oracle_pods: dict[str, asyncio.Task] = {}

    def gate(self, key: str) -> asyncio.Semaphore:
        if key not in self.gates:
            size = 1 if key == _LINKEDIN_GATE else _PER_HOST_CONCURRENCY
            self.gates[key] = asyncio.Semaphore(size)
        return self.gates[key]

    def queue_seconds(self) -> float:
        """How long a request may wait for its turn: never past the run's
        deadline (a short floor still lets it take a free gate)."""
        if self.deadline is None:
            return _GATE_QUEUE_SECONDS
        return min(_GATE_QUEUE_SECONDS, max(self.deadline - time.monotonic(), 0.05))

    async def pace(self, key: str) -> None:
        if key != _LINKEDIN_GATE or _LINKEDIN_MIN_INTERVAL <= 0:
            return
        last = self.last_done.get(key)
        if last is not None:
            wait = last + _LINKEDIN_MIN_INTERVAL - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)


_STATE_KEY = "__liveness_run_state__"


def run_cache(*, linkedin_cap: int | None = LINKEDIN_RUN_CAP) -> dict:
    """A fresh per-run cache for check_listing(s). ``linkedin_cap=None``
    lifts the per-run LinkedIn budget (a long one-time run); the pacing
    stays."""
    return {_STATE_KEY: _RunState(linkedin_cap=linkedin_cap)}


def _run_state(cache: dict | None) -> _RunState:
    if cache is None:
        return _RunState(single=True)
    state = cache.get(_STATE_KEY)
    if state is None:
        state = cache[_STATE_KEY] = _RunState()
    return state


async def _read_capped(response: httpx.Response, limit: int) -> tuple[str, bool]:
    """(body text, whether the cap cut it off)."""
    chunks: list[bytes] = []
    size = 0
    truncated = False
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            truncated = True
            break
    raw = b"".join(chunks)[:limit]
    try:
        return raw.decode(response.charset_encoding or "utf-8", errors="replace"), truncated
    except LookupError:
        return raw.decode("utf-8", errors="replace"), truncated


_NETWORK_ERROR = LivenessResult(UNKNOWN, "network_error")
_TIMEOUT = LivenessResult(UNKNOWN, "timeout")
_BLOCKED_ADDRESS = LivenessResult(UNKNOWN, "blocked_address")
_HOST_SKIPPED = LivenessResult(UNKNOWN, "host_skipped")
_HOST_BUDGET_SPENT = LivenessResult(UNKNOWN, "host_budget_spent")
_GATE_QUEUE_TIMEOUT = LivenessResult(UNKNOWN, "gate_queue_timeout")

# Results that say nothing about the posting and should leave the row's
# place in line alone: no request went out (the host was skipped after
# repeated failures, the run's budget for it was spent, the check never got
# its turn at the host's gate), or the host rate-limited us. The sweeps
# don't stamp last_probed_at for these, so the row leads the next run.
DEFERRED_REASONS = frozenset({
    "host_skipped", "host_budget_spent", "gate_queue_timeout", "bot_wall_429", "bot_wall_999",
})
# Plus the misses a click-time check doesn't record either: the request went
# out but nothing usable came back.
MISS_REASONS = DEFERRED_REASONS | {"network_error", "timeout", "error"}


def is_deferred(result: LivenessResult | None) -> bool:
    return result is not None and result.verdict == UNKNOWN and result.reason in DEFERRED_REASONS


def is_miss(result: LivenessResult | None) -> bool:
    return result is not None and result.verdict == UNKNOWN and result.reason in MISS_REASONS


async def _fetch(client, url: str, state: _RunState, *, headers: dict,
                 max_bytes: int = _MAX_HTML_BYTES, method: str = "GET",
                 json_body=None) -> _Page | LivenessResult:
    """One polite request (redirects followed, body capped): the page, or
    the LivenessResult for a request that produced none (network error,
    timeout, a non-public address, or one of the DEFERRED_REASONS)."""
    key = _gate_key((urlparse(url).hostname or "").lower())
    gate = state.gate(key)
    try:
        await asyncio.wait_for(gate.acquire(), state.queue_seconds())
    except asyncio.TimeoutError:
        return _GATE_QUEUE_TIMEOUT
    try:
        # checked inside the gate: requests queued behind a failing one see it
        if state.failures[key] >= _HOST_FAILURE_LIMIT:
            return _HOST_SKIPPED
        cap = state.caps.get(key)
        if cap is not None and state.sent[key] >= cap:
            return _HOST_BUDGET_SPENT
        await state.pace(key)
        state.sent[key] += 1
        try:
            page = await asyncio.wait_for(
                _send(client, method, url, headers=headers, max_bytes=max_bytes,
                      json_body=json_body),
                _PER_REQUEST_SECONDS,
            )
        except UnsafeAddressError as exc:
            logger.warning("liveness refused %s: %s", url, exc)
            return _BLOCKED_ADDRESS
        except asyncio.TimeoutError:
            state.failures[key] += 1
            return _TIMEOUT
        except Exception as exc:
            state.failures[key] += 1
            logger.debug("liveness %s failed for %s: %s", method, url, exc)
            return _NETWORK_ERROR
        finally:
            state.last_done[key] = time.monotonic()
    finally:
        gate.release()
    if page.status in _RATE_LIMIT_STATUSES:
        state.failures[key] += 1
    else:
        state.failures[key] = 0
    return page


async def _send(client, method: str, url: str, *, headers: dict, max_bytes: int,
                json_body=None) -> _Page:
    async with client.stream(method, url, headers=headers, json=json_body,
                             follow_redirects=True, timeout=REQUEST_TIMEOUT) as response:
        text, truncated = await _read_capped(response, max_bytes)
        return _Page(response.status_code, str(response.url), text,
                     bool(response.history), truncated)


def _json(page: _Page):
    try:
        return json.loads(page.text)
    except (ValueError, TypeError):
        return None


def visible_text(body: str, limit: int = _MAX_HTML_BYTES) -> str:
    """The text a user would actually see: invisible blocks, comments and
    tags stripped, entities decoded, whitespace collapsed."""
    text = _INVISIBLE_BLOCK_RE.sub(" ", body[:limit])
    text = _UNCLOSED_BLOCK_RE.sub(" ", text)
    text = _COMMENT_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def says_dead(body: str) -> bool:
    """True when the visible text of a page announces the posting is gone."""
    return _visible_says_dead(visible_text(body))


def _visible_says_dead(text: str) -> bool:
    return bool(DEAD_BODY_RE.search(_BENIGN_FILLED_RE.sub(" ", text)))


def _unknown_for_status(status: int) -> LivenessResult:
    if status in BOT_WALL_STATUSES:
        return LivenessResult(UNKNOWN, f"bot_wall_{status}")
    return LivenessResult(UNKNOWN, f"http_{status}")


# ─── Platform checks ─────────────────────────────────────────────────────────

def strip_workday_apply(url: str) -> str:
    """A Workday link to the posting's apply step ('/job/<slug>/apply',
    '/job/<slug>/apply/applyManually') as the posting's own URL; any other
    URL unchanged. The crawl lists '/job/<slug>' and the CXS API only knows
    that path (an '/apply' path answers 422)."""
    host = (urlparse(url or "").hostname or "").lower()
    if not (_host_is(host, "myworkdayjobs.com") or _host_is(host, "myworkdaysite.com")):
        return url
    return _WORKDAY_APPLY_RE.sub(r"\1", url, count=1)


def _workday_api_url(url: str) -> str:
    clean = strip_workday_apply(url.split("#")[0].split("?")[0])
    if clean.startswith("http://"):
        clean = "https://" + clean[len("http://"):]
    api = workday_cxs_url(clean)
    if api:
        return api
    m = _WORKDAY_SITE_RE.match(clean)
    if m:
        return f"https://{m.group(1)}/wday/cxs/{m.group(2)}/{m.group(3)}{m.group(4)}"
    return ""


async def _check_workday(client, api_url: str, state: _RunState) -> LivenessResult:
    """Workday's CXS job endpoint: 200 + jobPostingInfo = open, errorCode S21
    (never existed) / S22 (unpublished) = gone. The public page can't tell."""
    page = await _fetch(client, api_url, state, headers=JSON_HEADERS, max_bytes=_MAX_JSON_BYTES)
    if not isinstance(page, _Page):
        return page
    data = _json(page)
    if page.status == 200:
        if isinstance(data, dict) and data.get("jobPostingInfo"):
            return LivenessResult(ALIVE, "workday_cxs_200", True)
        return LivenessResult(UNKNOWN, "workday_cxs_no_posting")
    code = data.get("errorCode") if isinstance(data, dict) else None
    if code in ("S21", "S22"):
        return LivenessResult(DEAD, f"workday_cxs_{code}", True)
    if page.status in DEAD_HTTP_STATUSES:
        return LivenessResult(DEAD, f"workday_cxs_{page.status}", True)
    return _unknown_for_status(page.status)


async def _check_smartrecruiters(client, company: str, posting_id: str,
                                 state: _RunState) -> LivenessResult:
    """SmartRecruiters' postings API. The careers.* page 302s to the company
    careers home for live and closed postings alike."""
    api = (f"https://api.smartrecruiters.com/v1/companies/{quote(company, safe='')}"
           f"/postings/{posting_id}")
    page = await _fetch(client, api, state, headers=JSON_HEADERS, max_bytes=_MAX_JSON_BYTES)
    if not isinstance(page, _Page):
        return page
    if page.status in DEAD_HTTP_STATUSES:
        return LivenessResult(DEAD, "sr_api_404", True)
    if page.status == 200:
        data = _json(page)
        if not isinstance(data, dict):
            return LivenessResult(UNKNOWN, "sr_api_unreadable")
        if data.get("active") is False:
            return LivenessResult(DEAD, "sr_inactive", True)
        return LivenessResult(ALIVE, "sr_api_200", True)
    return _unknown_for_status(page.status)


async def _check_greenhouse_board(client, token: str, job_id: str,
                                  state: _RunState) -> LivenessResult:
    api = f"https://boards-api.greenhouse.io/v1/boards/{quote(token, safe='')}/jobs/{job_id}"
    page = await _fetch(client, api, state, headers=JSON_HEADERS, max_bytes=_MAX_JSON_BYTES)
    if not isinstance(page, _Page):
        return page
    if page.status in DEAD_HTTP_STATUSES:
        return LivenessResult(DEAD, "gh_api_404", True)
    if page.status == 200:
        if isinstance(_json(page), dict):
            return LivenessResult(ALIVE, "gh_api_200", True)
        return LivenessResult(UNKNOWN, "gh_api_unreadable")
    return _unknown_for_status(page.status)


async def _check_greenhouse_embed(client, job_id: str, state: _RunState) -> LivenessResult:
    """Token-less Greenhouse check for gh_jid links on custom career domains
    (whose own pages are often Cloudflare-walled): the embed form is 404 for a
    closed posting and a 'Job Application for ...' page for an open one. Only
    used when no board token is known: a board can switch its embed off
    (Jane Street's answers 404 for every open posting), and its boards-api
    is the honest answer then."""
    embed = f"https://boards.greenhouse.io/embed/job_app?token={job_id}"
    page = await _fetch(client, embed, state, headers=BROWSER_HEADERS)
    if not isinstance(page, _Page):
        return page
    if page.status in DEAD_HTTP_STATUSES:
        return LivenessResult(DEAD, "gh_embed_404", True)
    if page.status == 200 and "Job Application for" in page.text:
        return LivenessResult(ALIVE, "gh_embed_200", True)
    return _unknown_for_status(page.status)


async def _check_lever(client, api_host: str, company: str, posting_id: str,
                       state: _RunState) -> LivenessResult:
    api = f"https://{api_host}/v0/postings/{quote(company, safe='')}/{posting_id}"
    page = await _fetch(client, api, state, headers=JSON_HEADERS, max_bytes=_MAX_JSON_BYTES)
    if not isinstance(page, _Page):
        return page
    if page.status in DEAD_HTTP_STATUSES:
        return LivenessResult(DEAD, "lever_api_404", True)
    if page.status == 200:
        if isinstance(_json(page), dict):
            return LivenessResult(ALIVE, "lever_api_200", True)
        return LivenessResult(UNKNOWN, "lever_api_unreadable")
    return _unknown_for_status(page.status)


_ASHBY_POSTING_API = "https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting"
_ASHBY_POSTING_QUERY = (
    "query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) "
    "{ jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, "
    "jobPostingId: $jobPostingId) { id isListed } }"
)
_JSON_POST_HEADERS = dict(JSON_HEADERS, **{"Content-Type": "application/json"})


async def _fetch_ashby_board(client, org: str, state: _RunState):
    """The set of job ids one Ashby org's board lists, or the LivenessResult
    that explains why there is none."""
    api = f"https://api.ashbyhq.com/posting-api/job-board/{quote(org, safe='')}"
    page = await _fetch(client, api, state, headers=JSON_HEADERS,
                        max_bytes=_MAX_ASHBY_BOARD_BYTES)
    if not isinstance(page, _Page):
        return page
    if page.truncated:
        logger.warning("ashby board %s is over %d bytes; asking per posting instead",
                       org, _MAX_ASHBY_BOARD_BYTES)
        return LivenessResult(UNKNOWN, "ashby_board_truncated")
    data = _json(page)
    if page.status == 200 and isinstance(data, dict) and isinstance(data.get("jobs"), list):
        return {str(job.get("id", "")).lower() for job in data["jobs"] if isinstance(job, dict)}
    if page.status in DEAD_HTTP_STATUSES:
        return LivenessResult(DEAD, "ashby_board_404", True)
    if page.status == 200:
        return LivenessResult(UNKNOWN, "ashby_board_unreadable")
    return _unknown_for_status(page.status)


async def _check_ashby_posting(client, org: str, job_id: str, state: _RunState) -> LivenessResult:
    """Ashby's per-posting lookup, the public GraphQL its own job pages call:
    an open posting answers with itself, listed on the board or not (a
    posting shared by direct link is open but unlisted), and a closed or
    made-up one answers ``jobPosting: null``."""
    body = {
        "operationName": "ApiJobPosting",
        "variables": {"organizationHostedJobsPageName": org, "jobPostingId": job_id},
        "query": _ASHBY_POSTING_QUERY,
    }
    page = await _fetch(client, _ASHBY_POSTING_API, state, headers=_JSON_POST_HEADERS,
                        method="POST", json_body=body)
    if not isinstance(page, _Page):
        return page
    if page.status != 200:
        return _unknown_for_status(page.status)
    data = _json(page)
    payload = data.get("data") if isinstance(data, dict) else None
    if not isinstance(payload, dict) or "jobPosting" not in payload or data.get("errors"):
        return LivenessResult(UNKNOWN, "ashby_posting_unreadable")
    posting = payload["jobPosting"]
    if posting is None:
        return LivenessResult(DEAD, "ashby_posting_null", True)
    if isinstance(posting, dict) and str(posting.get("id", "")).lower() == job_id.lower():
        if posting.get("isListed") is False:
            return LivenessResult(ALIVE, "ashby_unlisted_open", True)
        return LivenessResult(ALIVE, "ashby_posting_open", True)
    return LivenessResult(UNKNOWN, "ashby_posting_unreadable")


async def _check_ashby(client, org: str, job_id: str, state: _RunState) -> LivenessResult:
    """Ashby job pages are an SPA shell for any id. In a batch run the org's
    board listing (fetched once per org per run) vouches for its listed
    postings; a posting the board doesn't list, a board that can't be read,
    and a one-off check go to the per-posting lookup, since the board omits
    open postings that are unlisted."""
    if state.single and org.lower() not in state.ashby_boards:
        return await _check_ashby_posting(client, org, job_id, state)
    key = org.lower()
    task = state.ashby_boards.get(key)
    if task is None:
        task = asyncio.ensure_future(_fetch_ashby_board(client, org, state))
        state.ashby_boards[key] = task
    # shield: one caller timing out must not cancel the fetch the others share
    board = await asyncio.shield(task)
    if isinstance(board, set) and job_id.lower() in board:
        return LivenessResult(ALIVE, "ashby_listed", True)
    if isinstance(board, LivenessResult) and board.verdict == DEAD:
        return board  # the org's whole board is gone
    return await _check_ashby_posting(client, org, job_id, state)


async def _check_oracle(client, host: str, site: str, job_id: str,
                        state: _RunState) -> LivenessResult:
    """Oracle HCM Candidate Experience is an SPA; its requisition REST finder
    returns the posting while open and an empty item list once it's gone."""
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
           f"?expand=all&onlyData=true&finder=ById;Id=%22{job_id}%22,siteNumber={quote(site, safe='')}")
    page = await _fetch(client, api, state, headers=JSON_HEADERS, max_bytes=_MAX_JSON_BYTES)
    if not isinstance(page, _Page):
        return page
    data = _json(page)
    if page.status == 200:
        if not (isinstance(data, dict) and isinstance(data.get("items"), list)):
            return LivenessResult(UNKNOWN, "oracle_api_unreadable")
        if data["items"]:
            return LivenessResult(ALIVE, "oracle_api_item", True)
        return LivenessResult(DEAD, "oracle_items_0", True)
    return _unknown_for_status(page.status)


async def _discover_oracle_pod(client, url: str, state: _RunState):
    """The oraclecloud.com pod an employer-domain Candidate Experience page
    loads its data from: the pod host; '' when a page that loaded names
    none; None when the page didn't load (worth another try); or the
    LivenessResult for a fetch that produced no page at all."""
    page = await _fetch(client, url, state, headers=BROWSER_HEADERS)
    if not isinstance(page, _Page):
        return page
    m = _ORACLE_POD_RE.search(page.text)
    if m:
        return m.group(0).lower()
    return "" if page.status == 200 else None


async def _check_oracle_vanity(client, url: str, host: str, site: str, job_id: str,
                               state: _RunState) -> LivenessResult:
    """Oracle HCM on the employer's own domain (jobs.nokia.com/en/sites/CX_1/
    job/38158): the page is the same SPA shell for live and closed ids, and
    the domain's own REST path bounces to an error page, but the page names
    its Oracle pod, whose requisition API answers honestly. The pod is looked
    up once per host per run; no pod falls back to the page check."""
    task = state.oracle_pods.get(host)
    if task is None:
        task = asyncio.ensure_future(_discover_oracle_pod(client, url, state))
        state.oracle_pods[host] = task
    pod = await asyncio.shield(task)
    if pod is None or isinstance(pod, LivenessResult):
        state.oracle_pods.pop(host, None)  # the next row tries the lookup again
        if isinstance(pod, LivenessResult):
            return pod
    if pod:
        return await _check_oracle(client, pod, site, job_id, state)
    return await _check_page(client, url, state)


async def _check_bamboohr(client, host: str, job_id: str, state: _RunState) -> LivenessResult:
    """BambooHR careers pages are an SPA whose closed postings bounce to the
    careers list; the posting's detail endpoint answers the opening while it
    is open and 404 {"type": "not_found"} once it is gone."""
    api = f"https://{host}/careers/{job_id}/detail"
    page = await _fetch(client, api, state, headers=JSON_HEADERS, max_bytes=_MAX_JSON_BYTES)
    if not isinstance(page, _Page):
        return page
    data = _json(page)
    if page.status == 404 and isinstance(data, dict) and data.get("type") == "not_found":
        return LivenessResult(DEAD, "bamboohr_not_found", True)
    if page.status == 200 and isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, dict) and isinstance(result.get("jobOpening"), dict):
            return LivenessResult(ALIVE, "bamboohr_api_200", True)
        return LivenessResult(UNKNOWN, "bamboohr_api_unreadable")
    return _unknown_for_status(page.status)


async def _check_recruitee(client, url: str, host: str, slug: str,
                           state: _RunState) -> LivenessResult:
    """Recruitee's public offers API answers a published offer by its slug.
    A 404 there is death only when the offer page is gone too: an edited
    title gets a new slug and the old one redirects to it, so a slug that
    bounces to another offer is left undecided."""
    api = f"https://{host}/api/offers/{quote(slug, safe='')}"
    page = await _fetch(client, api, state, headers=JSON_HEADERS, max_bytes=_MAX_JSON_BYTES)
    if not isinstance(page, _Page):
        return page
    if page.status == 200:
        data = _json(page)
        offer = data.get("offer") if isinstance(data, dict) else None
        if isinstance(offer, dict) and offer.get("id") and offer.get("status", "published") == "published":
            return LivenessResult(ALIVE, "recruitee_api_200", True)
        return LivenessResult(UNKNOWN, "recruitee_api_unreadable")
    if page.status not in DEAD_HTTP_STATUSES:
        return _unknown_for_status(page.status)
    offer_page = await _fetch(client, url, state, headers=BROWSER_HEADERS)
    if not isinstance(offer_page, _Page):
        return offer_page
    if offer_page.status in DEAD_HTTP_STATUSES:
        return LivenessResult(DEAD, "recruitee_offer_404", True)
    if offer_page.redirected:
        return LivenessResult(UNKNOWN, "recruitee_redirected")
    return _unknown_for_status(offer_page.status)


async def _check_linkedin(client, url: str, state: _RunState) -> LivenessResult:
    """LinkedIn's public guest page: closed jobs carry the 'No longer
    accepting applications' banner or the expired_jd_redirect trk token
    (LinkedIn serves either variant), an honest 404 also happens. A page
    without them is at best weakly alive, never enough to revive a row."""
    page = await _fetch(client, url, state, headers=BROWSER_HEADERS)
    if not isinstance(page, _Page):
        return page
    if page.status in DEAD_HTTP_STATUSES:
        return LivenessResult(DEAD, f"http_{page.status}", True)
    if page.status != 200:
        return _unknown_for_status(page.status)
    if "expired_jd_redirect" in page.url or any(m in page.text for m in _LINKEDIN_DEAD_MARKERS):
        return LivenessResult(DEAD, "linkedin_closed", True)
    if says_dead(page.text):
        return LivenessResult(DEAD, "body_closed", True)
    final_path = urlparse(page.url).path
    if "/authwall" in final_path or "/login" in final_path or "/signup" in final_path:
        return LivenessResult(UNKNOWN, "linkedin_authwall")
    if any(m in page.text for m in _LINKEDIN_LIVE_MARKERS):
        return LivenessResult(ALIVE, "linkedin_apply_cta")
    return LivenessResult(UNKNOWN, "http_200")


# ─── Generic pages ───────────────────────────────────────────────────────────

def registrable_domain(host: str) -> str:
    """'jobs.bosch.com' → 'bosch.com', 'careers.acme.co.uk' → 'acme.co.uk'.
    A small suffix list, not the PSL: only used to tell 'same company site'
    from 'somewhere else entirely'."""
    labels = [label for label in (host or "").lower().strip(".").split(".") if label]
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _is_career_host(host: str) -> bool:
    host = (host or "").lower()
    if any(host == h or host.endswith("." + h) for h in _CAREER_PLATFORM_HOSTS):
        return True
    return bool(_CAREER_HOST_LABEL_RE.match(host))


def _job_token(parsed) -> str:
    """The posting's own id as it appears in its URL (query id, a 4+ digit
    run, a UUID, or failing those the last path slug)."""
    query = {k.lower(): v for k, v in parse_qs(parsed.query).items()}
    for key in _JOB_QUERY_KEYS:
        if query.get(key) and query[key][0].strip():
            return query[key][0].strip()
    segments = [unquote(s) for s in parsed.path.split("/") if s]
    for segment in reversed(segments):
        uuid = re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                         segment, re.IGNORECASE)
        if uuid:
            return uuid.group(0)
        digits = re.findall(r"\d{4,}", segment)
        if digits:
            return max(digits, key=len)
    if segments and len(segments[-1]) >= 6:
        return segments[-1]
    return ""


def _is_careers_home(parsed) -> bool:
    segments = [s for s in parsed.path.split("/") if s]
    while segments and _LOCALE_SEGMENT_RE.match(segments[0]):
        segments = segments[1:]
    if not segments:
        return True
    return len(segments) == 1 and segments[0].lower() in _HOME_SEGMENTS


def _offsite_home_redirect(original_url: str, final_url: str) -> bool:
    """A job URL on a career-site host that redirected to ANOTHER company
    domain's root / careers home, with the posting id nowhere in the final
    URL: the posting was taken down and the ATS bounced to the employer site.
    Deliberately narrow; same-site bounces are left to the age-out rules."""
    original, final = urlparse(original_url), urlparse(final_url)
    if not _is_career_host(original.hostname or ""):
        return False
    if registrable_domain(original.hostname or "") == registrable_domain(final.hostname or ""):
        return False
    token = _job_token(original)
    if not token or token.lower() in final_url.lower():
        return False
    return _is_careers_home(final)


async def _check_page(client, url: str, state: _RunState) -> LivenessResult:
    """Unknown hosts: trust death signals, never a bare 200."""
    page = await _fetch(client, url, state, headers=BROWSER_HEADERS)
    if not isinstance(page, _Page):
        return page
    if page.status in DEAD_HTTP_STATUSES:
        return LivenessResult(DEAD, f"http_{page.status}")
    if page.status != 200:
        return _unknown_for_status(page.status)
    if _ERROR_URL_RE.search(page.url):
        return LivenessResult(DEAD, "error_redirect")
    if page.redirected and _offsite_home_redirect(url, page.url):
        return LivenessResult(DEAD, "offsite_home_redirect")
    text = visible_text(page.text)
    if _SECTION_UNAVAILABLE_RE.search(text):
        # a section-wide (maintenance) page: nothing about this posting
        return LivenessResult(UNKNOWN, "taleo_section_unavailable")
    if _visible_says_dead(text):
        return LivenessResult(DEAD, "body_closed")
    return LivenessResult(UNKNOWN, "http_200")


# ─── Routing ─────────────────────────────────────────────────────────────────

def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _greenhouse_token(board_key: str) -> str:
    """The Greenhouse board token a row's board_key names ('greenhouse:
    janestreet' -> 'janestreet'), '' for any other board."""
    platform, _, token = (board_key or "").partition(":")
    return token.strip() if platform.strip().lower() == "greenhouse" else ""


async def _check_with_adapter(client, url: str) -> LivenessResult | None:
    """A posting on a platform a v2 adapter owns (backend/ingest/adapters,
    matched by its HOST_PATTERNS) is asked through the adapter's own
    check_alive, when it has one and is enabled. None leaves the URL to the
    page check. The adapter paces its own requests (ingest/core/http)."""
    from backend.ingest.adapters import adapter_enabled, adapter_for_url

    adapter = adapter_for_url(url)
    check_alive = getattr(adapter, "check_alive", None)
    if adapter is None or check_alive is None or not adapter_enabled(adapter.PLATFORM):
        return None
    result = await check_alive(client, url)
    if isinstance(result, LivenessResult):
        return result
    logger.warning("%s check_alive returned %r for %s", adapter.PLATFORM, result, url)
    return LivenessResult(UNKNOWN, "error")


async def _route(client, url: str, state: _RunState, board_key: str = "") -> LivenessResult:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https"):
        return LivenessResult(UNKNOWN, "unsupported_url")
    if not host or "." not in host:
        # "https:/.workable.com/..."-style rows: no browser can open them.
        return LivenessResult(DEAD, "malformed_url")

    if _host_is(host, "indeed.com"):
        # Cloudflare walls every probe (401 "Authenticating..."); never judge it.
        return LivenessResult(UNKNOWN, "indeed_unprobeable")

    query = parse_qs(parsed.query)
    board_token = _greenhouse_token(board_key)

    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io"):
        # A board URL names its own token: the boards-api, never the embed.
        m = _GH_BOARD_PATH_RE.match(parsed.path)
        if m and m.group(1) != "embed":
            return await _check_greenhouse_board(client, unquote(m.group(1)), m.group(2), state)
        token = (query.get("token") or [""])[0].strip()
        if parsed.path.startswith("/embed/") and token.isdigit():
            board = (query.get("for") or [""])[0].strip() or board_token
            if board:
                return await _check_greenhouse_board(client, board, token, state)
            return await _check_greenhouse_embed(client, token, state)

    gh_jid = (query.get("gh_jid") or [""])[0].strip()
    if gh_jid.isdigit():
        if board_token:
            return await _check_greenhouse_board(client, board_token, gh_jid, state)
        return await _check_greenhouse_embed(client, gh_jid, state)

    if _host_is(host, "myworkdayjobs.com") or _host_is(host, "myworkdaysite.com"):
        api = _workday_api_url(url)
        if api:
            return await _check_workday(client, api, state)

    if host in ("careers.smartrecruiters.com", "jobs.smartrecruiters.com"):
        m = _SR_PATH_RE.match(parsed.path)
        if m:
            return await _check_smartrecruiters(client, unquote(m.group(1)), m.group(2), state)

    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        m = _LEVER_PATH_RE.match(parsed.path)
        if m:
            api_host = "api.eu.lever.co" if host == "jobs.eu.lever.co" else "api.lever.co"
            return await _check_lever(client, api_host, unquote(m.group(1)), m.group(2), state)

    if host == "jobs.ashbyhq.com":
        m = _ASHBY_PATH_RE.match(parsed.path)
        if m:
            return await _check_ashby(client, unquote(m.group(1)), m.group(2), state)

    if _host_is(host, "oraclecloud.com"):
        m = _ORACLE_PATH_RE.search(parsed.path)
        if m:
            return await _check_oracle(client, host, unquote(m.group(1)), m.group(2), state)
    else:
        m = _ORACLE_VANITY_PATH_RE.match(parsed.path)
        if m:
            return await _check_oracle_vanity(client, url, host, unquote(m.group(1)),
                                              m.group(2), state)

    if _host_is(host, "bamboohr.com") and host != "bamboohr.com":
        m = _BAMBOOHR_PATH_RE.match(parsed.path)
        if m:
            return await _check_bamboohr(client, host, m.group(1), state)

    if _host_is(host, "recruitee.com") and host != "recruitee.com":
        m = _RECRUITEE_PATH_RE.match(parsed.path)
        if m:
            return await _check_recruitee(client, url, host, unquote(m.group(1)), state)

    if _host_is(host, "linkedin.com"):
        return await _check_linkedin(client, url, state)

    owned = await _check_with_adapter(client, url)
    if owned is not None:
        return owned
    return await _check_page(client, url, state)


async def check_listing(client, url: str, *, cache: dict | None = None,
                        board_key: str = "") -> LivenessResult:
    """Is this posting still open? Never raises. ``cache`` carries per-run
    state (host politeness, Ashby boards) across calls; pass the same dict
    for every URL of one run. ``board_key`` is the row's board
    ('greenhouse:janestreet'): a Greenhouse token lets a gh_jid link be
    asked through the board's own API."""
    url = (url or "").strip()
    if not url:
        return LivenessResult(UNKNOWN, "no_url")
    try:
        return await asyncio.wait_for(_route(client, url, _run_state(cache), board_key or ""),
                                      _CHECK_SECONDS)
    except asyncio.TimeoutError:
        return _TIMEOUT
    except Exception as exc:
        logger.debug("liveness check failed for %s: %s", url, exc)
        return LivenessResult(UNKNOWN, "error")


def _interleave_by_host(urls: list[str]) -> list[str]:
    """Round-robin across hosts (LinkedIn's hosts as one, they share a gate)
    so eight workers don't all queue behind one host's gate (a stale backlog
    is often one big Workday board)."""
    by_host: dict[str, list[str]] = {}
    for url in urls:
        by_host.setdefault(_gate_key((urlparse(url).hostname or "").lower()), []).append(url)
    queues = list(by_host.values())
    ordered: list[str] = []
    depth = 0
    while len(ordered) < len(urls):
        for queue in queues:
            if depth < len(queue):
                ordered.append(queue[depth])
        depth += 1
    return ordered


async def check_listings(client, urls: list[str], *, concurrency: int = 8,
                         deadline: float | None = None,
                         cache: dict | None = None,
                         board_keys: Mapping[str, str] | None = None) -> dict[str, LivenessResult]:
    """Check many URLs concurrently (``concurrency`` in flight overall, at
    most two per host, LinkedIn one at a time). Never raises. ``deadline`` (a
    time.monotonic() value) stops STARTING new checks once passed: URLs not
    started are simply absent from the result, so the caller leaves them for
    the next run; a check still waiting for its turn at a host's gate then
    comes back ``gate_queue_timeout``. ``cache`` lets chunked callers share
    per-run state. ``board_keys`` maps a URL to its row's board_key."""
    unique = _interleave_by_host(list(dict.fromkeys(u for u in urls if u)))
    results: dict[str, LivenessResult] = {}
    if not unique:
        return results
    cache = {} if cache is None else cache
    _run_state(cache).deadline = deadline
    board_keys = board_keys or {}
    pending = iter(unique)

    async def worker():
        for url in pending:
            if deadline is not None and time.monotonic() >= deadline:
                return
            try:
                results[url] = await check_listing(client, url, cache=cache,
                                                   board_key=board_keys.get(url, ""))
            except Exception:
                results[url] = LivenessResult(UNKNOWN, "error")

    await asyncio.gather(*(worker() for _ in range(max(1, min(concurrency, len(unique))))))
    return results
