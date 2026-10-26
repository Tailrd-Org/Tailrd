"""Dead-job detection end to end: postings a complete board stops listing
close at its first reconcile (removed, closed_reason delisted, hidden), a
liveness pass leaves them closed, and one that is listed again comes back.
Every other way a row closes stamps its closed_reason too."""

import asyncio
import datetime

import httpx
import pytest

from backend.data import company_registry
from backend.db.models import ScrapedJob
from backend.ingest.core import pipeline
from backend.services import ats_scraper, listing_freshness
from backend.services.ats_scraper import ATSScraper
from backend.services.listing_freshness import (
    HIDDEN_LISTING_STATUSES,
    LISTING_ACTIVE,
    LISTING_EXPIRED,
    LISTING_REMOVED,
    LISTING_STALE,
)
from backend.services.platform_liveness import LivenessResult

NOW = datetime.datetime.utcnow()
TITLES = ("Software Engineering Intern", "Registered Nurse - ICU", "Senior Accountant",
          "Director of Operations", "Electrician Apprentice", "Data Analyst")


def _board_payload(ids):
    """A Greenhouse job-board API answer listing postings ``ids``: every
    level and family, all in the US or Canada."""
    return {"jobs": [{
        "id": n,
        "title": f"{TITLES[n % len(TITLES)]} {n}",
        "updated_at": "2026-10-20T12:00:00Z",
        "location": {"name": "Toronto, Ontario, Canada" if n % 2 else "Austin, TX"},
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{n}",
        "departments": [{"name": "Operations"}],
        "content": f"&lt;p&gt;Posting {n}: join the team.&lt;/p&gt;",
    } for n in ids]}


@pytest.fixture
def crawl(db_session, monkeypatch):
    """Crawl the fixture board through the real Greenhouse connector (served
    by an httpx MockTransport) and ingest it through the pipeline."""
    monkeypatch.setattr(ats_scraper, "_HOST_MIN_INTERVAL", 0)
    monkeypatch.setattr(company_registry, "load_board_countries", lambda: {})

    def run(ids):
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json=_board_payload(ids)))

        async def go():
            async with httpx.AsyncClient(transport=transport) as client:
                scraper = ATSScraper(filter_entry_level=False, filter_north_america=True)
                snapshot = await scraper.scrape_board(client, "greenhouse", "acme", "Acme")
                assert snapshot.complete and len(snapshot.all_urls) == len(ids)
                ctx = pipeline.new_context(db_session, ["Acme"], limits=pipeline.Limits())
                return await pipeline.ingest_snapshot(db_session, client, snapshot, ctx)

        return asyncio.run(go())

    return run


def _by_posting(db):
    db.expire_all()
    return {int(row.url.rsplit("/", 1)[1]): row for row in db.query(ScrapedJob).all()}


def test_postings_dropped_from_a_complete_board_close_and_one_relisted_comes_back(
        db_session, crawl, monkeypatch):
    board = crawl(range(1, 121))
    assert board["inserted"] == 120

    # The board drops 50 postings: the first reconcile closes them.
    board = crawl(range(51, 121))
    assert board["removed"] == 50 and board["held"] == 0
    rows = _by_posting(db_session)
    for n in range(1, 51):
        assert rows[n].listing_status == LISTING_REMOVED
        assert rows[n].listing_status in HIDDEN_LISTING_STATUSES
        assert rows[n].closed_reason == "delisted"
    assert all(rows[n].listing_status == LISTING_ACTIVE for n in range(51, 121))

    # A liveness pass whose every check answers "open" leaves them closed:
    # the sweeps only re-check rows that are still visible.
    async def all_open(client, urls, **kwargs):
        return {url: LivenessResult("alive", "gh_api_200", True) for url in urls}

    monkeypatch.setattr(listing_freshness, "check_listings", all_open)

    async def liveness_pass():
        for sweep in (listing_freshness.verify_stale_listings,
                      listing_freshness.verify_unconfirmed_active_listings,
                      listing_freshness.verify_recent_aggregator_listings):
            await sweep(db_session, None, now=NOW + datetime.timedelta(days=3))

    listing_freshness.sweep_stale(db_session, now=NOW + datetime.timedelta(days=3))
    asyncio.run(liveness_pass())
    listing_freshness.sweep_terminal_expiry(db_session, now=NOW + datetime.timedelta(days=3))
    rows = _by_posting(db_session)
    assert all(rows[n].listing_status == LISTING_REMOVED for n in range(1, 51))
    assert all(rows[n].closed_reason == "delisted" for n in range(1, 51))

    # Posting 7 is listed again: revived, its closed reason gone.
    board = crawl([7, *range(51, 121)])
    rows = _by_posting(db_session)
    assert board["revived"] == 1 and board["inserted"] == 0
    assert rows[7].listing_status == LISTING_ACTIVE and rows[7].closed_reason is None
    assert sum(row.listing_status == LISTING_REMOVED for row in rows.values()) == 49


