"""Freshness lifecycle: re-crawls confirm, vanishing removes, age expires,
ghosts get scored, and none of it deletes a row or hides a bookmark."""

import datetime

import pytest

from backend.db.models import ScrapedJob, User, UserSavedJob
from backend.services.ats_scraper import ATSJob
from backend.services.description_extractor import sanitize_description
from backend.services.listing_freshness import (
    AGGREGATOR_FAST_MAX_AGE_DAYS,
    AGGREGATOR_MAX_AGE_DAYS,
    GHOST_DAYS_OPEN,
    LISTING_ACTIVE,
    LISTING_EXPIRED,
    LISTING_REMOVED,
    LISTING_STALE,
    STALE_AFTER_HOURS,
    backfill_board_keys,
    board_key_from_url,
    build_new_row_fields,
    reconcile_board,
    refresh_known_listings,
    score_ghost_risk,
    sweep_aggregator_expiry,
    sweep_stale,
)
from backend.services.structured_extraction import compute_raw_hash

NOW = datetime.datetime(2026, 7, 16, 12, 0, 0)
BOARD = "greenhouse:acme"


def _job(url="https://boards.greenhouse.io/acme/jobs/1", title="Software Intern",
         **kwargs) -> ATSJob:
    defaults = dict(company="Acme", location="Ottawa, ON, Canada")
    defaults.update(kwargs)
    return ATSJob(title=title, url=url, **defaults)


def _row(db, url="https://boards.greenhouse.io/acme/jobs/1", **kwargs):
    defaults = dict(
        title="Software Intern", company="Acme", location="Ottawa, ON, Canada",
        source_platform="ats", board_key=BOARD, listing_status=LISTING_ACTIVE,
        first_seen_at=NOW - datetime.timedelta(days=1),
        last_seen_at=NOW - datetime.timedelta(days=1),
        scraped_at=NOW - datetime.timedelta(days=1),
    )
    defaults.update(kwargs)
    row = ScrapedJob(url=url, **defaults)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ─── build_new_row_fields ────────────────────────────────────────────────────

class TestBuildNewRowFields:
    def test_populates_freshness_and_extraction(self, db_session):
        job = _job(
            description="Build in Python on AWS. Visa sponsorship is available. Pay: $90,000 - $110,000 per year.",
            external_id="4285367",
        )
        fields = build_new_row_fields(job, BOARD)
        assert fields["listing_status"] == LISTING_ACTIVE
        assert fields["first_seen_at"] is not None
        assert fields["board_key"] == BOARD
        assert fields["external_id"] == "greenhouse:acme:4285367"
        assert fields["source_trust"] == "high"
        assert fields["salary_min"] == 90000
        assert fields["salary_max"] == 110000
        assert fields["visa_sponsorship"] == "yes"
        assert "python" in fields["skills"]
        assert fields["raw_hash"]

    def test_salary_text_beats_description(self, db_session):
        job = _job(salary_text="45000-55000 CAD", description="No numbers here.")
        fields = build_new_row_fields(job, BOARD)
        assert fields["salary_min"] == 45000
        assert fields["salary_currency"] == "CAD"


# ─── reconcile_board ─────────────────────────────────────────────────────────

class TestReconcileBoard:
    def test_confirms_live_and_removes_vanished(self, db_session):
        live = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/1")
        gone = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/2")

        stats = reconcile_board(db_session, BOARD, {live.url}, now=NOW)

        db_session.expire_all()
        assert stats["confirmed"] == 1
        assert stats["removed"] == 1
        assert db_session.get(ScrapedJob, live.id).last_seen_at == NOW
        assert db_session.get(ScrapedJob, live.id).listing_status == LISTING_ACTIVE
        assert db_session.get(ScrapedJob, gone.id).listing_status == LISTING_REMOVED
        assert db_session.get(ScrapedJob, gone.id).listing_status_changed_at == NOW

    def test_revives_removed_row_that_reappears(self, db_session):
        row = _row(db_session, listing_status=LISTING_REMOVED)
        stats = reconcile_board(db_session, BOARD, {row.url}, now=NOW)
        db_session.expire_all()
        assert stats["revived"] == 1
        assert db_session.get(ScrapedJob, row.id).listing_status == LISTING_ACTIVE

    def test_workday_apply_link_matches_its_listed_posting(self, db_session):
        """The crawl lists '/job/<slug>'; a row stored as '/job/<slug>/apply'
        is that posting, not a vanished one."""
        board = "workday:roche"
        base = "https://roche.wd3.myworkdayjobs.com/roche-ext/job/Mississauga/Analyst_202606-115566"
        apply_row = _row(db_session, url=base + "/apply", board_key=board,
                         listing_status=LISTING_STALE)
        gone = _row(db_session, url=base.replace("115566", "999999") + "/apply", board_key=board)

        stats = reconcile_board(db_session, board, {base}, now=NOW)

        db_session.expire_all()
        kept = db_session.get(ScrapedJob, apply_row.id)
        assert kept.listing_status == LISTING_ACTIVE and kept.last_seen_at == NOW
        assert db_session.get(ScrapedJob, gone.id).listing_status == LISTING_REMOVED
        assert stats == {"confirmed": 1, "revived": 1, "removed": 1, "off_target": 0, "held": 0}

    def test_does_not_touch_other_boards(self, db_session):
        other = _row(db_session, url="https://jobs.lever.co/other/1", board_key="lever:other")
        reconcile_board(db_session, BOARD, set(), now=NOW)
        db_session.expire_all()
        assert db_session.get(ScrapedJob, other.id).listing_status == LISTING_ACTIVE

    def test_empty_board_with_many_rows_degrades_to_stale_sweep(self, db_session):
        rows = [
            _row(db_session, url=f"https://boards.greenhouse.io/acme/jobs/{i}")
            for i in range(12)
        ]
        stats = reconcile_board(db_session, BOARD, set(), now=NOW)
        db_session.expire_all()
        assert stats["removed"] == 0
        assert all(
            db_session.get(ScrapedJob, r.id).listing_status == LISTING_ACTIVE
            for r in rows
        )

    def test_small_board_going_empty_is_real(self, db_session):
        row = _row(db_session)
        stats = reconcile_board(db_session, BOARD, set(), now=NOW)
        db_session.expire_all()
        assert stats["removed"] == 1
        assert db_session.get(ScrapedJob, row.id).listing_status == LISTING_REMOVED


# ─── refresh_known_listings ──────────────────────────────────────────────────

