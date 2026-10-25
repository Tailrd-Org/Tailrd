"""Cross-source dedup: LinkedIn/Indeed twins collapse into direct ATS rows."""

from backend.db.models import ScrapedJob
from backend.services.cross_source_dedup import (
    absorb_new_aggregator_rows,
    canonical_url,
    dedup_sweep,
    has_direct_twin,
    mark_inferior_twins,
    normalize_title,
)
from backend.services.location_parser import location_fields


def _mk(db_session, url, *, source="linkedin", title="Software Engineer Intern",
        company="Kinaxis", domain="kinaxis.com", location="Ottawa, ON, CA",
        description="", applicant_count=None, salary_range=""):
    row = ScrapedJob(
        title=title, company=company, url=url, location=location,
        description=description, country="CA", work_type="onsite",
        source_platform=source, experience_level="internship", easy_apply=0,
        match_score=0, company_domain=domain, salary_range=salary_range,
        applicant_count=applicant_count, title_norm=normalize_title(title),
        **location_fields(location),
    )
    db_session.add(row)
    db_session.commit()
    return row


# --- normalize_title -------------------------------------------------------

def test_normalize_title_strips_season_year_and_parentheticals():
    assert normalize_title("Software Engineer Intern (Summer 2026)") == "software engineer intern"
    assert normalize_title("Software Engineer Intern - Summer 2026") == "software engineer intern"
    assert normalize_title("Software Engineer Intern") == "software engineer intern"


def test_normalize_title_keeps_role_level_words():
    assert normalize_title("Software Engineer Intern") != normalize_title("Software Engineer, New Grad")
    assert normalize_title("Software Engineer Intern, Infrastructure") != normalize_title(
        "Software Engineer Intern"
    )


# --- has_direct_twin (ingest-batch guard) ----------------------------------

def test_has_direct_twin_finds_ats_row(db_session):
    _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/1", source="ats",
        description="Long real description " * 10)
    assert has_direct_twin(
        db_session, company="Kinaxis", company_domain="kinaxis.com",
        title="Software Engineer Intern (Summer 2026)", city="ottawa",
    )


def test_has_direct_twin_respects_city(db_session):
    _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/2", source="ats",
        location="Toronto, ON, CA")
    assert not has_direct_twin(
        db_session, company="Kinaxis", company_domain="kinaxis.com",
        title="Software Engineer Intern", city="ottawa",
    )


def test_has_direct_twin_multi_city_ats_absorbs(db_session):
    _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/3", source="ats",
        location="Ottawa,Ontario,Canada; Toronto,Ontario,Canada")
    assert has_direct_twin(
        db_session, company="Kinaxis", company_domain="kinaxis.com",
        title="Software Engineer Intern", city="toronto",
    )


def test_has_direct_twin_different_title_no_match(db_session):
    _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/4", source="ats")
    assert not has_direct_twin(
        db_session, company="Kinaxis", company_domain="kinaxis.com",
        title="Data Analyst Intern", city="ottawa",
    )


# --- mark_inferior_twins (cron-ats / aggregator hook) -----------------------

def test_mark_inferior_twins_hides_linkedin_and_enriches_winner(db_session):
    twin = _mk(db_session, "https://linkedin.com/jobs/view/1", source="linkedin",
               applicant_count=57, salary_range="$40-50/hr")
    winner = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/5",
                 source="ats", description="Full description " * 20)
    marked = mark_inferior_twins(db_session, winner)
    assert marked == 1
    db_session.refresh(twin)
    db_session.refresh(winner)
    assert twin.duplicate_of == winner.id
    assert winner.applicant_count == 57
    assert winner.salary_range == "$40-50/hr"


