"""Backfill cron: bounded description retries + location/domain repair +
the self-hosted logo harvest (Phase 3)."""

import asyncio
import datetime
import hashlib
import time
from dataclasses import dataclass, field
from typing import NamedTuple

import pytest

import backend.auth.dependencies as auth_deps
import backend.services.logo_harvester as logo_harvester
from backend.data import company_registry
from backend.db.models import CompanyLogo, ScrapedJob
from backend.services import logo_cache

SECRET = "test-cron-secret"
S2 = "https://www.google.com/s2/favicons?domain={}&sz=256"
LICDN = ("https://media.licdn.com/dms/image/v2/C4D0BAQE/company-logo_100_100/"
         "company-logo_100_100/0/1/kinaxis_logo?e=2147483647&v=beta&t=abc")


@dataclass
class FakeHints:
    """Mirror of the logo_harvester.LogoHints contract."""
    company: str
    domains: list = field(default_factory=list)
    job_urls: list = field(default_factory=list)
    existing_logo_urls: list = field(default_factory=list)
    blocked_shas: list = field(default_factory=list)


class FakeLogo(NamedTuple):
    data: bytes
    sha: str
    fmt: str
    width: int
    height: int


@dataclass
class FakeResult:
    logo: FakeLogo
    source: str = "linkedin"
    source_url: str = ""
    verified_domain: str | None = None


def _fake_logo(seed=b"k"):
    data = b"\x89PNG\r\n\x1a\n" + seed * 40
    return FakeLogo(data, hashlib.sha1(data).hexdigest(), "png", 128, 128)


@pytest.fixture(autouse=True)
def _no_network_harvest(monkeypatch):
    """The logo-harvest phase must never hit the network in tests."""
    async def fake_harvest(client, hints):
        return None

    async def never_bogus(client, domain):
        return False

    monkeypatch.setattr(logo_harvester, "LogoHints", FakeHints, raising=False)
    monkeypatch.setattr(logo_harvester, "harvest_company_logo", fake_harvest, raising=False)
    monkeypatch.setattr(logo_cache, "domain_is_bogus", never_bogus)
    monkeypatch.setattr(company_registry, "load_logo_map", lambda: {})


def _cron_headers(monkeypatch):
    monkeypatch.setattr(auth_deps, "CRON_SECRET", SECRET)
    return {"x-cron-secret": SECRET}


def _mk(db_session, url, description="", attempts=0, location="Ottawa, ON, CA",
        location_search="", company="Kinaxis"):
    row = ScrapedJob(
        title="Engineer", company=company, url=url, location=location,
        description=description, country="CA", work_type="onsite",
        source_platform="ats", experience_level="new_grad", easy_apply=0,
        match_score=0, desc_fetch_attempts=attempts,
        location_search=location_search,
    )
    db_session.add(row)
    db_session.commit()
    return row


async def _no_description(client_, url):
    return ""


def test_backfill_fetches_description_and_repairs_row(client, db_session, monkeypatch):
    row = _mk(db_session, "https://x.test/backfill-1")

    async def fake_extract(client_, url):
        return "A long and detailed description of the role " * 5

    monkeypatch.setattr(
        "backend.routers.jobs.extract_description_from_url", fake_extract
    )
    res = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["descriptions_fixed"] >= 1
    db_session.refresh(row)
    assert len(row.description) > 100
    assert row.desc_fetch_attempts == 1
    assert "|ottawa|" in row.location_search
    assert row.company_domain == "kinaxis.com"


