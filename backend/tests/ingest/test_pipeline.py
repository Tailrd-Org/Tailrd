"""The shared write path (ingest/core/pipeline.py): one board snapshot in, the
catalogue brought in line with it, inside the storage caps of
ingest/data/limits.json."""

import asyncio
import datetime
import json
import time
import types

import pytest

from backend.db.models import Company, ScrapedJob, SourceHealth
from backend.ingest import adapters, taxonomy
from backend.ingest.core import pipeline
from backend.services import ats_scraper, listing_freshness
from backend.services.ats_scraper import ATSJob, BoardSnapshot

NOW = datetime.datetime.utcnow()


def _job(n, *, title=None, location="Toronto, ON", posted=None, company="Acme",
         description="Build and ship backend services.", **kwargs):
    return ATSJob(title=title or f"Software Engineer {n}", company=company, location=location,
                  url=f"https://boards.greenhouse.io/acme/jobs/{n}", posted_date=posted,
                  description=description, external_id=str(n), **kwargs)


def _snapshot(jobs, *, platform="greenhouse", slug="acme", complete=True, listed=None):
    jobs = list(jobs)
    urls = {job.url for job in jobs} if listed is None else set(listed)
    return BoardSnapshot(platform=platform, slug=slug, company="Acme", jobs=jobs,
                         all_urls=urls, complete=complete, total_listed=len(urls))


def _ctx(db, **limits):
    return pipeline.new_context(db, ["Acme"], limits=pipeline.Limits(**limits))


def _ingest(db, snapshot, ctx, client=None):
    return asyncio.run(pipeline.ingest_snapshot(db, client, snapshot, ctx))


def _rows(db):
    db.expire_all()
    return db.query(ScrapedJob).order_by(ScrapedJob.id).all()


# ─── Idempotency and the new columns ─────────────────────────────────────────

def test_a_second_ingest_of_the_same_snapshot_adds_nothing(db_session):
    jobs = lambda: [_job(n, posted=NOW - datetime.timedelta(days=n)) for n in range(1, 6)]
    first = _ingest(db_session, _snapshot(jobs()), _ctx(db_session))
    assert first["inserted"] == 5 and len(_rows(db_session)) == 5

    ctx = _ctx(db_session)
    second = _ingest(db_session, _snapshot(jobs()), ctx)

    assert (second["new"], second["inserted"], second["known"]) == (0, 0, 5)
    assert ctx.totals["edited"] == 0 and ctx.totals["relabeled"] == 0
    assert len(_rows(db_session)) == 5


def test_new_rows_carry_the_v2_fields(db_session, monkeypatch):
    seen = {}

    def fake_years(text):
        return 6 if "6+ years" in text else None

    def fake_level(title, department="", employment_type="", years_required=None):
        seen["years"] = years_required
        return "senior" if (years_required or 0) >= 5 else "mid"

    monkeypatch.setattr(taxonomy, "years_required", fake_years)
    monkeypatch.setattr(taxonomy, "classify_experience", fake_level)
    monkeypatch.setattr(taxonomy, "classify_family",
                        lambda title, department="", description="": ("software_engineering",
                                                                      "data_ai_analytics"))
    db_session.add(Company(name="Acme", ats="greenhouse", ats_token="acme"))
    db_session.commit()
    company_id = db_session.query(Company.id).scalar()
    job = _job(1, title="Platform Engineer", description="You bring 6+ years of Go.",
               apply_url="https://acme.example/careers/1")

    _ingest(db_session, _snapshot([job]), _ctx(db_session))

    row = _rows(db_session)[0]
    assert seen["years"] == 6
    assert (row.experience_level, row.experience_years_required) == ("senior", 6)
    assert (row.career_family, row.career_family_secondary) == ("software_engineering",
                                                                "data_ai_analytics")
    assert row.enrichment_version == taxonomy.ENRICHMENT_VERSION
    assert row.role_category  # role_classifier keeps its 17 categories
    assert row.company_id == company_id
    assert row.apply_url == "https://acme.example/careers/1"
    assert row.closed_reason is None
    assert row.fingerprint == pipeline.fingerprint(
        row.company_domain, row.company, row.title, row.locations_json, row.location,
        row.employment_type)
    assert len(row.fingerprint) == 64


