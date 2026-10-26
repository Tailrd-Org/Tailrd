"""Liveness fixtures (scraper v2 Task 12): real exchanges, replayed offline.

For each source with a liveness check there are saved cases of postings that
were open on 2026-10-26 and of postings that were gone (backend/tests/fixtures/
ingest/liveness/<source>.json). A case holds every HTTP exchange the check made
against the live web that day: request, status, the headers it reads (location,
content-type, retry-after) and the body cut down to what the check reads (JSON
keeps its shape with long text cut; a page keeps its visible text, which is all
the generic page check looks at, plus the JSON-LD block or markers a check
needs; emails and phone numbers are masked, cookies were never kept). The
tests replay each case through ``platform_liveness.check_listing`` over an
``httpx.MockTransport``, with no network, and require the recorded verdict,
reason and authority: every case must score, 100%.

How the files were made, so they can be remade:
- URLs came from the dev branch (``active`` rows for open, ``removed`` rows for
  gone; SuccessFactors has no removed rows, so its gone cases are ``expired``
  ones). A source with no rows there (Job Bank, Teamtailor, Taleo's open side)
  took open URLs from its own live listing and gone URLs from archived pages
  that now answer gone.
- Each URL was checked live the same day, at most one request a second per
  host, with the TailrdBot user agent, reading robots.txt before any HTML page.
  A case is kept only when the platform's own answer and the row agree.
- A case marked ``known_gap`` is a posting that really is gone but that the
  check cannot call dead today. It is replayed with the true verdict required,
  as a strict expected failure, so the gap stays visible and the marker has to
  go the day the check learns it.
- Generic page checks (Jibe, Teamtailor, Taleo, SuccessFactors) can say dead or
  unknown, never alive: a bare 200 proves nothing. An open page there is
  therefore required to come back ``unknown / http_200``, not ``dead``.
"""

import asyncio
import datetime
import json
import pathlib
import re

import httpx
import pytest

from backend.ingest.adapters import jobbank_ca
from backend.ingest.core import http
from backend.services.platform_liveness import check_listing, make_client

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "ingest" / "liveness"

# The sources the spec names. teamtailor, taleo and successfactors have no
# check of their own and fall back to the generic page check, as does jibe
# (it claims no URLs); the rest answer through the platform's API or the
# adapter's check_alive.
SOURCES = (
    "greenhouse", "lever", "ashby", "smartrecruiters", "workday", "workable", "recruitee",
    "bamboohr", "oracle_hcm", "icims", "jibe", "jobbank_ca", "teamtailor", "taleo", "successfactors",
)
GENERIC_PAGE_CHECK = ("jibe", "teamtailor", "taleo", "successfactors")
MIN_CASES = 3


def _load() -> dict[str, dict]:
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURES.glob("*.json"))}


DOCS = _load()


def _params():
    out = []
    for source in SOURCES:
        for case in DOCS.get(source, {}).get("cases", []):
            marks = []
            if case.get("known_gap"):
                marks.append(pytest.mark.xfail(strict=True, reason=case["known_gap"]))
            out.append(pytest.param(source, case, id=case["id"], marks=marks))
    return out


@pytest.fixture(autouse=True)
def setup_db():
    """No database here: this module skips the root conftest's per-test
    create_all/drop_all, which would otherwise cost most of the run."""
    yield


class FakeClock:
    """Stands in for http._now and http._sleep so the adapters' one-request-a-
    second pacing (five seconds for Job Bank) costs no real time."""

    def __init__(self):
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(http, "_now", fake.time)
    monkeypatch.setattr(http, "_sleep", fake.sleep)
    monkeypatch.setattr(http, "_next_slot", {})
    monkeypatch.setattr(http, "_robots", {})
    return fake


