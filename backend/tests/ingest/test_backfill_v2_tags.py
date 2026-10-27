"""backend/scripts/backfill_v2_tags.py: rows that predate scraper v2 get a career
family (and ATS rows a recomputed level), only with --apply, in batches, reading
columns only and never overwriting a row that was tagged in the meantime. A row
stored as internship or new_grad moves at most to entry: a title that says mid
or above leaves its stored label alone."""

import math

import pytest
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

from backend.db import database
from backend.db.models import ScrapedJob
from backend.ingest import taxonomy
from backend.scripts import backfill_v2_tags as backfill

DESCRIPTION = "A long posting that the backfill must never read. " * 40

# name, title, the columns that differ from an ATS row stored as new_grad.
ROWS = [
    # An ATS row stored as new_grad or internship moves within the student feed:
    ("junior", "Junior Accountant", {}),                                   # new_grad -> entry
    ("marketing_intern", "Marketing Intern", {}),                          # new_grad -> internship
    ("new_grad_title", "Software Engineer, New Grad (2027)",
     {"experience_level": "internship"}),                                  # internship -> new_grad
    ("internship_junior", "Junior Accountant", {"experience_level": "internship"}),  # -> entry
    ("intern", "Software Engineer Intern",
     {"listing_status": "stale", "experience_level": "internship", "employment_type": "internship"}),
    # A Lever "Intern" commitment behind a bare title: the old crawler labeled it an
    # internship from the real commitment, so it keeps it...
    ("commitment", "RF Validation Associate",
     {"experience_level": "internship", "employment_type": "internship"}),
    # ...but an employment_type read from a description ("prior internship
    # experience") behind a new_grad label is no commitment.
    ("description_said_intern", "RF Validation Associate", {"employment_type": "internship"}),
    # A title that says mid or above would push it out of the student feed, which the
    # old crawler may have kept it in on a department or commitment not stored here:
    # the stored label stays.
    ("senior", "Senior Software Engineer", {}),
    ("dean", "Associate Dean", {"listing_status": "off_target"}),
    ("executive", "Chief Financial Officer", {}),
    ("lead_intern", "Software Engineering Manager", {"experience_level": "internship"}),
    ("years", "Software Engineer", {"experience_years_required": 6}),      # no level: 6 years
    ("no_signal", "Software Engineer", {}),                                # no level, no years: mid
    # Any other stored level is recomputed freely, up or down.
    ("blank_senior", "Senior Software Engineer", {"experience_level": ""}),
    ("entry_dean", "Associate Dean", {"experience_level": "entry"}),
    ("mid_junior", "Junior Accountant", {"experience_level": "mid"}),
    # Other sources keep the level their own path decided.
    ("list", "Analyst", {"source_platform": "github"}),
    ("linkedin", "Marketing Coordinator", {"source_platform": "linkedin", "experience_level": "entry"}),
    # Not candidates: closed rows, and a row a crawl already tagged.
    ("removed", "Registered Nurse", {"listing_status": "removed"}),
    ("expired", "Registered Nurse", {"listing_status": "expired"}),
    ("tagged", "Registered Nurse",
     {"career_family": "legal", "enrichment_version": 1, "experience_level": "mid"}),
]
SPEC = {name: (title, extra) for name, title, extra in ROWS}
NOT_CANDIDATES = ("removed", "expired", "tagged")
CANDIDATES = tuple(name for name, _title, _extra in ROWS if name not in NOT_CANDIDATES)
ATS_CANDIDATES = tuple(name for name in CANDIDATES if SPEC[name][1].get("source_platform", "ats") == "ats")
# The stored student labels the rule keeps.
KEPT = ("senior", "dean", "executive", "lead_intern", "years", "no_signal")


def _row(db, n, title, **kwargs):
    fields = dict(title=title, company="Acme", url=f"https://x/{n}", source_platform="ats",
                  listing_status="active", experience_level="new_grad", description=DESCRIPTION)
    fields.update(kwargs)
    row = ScrapedJob(**fields)
    db.add(row)
    db.commit()
    return row.id


@pytest.fixture
def seeded(db_session):
    return {name: _row(db_session, n, title, **extra) for n, (name, title, extra) in enumerate(ROWS, start=1)}


def _row_values(db, row_id):
    db.expire_all()
    row = db.get(ScrapedJob, row_id)
    return (row.career_family, row.career_family_secondary, row.enrichment_version,
            row.experience_level)