def test_mark_inferior_twins_ignores_other_cities_and_titles(db_session):
    other_city = _mk(db_session, "https://linkedin.com/jobs/view/2",
                     source="linkedin", location="Calgary, AB, CA")
    other_title = _mk(db_session, "https://linkedin.com/jobs/view/3",
                      source="linkedin", title="Data Analyst Intern")
    winner = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/6",
                 source="ats")
    assert mark_inferior_twins(db_session, winner) == 0
    db_session.refresh(other_city)
    db_session.refresh(other_title)
    assert other_city.duplicate_of is None
    assert other_title.duplicate_of is None


# --- dedup_sweep (one-time script core) -------------------------------------

def test_sweep_collapses_pairs_and_copies_description(db_session):
    loser = _mk(db_session, "https://linkedin.com/jobs/view/4", source="linkedin",
                description="A LinkedIn description long enough to be real content " * 10,
                applicant_count=12)
    winner = _mk(db_session, "https://github.example/direct/1", source="github",
                 description="")
    stats = dedup_sweep(db_session)
    db_session.refresh(loser)
    db_session.refresh(winner)
    assert loser.duplicate_of == winner.id
    assert winner.duplicate_of is None
    assert len(winner.description or "") > 50  # copied from the loser
    assert winner.applicant_count == 12
    assert stats["marked"] == 1


def test_sweep_multi_city_ats_absorbs_two_city_twins(db_session):
    ott = _mk(db_session, "https://linkedin.com/jobs/view/5", source="linkedin",
              location="Ottawa, ON, CA")
    tor = _mk(db_session, "https://linkedin.com/jobs/view/6", source="linkedin",
              location="Toronto, ON, CA")
    winner = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/7",
                 source="ats", description="Real description " * 10,
                 location="Ottawa,Ontario,Canada; Toronto,Ontario,Canada")
    stats = dedup_sweep(db_session)
    db_session.refresh(ott)
    db_session.refresh(tor)
    assert ott.duplicate_of == winner.id
    assert tor.duplicate_of == winner.id
    assert stats["marked"] == 2


def test_sweep_never_merges_across_cities_or_titles(db_session):
    a = _mk(db_session, "https://linkedin.com/jobs/view/7", source="linkedin",
            location="Ottawa, ON, CA")
    b = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/8",
            source="ats", location="Calgary, AB, CA")
    c = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/9",
            source="ats", title="Data Analyst Intern")
    dedup_sweep(db_session)
    for row in (a, b, c):
        db_session.refresh(row)
        assert row.duplicate_of is None


def test_sweep_is_idempotent(db_session):
    _mk(db_session, "https://linkedin.com/jobs/view/8", source="linkedin")
    _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/10", source="ats",
        description="Real description " * 10)
    first = dedup_sweep(db_session)
    second = dedup_sweep(db_session)
    assert first["marked"] == 1
    assert second["marked"] == 0


def test_sweep_falls_back_to_company_name_without_domains(db_session):
    loser = _mk(db_session, "https://linkedin.com/jobs/view/9", source="linkedin",
                company="Acme Widgets Inc.", domain="")
    winner = _mk(db_session, "https://boards.greenhouse.io/acme/jobs/1",
                 source="ats", company="Acme Widgets", domain="",
                 description="Real description " * 10)
    dedup_sweep(db_session)
    db_session.refresh(loser)
    assert loser.duplicate_of == winner.id


def test_sweep_never_merges_two_direct_rows(db_session):
    # Same employer, identical title, same city, DIFFERENT requisition ids:
    # distinct postings (per-team/per-country reqs). Both must survive.
    a = _mk(db_session, "https://boards.greenhouse.io/affirm/jobs/7724915003",
            source="ats", company="Affirm", domain="affirm.com",
            description="Req A " * 20)
    b = _mk(db_session, "https://boards.greenhouse.io/affirm/jobs/7724917003",
            source="ats", company="Affirm", domain="affirm.com",
            description="Req B " * 20)
    dedup_sweep(db_session)
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.duplicate_of is None
    assert b.duplicate_of is None