@pytest.mark.parametrize("stored, description, expected", [
    # A list's no-sponsorship mark (U+1F6C2 / US flag): a description that
    # says nothing about visas must not reset it to 'unknown'.
    ("no", "A long and detailed description of the role " * 5, "no"),
    ("yes", "We are unable to sponsor visas for this role. " * 5, "yes"),
    # An unknown is still filled from the description.
    ("unknown", "We are unable to sponsor visas for this role. " * 5, "no"),
    (None, "Visa sponsorship is available for this role. " * 5, "yes"),
], ids=["list-no-kept", "yes-kept", "unknown-filled", "null-filled"])
def test_backfill_only_fills_an_unknown_visa_answer(client, db_session, monkeypatch,
                                                    stored, description, expected):
    row = _mk(db_session, "https://x.test/backfill-visa")
    row.visa_sponsorship = stored
    db_session.commit()

    async def fake_extract(client_, url):
        return description

    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", fake_extract)
    res = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch))
    assert res.status_code == 200, res.text
    db_session.refresh(row)
    assert row.description
    assert row.visa_sponsorship == expected


def test_backfill_skips_rows_at_attempt_cap(client, db_session, monkeypatch):
    row = _mk(db_session, "https://x.test/backfill-2", attempts=3)
    called = {"n": 0}

    async def fake_extract(client_, url):
        called["n"] += 1
        return ""

    monkeypatch.setattr(
        "backend.routers.jobs.extract_description_from_url", fake_extract
    )
    client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch))
    db_session.refresh(row)
    assert row.desc_fetch_attempts == 3
    assert called["n"] == 0


def test_backfill_increments_attempts_on_failure(client, db_session, monkeypatch):
    row = _mk(db_session, "https://x.test/backfill-3")

    async def fake_extract(client_, url):
        return ""

    monkeypatch.setattr(
        "backend.routers.jobs.extract_description_from_url", fake_extract
    )
    client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch))
    db_session.refresh(row)
    assert row.desc_fetch_attempts == 1
    assert (row.description or "") == ""


def test_backfill_prioritizes_direct_urls_over_linkedin(client, db_session, monkeypatch):
    # The LinkedIn row is NEWER but the direct row must get the batch slot.
    direct = _mk(db_session, "https://boards.greenhouse.io/acme/jobs/9")
    linkedin = _mk(db_session, "https://www.linkedin.com/jobs/view/999999")

    async def fake_extract(client_, url):
        return ""

    monkeypatch.setattr(
        "backend.routers.jobs.extract_description_from_url", fake_extract
    )
    client.post("/jobs/cron-backfill", params={"batch_size": 1}, headers=_cron_headers(monkeypatch))
    db_session.refresh(direct)
    db_session.refresh(linkedin)
    assert direct.desc_fetch_attempts == 1
    assert (linkedin.desc_fetch_attempts or 0) == 0


def _attempted(db_session, *rows):
    for row in rows:
        db_session.refresh(row)
    return [(row.desc_fetch_attempts or 0) for row in rows]


def _no_fetch(monkeypatch):
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)


def test_backfill_fetches_active_jobs_before_closed_ones(client, db_session, monkeypatch):
    # An active job with no description reads "We couldn't score this" until it is filled; a
    # closed one (newer, and a direct URL) can wait.
    active = _mk(db_session, "https://www.linkedin.com/jobs/view/123")
    closed = _mk(db_session, "https://boards.greenhouse.io/acme/jobs/10")
    closed.listing_status = "removed"
    db_session.commit()
    _no_fetch(monkeypatch)
    client.post("/jobs/cron-backfill", params={"batch_size": 1}, headers=_cron_headers(monkeypatch))
    assert _attempted(db_session, active, closed) == [1, 0]


def test_backfill_fetches_the_newest_active_job_first(client, db_session, monkeypatch):
    older = _mk(db_session, "https://boards.greenhouse.io/acme/jobs/11")
    newer = _mk(db_session, "https://boards.greenhouse.io/acme/jobs/12")
    expired = _mk(db_session, "https://boards.greenhouse.io/acme/jobs/13")
    expired.listing_status = "expired"
    db_session.commit()
    _no_fetch(monkeypatch)
    client.post("/jobs/cron-backfill", params={"batch_size": 1}, headers=_cron_headers(monkeypatch))
    assert _attempted(db_session, older, newer, expired) == [0, 1, 0]