class TestRefreshKnownListings:
    def test_splits_new_from_known_and_bumps_seen(self, db_session):
        known = _row(db_session)
        jobs = [
            _job(url=known.url),
            _job(url="https://boards.greenhouse.io/acme/jobs/9", title="Data Intern"),
        ]
        new_jobs, stats = refresh_known_listings(db_session, BOARD, jobs, now=NOW)
        db_session.expire_all()
        assert [j.url for j in new_jobs] == ["https://boards.greenhouse.io/acme/jobs/9"]
        assert stats["refreshed"] == 1
        assert db_session.get(ScrapedJob, known.id).last_seen_at == NOW

    def test_title_change_logged(self, db_session):
        known = _row(db_session, title="Software Intern")
        jobs = [_job(url=known.url, title="Software Engineering Intern")]
        _new, stats = refresh_known_listings(db_session, BOARD, jobs, now=NOW)
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 1
        assert row.title == "Software Engineering Intern"
        assert row.edit_count == 1
        assert row.change_log[-1]["changed"] == ["title"]

    def test_salary_removed_flagged_once(self, db_session):
        stated = "Pay: $90,000 - $110,000 per year."
        known = _row(db_session, salary_min=90000, salary_max=110000, description=stated,
                     raw_hash=compute_raw_hash("Software Intern", "Ottawa, ON, Canada", stated))
        jobs = [_job(url=known.url,
                     description="A fresh description with no pay information at all.")]
        _new, stats = refresh_known_listings(db_session, BOARD, jobs, now=NOW)
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["salary_removed"] == 1
        assert "salary_removed" in row.change_log[-1]["changed"]
        assert (row.salary_min, row.salary_max) == (None, None)
        # The pay is gone now: the next crawl has nothing left to flag.
        _new, stats = refresh_known_listings(db_session, BOARD, jobs, now=NOW)
        db_session.expire_all()
        assert stats["salary_removed"] == 0 and stats["edited"] == 0
        assert db_session.get(ScrapedJob, known.id).edit_count == 1

    def test_unchanged_content_keeps_a_salary_the_parser_misses(self, db_session):
        """Veeva: "Starting Salary: $85,000" sits past parse_salary's window,
        so every crawl of the unchanged posting logged salary_removed (102
        entries on prod). The same content removed nothing."""
        text = "Compensation\nStarting Salary: competitive, see the recruiter."
        known = _row(db_session, salary_min=85000, salary_max=85000, description=text,
                     raw_hash=compute_raw_hash("Software Intern", "Ottawa, ON, Canada", text))
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, description=text)], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["salary_removed"] == 0 and stats["edited"] == 0 and not row.change_log
        assert row.salary_min == 85000

    def test_a_new_salary_reading_of_unchanged_content_is_no_edit(self, db_session):
        text = "Pay: $90,000 - $110,000 per year."
        known = _row(db_session, salary_min=50000, salary_max=60000, description=text,
                     raw_hash=compute_raw_hash("Software Intern", "Ottawa, ON, Canada", text))
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, description=text)], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 0 and not row.change_log
        assert (row.salary_min, row.salary_max) == (90000, 110000)

    def test_recrawl_of_an_inserted_description_is_no_edit(self, db_session):
        """cron-ats stores and hashes the sanitized description; the refresh
        hashed the raw text, so every new Greenhouse/Lever row logged a false
        'description' edit on its second crawl."""
        raw = "Build tools for R&D teams. Rotations <3 months> across the org."
        stored = sanitize_description(raw)
        assert stored != raw
        inserted = build_new_row_fields(_job(description=stored), BOARD)
        known = _row(db_session, description=stored, raw_hash=inserted["raw_hash"])

        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, description=raw)], now=NOW,
        )

        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 0 and not row.change_log
        assert (row.description, row.raw_hash) == (stored, inserted["raw_hash"])

    def test_a_row_the_raw_text_refresh_rewrote_heals_without_an_edit(self, db_session):
        """Such a false edit stored the raw text and its hash: the same
        content, so the next crawl heals it quietly."""
        raw = "Build tools for R&D teams."
        known = _row(db_session, description=raw,
                     raw_hash=compute_raw_hash("Software Intern", "Ottawa, ON, Canada", raw))

        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, description=raw)], now=NOW,
        )

        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 0 and not row.change_log
        assert row.description == sanitize_description(raw)
        assert row.raw_hash == compute_raw_hash(
            "Software Intern", "Ottawa, ON, Canada", sanitize_description(raw))

    def test_the_heal_keeps_what_was_read_from_the_same_content(self, db_session):
        """The heal rewrites only the text and its hash: ~1,160 prod rows
        carry an LLM section cache (description_sections) that clearing
        would send back to gpt-4o-mini on their next open (review 5)."""
        raw = "Build tools for R&D teams."
        sections = {"responsibilities": ["Build tools for R&D teams."]}
        known = _row(db_session, description=raw, description_sections=sections,
                     visa_sponsorship="yes", skills=["python"],
                     raw_hash=compute_raw_hash("Software Intern", "Ottawa, ON, Canada", raw))

        refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, description=raw)], now=NOW,
        )

        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert row.description == sanitize_description(raw)
        assert (row.description_sections, row.visa_sponsorship, row.skills) == (
            sections, "yes", ["python"])

    def test_a_description_edit_clears_what_was_read_from_the_old_text(self, db_session):
        old = sanitize_description("Build tools for R&D teams.")
        known = _row(db_session, description=old, description_sections={"about": [old]},
                     visa_sponsorship="yes", skills=["python"],
                     raw_hash=compute_raw_hash("Software Intern", "Ottawa, ON, Canada", old))

        refresh_known_listings(
            db_session, BOARD,
            [_job(url=known.url, description="Build Go services for the payments team.")],
            now=NOW,
        )

        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert row.description_sections is None
        assert row.visa_sponsorship == "unknown" and row.skills != ["python"]

    def test_a_real_description_edit_is_still_logged(self, db_session):
        old = "Build tools for R&D teams."
        known = _row(db_session, description=sanitize_description(old),
                     raw_hash=compute_raw_hash("Software Intern", "Ottawa, ON, Canada",
                                               sanitize_description(old)))
        _new, stats = refresh_known_listings(
            db_session, BOARD,
            [_job(url=known.url, description="Build tools for the payments team.")], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 1 and row.change_log[-1]["changed"] == ["description"]
        assert row.description == "Build tools for the payments team."

    def test_salary_is_read_from_the_description_as_inserted(self, db_session):
        """Greenhouse (Waymo, D2L, Relativity): the pay line sits at the end,
        and only the longer sanitized text reaches parse_salary's tail
        window, so the refresh read no pay where the insert read some."""
        body = "Work on R&D tooling & data. " * 180  # ~5,000 chars, ~5,700 sanitized
        raw = body + "\nThe pay range for this role is $60 - $70 per hour."
        assert len(raw) <= 5500 < len(sanitize_description(raw))
        known = _row(db_session)

        refresh_known_listings(db_session, BOARD, [_job(url=known.url, description=raw)], now=NOW)

        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert (row.salary_min, row.salary_max, row.salary_period) == (60, 70, "hour")

    def test_salary_falls_back_to_the_raw_description(self, db_session):
        """Veeva's "Financial Analyst" (Lever): the pay line sits at
        character 3,967 of 4,510, inside parse_salary's 4,000-character head,
        and the sanitized text's "&nbsp;" pushes it past, so the refresh read
        no pay the raw text states (review 5)."""
        body = "Our values:\n\xa0\n" * 30
        raw = (body + "x" * (3967 - len(body)) + "Base pay: $60,000 - $85,000\n"
               + "The salary range listed here has been set in good faith. " * 9)
        stored = sanitize_description(raw)
        assert raw.index("$60,000") < 4000 < stored.index("$60,000")
        assert len(stored) <= 5500
        known = _row(db_session)

        refresh_known_listings(db_session, BOARD, [_job(url=known.url, description=raw)], now=NOW)

        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert (row.salary_min, row.salary_max, row.salary_period) == (60000, 85000, "year")

    def test_empty_description_recrawl_is_not_an_edit(self, db_session):
        """SmartRecruiters/Workday list payloads carry no description, a
        refresh without one must not log a description change."""
        known = _row(db_session, description="Full stored description here.",
                     raw_hash="somehash")
        jobs = [_job(url=known.url, description="")]
        _new, stats = refresh_known_listings(db_session, BOARD, jobs, now=NOW)
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 0
        assert row.description == "Full stored description here."

    def test_a_crawler_row_takes_the_registry_company_name(self, db_session):
        """'Notion (Ashby)' was the registry's name, stored on every row.
        The registry now says 'Notion'; the next crawl renames the rows
        without logging an edit of the posting."""
        known = _row(db_session, company="Notion (Ashby)")
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, company="Notion")], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert row.company == "Notion"
        assert stats["edited"] == 0 and not row.change_log

    def test_a_list_row_keeps_its_company_spelling(self, db_session):
        known = _row(db_session, company="Manulife Financial", source_platform="github")
        refresh_known_listings(db_session, BOARD, [_job(url=known.url, company="Manulife")],
                               now=NOW)
        db_session.expire_all()
        assert db_session.get(ScrapedJob, known.id).company == "Manulife Financial"

    @pytest.mark.parametrize("stored, listed", [
        (" Fleet Operations Associate (Overnight Shift)", " Fleet Operations Associate (Overnight Shift)"),
        (" Fleet Operations Associate (Overnight Shift)", "Fleet Operations Associate (Overnight Shift)"),
    ])
    def test_title_padding_is_no_edit(self, db_session, stored, listed):
        """Carvana's API pads titles (43 visible rows): compared as-is, a
        fetcher that strips them would log a 'title' edit on every row."""
        known = self._consistent(db_session, title=stored)
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, title=listed)], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 0 and stats["retitled"] == 0 and not row.change_log
        assert row.title == "Fleet Operations Associate (Overnight Shift)"

    def test_location_padding_is_no_edit(self, db_session):
        known = self._consistent(db_session, location=" Toronto, ON ")
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, location="Toronto, ON")], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 0 and not row.change_log
        assert row.location == "Toronto, ON"

    def test_registry_names_carry_no_ats_tag(self):
        """A registry name is stored verbatim as every row's company."""
        import re

        from backend.data import company_registry

        names = {(platform, slug): name for platform, slug, name in
                 company_registry.load_companies(include_disabled=True, supported_only=False)}
        tagged = [name for name in names.values()
                  if re.search(r"\((?:ashby|lever|greenhouse|workday|smartrecruiters)\)", name, re.I)]
        assert tagged == []
        assert names[("ashby", "notion")] == "Notion"
        assert names[("lever", "neon")] == "Neon"

    def test_adopts_legacy_row_into_board(self, db_session):
        known = _row(db_session, board_key="")
        _new, _stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url)], now=NOW,
        )
        db_session.expire_all()
        assert db_session.get(ScrapedJob, known.id).board_key == BOARD

    # ── Derived fields follow the title and location ────────────────────────

    def _consistent(self, db, location="Ottawa, ON, Canada", country="CA", **kwargs):
        """A row whose derived fields agree with its location, as cron-ats
        would have inserted it."""
        from backend.services.listing_freshness import location_derived_fields, title_fields

        fields = location_derived_fields(location)
        fields.pop("country")
        title = kwargs.pop("title", "Software Intern")
        return _row(db, location=location, country=country, title=title,
                    **fields, **title_fields(title), **kwargs)

    def test_consistent_row_is_left_alone(self, db_session):
        known = self._consistent(db_session)
        _new, stats = refresh_known_listings(db_session, BOARD, [_job(url=known.url)], now=NOW)
        assert (stats["reparsed"], stats["recountried"], stats["retitled"],
                stats["relabeled"]) == (0, 0, 0, 0)

    @pytest.mark.parametrize("title, department, commitment, stored, healed", [
        # The old substring test filed these under internships. Since v2 the
        # weak entry tier (analyst, associate, level I) is Entry Level, not
        # new grad (docs/scraper-v2/DECISIONS.md 4)...
        ("Internal Audit Analyst", "", "", "internship", "entry"),
        ("Operations Analyst, Cooper Street", "", "", "internship", "entry"),
        # ...and these real internships under new grad.
        ("RF Validation Associate", "Payload Internships", "", "new_grad", "internship"),
        ("Thermal Associate Engineer (Summer 2027)", "", "", "new_grad", "internship"),
        ("Hardware Validation Associate", "", "Intern", "new_grad", "internship"),
        ("Contract Student Worker - Data Analyst", "", "Contract", "new_grad", "internship"),
    ])
    def test_heals_the_experience_level_of_a_crawler_row(
            self, db_session, title, department, commitment, stored, healed):
        known = self._consistent(db_session, title=title)
        db_session.query(ScrapedJob).filter_by(id=known.id).update({"experience_level": stored})
        db_session.commit()
        listing = _job(url=known.url, title=title, department=department,
                       employment_type=commitment)

        _new, stats = refresh_known_listings(db_session, BOARD, [listing], now=NOW)

        db_session.expire_all()
        assert stats["relabeled"] == 1 and stats["edited"] == 0
        assert db_session.get(ScrapedJob, known.id).experience_level == healed
        # Consistent now: the next crawl rewrites nothing.
        _new, stats = refresh_known_listings(db_session, BOARD, [listing], now=NOW)
        assert stats["relabeled"] == 0

    def test_a_description_mention_of_internships_is_no_label(self, db_session):
        """The stored employment_type reads the description too, and "prior
        internship experience" made SpaceX's "Financial Analyst" an
        internship there; the label reads the source's commitment only."""
        known = self._consistent(db_session, title="Financial Analyst",
                                 employment_type="internship")

        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, title="Financial Analyst")], now=NOW,
        )

        db_session.expire_all()
        assert stats["relabeled"] == 0
        # Entry Level, not new grad, since v2 (DECISIONS.md 4); never internship.
        assert db_session.get(ScrapedJob, known.id).experience_level == "entry"

    def test_list_rows_keep_their_list_label(self, db_session):
        """A GitHub-list row adopted into the board keeps the label its list
        gave it: an intern list's "Software Engineer" is an internship."""
        known = self._consistent(db_session, title="Software Engineer", source_platform="github")
        db_session.query(ScrapedJob).filter_by(id=known.id).update(
            {"experience_level": "internship"})
        db_session.commit()

        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, title="Software Engineer")], now=NOW,
        )

        db_session.expire_all()
        assert stats["relabeled"] == 0
        assert db_session.get(ScrapedJob, known.id).experience_level == "internship"

    def test_heals_the_country_of_a_bare_canadian_city(self, db_session):
        """cron-ats once stored a bare "Toronto" as "US"; the next crawl fixes it."""
        known = self._consistent(db_session, location="Toronto", country="US")
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, location="Toronto")], now=NOW,
        )
        db_session.expire_all()
        assert stats["recountried"] == 1 and stats["edited"] == 0
        assert db_session.get(ScrapedJob, known.id).country == "CA"

    def test_one_country_board_sets_the_country(self, db_session):
        known = self._consistent(db_session, location="London", country="US")
        refresh_known_listings(db_session, BOARD, [_job(url=known.url, location="London")],
                               now=NOW, board_country="CA")
        db_session.expire_all()
        assert db_session.get(ScrapedJob, known.id).country == "CA"

    def test_a_location_naming_both_countries_keeps_its_country(self, db_session):
        both = "Remote (United States | Canada)"
        known = self._consistent(db_session, location=both, country="US")
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, location=both)], now=NOW,
        )
        db_session.expire_all()
        assert stats["recountried"] == 0
        assert db_session.get(ScrapedJob, known.id).country == "US"

    def test_workday_count_takes_its_country_from_the_path_hint(self, db_session):
        known = self._consistent(db_session, location="3 Locations", country="US")
        _new, stats = refresh_known_listings(
            db_session, BOARD,
            [_job(url=known.url, location="3 Locations", location_hint="Toronto-ON")], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert row.country == "CA"
        # And its place: stored rows heal on their next crawl.
        assert stats["reparsed"] == 1
        assert (row.city, row.region) == ("toronto", "ON")
        assert "|toronto|" in row.location_search

    @pytest.mark.parametrize("stored, country, hint", [
        # Prod rows stored before the path hint let a count pass the filter.
        ("REMOTETELETRAVAIL QC CAN (10 Locations)", "CA", "REMOTETELETRAVAIL-QC-CAN"),
        ("Granby QC CAN (2 Locations)", "CA", "Granby-QC-CAN"),
        ("Georgia - Atlanta", "US", "Georgia---Atlanta"),
    ])
    def test_a_workday_count_keeps_a_stored_place(self, db_session, stored, country, hint):
        """The list payload says only "10 Locations": the stored text names
        the place, so it stays, and no location edit is logged."""
        known = self._consistent(db_session, location=stored, country=country)
        before = db_session.get(ScrapedJob, known.id)
        place = (before.city, before.region, before.location_search)
        listing = _job(url=known.url, location="10 Locations", location_hint=hint)

        _new, stats = refresh_known_listings(db_session, BOARD, [listing], now=NOW)

        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert row.location == stored
        assert stats["edited"] == 0 and not row.change_log
        assert stats["reparsed"] == 0 and (row.city, row.region, row.location_search) == place

    def test_a_workday_count_replaces_a_stored_count(self, db_session):
        """A stored count names no place: the new count is stored and the
        path hint places the row."""
        known = self._consistent(db_session, location="2 Locations", country="CA")
        _new, _stats = refresh_known_listings(
            db_session, BOARD,
            [_job(url=known.url, location="3 Locations", location_hint="Toronto-ON")], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert (row.location, row.city, row.region) == ("3 Locations", "toronto", "ON")

    def test_location_edit_reparses_city_and_country(self, db_session):
        known = self._consistent(db_session)
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, location="Austin, TX")], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 1 and row.change_log[-1]["changed"] == ["location"]
        assert (row.location, row.city, row.region, row.country) == ("Austin, TX", "austin", "TX", "US")
        assert row.location_search.startswith("|austin|tx|")
        assert row.locations_json[0]["city"] == "Austin"

    def test_heals_parsed_fields_left_stale_by_an_earlier_edit(self, db_session):
        """Prod had "Seattle, Washington" rows still filed under Dallas: the
        location was edited before edits re-parsed it."""
        known = _row(db_session, location="Seattle, WA", city="dallas",
                     region="TX", location_search="|dallas|tx|texas|united states|",
                     country="US")
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, location="Seattle, WA")], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["reparsed"] == 1 and stats["edited"] == 0
        assert (row.city, row.region) == ("seattle", "WA")
        assert "|seattle|" in row.location_search

        # Now consistent: nothing to rewrite on the next crawl.
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, location="Seattle, WA")], now=NOW,
        )
        assert stats["reparsed"] == 0

    def test_reparse_keeps_every_city_of_a_pipe_joined_location(self, db_session):
        """An Anthropic row as prod stored it, filed under New York City. Read
        as one comma list the heal rewrote it to "San Francisco, DC" and it
        dropped out of the New York filter."""
        both = "San Francisco, CA | New York City, NY | Washington, DC"
        known = _row(db_session, location=both, city="new york city", region="NY",
                     location_search="|new york city|ny|new york|united states|",
                     country="US")
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, location=both)], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["reparsed"] == 1
        for token in ("|new york city|", "|new york|", "|san francisco|", "|dc|"):
            assert token in row.location_search, token

    def test_filling_an_empty_location_is_not_an_edit(self, db_session):
        """Parsons' list rows carried no location until the bullet fallback."""
        known = self._consistent(db_session, location="", country="US")
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, location="US - CA, Pasadena")], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["edited"] == 0 and not row.change_log
        assert (row.location, row.city, row.region) == ("US - CA, Pasadena", "pasadena", "CA")

    def test_title_edit_rederives_the_title_fields(self, db_session):
        known = self._consistent(db_session, title="Business Systems Analyst")
        before = db_session.get(ScrapedJob, known.id)
        assert (before.experience_level, before.role_category) != ("internship", "Software Engineering")
        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, title="Software Engineering Intern")], now=NOW,
        )
        db_session.expire_all()
        row = db_session.get(ScrapedJob, known.id)
        assert stats["retitled"] == 1 and stats["edited"] == 1
        assert row.title_norm == "software engineering intern"
        assert row.experience_level == "internship"
        assert row.role_category == "Software Engineering"

    def test_heals_a_title_norm_left_stale_by_an_earlier_edit(self, db_session):
        known = self._consistent(
            db_session, title="Operations Analyst",
            change_log=[{"at": "2026-08-01T00:00:00", "changed": ["title"]}],
        )
        db_session.query(ScrapedJob).filter_by(id=known.id).update(
            {"title_norm": "business systems analyst"})
        db_session.commit()

        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, title="Operations Analyst")], now=NOW,
        )
        db_session.expire_all()
        assert stats["retitled"] == 1 and stats["edited"] == 0
        assert db_session.get(ScrapedJob, known.id).title_norm == "operations analyst"

    def test_unedited_title_norm_is_never_rewritten(self, db_session):
        """Only rows with a logged title edit are healed: an unedited row's
        title_norm is its insert's, whatever normalize_title version wrote it."""
        known = self._consistent(db_session, title="Operations Analyst")
        db_session.query(ScrapedJob).filter_by(id=known.id).update({"title_norm": "legacy key"})
        db_session.commit()

        _new, stats = refresh_known_listings(
            db_session, BOARD, [_job(url=known.url, title="Operations Analyst")], now=NOW,
        )
        db_session.expire_all()
        assert stats["retitled"] == 0
        assert db_session.get(ScrapedJob, known.id).title_norm == "legacy key"