def test_sweep_uses_url_not_label_for_rogue_rows(db_session):
    # Rogue-scraper rows are labeled source='ats' but carry LinkedIn URLs,
    # they are aggregator copies and must be absorbed by the real direct row.
    rogue = _mk(db_session, "https://www.linkedin.com/jobs/view/4414123646",
                source="ats", description="")
    winner = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/11",
                 source="ats", description="Real description " * 10)
    dedup_sweep(db_session)
    db_session.refresh(rogue)
    db_session.refresh(winner)
    assert rogue.duplicate_of == winner.id
    assert winner.duplicate_of is None


def test_sweep_linkedin_reposts_collapse_together(db_session):
    # The same posting spammed under many LinkedIn view ids collapses to one.
    first = _mk(db_session, "https://linkedin.com/jobs/view/201", source="linkedin",
                description="Snippet " * 60)
    second = _mk(db_session, "https://linkedin.com/jobs/view/202", source="linkedin",
                 description="")
    dedup_sweep(db_session)
    db_session.refresh(first)
    db_session.refresh(second)
    assert second.duplicate_of == first.id
    assert first.duplicate_of is None


def test_canonical_url_strips_only_tracking_params():
    assert canonical_url("https://www.workatastartup.com/jobs/86588?utm_source=vansh") == \
        "https://www.workatastartup.com/jobs/86588"
    assert canonical_url("https://x.test/jobs/1") == "https://x.test/jobs/1"
    # Functional query params survive.
    assert "gh_jid=123" in canonical_url("https://acme.com/careers?gh_jid=123&utm_source=list")
    assert "utm_source" not in canonical_url("https://acme.com/careers?gh_jid=123&utm_source=list")
    assert canonical_url("") == ""


def test_sweep_collapses_identical_canonical_urls_across_direct_rows(db_session):
    # Two GitHub-list rows for the SAME posting, differing only by utm junk.
    keeper = _mk(db_session, "https://www.tesla.com/careers/search/job/253464",
                 source="github", description="Real description " * 20)
    tracked = _mk(db_session,
                  "https://www.tesla.com/careers/search/job/253464?utm_source=vansh",
                  source="github", description="")
    stats = dedup_sweep(db_session)
    db_session.refresh(keeper)
    db_session.refresh(tracked)
    assert tracked.duplicate_of == keeper.id
    assert keeper.duplicate_of is None
    assert stats["url_twins_marked"] == 1


def test_sweep_different_requisitions_same_host_not_url_twins(db_session):
    a = _mk(db_session, "https://www.workatastartup.com/jobs/86588",
            source="github", title="Founding Engineer", description="A " * 60)
    b = _mk(db_session, "https://www.workatastartup.com/jobs/84154",
            source="github", title="Founding Engineer", description="B " * 60)
    dedup_sweep(db_session)
    db_session.refresh(a)
    db_session.refresh(b)
    assert a.duplicate_of is None
    assert b.duplicate_of is None


def test_absorb_new_indeed_row_into_described_linkedin_twin(db_session):
    # The Arcadis case: an Indeed row with no description arrives after the
    # sweep; a LinkedIn twin with a full description already exists.
    linkedin = _mk(db_session, "https://ca.linkedin.com/jobs/view/arc-1",
                   source="linkedin", company="Arcadis", domain="arcadis.com",
                   title="Disaster and Climate Resilience Co-op",
                   location="Vancouver, BC, CA",
                   description="Arcadis is the world leading company " * 20)
    indeed = _mk(db_session, "https://ca.indeed.com/viewjob?jk=arc1",
                 source="indeed", company="Arcadis", domain="arcadis.com",
                 title="Disaster and Climate Resilience Co-op",
                 location="Vancouver, BC, CA", description="")
    marked = absorb_new_aggregator_rows(db_session)
    assert marked == 1
    db_session.refresh(indeed)
    db_session.refresh(linkedin)
    assert indeed.duplicate_of == linkedin.id
    assert linkedin.duplicate_of is None