def test_an_apply_url_is_stored_only_as_an_http_page(db_session, fake_adapter):
    """Jibe keeps whatever its JSON says. The employer's apply link may live
    on another host (a Jibe site's is its iCIMS portal), never another scheme."""
    jobs = [_job(1, apply_url="javascript:alert(document.cookie)"),
            _job(2, apply_url="https://careers-acme.icims.com/jobs/2/login"),
            _job(3, apply_url="data:text/html,<script>alert(1)</script>")]

    _ingest(db_session, _snapshot(jobs), _ctx(db_session))
    detail = _adapter_job(4)
    fake_adapter.fetch_job_apply_url = "javascript:alert(2)"
    _ingest(db_session, _snapshot([detail], platform="fakeats"), _ctx(db_session))

    assert {row.url: row.apply_url for row in _rows(db_session)} == {
        jobs[0].url: None, jobs[1].url: "https://careers-acme.icims.com/jobs/2/login",
        jobs[2].url: None, detail.url: None}


def test_every_level_is_stored(db_session):
    jobs = [_job(1, title="Software Engineering Intern"), _job(2, title="Senior Staff Engineer"),
            _job(3, title="Director of Engineering")]

    _ingest(db_session, _snapshot(jobs), _ctx(db_session))

    assert {row.title: row.experience_level for row in _rows(db_session)} == {
        job.title: taxonomy.classify_experience(job.title) for job in jobs}


def test_the_fingerprint_is_a_match_key_not_an_identity():
    toronto = [{"city": "toronto", "region": "ON", "country": "CA"}]
    a = pipeline.fingerprint("www.acme.com", "Acme", "Software Engineer (2027)", toronto,
                             "Toronto, ON", "full_time")
    b = pipeline.fingerprint("acme.com", "Acme Inc.", "Software Engineer", toronto,
                             "Toronto, Ontario", "full_time")
    assert a == b  # one posting, two spellings
    assert a != pipeline.fingerprint("acme.com", "Acme", "Software Engineer", toronto,
                                     "Toronto, ON", "internship")
    assert a != pipeline.fingerprint("acme.com", "Acme", "Software Engineer",
                                     [{"city": "ottawa", "region": "ON", "country": "CA"}],
                                     "Ottawa, ON", "full_time")
    # No domain: the folded employer name stands in.
    assert pipeline.fingerprint("", "Acme Inc.", "Analyst", [], "Remote", "") == \
        pipeline.fingerprint("", "acme", "Analyst", [], "remote", "")


# ─── The caps ────────────────────────────────────────────────────────────────