class TestDerivedFieldHelpers:
    @pytest.mark.parametrize("title, department, employment_type, level", [
        ("Software Intern", "", "", "internship"),
        ("Co-op Student, Data", "", "", "internship"),
        ("Coop Engineer", "", "", "internship"),
        ("New Grad Software Engineer", "", "", "new_grad"),
        # The weak entry tier is Entry Level since v2 (DECISIONS.md 4).
        ("Analyst I", "", "", "entry"),
        ("Internal Audit Analyst", "", "", "entry"),
        ("RF Validation Associate", "Payload Internships", "", "internship"),
        ("Thermal Associate Engineer (Summer 2027)", "", "", "internship"),
        ("Hardware Validation Associate", "", "Intern", "internship"),
    ])
    def test_title_fields_experience_level(self, title, department, employment_type, level):
        from backend.services.listing_freshness import title_fields

        assert title_fields(title, department, employment_type)["experience_level"] == level

    def test_location_derived_fields(self):
        from backend.services.listing_freshness import location_derived_fields

        fields = location_derived_fields("Toronto")
        assert (fields["city"], fields["country"]) == ("toronto", "CA")
        assert location_derived_fields("London", "CA")["country"] == "CA"
        assert location_derived_fields("Austin, TX")["country"] == "US"

    @pytest.mark.parametrize("location, board_country, city, region", [
        ("CA-San Francisco", "US", "san francisco", "CA"),  # PwC, a US board
        ("CA-San Francisco", "", "san francisco", "CA"),    # the location says US
        ("NY-New York", "US", "new york", "NY"),
        ("DC-Washington", "US", "washington", "DC"),
        ("CA-Toronto", "", "toronto", ""),                  # CA is Canada here
        ("US-NY-New York", "", "new york", "NY"),           # Snowflake
        # No North American evidence: never read as Indiana.
        ("IN-Bengaluru", "", "in-bengaluru", ""),
    ])
    def test_code_prefixed_location(self, location, board_country, city, region):
        from backend.services.listing_freshness import location_derived_fields

        fields = location_derived_fields(location, board_country)
        assert (fields["city"], fields["region"]) == (city, region)

    def test_workday_count_is_placed_by_its_path_hint(self):
        from backend.services.listing_freshness import location_derived_fields

        fields = location_derived_fields("3 Locations", hint="Toronto-Ontario-Canada")
        assert (fields["city"], fields["region"], fields["country"]) == ("toronto", "ON", "CA")
        assert "|toronto|on|ontario|canada|" in fields["location_search"]
        assert fields["locations_json"] == []  # the card keeps "3 Locations"
        # A slug naming no North American place, or nothing we trust.
        assert location_derived_fields("3 Locations", hint="Bangalore")["location_search"] == ""
        assert location_derived_fields(
            "7 Locations", hint="TELUS-CAN-BC-510-W-Georgia-St")["location_search"] == ""
        # A location that names its place wins over the hint.
        assert location_derived_fields("Austin, TX", hint="Toronto-ON")["city"] == "austin"

    def test_a_board_country_never_places_a_foreign_hint(self):
        """A one-country board's registry country files its rows, but a slug
        naming a foreign place is no place of ours: "IN-Bengaluru" on a US
        board was stored as Bengaluru, Indiana."""
        from backend.services.listing_freshness import location_derived_fields

        fields = location_derived_fields("3 Locations", "US", hint="IN-Bengaluru")
        assert (fields["city"], fields["region"], fields["location_search"]) == ("", "", "")
        assert fields["country"] == "US"  # the registry's, as before
        # BDO Canada's bare "London" still reads as London, Ontario.
        fields = location_derived_fields("2 Locations", "CA", hint="London")
        assert (fields["city"], fields["location_search"]) == ("london", "|london|canada|")


class TestRepairCountry:
    def test_heals_contradicted_rows_and_is_idempotent(self, db_session):
        from backend.services.listing_freshness import location_derived_fields, repair_country

        def row(url, location, country, **kwargs):
            fields = location_derived_fields(location)
            fields.pop("country")
            return _row(db_session, url=url, location=location, country=country,
                        **{**fields, **kwargs})

        linkedin = row("https://www.linkedin.com/jobs/view/1", "Calgary, Alberta, Canada", "US",
                       source_platform="linkedin", board_key="")
        bare = row("https://boards.greenhouse.io/acme/jobs/2", "Toronto", "US")
        bdo = row("https://bdo.wd3.myworkdayjobs.com/Bdo/job/London/x_JR1", "London", "US",
                  board_key="workday:bdo")
        reverse = row("https://boards.greenhouse.io/acme/jobs/3", "Austin, TX, United States", "CA")
        both = row("https://boards.greenhouse.io/acme/jobs/4", "Toronto, ON; Seattle, WA", "US")
        right = row("https://boards.greenhouse.io/acme/jobs/5", "Toronto, ON", "CA")
        hidden = row("https://www.linkedin.com/jobs/view/6", "Ottawa, Ontario, Canada", "US",
                     source_platform="linkedin", board_key="", listing_status=LISTING_EXPIRED)
        foreign = row("https://www.linkedin.com/jobs/view/7", "London, England, United Kingdom",
                      "CA", source_platform="linkedin", board_key="")

        stats = repair_country(db_session, board_countries={"workday:bdo": "CA"})

        db_session.expire_all()
        country = {r.id: db_session.get(ScrapedJob, r.id).country
                   for r in (linkedin, bare, bdo, reverse, both, right, hidden, foreign)}
        assert country[linkedin.id] == "CA"
        assert country[bare.id] == "CA"
        assert country[bdo.id] == "CA"
        assert country[reverse.id] == "US"
        assert country[both.id] == "US"       # names both: keeps its value
        assert country[right.id] == "CA"
        assert country[hidden.id] == "US"     # not visible: not this pass's job
        assert country[foreign.id] == "CA"    # no NA verdict: left alone
        assert (stats["to_ca"], stats["to_us"], stats["repaired"]) == (3, 1, 4)

        again = repair_country(db_session, board_countries={"workday:bdo": "CA"})
        assert again["repaired"] == 0

    def test_is_bounded(self, db_session):
        from backend.services.listing_freshness import repair_country

        for i in range(3):
            _row(db_session, url=f"https://www.linkedin.com/jobs/view/{i}",
                 location="Calgary, Alberta, Canada", country="US",
                 location_search="|calgary|ab|alberta|canada|", source_platform="linkedin",
                 board_key="")

        stats = repair_country(db_session, limit=2, board_countries={})

        assert stats["checked"] == 2 and stats["repaired"] == 2
        assert repair_country(db_session, limit=2, board_countries={})["repaired"] == 1


# ─── Sweeps ──────────────────────────────────────────────────────────────────

class TestSweeps:
    def test_stale_sweep_marks_unconfirmed_ats_rows(self, db_session):
        old = _row(db_session, last_seen_at=NOW - datetime.timedelta(hours=STALE_AFTER_HOURS + 1))
        fresh = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/2",
                     last_seen_at=NOW - datetime.timedelta(hours=1))
        count = sweep_stale(db_session, now=NOW)
        db_session.expire_all()
        assert count == 1
        assert db_session.get(ScrapedJob, old.id).listing_status == LISTING_STALE
        assert db_session.get(ScrapedJob, fresh.id).listing_status == LISTING_ACTIVE

    def test_stale_sweep_ignores_aggregator_rows(self, db_session):
        li = _row(db_session, url="https://linkedin.com/jobs/view/1",
                  source_platform="linkedin", board_key="",
                  last_seen_at=NOW - datetime.timedelta(days=10))
        count = sweep_stale(db_session, now=NOW)
        db_session.expire_all()
        assert count == 0
        assert db_session.get(ScrapedJob, li.id).listing_status == LISTING_ACTIVE

    def test_aggregator_expiry_by_age(self, db_session):
        old = _row(db_session, url="https://linkedin.com/jobs/view/1",
                   source_platform="linkedin", board_key="",
                   posted_date=NOW - datetime.timedelta(days=AGGREGATOR_MAX_AGE_DAYS + 1))
        recent = _row(db_session, url="https://linkedin.com/jobs/view/2",
                      source_platform="linkedin", board_key="",
                      posted_date=NOW - datetime.timedelta(days=3))
        direct = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/3",
                      posted_date=NOW - datetime.timedelta(days=200))
        count = sweep_aggregator_expiry(db_session, now=NOW)
        db_session.expire_all()
        assert count == 1
        assert db_session.get(ScrapedJob, old.id).listing_status == LISTING_EXPIRED
        assert db_session.get(ScrapedJob, recent.id).listing_status == LISTING_ACTIVE
        assert db_session.get(ScrapedJob, direct.id).listing_status == LISTING_ACTIVE

    def test_linkedin_expires_sooner_than_github(self, db_session):
        """LinkedIn/Indeed churn fast, so they age out at the shorter fast
        window; curated GitHub lists keep the longer window."""
        age = datetime.timedelta(days=AGGREGATOR_FAST_MAX_AGE_DAYS + 2)  # 23d
        assert AGGREGATOR_FAST_MAX_AGE_DAYS + 2 < AGGREGATOR_MAX_AGE_DAYS
        li = _row(db_session, url="https://www.linkedin.com/jobs/view/50",
                  source_platform="linkedin", board_key="", posted_date=NOW - age)
        gh = _row(db_session, url="https://careers.example.com/list-role/50",
                  source_platform="github", board_key="", posted_date=NOW - age)
        count = sweep_aggregator_expiry(db_session, now=NOW)
        db_session.expire_all()
        assert db_session.get(ScrapedJob, li.id).listing_status == LISTING_EXPIRED
        assert db_session.get(ScrapedJob, gh.id).listing_status == LISTING_ACTIVE
        assert count == 1