def test_absorb_never_marks_direct_rows_or_unrelated(db_session):
    direct = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/absorb-1",
                 source="ats", description="")
    other_city = _mk(db_session, "https://ca.indeed.com/viewjob?jk=absorb2",
                     source="indeed", location="Calgary, AB, CA", description="")
    _mk(db_session, "https://ca.linkedin.com/jobs/view/absorb-3",
        source="linkedin", location="Ottawa, ON, CA",
        description="Full description " * 30)
    absorb_new_aggregator_rows(db_session)
    db_session.refresh(direct)
    db_session.refresh(other_city)
    assert direct.duplicate_of is None
    assert other_city.duplicate_of is None


def test_absorb_flattens_chains(db_session):
    oldest = _mk(db_session, "https://ca.linkedin.com/jobs/view/chain-1",
                 source="linkedin", description="Desc " * 100)
    middle = _mk(db_session, "https://ca.linkedin.com/jobs/view/chain-2",
                 source="linkedin", description="Desc " * 100)
    newest = _mk(db_session, "https://ca.indeed.com/viewjob?jk=chain3",
                 source="indeed", description="")
    absorb_new_aggregator_rows(db_session)
    db_session.refresh(oldest)
    db_session.refresh(middle)
    db_session.refresh(newest)
    assert oldest.duplicate_of is None
    for row in (middle, newest):
        assert row.duplicate_of == oldest.id, "chains must be flattened"


def test_sweep_remote_rows_require_same_country(db_session):
    us = _mk(db_session, "https://linkedin.com/jobs/view/203", source="linkedin",
             location="Remote")
    ca_direct = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/12",
                    source="ats", location="Remote",
                    description="Real description " * 10)
    # Force differing countries with empty cities ("Remote" parses city=Remote,
    # so blank the parsed fields to simulate truly unlocated rows).
    us.city = ""
    us.location_search = ""
    us.country = "US"
    ca_direct.city = ""
    ca_direct.location_search = ""
    ca_direct.country = "CA"
    db_session.commit()
    dedup_sweep(db_session)
    db_session.refresh(us)
    assert us.duplicate_of is None


# --- fuzzy title fallback ----------------------------------------------------

def test_titles_fuzzy_match_accepts_punctuation_noise():
    from backend.services.cross_source_dedup import titles_fuzzy_match
    assert titles_fuzzy_match(
        "software engineer intern payments",
        "software engineer intern payments team",
    ) is False  # extra qualifier word = different posting, length gap guard
    assert titles_fuzzy_match(
        "software engineer intern paymentss",
        "software engineer intern payments",
    ) is True  # single-character noise


def test_titles_fuzzy_match_rejects_short_and_different():
    from backend.services.cross_source_dedup import titles_fuzzy_match
    assert titles_fuzzy_match("intern", "intern") is True
    assert titles_fuzzy_match("qa intern", "ml intern") is False
    assert titles_fuzzy_match(
        "software engineer intern", "data engineer intern",
    ) is False


def test_absorb_fuzzy_falls_back_to_near_identical_direct_title(db_session):
    winner = _mk(
        db_session, "https://boards.greenhouse.io/kinaxis/jobs/50", source="ats",
        title="Software Developer Intern, Analytics",
        description="Full description " * 20,
    )
    # Aggregator copy with one-character title noise -> different title_norm.
    loser = _mk(
        db_session, "https://linkedin.com/jobs/view/50", source="linkedin",
        title="Software Developer Intern, Analytic",
    )
    assert loser.title_norm != winner.title_norm

    absorbed = absorb_new_aggregator_rows(db_session)
    db_session.refresh(loser)
    assert absorbed == 1
    assert loser.duplicate_of == winner.id