def test_backfill_counts_a_stale_or_unset_status_as_active(client, db_session, monkeypatch):
    stale = _mk(db_session, "https://boards.greenhouse.io/acme/jobs/14")
    stale.listing_status = "stale"
    unset = _mk(db_session, "https://boards.greenhouse.io/acme/jobs/15")
    unset.listing_status = None
    closed = _mk(db_session, "https://boards.greenhouse.io/acme/jobs/16")
    closed.listing_status = "removed"
    db_session.commit()
    _no_fetch(monkeypatch)
    client.post("/jobs/cron-backfill", params={"batch_size": 2}, headers=_cron_headers(monkeypatch))
    assert _attempted(db_session, stale, unset, closed) == [1, 1, 0]


def _hangs(started: list):
    """An extractor whose host answers only after 5 s; logs each start."""
    async def extract(client_, url):
        started.append(url)
        await asyncio.sleep(5)
        return "A description that arrives far too late " * 5
    return extract


def test_backfill_defers_what_the_budget_cuts_off_uncharged(client, db_session, monkeypatch):
    rows = [_mk(db_session, f"https://x.test/slow-{i}") for i in range(2)]
    started = []
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _hangs(started))
    # Well over the 1 s floor even after the client's setup, so both fetches
    # start and it is the budget, mid-flight, that cuts them off.
    monkeypatch.setattr("backend.routers.jobs.DESCRIPTION_BUDGET_S", 3.0)

    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()
    assert len(started) == 2
    assert body["descriptions_deferred"] == 2
    assert body["elapsed_s"] < 4.5  # ended with the budget, not with the host
    for row in rows:
        db_session.refresh(row)
        # The budget's fault, not the job's: no attempt spent.
        assert (row.desc_fetch_attempts or 0) == 0
        assert (row.description or "") == ""
        assert "|ottawa|" in row.location_search  # the repairs with no network still ran


def test_backfill_defers_what_the_budget_never_reached(client, db_session, monkeypatch):
    row = _mk(db_session, "https://x.test/unreached")
    started = []
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _hangs(started))
    monkeypatch.setattr("backend.routers.jobs.DESCRIPTION_BUDGET_S", 0.0)

    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()
    assert started == []
    assert body["descriptions_deferred"] == 1
    db_session.refresh(row)
    assert (row.desc_fetch_attempts or 0) == 0


def test_backfill_charges_a_fetch_that_hits_its_own_cap(client, db_session, monkeypatch):
    row = _mk(db_session, "https://x.test/tarpit")
    started = []
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _hangs(started))
    monkeypatch.setattr("backend.routers.jobs.DESCRIPTION_FETCH_TIMEOUT_S", 0.2)

    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()
    assert started == [row.url]
    assert body["descriptions_deferred"] == 0
    assert body["descriptions_fixed"] == 0  # cut at the cap, never waited out
    assert body["elapsed_s"] < 4
    db_session.refresh(row)
    assert row.desc_fetch_attempts == 1  # a slow host is a failed attempt
    assert (row.description or "") == ""


def test_backfill_charges_a_row_with_no_url(client, db_session, monkeypatch):
    row = _mk(db_session, "")
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)
    client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch))
    db_session.refresh(row)
    assert row.desc_fetch_attempts == 1  # never deferred, or it holds a slot forever


# --- Phase 3: self-hosted logos ------------------------------------------------