# ─── Terminal lifecycle rules ────────────────────────────────────────────────

class TestTerminalLifecycle:
    def test_fast_expiry_keys_on_url_host(self, db_session):
        """The external scraper stored LinkedIn cards as source_platform='ats'
        (board_key 'unknown', stale): the URL host puts them on the 21-day
        clock regardless."""
        old = NOW - datetime.timedelta(days=AGGREGATOR_FAST_MAX_AGE_DAYS + 5)
        ext_li = _row(db_session, url="https://ca.linkedin.com/jobs/view/intern-at-acme-4384617179",
                      board_key="unknown", listing_status=LISTING_STALE,
                      first_seen_at=old, scraped_at=old, last_seen_at=NOW - datetime.timedelta(days=2))
        count = sweep_aggregator_expiry(db_session, now=NOW)
        db_session.expire_all()
        assert count == 1
        assert db_session.get(ScrapedJob, ext_li.id).listing_status == LISTING_EXPIRED

    def test_future_dated_list_rows_cannot_escape(self, db_session):
        """A year-less 'Nov 30' parsed into the future used to keep a GitHub
        row fresh forever; age now runs from the earliest known date."""
        seen_long_ago = NOW - datetime.timedelta(days=AGGREGATOR_MAX_AGE_DAYS + 10)
        future = _row(db_session, url="https://careers.example.com/list/1",
                      source_platform="github", board_key="",
                      posted_date=NOW + datetime.timedelta(days=60),
                      first_seen_at=seen_long_ago, scraped_at=seen_long_ago)
        fresh = _row(db_session, url="https://careers.example.com/list/2",
                     source_platform="github", board_key="",
                     posted_date=NOW - datetime.timedelta(days=3),
                     first_seen_at=NOW - datetime.timedelta(days=3),
                     scraped_at=NOW - datetime.timedelta(days=3))
        adopted = _row(db_session, url="https://jobs.ashbyhq.com/acme/1",
                       source_platform="github", board_key="ashby:acme",
                       posted_date=seen_long_ago, first_seen_at=seen_long_ago,
                       scraped_at=seen_long_ago)
        count = sweep_aggregator_expiry(db_session, now=NOW)
        db_session.expire_all()
        assert count == 1
        assert db_session.get(ScrapedJob, future.id).listing_status == LISTING_EXPIRED
        assert db_session.get(ScrapedJob, fresh.id).listing_status == LISTING_ACTIVE
        # adopted into a real board: the crawl reconciles it, age doesn't
        assert db_session.get(ScrapedJob, adopted.id).listing_status == LISTING_ACTIVE

    def test_stale_without_evidence_expires(self, db_session):
        from backend.services.listing_freshness import STALE_TERMINAL_DAYS, sweep_terminal_expiry

        abandoned = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/1",
                         listing_status=LISTING_STALE,
                         last_seen_at=NOW - datetime.timedelta(days=STALE_TERMINAL_DAYS + 1),
                         last_probed_at=NOW - datetime.timedelta(hours=3))
        # a partial crawl still lists this one, so it keeps living
        listed = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/2",
                      listing_status=LISTING_STALE,
                      last_seen_at=NOW - datetime.timedelta(days=1))
        # board-confirmed active rows are never this sweep's business
        board_row = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/3",
                         last_seen_at=NOW - datetime.timedelta(days=STALE_TERMINAL_DAYS + 30))

        stats = sweep_terminal_expiry(db_session, now=NOW)
        db_session.expire_all()
        assert stats["stale_expired"] == 1
        assert db_session.get(ScrapedJob, abandoned.id).listing_status == LISTING_EXPIRED
        assert db_session.get(ScrapedJob, abandoned.id).listing_status_changed_at == NOW
        assert db_session.get(ScrapedJob, listed.id).listing_status == LISTING_STALE
        assert db_session.get(ScrapedJob, board_row.id).listing_status == LISTING_ACTIVE

    def test_unreconcilable_boards_age_out(self, db_session):
        from backend.services.listing_freshness import (
            UNRECONCILABLE_MAX_AGE_DAYS,
            sweep_terminal_expiry,
        )

        long_ago = NOW - datetime.timedelta(days=UNRECONCILABLE_MAX_AGE_DAYS + 1)
        orphan = _row(db_session, url="https://jobs.bombardier.com/job/11101", board_key="unknown",
                      last_seen_at=long_ago, last_probed_at=NOW - datetime.timedelta(hours=2))
        orphan_recent = _row(db_session, url="https://jobs.bombardier.com/job/11102",
                             board_key="unknown", last_seen_at=NOW - datetime.timedelta(days=10))
        on_board = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/5",
                        last_seen_at=long_ago)
        list_row = _row(db_session, url="https://careers.example.com/list/9",
                        source_platform="github", board_key="", last_seen_at=long_ago,
                        posted_date=NOW - datetime.timedelta(days=2))

        stats = sweep_terminal_expiry(db_session, now=NOW)
        db_session.expire_all()
        assert stats["unreconcilable_expired"] == 1
        assert db_session.get(ScrapedJob, orphan.id).listing_status == LISTING_EXPIRED
        assert db_session.get(ScrapedJob, orphan_recent.id).listing_status == LISTING_ACTIVE
        # real board: sweep_stale + the verifier handle it, never an age cap
        assert db_session.get(ScrapedJob, on_board.id).listing_status == LISTING_ACTIVE
        # aggregator rows run on sweep_aggregator_expiry's clock instead
        assert db_session.get(ScrapedJob, list_row.id).listing_status == LISTING_ACTIVE

    def test_terminal_expiry_needs_a_check_since_the_evidence(self, db_session):
        """Nothing ages out unchecked: a row the verifier never reached
        (budget, time box, a skipped host), or last checked before its last
        evidence, stays for the next run to check. A check that came back
        without a verdict (a bot wall) still lets it end on schedule."""
        from backend.services.listing_freshness import (
            STALE_TERMINAL_DAYS,
            UNRECONCILABLE_MAX_AGE_DAYS,
            sweep_terminal_expiry,
        )

        stale_seen = NOW - datetime.timedelta(days=STALE_TERMINAL_DAYS + 5)
        orphan_seen = NOW - datetime.timedelta(days=UNRECONCILABLE_MAX_AGE_DAYS + 5)
        never_checked = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/21",
                             listing_status=LISTING_STALE, last_seen_at=stale_seen)
        checked_before = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/22",
                              listing_status=LISTING_STALE, last_seen_at=stale_seen,
                              last_probed_at=stale_seen - datetime.timedelta(days=1))
        walled = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/23",
                      listing_status=LISTING_STALE, last_seen_at=stale_seen,
                      last_probed_at=NOW - datetime.timedelta(hours=1))
        orphan_unchecked = _row(db_session, url="https://jobs.bombardier.com/job/1",
                                board_key="unknown", last_seen_at=orphan_seen)
        # no last_seen_at at all: first_seen_at is the evidence
        orphan_checked = _row(db_session, url="https://jobs.bombardier.com/job/2",
                              board_key="unknown", last_seen_at=None,
                              first_seen_at=orphan_seen, scraped_at=orphan_seen,
                              last_probed_at=NOW - datetime.timedelta(hours=1))

        stats = sweep_terminal_expiry(db_session, now=NOW)
        db_session.expire_all()
        assert stats == {"stale_expired": 1, "unreconcilable_expired": 1}
        assert db_session.get(ScrapedJob, never_checked.id).listing_status == LISTING_STALE
        assert db_session.get(ScrapedJob, checked_before.id).listing_status == LISTING_STALE
        assert db_session.get(ScrapedJob, walled.id).listing_status == LISTING_EXPIRED
        assert db_session.get(ScrapedJob, orphan_unchecked.id).listing_status == LISTING_ACTIVE
        assert db_session.get(ScrapedJob, orphan_checked.id).listing_status == LISTING_EXPIRED


# ─── Ghost scoring ───────────────────────────────────────────────────────────

class TestGhostScoring:
    def test_long_open_evergreen_scores_high(self, db_session):
        row = _row(db_session,
                   first_seen_at=NOW - datetime.timedelta(days=100),
                   description="We are always accepting applications for this role.")
        score_ghost_risk(db_session, now=NOW)
        db_session.expire_all()
        row = db_session.get(ScrapedJob, row.id)
        assert row.ghost_risk_score >= 65  # 40 (age>90) + 25 (evergreen)
        assert row.ghost_risk_factors["evergreen"] is True
        assert row.ghost_risk_factors["days_open"] == 100

    def test_fresh_normal_posting_scores_zero(self, db_session):
        row = _row(db_session, first_seen_at=NOW - datetime.timedelta(days=2),
                   description="One opening on the payments team, starting September.")
        score_ghost_risk(db_session, now=NOW)
        db_session.expire_all()
        row = db_session.get(ScrapedJob, row.id)
        assert row.ghost_risk_score == 0
        assert row.ghost_risk_factors["evergreen"] is False

    def test_repost_signal(self, db_session):
        _row(db_session, url="https://boards.greenhouse.io/acme/jobs/old",
             title_norm="software intern", listing_status=LISTING_REMOVED)
        fresh = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/new",
                     title_norm="software intern",
                     first_seen_at=NOW - datetime.timedelta(days=1))
        score_ghost_risk(db_session, now=NOW)
        db_session.expire_all()
        fresh = db_session.get(ScrapedJob, fresh.id)
        assert fresh.ghost_risk_factors.get("reposts") == 1
        assert fresh.ghost_risk_score >= 20

    def test_rescore_pass_updates_aging_rows(self, db_session):
        row = _row(db_session,
                   first_seen_at=NOW - datetime.timedelta(days=GHOST_DAYS_OPEN + 20),
                   ghost_risk_score=0,
                   ghost_risk_factors={"evergreen": False,
                                       "scored_at": (NOW - datetime.timedelta(days=30)).isoformat()})
        stats = score_ghost_risk(db_session, now=NOW)
        db_session.expire_all()
        row = db_session.get(ScrapedJob, row.id)
        assert stats["rescored"] == 1
        assert row.ghost_risk_score >= 25  # age factor now applies

    def test_rescore_rotates_through_every_aging_row(self, db_session):
        """Ordered by first sighting, the rescore pass took the same oldest
        batch every run: a row past it kept its first score for good."""
        def aging(url, days_seen, scored_days_ago):
            return _row(db_session, url=url,
                        first_seen_at=NOW - datetime.timedelta(days=days_seen),
                        ghost_risk_score=0,
                        ghost_risk_factors={"evergreen": False, "scored_at": (
                            NOW - datetime.timedelta(days=scored_days_ago)).isoformat()})

        oldest = aging("https://boards.greenhouse.io/acme/jobs/a", 300, 1)
        older = aging("https://boards.greenhouse.io/acme/jobs/b", 200, 1)
        stuck = aging("https://boards.greenhouse.io/acme/jobs/c", 100, 60)

        stats = score_ghost_risk(db_session, now=NOW, batch_size=2)

        db_session.expire_all()
        assert stats["rescored"] == 2
        assert db_session.get(ScrapedJob, stuck.id).ghost_risk_score == 40
        # The next run takes the row left out, and so on round.
        later = NOW + datetime.timedelta(hours=4)
        score_ghost_risk(db_session, now=later, batch_size=2)
        db_session.expire_all()
        scored = {row.id: row.ghost_risk_factors["scored_at"]
                  for row in (db_session.get(ScrapedJob, r.id) for r in (oldest, older, stuck))}
        assert all(at >= NOW.isoformat() for at in scored.values())

    def test_a_years_old_posting_scores_as_old(self, db_session):
        """Lever lists GoPuff postings from 2021 that we first saw in 2026:
        days open count from the source's date, not our sighting."""
        posted = NOW - datetime.timedelta(days=3 * 365)
        row = _row(db_session, first_seen_at=NOW - datetime.timedelta(days=10),
                   posted_date=posted,
                   description="One opening on the payments team, starting September.")
        # The rescore pass has room for one row, and an older one takes it:
        # the first scoring alone must get the age right.
        _row(db_session, url="https://boards.greenhouse.io/acme/jobs/old",
             first_seen_at=NOW - datetime.timedelta(days=300),
             ghost_risk_factors={"evergreen": False,
                                 "scored_at": (NOW - datetime.timedelta(days=30)).isoformat()})
        score_ghost_risk(db_session, now=NOW, batch_size=1)
        db_session.expire_all()
        row = db_session.get(ScrapedJob, row.id)
        assert row.ghost_risk_score >= 40
        assert row.ghost_risk_factors["days_open"] == (NOW - posted).days

    @pytest.mark.parametrize("posted_date", [
        datetime.datetime(1970, 1, 1),                # a placeholder, not a date
        NOW - datetime.timedelta(days=5),             # after our first sighting: a repost stamp
    ])
    def test_an_implausible_posted_date_is_ignored(self, db_session, posted_date):
        row = _row(db_session, first_seen_at=NOW - datetime.timedelta(days=100),
                   posted_date=posted_date,
                   description="One opening on the payments team, starting September.")
        score_ghost_risk(db_session, now=NOW)
        db_session.expire_all()
        row = db_session.get(ScrapedJob, row.id)
        assert row.ghost_risk_factors["days_open"] == 100  # from our first sighting

    def test_rescore_reaches_a_recently_seen_old_posting(self, db_session):
        """Scored while young by our sighting, old by its source's date: the
        rescore pass must pick it up, or its first score stands."""
        row = _row(db_session,
                   first_seen_at=NOW - datetime.timedelta(days=10),
                   posted_date=NOW - datetime.timedelta(days=200),
                   ghost_risk_score=0,
                   ghost_risk_factors={"evergreen": False,
                                       "scored_at": (NOW - datetime.timedelta(days=1)).isoformat()})
        stats = score_ghost_risk(db_session, now=NOW)
        db_session.expire_all()
        row = db_session.get(ScrapedJob, row.id)
        assert stats["rescored"] == 1
        assert row.ghost_risk_score == 40 and row.ghost_risk_factors["days_open"] == 200

    def test_hidden_duplicates_not_scored(self, db_session):
        winner = _row(db_session)
        twin = _row(db_session, url="https://linkedin.com/jobs/view/9",
                    source_platform="linkedin", duplicate_of=winner.id,
                    first_seen_at=NOW - datetime.timedelta(days=100))
        score_ghost_risk(db_session, now=NOW)
        db_session.expire_all()
        assert db_session.get(ScrapedJob, twin.id).ghost_risk_score == 0