def test_absorb_fuzzy_never_merges_actually_different_jobs(db_session):
    winner = _mk(
        db_session, "https://boards.greenhouse.io/kinaxis/jobs/51", source="ats",
        title="Software Engineer Intern, Infrastructure",
        description="Full description " * 20,
    )
    different = _mk(
        db_session, "https://linkedin.com/jobs/view/51", source="linkedin",
        title="Software Engineer Intern",
    )
    absorb_new_aggregator_rows(db_session)
    db_session.refresh(different)
    assert different.duplicate_of is None


# --- logo inheritance ---------------------------------------------------------

_S2 = "https://www.google.com/s2/favicons?domain=kinaxis.com&sz=256"
_LICDN = ("https://media.licdn.com/dms/image/v2/C4D0BAQE/company-logo_100_100/"
          "company-logo_100_100/0/1/kinaxis_logo?e=2147483647&v=beta&t=abc")


def test_mark_inferior_twins_hands_the_real_logo_to_a_generated_winner(db_session):
    twin = _mk(db_session, "https://linkedin.com/jobs/view/logo-1", source="linkedin")
    twin.company_logo = _LICDN
    winner = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/logo-1", source="ats")
    winner.company_logo = _S2
    db_session.commit()
    assert mark_inferior_twins(db_session, winner) == 1
    db_session.refresh(winner)
    assert winner.company_logo == _LICDN


def test_mark_inferior_twins_keeps_a_real_or_stored_winner_logo(db_session):
    stored = "/jobs/logo/" + "a" * 40 + ".png"
    twin = _mk(db_session, "https://linkedin.com/jobs/view/logo-2", source="linkedin")
    twin.company_logo = _LICDN
    winner = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/logo-2", source="ats")
    winner.company_logo = stored
    db_session.commit()
    mark_inferior_twins(db_session, winner)
    db_session.refresh(winner)
    assert winner.company_logo == stored


def test_mark_inferior_twins_never_copies_a_generated_logo(db_session):
    twin = _mk(db_session, "https://linkedin.com/jobs/view/logo-3", source="linkedin")
    twin.company_logo = "https://logo.clearbit.com/kinaxis.com"
    winner = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/logo-3", source="ats")
    winner.company_logo = ""
    db_session.commit()
    mark_inferior_twins(db_session, winner)
    db_session.refresh(winner)
    assert winner.company_logo == ""


def test_absorb_hands_the_real_logo_to_the_surviving_twin(db_session):
    survivor = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/logo-4",
                   source="ats", description="Full description " * 30)
    survivor.company_logo = _S2
    newcomer = _mk(db_session, "https://ca.linkedin.com/jobs/view/logo-4", source="linkedin")
    newcomer.company_logo = _LICDN
    db_session.commit()
    assert absorb_new_aggregator_rows(db_session) == 1
    db_session.refresh(survivor)
    db_session.refresh(newcomer)
    assert newcomer.duplicate_of == survivor.id
    assert survivor.company_logo == _LICDN


# --- closed winners (a live repost must not hide behind a closed copy) -------

_LI_OLD = "https://ca.linkedin.com/jobs/view/software-engineer-intern-at-kinaxis-4440083739"
_LI_NEW = "https://ca.linkedin.com/jobs/view/software-engineer-intern-at-kinaxis-4470027097"
_DESC = "Kinaxis builds supply chain planning software for the world " * 10


def _closed(db_session, row, status):
    row.listing_status = status
    db_session.commit()
    return row


def test_posting_key_ignores_host_spelling_and_slugs():
    from backend.services.cross_source_dedup import posting_key
    assert posting_key("https://www.linkedin.com/jobs/view/4462216626") == \
        posting_key("https://ca.linkedin.com/jobs/view/software-developer-at-ibm-4462216626/?trk=x")
    assert posting_key("https://www.linkedin.com/jobs/search?currentJobId=4462216626") == \
        "linkedin:4462216626"
    assert posting_key(_LI_OLD) != posting_key(_LI_NEW)
    assert posting_key("https://ca.indeed.com/viewjob?jk=AB12") == \
        posting_key("https://www.indeed.com/viewjob?jk=ab12&from=serp")
    assert posting_key("https://boards.greenhouse.io/acme/jobs/1?utm_source=x") == \
        posting_key("https://boards.greenhouse.io/acme/jobs/1")