def test_backfill_harvests_stores_and_propagates(client, db_session, monkeypatch):
    rows = [_mk(db_session, f"https://boards.greenhouse.io/kinaxis/jobs/{i}") for i in range(3)]
    for row in rows:
        row.company_domain = "kinaxis.com"
        row.company_logo = S2.format("kinaxis.com")
    # A hidden LinkedIn twin still donates its posting URL and real logo.
    twin = _mk(db_session, "https://www.linkedin.com/jobs/view/4417990546",
               company="Kinaxis Inc.")
    twin.company_logo = LICDN
    twin.duplicate_of = rows[0].id
    db_session.commit()

    monkeypatch.setattr(company_registry, "load_logo_map", lambda: {
        "kinaxis": "https://www.google.com/s2/favicons?domain=kinaxis.com&sz=128",
    })
    seen = {}

    async def fake_harvest(client_, hints):
        seen[hints.company] = hints
        return FakeResult(_fake_logo(), verified_domain="kinaxis.com")

    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)
    monkeypatch.setattr(logo_harvester, "harvest_company_logo", fake_harvest, raising=False)

    res = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["logos_harvested"] == 1
    assert body["logo_domains_probed"] == 1
    assert body["logo_harvest"]["stored"] == 1

    hints = seen["Kinaxis"]
    assert hints.domains[0] == "kinaxis.com"
    assert hints.job_urls[0] == twin.url  # LinkedIn postings first, hidden ones included
    assert "https://boards.greenhouse.io/kinaxis/jobs/2" in hints.job_urls
    assert hints.existing_logo_urls == [LICDN]

    path = logo_cache.lookup_logo(db_session, "Kinaxis")
    db_session.expire_all()
    for row in rows:
        assert db_session.get(ScrapedJob, row.id).company_logo == path
    assert db_session.get(ScrapedJob, twin.id).company_logo == LICDN  # trusted, kept


def test_backfill_miss_backs_off_and_clears_bogus_domain(client, db_session, monkeypatch):
    row = _mk(db_session, "https://x.test/nologo-1", company="NoLogo Corp")
    row.company_domain = "nologo.com"  # the name guess
    row.company_logo = S2.format("nologo.com")
    db_session.commit()
    calls = []

    async def fake_harvest(client_, hints):
        calls.append(hints.company)
        return None

    async def bogus(client_, domain):
        return domain == "nologo.com"

    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)
    monkeypatch.setattr(logo_harvester, "harvest_company_logo", fake_harvest, raising=False)
    monkeypatch.setattr(logo_cache, "domain_is_bogus", bogus)

    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()
    assert body["logo_harvest"]["missed"] == 1
    assert body["logo_harvest"]["domains_rejected"] == 1
    db_session.expire_all()
    stored = db_session.get(ScrapedJob, row.id)
    assert stored.company_domain == ""
    assert stored.company_logo == ""
    record = db_session.query(CompanyLogo).filter_by(company_key="nologo").one()
    assert record.status == "miss" and record.attempts == 1
    assert record.next_retry_at > datetime.datetime.utcnow() + datetime.timedelta(days=13)

    # Not due yet: the next run leaves it alone, and the description phase's
    # domain repair must not plant the bogus guess again.
    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()
    assert calls == ["NoLogo Corp"]
    assert body["logo_harvest"]["companies_considered"] == 0
    db_session.expire_all()
    assert db_session.get(ScrapedJob, row.id).company_domain == ""


def test_backfill_retries_a_due_miss(client, db_session, monkeypatch):
    _mk(db_session, "https://x.test/due-1", company="Retry Me")
    db_session.add(CompanyLogo(
        company_key="retry me", status="miss", attempts=2,
        next_retry_at=datetime.datetime.utcnow() - datetime.timedelta(hours=1),
    ))
    db_session.commit()
    calls = []

    async def fake_harvest(client_, hints):
        calls.append(hints.company)
        return None

    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)
    monkeypatch.setattr(logo_harvester, "harvest_company_logo", fake_harvest, raising=False)
    client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch))
    assert calls == ["Retry Me"]
    db_session.expire_all()
    assert db_session.query(CompanyLogo).filter_by(company_key="retry me").one().attempts == 3