# ─── board_key derivation / backfill ─────────────────────────────────────────

class TestBoardKeyBackfill:
    def test_derivations(self):
        assert board_key_from_url("https://boards.greenhouse.io/stripe/jobs/1") == "greenhouse:stripe"
        assert board_key_from_url("https://job-boards.greenhouse.io/stripe/jobs/1") == "greenhouse:stripe"
        assert board_key_from_url("https://jobs.lever.co/spotify/abc") == "lever:spotify"
        assert board_key_from_url("https://jobs.ashbyhq.com/ramp/xyz") == "ashby:ramp"
        assert board_key_from_url("https://jobs.smartrecruiters.com/Visa/123-analyst") == "smartrecruiters:Visa"
        assert board_key_from_url("https://bmo.wd3.myworkdayjobs.com/external/job/x_R-1") == "workday:bmo"
        assert board_key_from_url("https://linkedin.com/jobs/view/1") == ""

    def test_backfill_adopts_and_marks_unknown(self, db_session):
        gh = _row(db_session, board_key="")
        weird = _row(db_session, url="https://careers.example.com/1", board_key="")
        adopted = backfill_board_keys(db_session)
        db_session.expire_all()
        assert adopted == 1
        assert db_session.get(ScrapedJob, gh.id).board_key == BOARD
        assert db_session.get(ScrapedJob, weird.id).board_key == "unknown"


# ─── cron-ats end to end ─────────────────────────────────────────────────────

class TestCronAtsFreshness:
    def _cron_headers(self, monkeypatch):
        import backend.auth.dependencies as auth_deps
        monkeypatch.setattr(auth_deps, "CRON_SECRET", "test-cron-secret")
        return {"x-cron-secret": "test-cron-secret"}

    def _mock_board(self, monkeypatch, jobs, all_urls=None, complete=True):
        from backend.data import company_registry
        from backend.services.ats_scraper import ATSScraper, BoardSnapshot

        monkeypatch.setattr(
            company_registry, "load_companies",
            lambda **kw: [("greenhouse", "acme", "Acme")],
        )

        async def fake_scrape_board(self, client, platform, slug, company_name):
            return BoardSnapshot(
                platform=platform, slug=slug, company=company_name,
                jobs=jobs, all_urls=all_urls if all_urls is not None
                else {j.url for j in jobs},
                complete=complete,
            )

        monkeypatch.setattr(ATSScraper, "scrape_board", fake_scrape_board)

    def test_run_reconfirms_known_and_removes_vanished(self, client, db_session, monkeypatch):
        known = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/1")
        vanished = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/2")
        self._mock_board(monkeypatch, [_job(url=known.url)])

        res = client.post("/github-sources/cron-ats", headers=self._cron_headers(monkeypatch))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["new_jobs"] == 0
        assert body["refreshed"] == 1
        assert body["removed"] == 1

        db_session.expire_all()
        assert db_session.get(ScrapedJob, known.id).last_seen_at > NOW - datetime.timedelta(minutes=5)
        assert db_session.get(ScrapedJob, vanished.id).listing_status == LISTING_REMOVED

    def test_new_job_inserted_with_freshness_fields(self, client, db_session, monkeypatch):
        job = _job(
            url="https://boards.greenhouse.io/acme/jobs/7",
            title="Platform Intern",
            description="Kubernetes and Go. Visa sponsorship is available. $40/hr.",
            external_id="777",
        )
        self._mock_board(monkeypatch, [job])

        res = client.post("/github-sources/cron-ats", headers=self._cron_headers(monkeypatch))
        assert res.json()["new_jobs"] == 1

        row = db_session.query(ScrapedJob).filter(ScrapedJob.url == job.url).one()
        assert row.board_key == BOARD
        assert row.external_id == "greenhouse:acme:777"
        assert row.listing_status == LISTING_ACTIVE
        assert row.first_seen_at is not None
        assert row.source_trust == "high"
        assert row.visa_sponsorship == "yes"
        assert "kubernetes" in (row.skills or [])
        assert row.salary_min == 40 and row.salary_period == "hour"

    def test_incomplete_snapshot_never_removes(self, client, db_session, monkeypatch):
        stored = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/1")
        self._mock_board(monkeypatch, [], all_urls=set(), complete=False)

        res = client.post("/github-sources/cron-ats", headers=self._cron_headers(monkeypatch))
        assert res.json()["removed"] == 0
        db_session.expire_all()
        assert db_session.get(ScrapedJob, stored.id).listing_status == LISTING_ACTIVE

    def test_circuit_breaker_skips_repeat_failer(self, client, db_session, monkeypatch):
        from backend.db.models import SourceHealth
        from backend.services.source_health import FAILURE_THRESHOLD

        db_session.add(SourceHealth(
            board_key=BOARD, platform="greenhouse", slug="acme",
            consecutive_failures=FAILURE_THRESHOLD,
            last_failure_at=datetime.datetime.utcnow() - datetime.timedelta(hours=1),
        ))
        db_session.commit()

        called = []

        def _mock(monkeypatch=monkeypatch):
            from backend.data import company_registry
            from backend.services.ats_scraper import ATSScraper, BoardSnapshot
            monkeypatch.setattr(
                company_registry, "load_companies",
                lambda **kw: [("greenhouse", "acme", "Acme")],
            )

            async def fake_scrape_board(self, client, platform, slug, company_name):
                called.append(slug)
                return BoardSnapshot(platform=platform, slug=slug, company=company_name)

            monkeypatch.setattr(ATSScraper, "scrape_board", fake_scrape_board)

        _mock()
        res = client.post("/github-sources/cron-ats", headers=self._cron_headers(monkeypatch))
        assert res.json()["boards_skipped_cooldown"] == 1
        assert called == []

    def test_board_failure_recorded(self, client, db_session, monkeypatch):
        from backend.db.models import SourceHealth
        from backend.data import company_registry
        from backend.services.ats_scraper import ATSScraper

        monkeypatch.setattr(
            company_registry, "load_companies",
            lambda **kw: [("greenhouse", "acme", "Acme")],
        )

        async def exploding_scrape_board(self, client, platform, slug, company_name):
            raise RuntimeError("board renamed")

        monkeypatch.setattr(ATSScraper, "scrape_board", exploding_scrape_board)
        res = client.post("/github-sources/cron-ats", headers=self._cron_headers(monkeypatch))
        assert res.json()["boards_failed"] == 1

        health = db_session.query(SourceHealth).filter_by(board_key=BOARD).one()
        assert health.consecutive_failures == 1
        assert "board renamed" in health.last_error


# ─── URL liveness verification (dead list links) ─────────────────────────────

class _StatusTransport:
    """httpx transport answering a fixed status per URL fragment."""

    def __init__(self, statuses: dict):
        self.statuses = statuses

    async def handle(self, request):
        import httpx
        for fragment, status in self.statuses.items():
            if fragment in str(request.url):
                return httpx.Response(status, text="page")
        return httpx.Response(200, text="page")


def _status_client(statuses: dict):
    import httpx

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await _StatusTransport(statuses).handle(request)

    return httpx.AsyncClient(transport=T())