# ─── A listing that would close most of the board ────────────────────────────

def test_a_complete_listing_that_would_close_most_of_the_board_is_held(
        db_session, crawl, caplog):
    """A source that answers short looks exactly like this, and three
    adapters were caught calling a partial listing complete: the rows it
    lists are confirmed, nothing is removed, and a warning says why."""
    crawl(range(1, 61))

    with caplog.at_level("WARNING", logger="backend.services.listing_freshness"):
        board = crawl(range(51, 61))

    assert (board["removed"], board["held"]) == (0, 50)
    rows = _by_posting(db_session)
    assert all(row.listing_status == LISTING_ACTIVE for row in rows.values())
    assert rows[55].last_seen_at > rows[5].last_seen_at  # the listed rows are confirmed
    assert any("would close 50 of 60 open rows" in r.getMessage() for r in caplog.records)


def test_a_small_drop_closes_whatever_its_share(db_session, crawl):
    """Under MASS_REMOVAL_MIN the share does not matter: a 15-posting board
    that drops 12 closes them."""
    crawl(range(1, 16))

    board = crawl(range(13, 16))

    assert (board["removed"], board["held"]) == (12, 0)


def test_the_guard_is_more_than_half_and_more_than_twenty():
    from backend.services.listing_freshness import mass_removal

    assert mass_removal(21, 41) and not mass_removal(21, 42)  # more than half
    assert not mass_removal(20, 20)  # and more than twenty
    assert mass_removal(500, 501)


def test_a_closed_row_the_board_does_not_list_is_not_read_back(db_session):
    """reconcile_board reads the board's open rows and, of its closed
    history, only what the listing names (plus Workday apply-step links)."""
    base = "https://acme.wd3.myworkdayjobs.com/External/job/T"
    for n, status in ((1, LISTING_ACTIVE), (2, LISTING_REMOVED), (3, LISTING_REMOVED),
                      (4, LISTING_EXPIRED)):
        db_session.add(ScrapedJob(title="Analyst", company="Acme", url=f"{base}/{n}",
                                  listing_status=status, board_key="workday:acme",
                                  source_platform="ats"))
    db_session.add(ScrapedJob(title="Analyst", company="Acme", url=f"{base}/5/apply",
                              listing_status=LISTING_REMOVED, board_key="workday:acme",
                              source_platform="ats"))
    db_session.commit()

    stats = listing_freshness.reconcile_board(
        db_session, "workday:acme", {f"{base}/1", f"{base}/3", f"{base}/5"}, now=NOW)

    assert stats == {"confirmed": 3, "revived": 2, "removed": 0, "off_target": 0, "held": 0}
    db_session.expire_all()
    status = {row.url.split("/T/")[1]: row.listing_status
              for row in db_session.query(ScrapedJob).all()}
    assert status == {"1": LISTING_ACTIVE, "2": LISTING_REMOVED, "3": LISTING_ACTIVE,
                      "4": LISTING_EXPIRED, "5/apply": LISTING_ACTIVE}


# ─── Every closing path stamps a reason ──────────────────────────────────────