def test_backfill_repoints_rows_planted_after_a_store(client, db_session, monkeypatch):
    first = _mk(db_session, "https://x.test/kx-1")
    logo_cache.store_logo(db_session, "Kinaxis", FakeResult(_fake_logo()))
    planted = _mk(db_session, "https://x.test/kx-2")
    planted.company_logo = "https://www.kinaxis.com/og/share.jpg"
    db_session.commit()
    calls = []

    async def fake_harvest(client_, hints):
        calls.append(hints.company)
        return None

    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)
    monkeypatch.setattr(logo_harvester, "harvest_company_logo", fake_harvest, raising=False)
    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()
    assert body["logo_harvest"]["repropagated_rows"] == 1
    assert calls == []  # already stored, never re-harvested
    db_session.expire_all()
    path = logo_cache.lookup_logo(db_session, "Kinaxis")
    assert db_session.get(ScrapedJob, first.id).company_logo == path
    assert db_session.get(ScrapedJob, planted.id).company_logo == path


def test_harvest_respects_the_wall_clock_budget(db_session, monkeypatch):
    _mk(db_session, "https://x.test/budget-1", company="Late Co")
    calls = []

    async def fake_harvest(client_, hints):
        calls.append(hints.company)
        return None

    monkeypatch.setattr(logo_harvester, "harvest_company_logo", fake_harvest, raising=False)
    stats = asyncio.run(logo_cache.harvest_missing_logos(db_session, None, budget_s=0))
    assert stats["skipped_budget"] == 1
    assert calls == []
    # Skipped is not a miss: nothing recorded, so the next run tries it.
    assert db_session.query(CompanyLogo).count() == 0


def _harvest_calls(monkeypatch) -> list:
    calls = []

    async def fake_harvest(client_, hints):
        calls.append(hints.company)
        return None

    monkeypatch.setattr(logo_harvester, "harvest_company_logo", fake_harvest, raising=False)
    return calls


def test_backfill_harvest_gets_only_what_the_descriptions_left(client, db_session, monkeypatch):
    _mk(db_session, "https://x.test/spent-1", company="Late Co")
    calls = _harvest_calls(monkeypatch)
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _hangs([]))
    # Descriptions use 2.5 s of a 3 s pass: the harvest's budget is what
    # the pass has left, not a fresh one of its own, so it never starts.
    monkeypatch.setattr("backend.routers.jobs.DESCRIPTION_BUDGET_S", 2.5)
    monkeypatch.setattr("backend.routers.jobs.BACKFILL_BUDGET_S", 3.0)
    monkeypatch.setattr("backend.routers.jobs.POST_HARVEST_RESERVE_S", 0.0)

    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()
    assert body["logo_harvest"]["skipped_budget"] == 1
    assert calls == []
    assert db_session.query(CompanyLogo).count() == 0  # retried next run, not a miss


def test_backfill_harvest_leaves_the_reserve(client, db_session, monkeypatch):
    _mk(db_session, "https://x.test/reserve-1", company="Late Co")
    calls = _harvest_calls(monkeypatch)
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)
    # The whole pass is reserve: the harvest gets nothing.
    monkeypatch.setattr("backend.routers.jobs.BACKFILL_BUDGET_S", 10.0)
    monkeypatch.setattr("backend.routers.jobs.POST_HARVEST_RESERVE_S", 10.0)

    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()
    assert body["logo_harvest"]["skipped_budget"] == 1
    assert calls == []


def test_harvest_holds_no_transaction_while_on_the_network(db_session, monkeypatch):
    """Phase 3's reads end before the harvest: a pooled connection must not
    sit idle in transaction for the whole budget."""
    _mk(db_session, "https://x.test/tx-1", company="Acme")
    in_transaction = []

    async def fake_harvest(client_, hints):
        in_transaction.append(db_session.in_transaction())
        return None

    monkeypatch.setattr(logo_harvester, "harvest_company_logo", fake_harvest, raising=False)
    stats = asyncio.run(logo_cache.harvest_missing_logos(db_session, None, budget_s=10))
    assert in_transaction == [False]
    assert stats["missed"] == 1  # the write afterwards still lands
    assert db_session.query(CompanyLogo).filter_by(company_key="acme").one().status == "miss"


class _Client:
    """Stands in for the HTTP client: the fakes never use it, and the
    harvester's LinkedIn gate only needs something to key weakly."""