def _body_client(mapping: dict):
    """httpx client answering (status, body) per URL fragment; default 200 'ok'.
    Lets a test drive the soft-404 body check, not just the status code."""
    import httpx

    class T(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            for fragment, (status, body) in mapping.items():
                if fragment in str(request.url):
                    return httpx.Response(status, text=body)
            return httpx.Response(200, text="ok")

    return httpx.AsyncClient(transport=T())


class TestUrlLiveness:
    @pytest.mark.asyncio
    async def test_probe_verdicts(self):
        from backend.services.listing_freshness import probe_url_liveness
        async with _status_client({"/dead": 404, "/gone": 410, "/wall": 403,
                                   "/live": 200}) as client:
            assert await probe_url_liveness(client, "https://x.test/dead") == "dead"
            assert await probe_url_liveness(client, "https://x.test/gone") == "dead"
            # Bot walls are not evidence of death, a real browser gets through.
            assert await probe_url_liveness(client, "https://x.test/wall") == "unknown"
            # A page that merely loads proves nothing (Workday answers 200 for
            # made-up ids); only a platform API can say "alive".
            assert await probe_url_liveness(client, "https://x.test/live") == "unknown"

    @pytest.mark.asyncio
    async def test_soft_404_body_read_from_visible_text(self):
        """A 200 that says 'no longer accepting applications' is dead, on any
        host now: the check reads only VISIBLE text (scripts/templates are
        stripped), which is what made SPA hosts unsafe before. The same phrase
        inside an SPA's script bundle is still ignored."""
        from backend.services.listing_freshness import probe_url_liveness
        closed = "<h1>Sorry, this job is no longer accepting applications.</h1>"
        # LinkedIn's other closed variant: the expired JD redirects to a search
        # page whose nav links carry this trk token (a live page never has it).
        expired = '<a href="/login?trk=expired_jd_redirect">Sign in</a>'
        live = '<h1 class="topcard__title">Software Intern</h1><a class="apply-button">Apply</a>'
        bundled = "<script>t('no longer accepting applications')</script><h1>Intern</h1>"
        async with _body_client({
            "linkedin.com/jobs/view/1": (200, closed),
            "linkedin.com/jobs/view/2": (200, live),
            "linkedin.com/jobs/view/3": (200, expired),
            "spa-careers.example/1": (200, closed),
            "spa-careers.example/2": (200, bundled),
        }) as client:
            assert await probe_url_liveness(client, "https://www.linkedin.com/jobs/view/1") == "dead"
            assert await probe_url_liveness(client, "https://www.linkedin.com/jobs/view/2") == "alive"
            assert await probe_url_liveness(client, "https://www.linkedin.com/jobs/view/3") == "dead"
            assert await probe_url_liveness(client, "https://spa-careers.example/1") == "dead"
            assert await probe_url_liveness(client, "https://spa-careers.example/2") == "unknown"

    @pytest.mark.asyncio
    async def test_verify_recent_removes_dead_github_rows(self, db_session):
        from backend.services.listing_freshness import verify_recent_aggregator_listings

        dead = _row(db_session, url="https://careers.example.com/jobs/dead",
                    source_platform="github", board_key="", last_seen_at=None)
        alive = _row(db_session, url="https://careers.example.com/jobs/alive",
                     source_platform="github", board_key="", last_seen_at=None)
        walled = _row(db_session, url="https://careers.example.com/jobs/wall",
                      source_platform="github", board_key="", last_seen_at=None)
        ats = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/dead2",
                   last_seen_at=None)  # ats rows are the board's job, not this sweep's

        async with _status_client({"/jobs/dead": 404, "/jobs/wall": 403}) as client:
            stats = await verify_recent_aggregator_listings(db_session, client, now=NOW)

        db_session.expire_all()
        assert stats["removed"] == 1
        assert db_session.get(ScrapedJob, dead.id).listing_status == LISTING_REMOVED
        loaded = db_session.get(ScrapedJob, alive.id)
        assert loaded.listing_status == LISTING_ACTIVE
        # A page that loaded is a probe, not a confirmation.
        assert loaded.last_probed_at == NOW
        assert loaded.last_seen_at is None
        assert db_session.get(ScrapedJob, walled.id).listing_status == LISTING_ACTIVE
        assert db_session.get(ScrapedJob, ats.id).listing_status == LISTING_ACTIVE
        assert db_session.get(ScrapedJob, ats.id).last_probed_at is None

    @pytest.mark.asyncio
    async def test_verify_recent_skips_recently_probed(self, db_session):
        from backend.services.listing_freshness import verify_recent_aggregator_listings

        _row(db_session, url="https://careers.example.com/jobs/dead",
             source_platform="github", board_key="",
             last_probed_at=NOW - datetime.timedelta(hours=1))

        async with _status_client({"/jobs/dead": 404}) as client:
            stats = await verify_recent_aggregator_listings(db_session, client, now=NOW)
        assert stats["checked"] == 0

    @pytest.mark.asyncio
    async def test_verify_recent_covers_soft_dead_linkedin(self, db_session):
        """Active LinkedIn rows (never covered by the old github-only sweep) get
        probed; a soft-404 guest page removes them, a live one is stamped."""
        from backend.services.listing_freshness import verify_recent_aggregator_listings

        dead_li = _row(db_session, url="https://www.linkedin.com/jobs/view/10",
                       source_platform="linkedin", board_key="", last_seen_at=None)
        live_li = _row(db_session, url="https://www.linkedin.com/jobs/view/20",
                       source_platform="linkedin", board_key="", last_seen_at=None)
        async with _body_client({
            "/jobs/view/10": (200, "No longer accepting applications"),
            "/jobs/view/20": (200, '<a class="apply-button">Apply now</a>'),
        }) as client:
            stats = await verify_recent_aggregator_listings(db_session, client, now=NOW)

        db_session.expire_all()
        assert db_session.get(ScrapedJob, dead_li.id).listing_status == LISTING_REMOVED
        live = db_session.get(ScrapedJob, live_li.id)
        assert live.listing_status == LISTING_ACTIVE
        # A guest page is never authoritative: stamped as probed only.
        assert live.last_probed_at == NOW
        assert live.last_seen_at is None
        assert stats["removed"] == 1