def test_absorb_skips_a_closed_aggregator_twin(db_session):
    # The Affirm case: an older, described LinkedIn copy that has closed
    # must not absorb a newer live repost that carries its own posting id.
    for status in ("expired", "removed"):
        old = _closed(db_session, _mk(db_session, _LI_OLD + status, description=_DESC), status)
        repost = _mk(db_session, _LI_NEW + status, description=_DESC)

        assert absorb_new_aggregator_rows(db_session) == 0
        db_session.refresh(repost)
        assert repost.duplicate_of is None
        # Out of the next round's way.
        old.title = old.title_norm = repost.title = repost.title_norm = "retired " + status
        db_session.commit()


def test_absorb_skips_an_expired_direct_twin_but_not_a_removed_one(db_session):
    direct = _closed(db_session, _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/70",
                                     source="ats", description=_DESC), "expired")
    fresh = _mk(db_session, "https://ca.linkedin.com/jobs/view/4470000070")
    assert absorb_new_aggregator_rows(db_session) == 0
    db_session.refresh(fresh)
    assert fresh.duplicate_of is None

    # A removed direct row is the board's death verdict on the requisition
    # its LinkedIn mirrors copy: they stay hidden behind it.
    _closed(db_session, direct, "removed")
    assert absorb_new_aggregator_rows(db_session) == 1
    db_session.refresh(fresh)
    assert fresh.duplicate_of == direct.id


def test_absorb_fuzzy_fallback_skips_a_closed_direct_twin(db_session):
    _closed(db_session, _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/71",
                            source="ats", title="Software Developer Intern, Analytics",
                            description=_DESC), "expired")
    loser = _mk(db_session, "https://linkedin.com/jobs/view/4470000071",
                title="Software Developer Intern, Analytic")
    assert absorb_new_aggregator_rows(db_session) == 0
    db_session.refresh(loser)
    assert loser.duplicate_of is None


def test_has_direct_twin_ignores_an_expired_direct_row(db_session):
    row = _closed(db_session, _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/72",
                                  source="ats"), "expired")
    kwargs = dict(company="Kinaxis", company_domain="kinaxis.com",
                  title="Software Engineer Intern", city="ottawa")
    assert not has_direct_twin(db_session, **kwargs)
    _closed(db_session, row, "removed")
    assert has_direct_twin(db_session, **kwargs)
    _closed(db_session, row, "stale")
    assert has_direct_twin(db_session, **kwargs)


def test_release_gives_back_a_repost_hidden_behind_a_closed_copy(db_session):
    import datetime

    from backend.services.cross_source_dedup import release_from_closed_winners

    old = _closed(db_session, _mk(db_session, _LI_OLD, description=_DESC), "expired")
    repost = _mk(db_session, _LI_NEW, description=_DESC)
    indeed = _mk(db_session, "https://ca.indeed.com/viewjob?jk=kin01", source="indeed")
    same_posting = _mk(db_session, "https://www.linkedin.com/jobs/view/4440083739")
    for row in (repost, indeed, same_posting):
        row.duplicate_of = old.id
        row.last_probed_at = datetime.datetime(2026, 9, 1)
    db_session.commit()

    moved = release_from_closed_winners(db_session)

    for row in (repost, indeed, same_posting):
        db_session.refresh(row)
    # The repost comes back, first in line for the verifier.
    assert repost.duplicate_of is None and repost.last_probed_at is None
    # The Indeed copy moves under the live repost (same job, better tier).
    assert indeed.duplicate_of == repost.id
    # Same LinkedIn posting id as the closed row: that closure is its own.
    assert same_posting.duplicate_of == old.id
    assert sorted(moved) == sorted([(repost.id, old.id, None), (indeed.id, old.id, repost.id)])
    assert repost.listing_status == "active"  # status untouched, never deleted
    assert release_from_closed_winners(db_session) == []  # idempotent