@pytest.mark.parametrize("dead_reason, closed_reason", [
    ("http_404", "404"),
    ("workday_cxs_S22", "404"),
    ("gh_api_404", "404"),
    ("oracle_items_0", "404"),
    ("ashby_posting_null", "delisted"),
    ("sr_inactive", "closed_text"),
    ("linkedin_closed", "closed_text"),
    ("body_closed", "closed_text"),
    ("error_redirect", "redirect_home"),
    ("offsite_home_redirect", "redirect_home"),
    # An adapter's check_alive may answer in closed_reason terms already.
    ("redirect_home", "redirect_home"),
    ("closed_text", "closed_text"),
    ("posting_expired", "closed_text"),
    ("", "404"),
])
def test_closed_reason_for_a_dead_verdict(dead_reason, closed_reason):
    assert listing_freshness.closed_reason_for(dead_reason) == closed_reason


def _row(db, n, **kwargs):
    defaults = dict(title="Analyst", company="Acme", url=f"https://boards.greenhouse.io/acme/jobs/{n}",
                    source_platform="ats", board_key="greenhouse:acme",
                    listing_status=LISTING_ACTIVE, first_seen_at=NOW, last_seen_at=NOW,
                    scraped_at=NOW)
    defaults.update(kwargs)
    row = ScrapedJob(**defaults)
    db.add(row)
    db.commit()
    return row.id


def _get(db, row_id):
    db.expire_all()
    return db.get(ScrapedJob, row_id)


def test_a_dead_check_stamps_its_reason_and_a_revival_clears_it(db_session):
    row_id = _row(db_session, 1)
    listing_freshness.record_liveness(db_session, row_id, LISTING_ACTIVE,
                                      LivenessResult("dead", "offsite_home_redirect"), now=NOW)
    db_session.commit()
    row = _get(db_session, row_id)
    assert (row.listing_status, row.closed_reason) == (LISTING_REMOVED, "redirect_home")

    listing_freshness.record_liveness(db_session, row_id, LISTING_REMOVED,
                                      LivenessResult("alive", "gh_api_200", True), now=NOW)
    db_session.commit()
    row = _get(db_session, row_id)
    assert (row.listing_status, row.closed_reason) == (LISTING_ACTIVE, None)


def test_the_verify_sweeps_stamp_each_rows_reason(db_session, monkeypatch):
    gone = _row(db_session, 1, listing_status=LISTING_STALE)
    closed = _row(db_session, 2, listing_status=LISTING_STALE)
    verdicts = {f"https://boards.greenhouse.io/acme/jobs/{n}": reason
                for n, reason in ((1, "gh_api_404"), (2, "body_closed"))}

    async def dead(client, urls, **kwargs):
        return {url: LivenessResult("dead", verdicts[url], True) for url in urls}

    monkeypatch.setattr(listing_freshness, "check_listings", dead)
    stats = asyncio.run(listing_freshness.verify_stale_listings(db_session, None, now=NOW))

    assert stats["removed"] == 2
    assert _get(db_session, gone).closed_reason == "404"
    assert _get(db_session, closed).closed_reason == "closed_text"


def test_age_outs_close_with_reason_ttl(db_session):
    old = NOW - datetime.timedelta(days=60)
    linkedin = _row(db_session, 1, url="https://www.linkedin.com/jobs/view/1",
                    source_platform="linkedin", board_key="", first_seen_at=old,
                    scraped_at=old, last_seen_at=old)
    stale = _row(db_session, 2, listing_status=LISTING_STALE, first_seen_at=old,
                 last_seen_at=old, scraped_at=old, last_probed_at=NOW)

    assert listing_freshness.sweep_aggregator_expiry(db_session, now=NOW) == 1
    assert listing_freshness.sweep_terminal_expiry(db_session, now=NOW)["stale_expired"] == 1
    for row_id in (linkedin, stale):
        row = _get(db_session, row_id)
        assert (row.listing_status, row.closed_reason) == (LISTING_EXPIRED, "ttl")