def test_limits_json_names_every_cap():
    with open(pipeline.LIMITS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    limits = pipeline.load_limits()
    for name in pipeline.Limits.__dataclass_fields__:
        assert isinstance(raw[name], (int, float)) and raw[name] > 0, name
        assert getattr(limits, name) == raw[name]


def test_an_unreadable_limits_file_fails_closed(tmp_path):
    broken = tmp_path / "limits.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        pipeline.load_limits(str(broken))


def test_new_jobs_go_in_newest_first_up_to_the_run_cap(db_session):
    jobs = [_job(1, posted=NOW - datetime.timedelta(days=9)), _job(2),
            _job(3, posted=NOW - datetime.timedelta(days=1)),
            _job(4, posted=NOW - datetime.timedelta(days=3))]
    ctx = _ctx(db_session, max_new_rows_per_run=2)

    board = _ingest(db_session, _snapshot(jobs), ctx)

    assert board["inserted"] == 2
    assert [row.url for row in _rows(db_session)] == [jobs[2].url, jobs[3].url]
    assert ctx.caps["run_cap"] == 2

    # The run is spent: the next board stores nothing at all.
    later = [_job(n) for n in (7, 8)]
    for job in later:
        job.url = job.url.replace("/acme/", "/other/")
    _ingest(db_session, _snapshot(later, slug="other"), ctx)
    assert len(_rows(db_session)) == 2 and ctx.caps["run_cap"] == 4


def test_postings_already_old_when_first_seen_are_skipped(db_session):
    old = _job(1, posted=NOW - datetime.timedelta(days=200))
    fresh = _job(2, posted=NOW - datetime.timedelta(days=3))
    undated = _job(3)
    ctx = _ctx(db_session, skip_posted_older_than_days=90)

    _ingest(db_session, _snapshot([old, fresh, undated]), ctx)

    assert {row.url for row in _rows(db_session)} == {fresh.url, undated.url}
    assert ctx.caps["too_old"] == 1


def test_an_employer_at_its_cap_of_live_rows_inserts_no_more(db_session):
    for n, status in ((90, "active"), (91, "stale"), (92, "removed")):
        db_session.add(ScrapedJob(title="Old", company="Acme", url=f"https://x/{n}",
                                  listing_status=status, board_key="lever:acme",
                                  source_platform="ats"))
    db_session.commit()
    ctx = _ctx(db_session, max_live_per_company=3)

    board = _ingest(db_session, _snapshot([_job(n) for n in range(1, 5)]), ctx)

    assert board["inserted"] == 1  # 2 live (the removed row holds no place) + 1
    assert ctx.caps["company_cap"] == 3
    assert ctx.live_counts["Acme"] == 3


def _live_rows(db, count, *, source="ats", level="mid"):
    """``count`` live rows of Acme on another of its boards (or another source)."""
    for n in range(count):
        db.add(ScrapedJob(title=f"Engineer {n}", company="Acme", url=f"https://x/{source}/{n}",
                          listing_status="active", board_key="lever:acme",
                          source_platform=source, experience_level=level))
    db.commit()


def test_a_student_posting_is_stored_at_an_employer_at_its_cap(db_session):
    """Spec decision 1: the internship and new-grad feed must not shrink. The
    cap holds back the employer's other postings, never a student one, and a
    student row takes no place."""
    _live_rows(db_session, 3)
    ctx = _ctx(db_session, max_live_per_company=3)
    senior = _job(1, title="Senior Software Engineer", posted=NOW)
    intern = _job(2, title="Software Engineering Intern (Summer 2027)",
                  posted=NOW - datetime.timedelta(days=30))

    board = _ingest(db_session, _snapshot([senior, intern]), ctx)

    stored = {row.url for row in _rows(db_session)}
    assert board["inserted"] == 1 and ctx.caps["company_cap"] == 1
    assert intern.url in stored and senior.url not in stored
    assert ctx.live_counts["Acme"] == 3


def test_student_rows_already_stored_take_no_place_either(db_session):
    _live_rows(db_session, 3, level="internship")
    ctx = _ctx(db_session, max_live_per_company=3)

    board = _ingest(db_session, _snapshot([_job(1), _job(2)]), ctx)

    assert board["inserted"] == 2 and ctx.caps["company_cap"] == 0


def test_aggregator_rows_of_the_same_name_do_not_fill_a_boards_cap(db_session):
    """LinkedIn, Indeed and GitHub-list rows are not the board's to count."""
    _live_rows(db_session, 3, source="linkedin", level="")

    board = _ingest(db_session, _snapshot([_job(n) for n in range(1, 4)]),
                    _ctx(db_session, max_live_per_company=3))

    assert board["inserted"] == 3


def test_student_postings_go_in_first_under_the_run_cap(db_session):
    newer = _job(1, title="Software Engineer", posted=NOW)
    older_intern = _job(2, title="Software Engineering Intern",
                        posted=NOW - datetime.timedelta(days=20))
    ctx = _ctx(db_session, max_new_rows_per_run=1)

    _ingest(db_session, _snapshot([newer, older_intern]), ctx)

    assert [row.url for row in _rows(db_session)] == [older_intern.url]
    assert ctx.caps["run_cap"] == 1


def test_a_held_mass_removal_is_counted_and_frees_no_places(db_session):
    _ingest(db_session, _snapshot([_job(n) for n in range(30)]), _ctx(db_session))
    ctx = _ctx(db_session, max_live_per_company=30)

    board = _ingest(db_session, _snapshot([_job(n) for n in range(3)] + [_job(99)]), ctx)

    assert (board["removed"], board["held"], board["inserted"]) == (0, 27, 0)
    assert ctx.totals["removals_held"] == 27 and ctx.caps["company_cap"] == 1


def test_reconciliation_frees_places_before_the_cap_is_counted(db_session):
    ctx = _ctx(db_session, max_live_per_company=2)
    _ingest(db_session, _snapshot([_job(1), _job(2)]), ctx)

    # The board dropped both postings and lists two new ones.
    ctx = _ctx(db_session, max_live_per_company=2)
    board = _ingest(db_session, _snapshot([_job(3), _job(4)]), ctx)

    assert board["removed"] == 2 and board["inserted"] == 2


def test_inserting_stops_past_the_storage_size_but_reconciling_goes_on(db_session, monkeypatch):
    _ingest(db_session, _snapshot([_job(1)]), _ctx(db_session))
    sizes = []

    def full(db):
        sizes.append(1)
        return 500.0

    monkeypatch.setattr(pipeline, "database_size_mb", full)
    ctx = _ctx(db_session, stop_inserting_at_mb=430)

    board = _ingest(db_session, _snapshot([_job(2), _job(3)]), ctx)

    assert board["inserted"] == 0 and board["removed"] == 1
    assert ctx.stopped == "storage" and ctx.caps["storage"] == 2
    assert sizes == [1]  # read once; the run then stays stopped
    assert listing_freshness.LISTING_REMOVED == _rows(db_session)[0].listing_status


def test_the_database_size_is_read_every_size_check_rows(db_session, monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "SIZE_CHECK_EVERY", 2)
    monkeypatch.setattr(pipeline, "database_size_mb", lambda db: calls.append(1) or 100.0)
    ctx = _ctx(db_session, stop_inserting_at_mb=430)

    _ingest(db_session, _snapshot([_job(n) for n in range(1, 6)]), ctx)

    assert ctx.inserted == 5 and len(calls) == 3  # before rows 1, 3 and 5


def test_nothing_is_inserted_past_the_insert_deadline(db_session):
    ctx = _ctx(db_session)
    ctx.insert_deadline = time.monotonic() - 1

    board = _ingest(db_session, _snapshot([_job(1), _job(2)]), ctx)

    assert board["inserted"] == 0 and ctx.stopped == "deadline"
    assert ctx.caps["deadline"] == 2


def test_the_stored_description_is_cut_but_hashed_whole(db_session):
    text = "Python. " * 40  # 320 characters
    ctx = _ctx(db_session, description_max_chars=50)
    _ingest(db_session, _snapshot([_job(1, description=text)]), ctx)

    row = _rows(db_session)[0]
    assert len(row.description) == 50 and ctx.caps["descriptions_cut"] == 1
    whole = listing_freshness.sanitize_description(text.strip())
    assert row.raw_hash == listing_freshness.compute_raw_hash(row.title, row.location, whole, "")

    # Re-listed unchanged: no edit, and the stored copy stays cut.
    ctx = _ctx(db_session, description_max_chars=50)
    _ingest(db_session, _snapshot([_job(1, description=text)]), ctx)
    row = _rows(db_session)[0]
    assert ctx.totals["edited"] == 0 and len(row.description) == 50


# ─── Detail requests ─────────────────────────────────────────────────────────

def test_detail_requests_stay_within_the_board_budget(db_session, monkeypatch):
    fetched = []

    async def fake_detail(client, slug, detail_ref):
        fetched.append(detail_ref)
        return {"description": "Ship things.", "employment_type": "Full time"}

    monkeypatch.setattr(ats_scraper, "fetch_workday_detail", fake_detail)
    jobs = [ATSJob(title=f"Analyst {n}", company="Acme", location="Toronto, ON",
                   url=f"https://acme.wd3.myworkdayjobs.com/External/job/T/{n}",
                   detail_ref=f"/job/T/{n}") for n in range(5)]
    ctx = pipeline.new_context(db_session, ["Acme"], limits=pipeline.Limits(),
                               detail_per_board=2, detail_concurrency=2)

    board = _ingest(db_session, _snapshot(jobs, platform="workday"), ctx)

    assert board["inserted"] == 2 and len(fetched) == 2
    assert ctx.caps["detail_budget"] == 3
    assert {row.description for row in _rows(db_session)} == {"Ship things."}


def test_student_postings_get_the_detail_budget_first(db_session, monkeypatch):
    fetched = []

    async def fake_detail(client, slug, detail_ref):
        fetched.append(detail_ref)
        return {"description": "Ship things.", "employment_type": "Full time"}

    monkeypatch.setattr(ats_scraper, "fetch_workday_detail", fake_detail)
    base = "https://acme.wd3.myworkdayjobs.com/External/job/T"
    newer = ATSJob(title="Software Engineer", company="Acme", location="Toronto, ON",
                   url=f"{base}/new", detail_ref="/job/T/new", posted_date=NOW)
    older_intern = ATSJob(title="Software Engineering Intern", company="Acme",
                          location="Toronto, ON", url=f"{base}/intern", detail_ref="/job/T/intern",
                          posted_date=NOW - datetime.timedelta(days=20))
    ctx = pipeline.new_context(db_session, ["Acme"], limits=pipeline.Limits(), detail_per_board=1)

    _ingest(db_session, _snapshot([newer, older_intern], platform="workday"), ctx)

    assert fetched == ["/job/T/intern"]
    assert [row.url for row in _rows(db_session)] == [older_intern.url]


@pytest.fixture
def fake_adapter(monkeypatch):
    """A v2 adapter whose listings carry no description: fetch_job answers."""
    module = types.ModuleType("fake_adapter")
    module.PLATFORM, module.REQUIRED_ENV = "fakeats", ()
    module.asked = []
    module.fetch_job_apply_url = None  # a test sets what the detail page says

    async def list_jobs(client, slug, company_name, *, config, deadline=None):
        return [], True, 0

    async def fetch_job(client, slug, ref, *, config):
        module.asked.append((slug, ref))
        return {"description": "From the detail page.", "employment_type": "Contract",
                "apply_url": module.fetch_job_apply_url or f"https://acme.example/apply/{ref}"}

    module.list_jobs, module.fetch_job = list_jobs, fetch_job
    monkeypatch.setattr(adapters, "_registry", {"fakeats": module})
    return module


def _adapter_job(n):
    return ATSJob(title=f"Nurse {n}", company="Acme", location="Toronto, ON",
                  url=f"https://jobs.fakeats.test/acme/{n}", detail_ref=str(n),
                  external_id=f"fakeats:acme:{n}")


def test_an_adapter_without_descriptions_is_asked_through_fetch_job(db_session, fake_adapter):
    _ingest(db_session, _snapshot([_adapter_job(1)], platform="fakeats"), _ctx(db_session))

    row = _rows(db_session)[0]
    assert fake_adapter.asked == [("acme", "1")]
    assert row.description == "From the detail page." and row.employment_type == "contract"
    assert row.apply_url == "https://acme.example/apply/1"


def test_a_capped_employer_costs_no_detail_requests(db_session, fake_adapter):
    # Its one live row sits on another of its boards.
    db_session.add(ScrapedJob(title="Old", company="Acme", url="https://x/1",
                              listing_status="active", board_key="lever:acme",
                              source_platform="ats"))
    db_session.commit()
    ctx = _ctx(db_session, max_live_per_company=1)

    board = _ingest(db_session, _snapshot([_adapter_job(n) for n in range(3)],
                                          platform="fakeats"), ctx)

    assert board["inserted"] == 0 and fake_adapter.asked == []
    assert ctx.caps["company_cap"] == 3


def test_external_ids_carry_the_board_key_once(db_session, fake_adapter):
    """The built-in fetchers give the source's bare id; an adapter's id is
    already "{platform}:{slug}:{id}" and is stored as it is."""
    _ingest(db_session, _snapshot([_job(4285367)]), _ctx(db_session))
    _ingest(db_session, _snapshot([_adapter_job(7)], platform="fakeats"), _ctx(db_session))

    assert [row.external_id for row in _rows(db_session)] == [
        "greenhouse:acme:4285367", "fakeats:acme:7"]

    # A row stored without one gets it from its next crawl, the same way.
    for row in _rows(db_session):
        row.external_id = ""
    db_session.commit()
    _ingest(db_session, _snapshot([_job(4285367)]), _ctx(db_session))
    _ingest(db_session, _snapshot([_adapter_job(7)], platform="fakeats"), _ctx(db_session))
    assert [row.external_id for row in _rows(db_session)] == [
        "greenhouse:acme:4285367", "fakeats:acme:7"]


# ─── Source health ───────────────────────────────────────────────────────────

def _health(db, key="greenhouse:acme"):
    db.expire_all()
    return db.query(SourceHealth).filter(SourceHealth.board_key == key).one()


def test_a_board_keeps_a_seven_day_yield_average_and_warns_on_a_drop(db_session):
    jobs = [_job(n) for n in range(20)]
    ctx = _ctx(db_session)
    assert pipeline.record_board_success(db_session, _snapshot(jobs), ctx, now=NOW) is None
    assert _health(db_session).jobs_7d_avg == 20

    # A day later the board lists 5: a 75% drop.
    warning = pipeline.record_board_success(db_session, _snapshot(jobs[:5]), ctx,
                                            now=NOW + datetime.timedelta(days=1))
    health = _health(db_session)
    assert warning and "yield warning" in health.last_error
    assert ctx.warnings[0]["board"] == "greenhouse:acme"
    assert 5 < health.jobs_7d_avg < 20

    # A partial listing's count moves nothing.
    before = health.jobs_7d_avg
    pipeline.record_board_success(db_session, _snapshot(jobs[:1], complete=False), ctx,
                                  now=NOW + datetime.timedelta(days=2))
    health = _health(db_session)
    assert health.jobs_7d_avg == before and health.last_error == ""


def test_small_boards_do_not_raise_yield_warnings(db_session):
    ctx = _ctx(db_session)
    pipeline.record_board_success(db_session, _snapshot([_job(1), _job(2)]), ctx, now=NOW)
    assert pipeline.record_board_success(db_session, _snapshot([]), ctx,
                                         now=NOW + datetime.timedelta(days=1)) is None


def test_the_breaker_stamps_disabled_until_when_it_opens(db_session):
    for attempt in range(5):
        opened = pipeline.record_board_failure(db_session, "lever:acme", "lever", "acme",
                                               "HTTP 404", now=NOW)
        assert opened == (attempt == 4)
    health = _health(db_session, "lever:acme")
    assert health.disabled_until == NOW + datetime.timedelta(hours=24)
    assert pipeline.in_cooldown(health, NOW + datetime.timedelta(hours=1))
    assert not pipeline.in_cooldown(health, NOW + datetime.timedelta(hours=25))


def test_a_board_parked_by_hand_stays_parked(db_session):
    parked = SourceHealth(board_key="lever:acme", platform="lever", slug="acme",
                          consecutive_failures=0,
                          disabled_until=NOW + datetime.timedelta(days=30))
    assert pipeline.in_cooldown(parked, NOW)
    assert not pipeline.in_cooldown(None, NOW)