def _linkedin_gives_up(results: dict):
    """LinkedIn rate-limits the run's very first call, so it is skipped for
    everyone (the cron's rule); each company then gets results.get(name)."""
    async def harvest(client_, hints):
        gate = logo_harvester._linkedin_gates.get(client_)
        if gate is None:
            gate = logo_harvester._linkedin_gates[client_] = logo_harvester._LinkedInGate()
        if not gate.blocked:
            gate.blocked, gate.blocked_at = True, time.monotonic()
        return results.get(hints.company)
    return harvest


def test_after_a_linkedin_429_lower_tier_picks_are_not_final(db_session, monkeypatch):
    home = "https://www.acme.com/apple-touch-icon.png"
    shown = _mk(db_session, "https://x.test/acme-1", company="Acme")
    shown.company_logo = home
    blank = _mk(db_session, "https://x.test/acme-2", company="Acme")
    for name in ("Beta", "Gamma", "Delta"):
        _mk(db_session, f"https://x.test/{name.lower()}-1", company=name)
    db_session.commit()
    results = {
        "Acme": FakeResult(_fake_logo(b"a"), source="homepage", verified_domain="acme.com"),
        "Gamma": FakeResult(_fake_logo(b"g"), source="linkedin_search"),
        "Delta": FakeResult(_fake_logo(b"d"), source="ats_greenhouse"),
    }
    monkeypatch.setattr(logo_harvester, "harvest_company_logo", _linkedin_gives_up(results),
                        raising=False)
    before = datetime.datetime.utcnow()
    stats = asyncio.run(logo_cache.harvest_missing_logos(db_session, _Client(), budget_s=10))
    assert (stats["stored"], stats["provisional"]) == (3, 1)
    assert (stats["missed"], stats["linkedin_deferred"]) == (1, 1)

    def record(key):
        return db_session.query(CompanyLogo).filter_by(company_key=key).one()

    db_session.expire_all()
    # Acme's homepage pick is shown where nothing real was, and re-checked soon.
    acme = record("acme")
    assert acme.status == "ok" and acme.next_retry_at - before < datetime.timedelta(days=2)
    path = logo_cache.lookup_logo(db_session, "Acme")
    assert db_session.get(ScrapedJob, blank.id).company_logo == path
    assert db_session.get(ScrapedJob, shown.id).company_logo == home
    # LinkedIn's own and the ATS board's picks stand; Beta's miss proves little.
    assert record("gamma").next_retry_at is None and record("delta").next_retry_at is None
    beta = record("beta")
    assert beta.status == "miss" and beta.next_retry_at - before < datetime.timedelta(days=2)

    # A day later LinkedIn answers: the re-check makes Acme's logo final.
    db_session.query(CompanyLogo).filter(CompanyLogo.company_key.in_(["acme", "beta"])).update(
        {"next_retry_at": datetime.datetime.utcnow() - datetime.timedelta(minutes=1)},
        synchronize_session=False,
    )
    db_session.commit()
    seen = []

    async def linkedin_answers(client_, hints):
        seen.append(hints.company)
        return FakeResult(_fake_logo(b"li"), source="linkedin_job") if hints.company == "Acme" else None

    monkeypatch.setattr(logo_harvester, "harvest_company_logo", linkedin_answers, raising=False)
    asyncio.run(logo_cache.harvest_missing_logos(db_session, _Client(), budget_s=10))
    assert seen == ["Beta", "Acme"]  # an employer with no logo first, the re-check after
    db_session.expire_all()
    acme = record("acme")
    assert acme.next_retry_at is None and acme.source == "linkedin_job"
    path = logo_cache.lookup_logo(db_session, "Acme")
    assert {db_session.get(ScrapedJob, r.id).company_logo for r in (shown, blank)} == {path}
    assert acme.prior_logo_urls == [home]
    assert record("beta").next_retry_at > datetime.datetime.utcnow() + datetime.timedelta(days=13)