def test_release_leaves_mirrors_of_a_removed_direct_row_and_closed_rows(db_session):
    from backend.services.cross_source_dedup import release_from_closed_winners

    direct = _closed(db_session, _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/73",
                                     source="ats", description=_DESC), "removed")
    mirror = _mk(db_session, "https://ca.linkedin.com/jobs/view/4465089143")
    old = _closed(db_session, _mk(db_session, _LI_OLD, title="Data Analyst Intern",
                                  description=_DESC), "expired")
    closed_repost = _closed(db_session, _mk(db_session, _LI_NEW, title="Data Analyst Intern"),
                            "expired")
    mirror.duplicate_of = direct.id
    closed_repost.duplicate_of = old.id
    db_session.commit()

    assert release_from_closed_winners(db_session) == []
    db_session.refresh(mirror)
    db_session.refresh(closed_repost)
    assert mirror.duplicate_of == direct.id
    assert closed_repost.duplicate_of == old.id


def test_a_stale_spell_of_the_direct_row_never_frees_its_mirror(db_session):
    """stale is hidden from the feed (scraper v2) but usually recovers. Its
    LinkedIn mirror must stay hidden through the stale spell: released, it
    would sit beside the recovered row as a duplicate nothing hides again."""
    from backend.services.cross_source_dedup import (
        absorb_new_aggregator_rows,
        release_from_closed_winners,
        stands_in_for_twins,
    )

    direct = _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/74",
                 source="ats", description=_DESC)
    mirror = _mk(db_session, "https://ca.linkedin.com/jobs/view/4465089144")
    mirror.duplicate_of = direct.id
    db_session.commit()

    _closed(db_session, direct, "stale")
    assert stands_in_for_twins("stale", "ats", direct.url)
    # A stale aggregator row stands in for no one: only direct rows speak when hidden.
    assert not stands_in_for_twins("stale", "linkedin", mirror.url)
    assert release_from_closed_winners(db_session) == []
    absorb_new_aggregator_rows(db_session)  # the hourly backfill pass
    db_session.refresh(mirror)
    assert mirror.duplicate_of == direct.id
    # A new copy arriving while the direct row is stale is hidden too.
    assert has_direct_twin(db_session, company="Kinaxis", company_domain="kinaxis.com",
                           title="Software Engineer Intern", city="ottawa")

    _closed(db_session, direct, "active")  # the board lists it again
    absorb_new_aggregator_rows(db_session)
    db_session.refresh(mirror)
    assert mirror.duplicate_of == direct.id


# One LinkedIn posting under two host spellings (prod TD 61534/62435,
# Deloitte 61647/61682, Skyworks 61937/61963).
_SAME_SLUG = "https://ca.linkedin.com/jobs/view/software-engineer-intern-at-kinaxis-4464900190"
_SAME_BARE = "https://www.linkedin.com/jobs/view/4464900190"


def _release_pair(db_session, first, second):
    """Hide two copies (created in this order, so ``first`` has the lower id)
    behind an older copy that has since expired, then release them. Returns
    the rows refreshed, the move list, and the ids left in the feed."""
    from backend.services.cross_source_dedup import release_from_closed_winners

    old = _closed(db_session, _mk(db_session, _LI_OLD, description=_DESC), "expired")
    rows = [_mk(db_session, url, source=source, description=description)
            for url, source, description in (first, second)]
    for row in rows:
        row.duplicate_of = old.id
    db_session.commit()

    moved = release_from_closed_winners(db_session)

    for row in rows:
        db_session.refresh(row)
    visible = [row.id for row in rows if row.duplicate_of is None]
    return old, rows, moved, visible