def _levels(db, seeded):
    return {name: _row_values(db, seeded[name])[3] for name in CANDIDATES}


def test_a_dry_run_counts_and_changes_nothing(db_session, seeded):
    stats = backfill.backfill(db_session)

    assert stats["rows"] == len(CANDIDATES) and stats["updated"] == 0 and not stats["applied"]
    assert stats["kept"] == len(KEPT) and stats["ats_rows"] == len(ATS_CANDIDATES)
    for name in CANDIDATES:
        family, secondary, version, _level = _row_values(db_session, seeded[name])
        assert (family, secondary, version) == (None, None, None), name
    assert _row_values(db_session, seeded["junior"])[3] == "new_grad"


def test_apply_tags_the_family_of_every_open_row(db_session, seeded):
    stats = backfill.backfill(db_session, apply=True)

    assert stats["rows"] == stats["updated"] == len(CANDIDATES)
    expected = {
        "senior": "software_engineering", "dean": "education_research",
        "junior": "finance_accounting", "intern": "software_engineering",
        "list": "data_ai_analytics", "linkedin": "marketing_communications",
    }
    for name, family in expected.items():
        values = _row_values(db_session, seeded[name])
        assert values[0] == family and values[2] == taxonomy.ENRICHMENT_VERSION, name
    for name in CANDIDATES:
        assert _row_values(db_session, seeded[name])[2] == taxonomy.ENRICHMENT_VERSION, name
    assert stats["families"]["software_engineering"] >= 4


def test_a_student_label_moves_only_within_the_feed_levels(db_session, seeded):
    backfill.backfill(db_session, apply=True)

    level = _levels(db_session, seeded)
    assert level["junior"] == "entry"                    # the weak tier is entry, no longer new_grad
    assert level["marketing_intern"] == "internship"
    assert level["new_grad_title"] == "new_grad"
    assert level["internship_junior"] == "entry"
    assert level["intern"] == "internship"
    assert level["commitment"] == "internship"           # kept by the real commitment
    assert level["description_said_intern"] == "entry"


@pytest.mark.parametrize("name", KEPT)
def test_a_title_saying_mid_or_above_keeps_the_stored_student_label(db_session, seeded, name):
    title, extra = SPEC[name]
    stored = extra.get("experience_level", "new_grad")
    # The title alone would move it out of the student feed...
    assert stored in backfill.STUDENT_LABELS
    assert taxonomy.classify_experience(
        title, years_required=extra.get("experience_years_required")) not in backfill.FEED_LEVELS

    backfill.backfill(db_session, apply=True)

    # ...but its label stays, and it is tagged all the same.
    family, _secondary, version, level = _row_values(db_session, seeded[name])
    assert level == stored and version == taxonomy.ENRICHMENT_VERSION and family


def test_the_run_counts_the_rows_it_kept(db_session, seeded):
    stats = backfill.backfill(db_session, apply=True)

    assert stats["kept"] == len(KEPT) and stats["ats_rows"] == len(ATS_CANDIDATES)
    # A kept row ends on its stored label and is no level change: nothing stored
    # as internship or new_grad is reported as moved above entry.
    assert not any(old in backfill.STUDENT_LABELS and new not in backfill.FEED_LEVELS
                   for old, new in stats["level_changes"])
    assert ("new_grad", "entry") in stats["level_changes"]


def test_every_other_stored_level_is_recomputed(db_session, seeded):
    backfill.backfill(db_session, apply=True)

    level = _levels(db_session, seeded)
    assert level["blank_senior"] == "senior"
    assert level["entry_dean"] == "director"             # 'dean' is a hard senior word
    assert level["mid_junior"] == "entry"                # and down as well as up