class TestVerifyStaleListings:
    @pytest.mark.asyncio
    async def test_dead_removes_anywhere_alive_revives_only_on_platform_api(self, db_session):
        from backend.services.listing_freshness import verify_stale_listings

        stale_seen = NOW - datetime.timedelta(days=4)
        dead_site = _row(db_session, url="https://careers.example.com/jobs/1",
                         listing_status=LISTING_STALE, last_seen_at=stale_seen)
        live_gh = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/2",
                       listing_status=LISTING_STALE, last_seen_at=stale_seen)
        spa_site = _row(db_session, url="https://careers.spa-co.com/jobs/3",
                        listing_status=LISTING_STALE, last_seen_at=stale_seen)
        walled_li = _row(db_session, url="https://linkedin.com/jobs/view/4",
                         source_platform="linkedin", board_key="",
                         listing_status=LISTING_STALE, last_seen_at=stale_seen)

        async with _body_client({
            "careers.example.com": (404, ""),
            "boards-api.greenhouse.io/v1/boards/acme/jobs/2": (200, '{"id": 2}'),
        }) as client:
            stats = await verify_stale_listings(db_session, client, now=NOW)

        db_session.expire_all()
        # Honest 404 on a plain company site → removed.
        assert db_session.get(ScrapedJob, dead_site.id).listing_status == LISTING_REMOVED
        # Greenhouse's own API says open → revived, and that IS evidence.
        gh = db_session.get(ScrapedJob, live_gh.id)
        assert gh.listing_status == LISTING_ACTIVE
        assert gh.last_seen_at == NOW and gh.listing_status_changed_at == NOW
        # 200 on an arbitrary site proves nothing: stays stale, only the probe
        # is stamped, last_seen_at keeps meaning "last positive evidence".
        spa = db_session.get(ScrapedJob, spa_site.id)
        assert spa.listing_status == LISTING_STALE
        assert spa.last_probed_at == NOW
        assert spa.last_seen_at == stale_seen
        # LinkedIn IS probed (guest page), but a page with no dead banner is
        # never revived. It stays stale with the probe stamped.
        li = db_session.get(ScrapedJob, walled_li.id)
        assert li.listing_status == LISTING_STALE
        assert li.last_probed_at == NOW
        assert li.last_seen_at == stale_seen
        assert stats["removed"] == 1 and stats["revived"] == 1

    @pytest.mark.asyncio
    async def test_soft_dead_linkedin_stale_row_removed(self, db_session):
        """A stale LinkedIn row whose guest page shows the closed banner is
        removed (the 57%-dead cohort the old probe skipped entirely)."""
        from backend.services.listing_freshness import verify_stale_listings

        dead_li = _row(db_session, url="https://www.linkedin.com/jobs/view/900",
                       source_platform="linkedin", board_key="",
                       listing_status=LISTING_STALE, last_seen_at=None)
        async with _body_client({
            "/jobs/view/900": (200, "This job is no longer accepting applications."),
        }) as client:
            stats = await verify_stale_listings(db_session, client, now=NOW)

        db_session.expire_all()
        assert db_session.get(ScrapedJob, dead_li.id).listing_status == LISTING_REMOVED
        assert stats["removed"] == 1

    @pytest.mark.asyncio
    async def test_workday_revive_loop_is_broken(self, db_session):
        """Workday pages answer 200 for closed postings, which used to revive
        every dead stale row (and send it round the stale/revive loop). The
        CXS API decides now: S22 removes, a live posting revives."""
        from backend.services.listing_freshness import verify_stale_listings

        base = "https://bmo.wd3.myworkdayjobs.com/external/job/Calgary-AB-CAN/"
        closed = _row(db_session, url=base + "Client-Service-Associate_R260019629",
                      board_key="workday:bmo", listing_status=LISTING_STALE)
        live = _row(db_session, url=base + "Credit-Analyst-Intern_R260025783",
                    board_key="workday:bmo", listing_status=LISTING_STALE)
        bosch = _row(db_session, url="https://careers.smartrecruiters.com/BoschGroup/744000135554529",
                     board_key="smartrecruiters:BoschGroup", listing_status=LISTING_STALE)

        async with _body_client({
            "/wday/cxs/bmo/external/job/Calgary-AB-CAN/Client-Service-Associate_R260019629":
                (403, '{"errorCode": "S22", "httpStatus": 403}'),
            "/wday/cxs/bmo/external/job/Calgary-AB-CAN/Credit-Analyst-Intern_R260025783":
                (200, '{"jobPostingInfo": {"id": "abc"}}'),
            "api.smartrecruiters.com": (200, '{"id": "744000135554529", "active": false}'),
            # the public pages all "load", which must not matter
            "myworkdayjobs.com/external": (200, "<html><div id='root'></div></html>"),
            "careers.smartrecruiters.com": (200, "<h1>Careers at Bosch</h1>"),
        }) as client:
            stats = await verify_stale_listings(db_session, client, now=NOW)

        db_session.expire_all()
        assert db_session.get(ScrapedJob, closed.id).listing_status == LISTING_REMOVED
        assert db_session.get(ScrapedJob, bosch.id).listing_status == LISTING_REMOVED
        revived = db_session.get(ScrapedJob, live.id)
        assert revived.listing_status == LISTING_ACTIVE
        assert revived.last_seen_at == NOW
        assert stats["removed"] == 2 and stats["revived"] == 1
        assert stats["reasons"]["workday_cxs_S22"] == 1

    @pytest.mark.asyncio
    async def test_least_recently_probed_first_and_recheck_window(self, db_session):
        from backend.services.listing_freshness import verify_stale_listings

        never = _row(db_session, url="https://careers.example.com/jobs/never",
                     listing_status=LISTING_STALE, last_probed_at=None)
        oldest = _row(db_session, url="https://careers.example.com/jobs/oldest",
                      listing_status=LISTING_STALE,
                      last_probed_at=NOW - datetime.timedelta(days=9))
        newer = _row(db_session, url="https://careers.example.com/jobs/newer",
                     listing_status=LISTING_STALE,
                     last_probed_at=NOW - datetime.timedelta(days=2))
        recent = _row(db_session, url="https://careers.example.com/jobs/recent",
                      listing_status=LISTING_STALE,
                      last_probed_at=NOW - datetime.timedelta(hours=2))

        async with _status_client({}) as client:
            stats = await verify_stale_listings(db_session, client, limit=2, now=NOW)

        db_session.expire_all()
        assert stats["checked"] == 2
        assert db_session.get(ScrapedJob, never.id).last_probed_at == NOW
        assert db_session.get(ScrapedJob, oldest.id).last_probed_at == NOW
        assert db_session.get(ScrapedJob, newer.id).last_probed_at == NOW - datetime.timedelta(days=2)

        # Next run: the two just probed sit inside the recheck window, so the
        # rotation reaches the next-oldest; the 2h-old probe is still skipped.
        async with _status_client({}) as client:
            stats = await verify_stale_listings(
                db_session, client, limit=5, now=NOW + datetime.timedelta(hours=1))
        db_session.expire_all()
        assert stats["checked"] == 1
        assert db_session.get(ScrapedJob, newer.id).last_probed_at == NOW + datetime.timedelta(hours=1)
        assert db_session.get(ScrapedJob, recent.id).last_probed_at == NOW - datetime.timedelta(hours=2)

    @pytest.mark.asyncio
    async def test_hidden_rows_are_not_worth_a_probe(self, db_session):
        from backend.services.listing_freshness import verify_stale_listings

        winner = _row(db_session, url="https://careers.example.com/jobs/1")
        _row(db_session, url="https://careers.example.com/jobs/twin",
             listing_status=LISTING_STALE, duplicate_of=winner.id)
        _row(db_session, url="https://careers.example.com/jobs/nameless",
             listing_status=LISTING_STALE, company="  ")
        _row(db_session, url="https://ca.indeed.com/viewjob?jk=1",
             listing_status=LISTING_STALE)
        async with _status_client({}) as client:
            stats = await verify_stale_listings(db_session, client, now=NOW)
        assert stats["checked"] == 0

    @pytest.mark.asyncio
    async def test_time_box_defers_the_rest(self, db_session, monkeypatch):
        """Once the deadline passes no new probe starts; the unprobed rows keep
        last_probed_at NULL and so lead the next run."""
        import asyncio
        import time

        import httpx
        from backend.services import listing_freshness
        from backend.services.listing_freshness import verify_stale_listings

        monkeypatch.setattr(listing_freshness, "_VERIFY_CHUNK", 1)
        rows = [
            _row(db_session, url=f"https://careers.example.com/jobs/{i}",
                 listing_status=LISTING_STALE,
                 posted_date=NOW - datetime.timedelta(days=i))
            for i in range(3)
        ]

        class Slow(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                await asyncio.sleep(0.6)  # the first probe blows through the box
                return httpx.Response(200, text="page")

        async with httpx.AsyncClient(transport=Slow()) as client:
            stats = await verify_stale_listings(db_session, client, now=NOW,
                                                deadline=time.monotonic() + 0.3)

        db_session.expire_all()
        assert stats["checked"] == 1
        assert stats["deferred"] == 2
        probed = [r for r in rows if db_session.get(ScrapedJob, r.id).last_probed_at == NOW]
        assert len(probed) == 1

    @pytest.mark.asyncio
    async def test_past_deadline_probes_nothing(self, db_session):
        import time
        from backend.services.listing_freshness import verify_stale_listings

        row = _row(db_session, url="https://careers.example.com/jobs/1",
                   listing_status=LISTING_STALE)
        async with _status_client({"careers.example.com": 404}) as client:
            stats = await verify_stale_listings(db_session, client, now=NOW,
                                                deadline=time.monotonic() - 1)
        db_session.expire_all()
        assert stats["checked"] == 0 and stats["deferred"] == 1
        assert db_session.get(ScrapedJob, row.id).listing_status == LISTING_STALE
        assert db_session.get(ScrapedJob, row.id).last_probed_at is None


def test_record_liveness_single_row_rules(db_session):
    """The single-row path (a job detail's live check) follows the sweep's
    rules: only the platform's own word revives or counts as evidence."""
    from backend.services.listing_freshness import record_liveness
    from backend.services.platform_liveness import LivenessResult

    seen = NOW - datetime.timedelta(days=5)
    stale = _row(db_session, listing_status=LISTING_STALE, last_seen_at=seen)
    page_only = _row(db_session, url="https://careers.example.com/jobs/2",
                     listing_status=LISTING_STALE, last_seen_at=seen)
    dead = _row(db_session, url="https://careers.example.com/jobs/3")

    assert record_liveness(db_session, stale.id, LISTING_STALE,
                           LivenessResult("alive", "gh_api_200", True), now=NOW) == LISTING_ACTIVE
    assert record_liveness(db_session, page_only.id, LISTING_STALE,
                           LivenessResult("alive", "linkedin_apply_cta", False), now=NOW) == LISTING_STALE
    assert record_liveness(db_session, dead.id, LISTING_ACTIVE,
                           LivenessResult("dead", "http_404"), now=NOW) == LISTING_REMOVED
    db_session.commit()
    db_session.expire_all()

    revived = db_session.get(ScrapedJob, stale.id)
    assert revived.listing_status == LISTING_ACTIVE and revived.last_seen_at == NOW
    weak = db_session.get(ScrapedJob, page_only.id)
    assert weak.listing_status == LISTING_STALE
    assert weak.last_seen_at == seen and weak.last_probed_at == NOW
    assert db_session.get(ScrapedJob, dead.id).listing_status == LISTING_REMOVED


class TestVerifyUnconfirmedActive:
    @pytest.mark.asyncio
    async def test_unconfirmed_direct_rows_checked_before_going_stale(self, db_session):
        from backend.services.listing_freshness import verify_unconfirmed_active_listings

        base = "https://cibc.wd3.myworkdayjobs.com/search/job/Paris-ON/"
        unconfirmed = NOW - datetime.timedelta(hours=50)
        dead = _row(db_session, url=base + "Client-Rep_2614006", board_key="workday:cibc",
                    last_seen_at=unconfirmed)
        live = _row(db_session, url=base + "Analyst-Intern_2614350", board_key="workday:cibc",
                    last_seen_at=unconfirmed)
        confirmed = _row(db_session, url=base + "Teller_2614999", board_key="workday:cibc",
                         last_seen_at=NOW - datetime.timedelta(hours=1))
        linkedin = _row(db_session, url="https://www.linkedin.com/jobs/view/77",
                        source_platform="linkedin", board_key="", last_seen_at=unconfirmed)

        async with _body_client({
            "Client-Rep_2614006": (404, '{"errorCode": "S21", "httpStatus": 404}'),
            "Analyst-Intern_2614350": (200, '{"jobPostingInfo": {"id": "x"}}'),
        }) as client:
            stats = await verify_unconfirmed_active_listings(db_session, client, now=NOW)

        db_session.expire_all()
        assert stats["checked"] == 2
        assert db_session.get(ScrapedJob, dead.id).listing_status == LISTING_REMOVED
        alive = db_session.get(ScrapedJob, live.id)
        assert alive.listing_status == LISTING_ACTIVE
        assert alive.last_seen_at == NOW  # API-confirmed: won't go stale at 72h
        assert stats["revived"] == 0 and stats["confirmed"] == 1
        assert db_session.get(ScrapedJob, confirmed.id).last_probed_at is None
        assert db_session.get(ScrapedJob, linkedin.id).last_probed_at is None


def _mock_client(handler):
    import httpx

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestVerifyStampsOnlyRealChecks:
    """last_probed_at means "a check came back": a row the check never sent
    a request for (host skipped, LinkedIn's per-run budget spent, no turn at
    the gate) or that the host rate-limited keeps its place in line."""

    @pytest.fixture(autouse=True)
    def _no_pacing(self, monkeypatch):
        from backend.services import platform_liveness
        monkeypatch.setattr(platform_liveness, "_LINKEDIN_MIN_INTERVAL", 0)

    @pytest.mark.asyncio
    async def test_rate_limited_and_skipped_rows_are_deferred_not_stamped(self, db_session):
        import httpx
        from backend.services.listing_freshness import verify_recent_aggregator_listings

        rows = [_row(db_session, url=f"https://{host}.linkedin.com/jobs/view/{i}",
                     source_platform="linkedin", board_key="", last_seen_at=None)
                for i in range(10) for host in ("www", "ca")]
        sent: list[str] = []

        def handler(request):
            sent.append(str(request.url))
            return httpx.Response(429, text="slow down")

        async with _mock_client(handler) as client:
            stats = await verify_recent_aggregator_listings(db_session, client, now=NOW)

        db_session.expire_all()
        # www and ca are one LinkedIn: three 429s trip the breaker for both
        assert len(sent) == 3
        assert stats["checked"] == 0 and stats["deferred"] == 20
        assert stats["reasons"] == {"bot_wall_429": 3, "host_skipped": 17}
        assert all(db_session.get(ScrapedJob, r.id).last_probed_at is None for r in rows)

    @pytest.mark.asyncio
    async def test_network_errors_are_stamped_breaker_skips_are_not(self, db_session):
        import httpx
        from backend.services import platform_liveness
        from backend.services.listing_freshness import verify_stale_listings

        rows = [_row(db_session, url=f"https://careers.hang.com/jobs/{i}",
                     listing_status=LISTING_STALE, posted_date=NOW - datetime.timedelta(days=i))
                for i in range(8)]
        sent: list[str] = []

        def handler(request):
            sent.append(str(request.url))
            raise httpx.ConnectError("boom")

        async with _mock_client(handler) as client:
            stats = await verify_stale_listings(db_session, client, now=NOW)

        db_session.expire_all()
        stamped = {r.url for r in rows if db_session.get(ScrapedJob, r.id).last_probed_at == NOW}
        # a request went out for exactly the stamped rows (two in flight at a
        # time, so the breaker trips after three or four)
        assert platform_liveness._HOST_FAILURE_LIMIT <= len(sent) < 8
        assert stamped == set(sent)
        assert stats["checked"] == len(sent) and stats["deferred"] == 8 - len(sent)
        assert stats["reasons"]["host_skipped"] == 8 - len(sent)

    @pytest.mark.asyncio
    async def test_coverage_moves_forward_across_runs(self, db_session):
        """A walled host answers a few requests per run: the rows it never
        answered lead the next run instead of the same head rows being
        checked every cycle while the rest never are."""
        import httpx
        from backend.services.listing_freshness import verify_recent_aggregator_listings

        rows = [_row(db_session, url=f"https://www.linkedin.com/jobs/view/{i}",
                     source_platform="linkedin", board_key="", last_seen_at=None,
                     posted_date=NOW - datetime.timedelta(hours=i))
                for i in range(12)]
        answered: list[str] = []
        budget = {"left": 4}

        def handler(request):
            if budget["left"] > 0:
                budget["left"] -= 1
                answered.append(str(request.url))
                return httpx.Response(200, text='<a class="apply-button">Apply</a>')
            return httpx.Response(429)

        for run in range(3):
            budget["left"] = 4
            async with _mock_client(handler) as client:
                await verify_recent_aggregator_listings(
                    db_session, client, now=NOW + datetime.timedelta(hours=21 * run))

        assert len(answered) == 12
        assert len(set(answered)) == 12  # every row got a real check, none twice
        db_session.expire_all()
        assert all(db_session.get(ScrapedJob, r.id).last_probed_at is not None for r in rows)

    @pytest.mark.asyncio
    async def test_linkedin_rows_cannot_crowd_out_the_other_rows(self, db_session):
        import httpx
        from backend.services.listing_freshness import verify_recent_aggregator_listings

        for i in range(30):
            _row(db_session, url=f"https://www.linkedin.com/jobs/view/{i}",
                 source_platform="linkedin", board_key="", last_seen_at=None,
                 posted_date=NOW - datetime.timedelta(hours=i))
        github = _row(db_session, url="https://careers.example.com/jobs/list-row",
                      source_platform="github", board_key="", last_seen_at=None,
                      posted_date=NOW - datetime.timedelta(days=10))

        def handler(request):
            if request.url.host.endswith("linkedin.com"):
                return httpx.Response(429)  # walled all day
            return httpx.Response(200, text="<h1>Intern</h1>")

        async with _mock_client(handler) as client:
            stats = await verify_recent_aggregator_listings(db_session, client, now=NOW,
                                                            limit=12, linkedin_quota=5)

        db_session.expire_all()
        # 5 LinkedIn rows (all walled, deferred) + the GitHub row, which was
        # older than every LinkedIn row but still got its check
        assert stats["checked"] + stats["deferred"] == 6
        assert db_session.get(ScrapedJob, github.id).last_probed_at == NOW

    @pytest.mark.asyncio
    async def test_board_key_reaches_the_platform_check(self, db_session):
        """A Jane Street gh_jid row checked through its own board's API, not
        the embed Jane Street switched off."""
        import httpx
        from backend.services.listing_freshness import verify_stale_listings

        row = _row(db_session,
                   url="https://www.janestreet.com/join-jane-street/apply/8631912002?gh_jid=8631912002",
                   board_key="greenhouse:janestreet", listing_status=LISTING_STALE)
        sent: list[str] = []

        def handler(request):
            sent.append(str(request.url))
            if "boards-api.greenhouse.io/v1/boards/janestreet/jobs/8631912002" in str(request.url):
                return httpx.Response(200, json={"id": 8631912002})
            return httpx.Response(404)

        async with _mock_client(handler) as client:
            stats = await verify_stale_listings(db_session, client, now=NOW)

        db_session.expire_all()
        assert stats["revived"] == 1
        assert db_session.get(ScrapedJob, row.id).listing_status == LISTING_ACTIVE
        assert sent == ["https://boards-api.greenhouse.io/v1/boards/janestreet/jobs/8631912002"]


class TestAggregatorIngestProbe:
    def test_dead_url_stored_as_removed(self, db_session):
        """A list row whose apply URL already 404s must never surface."""
        from backend.services.aggregator import AggregatorService
        from backend.services.markdown_parser import ParsedJob
        from backend.db.models import GitHubSource

        source = GitHubSource(repo_url="https://github.com/x/newgrad", repo_owner="x",
                              repo_name="New-Grad-Positions", role_category="software")
        db_session.add(source)
        db_session.commit()

        svc = AggregatorService(db_session)
        job = ParsedJob(title="Software Engineer, New Grad", company="DeadCo",
                        location="Toronto, ON, Canada",
                        url="https://careers.deadco.com/jobs/1")
        stored = svc._classify_and_store(job, source,
                                         dead_urls={"https://careers.deadco.com/jobs/1"})
        assert stored is False

        row = db_session.query(ScrapedJob).filter(
            ScrapedJob.url == "https://careers.deadco.com/jobs/1").one()
        assert row.listing_status == LISTING_REMOVED
        assert row.first_seen_at is not None
        assert row.source_trust == "medium"

    def test_live_url_stored_active_with_freshness_stamps(self, db_session):
        from backend.services.aggregator import AggregatorService
        from backend.services.markdown_parser import ParsedJob
        from backend.db.models import GitHubSource

        source = GitHubSource(repo_url="https://github.com/x/newgrad2", repo_owner="x",
                              repo_name="New-Grad-Positions", role_category="software")
        db_session.add(source)
        db_session.commit()

        svc = AggregatorService(db_session)
        job = ParsedJob(title="Software Engineer, New Grad", company="LiveCo",
                        location="Toronto, ON, Canada",
                        url="https://careers.liveco.com/jobs/1")
        assert svc._classify_and_store(job, source) is True

        row = db_session.query(ScrapedJob).filter(
            ScrapedJob.url == "https://careers.liveco.com/jobs/1").one()
        assert row.listing_status == LISTING_ACTIVE
        assert row.last_seen_at is not None
        assert row.source_trust == "medium"


# ─── migration ───────────────────────────────────────────────────────────────

def test_ingestion_freshness_migration_idempotent():
    from sqlalchemy import inspect as sa_inspect
    from backend.db.database import engine
    from backend.migrations.add_ingestion_freshness import run_migration

    run_migration()
    run_migration()  # second run must be a no-op
    cols = {c["name"] for c in sa_inspect(engine).get_columns("scraped_jobs")}
    assert {"listing_status", "first_seen_at", "last_seen_at", "board_key",
            "external_id", "raw_hash", "edit_count", "change_log",
            "ghost_risk_score", "ghost_risk_factors", "source_trust",
            "salary_min", "salary_max", "salary_currency", "salary_period",
            "employment_type", "visa_sponsorship", "skills"} <= cols


def test_listing_probe_migration_idempotent():
    from sqlalchemy import inspect as sa_inspect
    from backend.db.database import engine
    from backend.migrations.add_listing_probe_columns import run_migration

    run_migration()
    run_migration()  # second run must be a no-op
    inspector = sa_inspect(engine)
    assert "last_probed_at" in {c["name"] for c in inspector.get_columns("scraped_jobs")}
    assert "ix_scraped_jobs_last_probed_at" in {
        i["name"] for i in inspector.get_indexes("scraped_jobs")
    }


# ─── cron-freshness + metrics endpoints ──────────────────────────────────────

class TestFreshnessEndpoints:
    def _cron_headers(self, monkeypatch):
        import backend.auth.dependencies as auth_deps
        monkeypatch.setattr(auth_deps, "CRON_SECRET", "test-cron-secret")
        return {"x-cron-secret": "test-cron-secret"}

    def test_cron_freshness_sweeps_and_scores(self, client, db_session, monkeypatch):
        aged = _row(db_session, url="https://linkedin.com/jobs/view/9",
                    source_platform="linkedin", board_key="",
                    posted_date=datetime.datetime.utcnow() - datetime.timedelta(days=60),
                    scraped_at=datetime.datetime.utcnow() - datetime.timedelta(days=60))
        res = client.post("/jobs/cron-freshness", headers=self._cron_headers(monkeypatch))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["expired"] == 1
        assert body["ghost_scoring"]["scored_new"] >= 0
        db_session.expire_all()
        assert db_session.get(ScrapedJob, aged.id).listing_status == LISTING_EXPIRED

    def test_cron_freshness_verifies_with_platform_apis(self, client, db_session, monkeypatch):
        """End to end: the three time-boxed verify phases through the platform
        checks (mock transport, no network), then terminal expiry, which ends
        only a row this run's check reached."""
        import httpx
        from backend.services import platform_liveness

        utcnow = datetime.datetime.utcnow()
        dead_wd = _row(db_session,
                       url="https://bmo.wd3.myworkdayjobs.com/external/job/X/Closed_R1",
                       board_key="workday:bmo", listing_status=LISTING_STALE,
                       last_seen_at=utcnow - datetime.timedelta(days=4))
        unconfirmed = _row(db_session,
                           url="https://bmo.wd3.myworkdayjobs.com/external/job/X/Open_R2",
                           board_key="workday:bmo",
                           last_seen_at=utcnow - datetime.timedelta(hours=50))
        abandoned = _row(db_session, url="https://careers.example.com/jobs/old",
                         board_key="unknown", listing_status=LISTING_STALE,
                         last_seen_at=utcnow - datetime.timedelta(days=40))

        def handler(request):
            if "Closed_R1" in str(request.url):
                return httpx.Response(403, json={"errorCode": "S22"})
            if "Open_R2" in str(request.url):
                return httpx.Response(200, json={"jobPostingInfo": {"id": "2"}})
            return httpx.Response(200, text="<h1>Careers</h1>")  # proves nothing

        real_make_client = platform_liveness.make_client
        monkeypatch.setattr(platform_liveness, "make_client",
                            lambda **kw: real_make_client(transport=httpx.MockTransport(handler)))

        res = client.post("/jobs/cron-freshness", headers=self._cron_headers(monkeypatch))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["terminal_expired"]["stale_expired"] == 1
        assert body["stale_verified"]["removed"] == 1
        assert body["stale_verified"]["unverified"] == 1
        assert body["unconfirmed_verified"]["confirmed"] == 1
        assert body["recent_verified"]["checked"] == 0
        assert body["verify_seconds"] >= 0

        db_session.expire_all()
        ended = db_session.get(ScrapedJob, abandoned.id)
        assert ended.listing_status == LISTING_EXPIRED
        assert ended.last_probed_at is not None  # checked first, then ended
        assert db_session.get(ScrapedJob, dead_wd.id).listing_status == LISTING_REMOVED
        still_open = db_session.get(ScrapedJob, unconfirmed.id)
        assert still_open.listing_status == LISTING_ACTIVE
        assert still_open.last_seen_at > utcnow - datetime.timedelta(minutes=5)

    def test_cron_checks_before_terminal_expiry(self, client, db_session, monkeypatch):
        """A stale row past STALE_TERMINAL_DAYS that its platform says is
        open is revived by this run's check; before, the terminal sweep ran
        first and expired it unchecked, and nothing ever checked it again."""
        import httpx
        from backend.services import platform_liveness

        utcnow = datetime.datetime.utcnow()
        live_old = _row(db_session,
                        url="https://hitachi.wd1.myworkdayjobs.com/hitachi/job/Toronto/Eng_R0135776",
                        board_key="workday:hitachi", listing_status=LISTING_STALE,
                        last_seen_at=utcnow - datetime.timedelta(days=25))
        calls: list[str] = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, json={"jobPostingInfo": {"id": "R0135776"}})

        real_make_client = platform_liveness.make_client
        monkeypatch.setattr(platform_liveness, "make_client",
                            lambda **kw: real_make_client(transport=httpx.MockTransport(handler)))

        body = client.post("/jobs/cron-freshness",
                           headers=self._cron_headers(monkeypatch)).json()

        assert body["terminal_expired"]["stale_expired"] == 0
        assert body["stale_verified"]["revived"] == 1
        assert calls and "/wday/cxs/hitachi/hitachi/job/" in calls[0]
        db_session.expire_all()
        row = db_session.get(ScrapedJob, live_old.id)
        assert row.listing_status == LISTING_ACTIVE
        assert row.last_seen_at > utcnow - datetime.timedelta(minutes=5)

    def test_cron_phases_share_one_liveness_state(self, client, db_session, monkeypatch):
        """One run is one set of host breakers and one LinkedIn budget, not
        three: the phases hand the same cache along."""
        from backend.services import listing_freshness

        seen: list = []

        async def fake_verify(db, client_, limit=0, now=None, *, deadline=None, cache=None, **kw):
            seen.append(cache)
            return {"checked": 0}

        for name in ("verify_stale_listings", "verify_unconfirmed_active_listings",
                     "verify_recent_aggregator_listings"):
            monkeypatch.setattr(listing_freshness, name, fake_verify)

        res = client.post("/jobs/cron-freshness", headers=self._cron_headers(monkeypatch))
        assert res.status_code == 200, res.text
        assert len(seen) == 3
        assert isinstance(seen[0], dict) and all(cache is seen[0] for cache in seen)

    def test_ingest_metrics_shape(self, client, db_session, monkeypatch):
        from backend.db.models import SourceHealth
        _row(db_session)
        _row(db_session, url="https://boards.greenhouse.io/acme/jobs/2",
             listing_status=LISTING_REMOVED,
             listing_status_changed_at=datetime.datetime.utcnow())
        from backend.data import company_registry
        db_session.add(SourceHealth(board_key="lever:broken", platform="lever",
                                    slug="broken", consecutive_failures=3,
                                    last_error="HTTP 404"))
        db_session.commit()
        monkeypatch.setattr(company_registry, "load_companies",
                            lambda **kw: [("lever", "broken", "Broken")])

        res = client.get("/jobs/ingest-metrics", headers=self._cron_headers(monkeypatch))
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["by_listing_status"]["active"] == 1
        assert body["by_listing_status"]["removed"] == 1
        assert body["removed_24h"] == 1
        assert body["active_total"] == 1
        assert body["failing_boards"][0]["board_key"] == "lever:broken"
        assert "median_active_age_days" in body
        assert "dedup_rate" in body

    def test_ingest_metrics_skips_boards_no_longer_crawled(self, client, db_session, monkeypatch):
        """A board disabled or dropped from the registry keeps its health row,
        but nothing crawls it any more: it must not read as broken forever."""
        from backend.data import company_registry
        from backend.db.models import SourceHealth
        for key, failures in (("greenhouse:live", 5), ("greenhouse:retired", 50)):
            platform, slug = key.split(":")
            db_session.add(SourceHealth(board_key=key, platform=platform, slug=slug,
                                        consecutive_failures=failures,
                                        last_error="HTTP 404"))
        db_session.commit()
        monkeypatch.setattr(company_registry, "load_companies",
                            lambda **kw: [("greenhouse", "live", "Live")])

        body = client.get("/jobs/ingest-metrics", headers=self._cron_headers(monkeypatch)).json()
        assert [b["board_key"] for b in body["failing_boards"]] == ["greenhouse:live"]
        assert body["boards_in_cooldown"] == 1