def test_release_shows_one_posting_once_when_the_lower_id_has_no_description(db_session):
    # By id, the bare copy came back first while its described twin was
    # still hidden, then the described copy found no better twin (the bare
    # one isn't): the same posting showed in the feed twice.
    old, (bare, described), moved, visible = _release_pair(
        db_session, (_SAME_SLUG, "linkedin", ""), (_SAME_BARE, "linkedin", _DESC))

    assert visible == [described.id]
    assert bare.duplicate_of == described.id
    assert sorted(moved) == sorted([(described.id, old.id, None),
                                    (bare.id, old.id, described.id)])


def test_release_shows_one_posting_once_when_neither_copy_has_a_description(db_session):
    # Neither copy is `better` than the other, in any order: only the
    # posting id says they are one posting.
    _old, (first, second), _moved, visible = _release_pair(
        db_session, (_SAME_BARE, "linkedin", ""), (_SAME_SLUG, "linkedin", ""))

    assert visible == [first.id]
    assert second.duplicate_of == first.id


def test_release_keeps_the_older_described_copy_whatever_the_lengths(db_session):
    # `better` prefers the older of two described copies, not the longer:
    # releasing the longer one first would leave the older one no better
    # twin, and the posting would show twice.
    _old, (older, longer), _moved, visible = _release_pair(
        db_session, (_SAME_SLUG, "linkedin", _DESC), (_SAME_BARE, "linkedin", _DESC * 2))

    assert visible == [older.id]
    assert longer.duplicate_of == older.id


def test_release_order_mirrors_the_better_twin(db_session):
    # Two reposts with posting ids of their own: the described one comes back
    # first and the bare one goes under it, even when the bare one is older.
    _old, (bare, described), _moved, visible = _release_pair(
        db_session, (_LI_NEW, "linkedin", ""),
        ("https://www.linkedin.com/jobs/view/4470027999", "linkedin", _DESC))
    assert visible == [described.id]
    assert bare.duplicate_of == described.id


def test_release_order_puts_linkedin_before_an_older_indeed_copy(db_session):
    _old, (indeed, linkedin), _moved, visible = _release_pair(
        db_session, ("https://ca.indeed.com/viewjob?jk=kin02", "indeed", _DESC),
        (_LI_NEW, "linkedin", ""))
    assert visible == [linkedin.id]
    assert indeed.duplicate_of == linkedin.id


def test_absorb_pass_runs_the_release(db_session):
    # The hourly cron keeps it converged: a winner that closes after it
    # absorbed a repost hands the repost back on the next pass.
    old = _mk(db_session, _LI_OLD, description=_DESC)
    repost = _mk(db_session, _LI_NEW, description=_DESC)
    assert absorb_new_aggregator_rows(db_session) == 1
    db_session.refresh(repost)
    assert repost.duplicate_of == old.id

    _closed(db_session, old, "expired")
    absorb_new_aggregator_rows(db_session)
    db_session.refresh(repost)
    assert repost.duplicate_of is None


def test_sweep_never_hides_a_live_copy_behind_a_closed_one(db_session):
    old = _closed(db_session, _mk(db_session, _LI_OLD, description=_DESC), "expired")
    repost = _mk(db_session, _LI_NEW, description="")
    expired_direct = _closed(db_session, _mk(db_session, "https://boards.greenhouse.io/kinaxis/jobs/74",
                                             source="ats", title="QA Intern",
                                             description=_DESC), "expired")
    copy = _mk(db_session, "https://ca.linkedin.com/jobs/view/4470000074", title="QA Intern")
    dedup_sweep(db_session)
    for row in (old, repost, expired_direct, copy):
        db_session.refresh(row)
    assert repost.duplicate_of is None
    assert copy.duplicate_of is None
    assert old.duplicate_of is None and expired_direct.duplicate_of is None