def test_a_kept_label_is_not_even_written(db_session):
    _row(db_session, 1, "Senior Software Engineer")
    statements = []

    @event.listens_for(db_session.get_bind(), "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    try:
        stats = backfill.backfill(db_session, apply=True)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", capture)

    updates = [s for s in statements if s.startswith("update")]
    assert stats["kept"] == 1 and stats["updated"] == 1 and len(updates) == 1
    assert "career_family" in updates[0] and "experience_level" not in updates[0]


def test_other_sources_keep_their_level(db_session, seeded):
    backfill.backfill(db_session, apply=True)

    # taxonomy would call a bare "Analyst" entry; the list said new_grad.
    assert taxonomy.classify_experience("Analyst") == "entry"
    assert _row_values(db_session, seeded["list"])[3] == "new_grad"
    assert _row_values(db_session, seeded["linkedin"])[3] == "entry"


def test_closed_and_already_tagged_rows_are_left_alone(db_session, seeded):
    backfill.backfill(db_session, apply=True)

    for name in ("removed", "expired"):
        assert _row_values(db_session, seeded[name]) == (None, None, None, "new_grad"), name
    assert _row_values(db_session, seeded["tagged"]) == ("legal", None, 1, "mid")


def test_a_second_run_finds_nothing_to_do(db_session, seeded):
    backfill.backfill(db_session, apply=True)

    again = backfill.backfill(db_session, apply=True)
    assert again["rows"] == 0 and again["updated"] == 0 and again["batches"] == 0


def test_the_batch_size_splits_the_work(db_session, seeded):
    stats = backfill.backfill(db_session, apply=True, batch=3)

    assert stats["batches"] == math.ceil(len(CANDIDATES) / 3) and stats["updated"] == len(CANDIDATES)
    assert all(_row_values(db_session, seeded[name])[2] == taxonomy.ENRICHMENT_VERSION
               for name in CANDIDATES)


def test_a_row_tagged_meanwhile_keeps_its_fresher_tags(db_session, seeded, monkeypatch):
    """A crawl can tag a row between this script's select and its update."""
    real_retag = backfill.retag
    raced = []

    def retag(row):
        if not raced:
            raced.append(True)
            db_session.query(ScrapedJob).filter(ScrapedJob.id == seeded["junior"]).update(
                {"career_family": "legal", "enrichment_version": 7}, synchronize_session=False)
            db_session.commit()
        return real_retag(row)

    monkeypatch.setattr(backfill, "retag", retag)
    stats = backfill.backfill(db_session, apply=True)

    assert _row_values(db_session, seeded["junior"])[0:3] == ("legal", None, 7)
    assert stats["updated"] == len(CANDIDATES) - 1


def test_it_never_reads_the_description(db_session, seeded):
    statements = []

    @event.listens_for(db_session.get_bind(), "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    try:
        backfill.backfill(db_session, apply=True)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", capture)

    assert statements and not [s for s in statements if "description" in s]
    assert any(s.startswith("select") for s in statements)
    assert any(s.startswith("update") for s in statements)


def test_one_update_covers_the_rows_that_share_their_tags(db_session):
    for n in range(5):
        _row(db_session, 100 + n, "Registered Nurse", source_platform="linkedin")
    statements = []

    @event.listens_for(db_session.get_bind(), "before_cursor_execute")
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    try:
        stats = backfill.backfill(db_session, apply=True)
    finally:
        event.remove(db_session.get_bind(), "before_cursor_execute", capture)

    assert stats["updated"] == 5
    assert len([s for s in statements if s.startswith("update")]) == 1


def test_the_report_names_the_families_the_level_changes_and_the_kept_rows(db_session, seeded):
    report = backfill.format_report(backfill.backfill(db_session))

    assert f"{len(CANDIDATES)} rows would tag" in report
    assert "software_engineering" in report and "education_research" in report
    assert "by status:" in report and "off_target 1" in report
    assert "by source:" in report and "github 1" in report
    assert f"experience level ({len(ATS_CANDIDATES)} ATS rows, recomputed):" in report
    assert "new_grad -> entry" in report
    assert (f"kept the stored internship/new_grad label on {len(KEPT)} of "
            f"{len(ATS_CANDIDATES)} ATS rows") in report


def test_the_report_says_when_nothing_was_kept(db_session):
    _row(db_session, 1, "Junior Accountant")

    report = backfill.format_report(backfill.backfill(db_session))

    assert "kept the stored internship/new_grad label on 0 of 1 ATS rows" in report


def test_the_command_is_a_dry_run_unless_told(db_session, seeded, monkeypatch, capsys):
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=db_session.get_bind()))

    assert backfill.main([]) == 0
    out = capsys.readouterr().out
    assert "database:" in out and "would tag" in out and "dry run: nothing changed" in out
    assert "kept the stored internship/new_grad label" in out
    assert _row_values(db_session, seeded["senior"])[0] is None

    assert backfill.main(["--apply", "--batch", "4"]) == 0
    out = capsys.readouterr().out
    assert "would tag" not in out and f"{len(CANDIDATES)} rows tagged" in out
    assert _row_values(db_session, seeded["senior"])[0] == "software_engineering"
    assert _row_values(db_session, seeded["senior"])[3] == "new_grad"