# ─── API visibility ──────────────────────────────────────────────────────────

class TestListVisibility:
    def test_removed_and_expired_hidden_from_catalogue(self, client, db_session):
        _row(db_session, url="https://boards.greenhouse.io/acme/jobs/1")
        _row(db_session, url="https://boards.greenhouse.io/acme/jobs/2",
             listing_status=LISTING_REMOVED)
        _row(db_session, url="https://boards.greenhouse.io/acme/jobs/3",
             listing_status=LISTING_EXPIRED)
        _row(db_session, url="https://boards.greenhouse.io/acme/jobs/4",
             listing_status=LISTING_STALE)

        res = client.get("/jobs")
        assert res.status_code == 200
        urls = {j["url"] for j in res.json()}
        assert "https://boards.greenhouse.io/acme/jobs/1" in urls
        assert "https://boards.greenhouse.io/acme/jobs/2" not in urls
        assert "https://boards.greenhouse.io/acme/jobs/3" not in urls
        # stale (missed a listing or failed a check) is hidden too since scraper v2
        assert "https://boards.greenhouse.io/acme/jobs/4" not in urls

    def test_saved_view_keeps_removed_jobs(self, client, db_session):
        removed = _row(db_session, url="https://boards.greenhouse.io/acme/jobs/2",
                       listing_status=LISTING_REMOVED)
        db_session.add(User(id=1, email="t@t.co", hashed_password="x"))
        db_session.add(UserSavedJob(user_id=1, job_id=removed.id))
        db_session.commit()

        res = client.get("/jobs?saved=1")
        assert res.status_code == 200
        assert {j["id"] for j in res.json()} == {removed.id}

    def test_listing_fields_serialized(self, client, db_session):
        _row(db_session, ghost_risk_score=45, salary_min=90000, salary_max=110000,
             salary_currency="CAD", salary_period="year", visa_sponsorship="yes",
             employment_type="internship", skills=["python"], source_trust="high")
        res = client.get("/jobs")
        job = res.json()[0]
        assert job["listing_status"] == "active"
        assert job["ghost_risk_score"] == 45
        assert job["salary_min"] == 90000
        assert job["visa_sponsorship"] == "yes"
        assert job["skills"] == ["python"]
        assert job["source_trust"] == "high"
        assert job["last_seen_at"] is not None