def test_after_linkedin_block_keeps_what_finished_before_it():
    def outcome(key, status, source=None):
        plan = logo_cache._Plan(key=key, display=key, names=[key], hints=None)
        result = FakeResult(_fake_logo(), source=source) if source else None
        return logo_cache.HarvestOutcome(plan, status, result, [])

    outcomes = [outcome("early", "ok", "homepage"), outcome("s2", "ok", "s2"),
                outcome("wiki", "ok", "wikidata"), outcome("ats", "ok", "ats_lever"),
                outcome("seed", "ok", "existing"), outcome("gone", "miss"),
                outcome("slow", "timeout")]
    finished = {"early": 1.0, "s2": 5.0, "wiki": 5.0, "ats": 5.0, "seed": 5.0, "gone": 5.0, "slow": 5.0}
    got = {o.plan.key: o.status for o in logo_cache.after_linkedin_block(outcomes, finished, 2.0)}
    assert got == {"early": "ok", "s2": "provisional", "wiki": "provisional", "ats": "ok",
                   "seed": "ok", "gone": "retry", "slow": "retry"}
    assert logo_cache.after_linkedin_block(outcomes, finished, None) == outcomes


def test_after_linkedin_block_covers_harvests_linkedin_never_answered():
    """No 429 ever tripped the gate (blocked_at None), but LinkedIn gave
    these harvests no answer (a tarpit, errors): same rule."""
    def outcome(key, status, source=None, missing=True):
        plan = logo_cache._Plan(key=key, display=key, names=[key], hints=None)
        result = FakeResult(_fake_logo(), source=source) if source else None
        return logo_cache.HarvestOutcome(plan, status, result, [], missing)

    outcomes = [outcome("home", "ok", "homepage"), outcome("ats", "ok", "ats_lever"),
                outcome("gone", "miss"), outcome("slow", "timeout"),
                outcome("answered", "miss", missing=False), outcome("capped", "timeout", missing=False)]
    got = {o.plan.key: o.status for o in logo_cache.after_linkedin_block(outcomes, {}, None)}
    assert got == {"home": "provisional", "ats": "ok", "gone": "retry", "slow": "retry",
                   "answered": "miss", "capped": "timeout"}


def test_backfill_without_the_harvester_contract_still_succeeds(client, db_session, monkeypatch):
    _mk(db_session, "https://x.test/nh-1")
    monkeypatch.delattr(logo_harvester, "harvest_company_logo", raising=False)
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)
    res = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch))
    assert res.status_code == 200, res.text
    assert res.json()["logo_harvest"]["harvester_available"] is False


# --- country repair (DB-only step) --------------------------------------------

def _linkedin_us_row(db_session, url="https://www.linkedin.com/jobs/view/77"):
    row = ScrapedJob(
        title="Software Intern", company="Acme", url=url,
        location="Calgary, Alberta, Canada", description="x" * 80, country="US",
        work_type="onsite", source_platform="linkedin", experience_level="internship",
        easy_apply=0, match_score=0, location_search="|calgary|ab|alberta|canada|",
        city="calgary", company_domain="acme.com",
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_backfill_repairs_a_contradicted_country(client, db_session, monkeypatch):
    row = _linkedin_us_row(db_session)
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)

    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()

    assert body["country_repair"]["repaired"] == 1
    db_session.refresh(row)
    assert row.country == "CA"


def test_backfill_country_repair_never_runs_past_the_harvest_mark(client, db_session, monkeypatch):
    """With no time left before the harvest's deadline the repair is skipped
    (next run), so it can never push the pass past its plan."""
    row = _linkedin_us_row(db_session)
    monkeypatch.setattr("backend.routers.jobs.extract_description_from_url", _no_description)
    monkeypatch.setattr("backend.routers.jobs.BACKFILL_BUDGET_S", 10.0)
    monkeypatch.setattr("backend.routers.jobs.POST_HARVEST_RESERVE_S", 10.0)

    body = client.post("/jobs/cron-backfill", headers=_cron_headers(monkeypatch)).json()

    assert body["country_repair"] == {"skipped": True}
    db_session.refresh(row)
    assert row.country == "US"