class Replay:
    """The handler of an httpx.MockTransport that serves a case's recorded
    exchanges. A request matches on method, URL and JSON body, and each exchange
    is served once; anything the check asks that was not recorded is kept in
    ``unmatched`` (the check swallows transport errors, so the test reads the
    list instead), and an exchange the check never asked for stays in
    ``pending``. Redirects are exchanges of their own: httpx follows the
    recorded ``location`` and asks again."""

    def __init__(self, exchanges: list[dict]):
        self.pending = list(exchanges)
        self.unmatched: list[str] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content) if request.content else None
        for index, exchange in enumerate(self.pending):
            if (exchange["method"], exchange["url"], exchange.get("request_json")) == (
                request.method, str(request.url), sent,
            ):
                del self.pending[index]
                return httpx.Response(exchange["status"], headers=exchange.get("headers", {}),
                                      content=self._body(exchange))
        self.unmatched.append(f"{request.method} {request.url}")
        return httpx.Response(599, content=b"no recorded exchange")

    @staticmethod
    def _body(exchange: dict) -> bytes:
        if "body_json" in exchange:
            return json.dumps(exchange["body_json"]).encode("utf-8")
        if "body_file" in exchange:
            return (FIXTURES / exchange["body_file"]).read_text(encoding="utf-8").encode("utf-8")
        return exchange.get("body", "").encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("source,case", _params())
async def test_replayed_check_gives_the_recorded_verdict(source, case, clock, monkeypatch):
    # Job Bank compares a posting's validThrough with today; hold today at the
    # day of the recording so an open page does not turn expired in the replay.
    recorded_on = datetime.date.fromisoformat(DOCS[source]["recorded_on"])
    monkeypatch.setattr(jobbank_ca, "_today", lambda: recorded_on)
    replay = Replay(case["exchanges"])
    async with make_client(transport=replay.transport()) as client:
        result = await check_listing(client, case["url"], board_key=case.get("board_key", ""))
    expect = case["expect"]
    assert result.verdict == expect["verdict"], (result, expect)
    if "reason" in expect:
        assert result.reason == expect["reason"]
    if "authoritative" in expect:
        assert result.authoritative is expect["authoritative"]
    # The check made exactly the requests that were recorded, so a routing
    # change (another endpoint, an extra page) cannot pass unnoticed.
    assert replay.unmatched == []
    assert replay.pending == []


def test_every_source_has_a_fixture_file():
    assert sorted(DOCS) == sorted(SOURCES)


@pytest.mark.parametrize("source", SOURCES)
def test_each_source_has_three_alive_and_three_dead_cases_or_says_why_not(source):
    doc = DOCS[source]
    shortfall = doc.get("shortfall", {})
    for truth in ("alive", "dead"):
        scoring = [c for c in doc["cases"] if c["truth"] == truth and not c.get("known_gap")]
        if truth in shortfall:
            # a stale reason would hide a source that has since reached three
            assert len(scoring) < MIN_CASES, f"{source}: {truth} no longer falls short"
            assert shortfall[truth].strip()
        else:
            assert len(scoring) >= MIN_CASES, f"{source}: {len(scoring)} {truth} cases"


@pytest.mark.parametrize("source", SOURCES)
def test_an_open_case_is_never_called_dead_and_a_gone_case_is_required_dead(source):
    for case in DOCS[source]["cases"]:
        if case["truth"] == "dead":
            assert case["expect"]["verdict"] == "dead", case["id"]
        elif source in GENERIC_PAGE_CHECK:
            # no page check can confirm a posting open: unknown is its best answer
            assert case["expect"] == {"verdict": "unknown", "reason": "http_200", "authoritative": False}, case["id"]
        else:
            assert case["expect"]["verdict"] == "alive", case["id"]


def test_every_body_file_exists_and_no_file_is_left_unused():
    referenced = {
        exchange["body_file"]
        for doc in DOCS.values() for case in doc["cases"] for exchange in case["exchanges"]
        if "body_file" in exchange
    }
    on_disk = {
        path.relative_to(FIXTURES).as_posix() for path in FIXTURES.rglob("*")
        if path.is_file() and path.suffix != ".json"
    }
    assert referenced == on_disk


def test_fixtures_hold_nothing_personal_and_no_em_dash():
    emails = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
    allowed_headers = {"content-type", "location", "retry-after"}
    for path in FIXTURES.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text, path.name
        # example.com addresses stand in for the real ones, which are cut out
        assert not [m for m in emails.findall(text) if not m.endswith("@example.com")], path.name
    for doc in DOCS.values():
        for case in doc["cases"]:
            for exchange in case["exchanges"]:
                assert set(exchange.get("headers", {})) <= allowed_headers, case["id"]
