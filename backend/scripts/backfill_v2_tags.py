"""
Tag the rows that predate scraper v2:
``python -m backend.scripts.backfill_v2_tags [--apply]``.

A row crawled, listed or ingested before scraper v2 has no career_family, and
an ATS row's experience_level is still one of the two values the old
entry-level crawler wrote (internship, new_grad). This tags the rows that are
active, stale or off_target and have no enrichment_version yet. Rows that
closed (removed, expired) are left alone: nobody sees them.

A dry run by default: it classifies every such row and prints the counts per
family and per level. ``--apply`` writes them, in batches of ``--batch``
(1,000) rows, one transaction each.

Every row gets career_family, career_family_secondary and enrichment_version
from its title (taxonomy.classify_family). ATS rows (source_platform 'ats')
also get experience_level recomputed with taxonomy.classify_experience, with
one limit: a row stored as internship or new_grad moves at most to entry.
Those rows passed the old entry-level filter, perhaps on a department or a
commitment this script cannot see, so a title that says mid or above is no
proof that the row left the student feed. Such a row keeps its stored label
(the run prints how many) and the next crawl, which has the department,
derives the level. Rows stored with any other level are recomputed freely.
GitHub-list and LinkedIn/Indeed rows keep the level their own path decided
(they are student lists).

Cheap on Neon transfer: it selects the id and the few short columns the rules
read (title, source_platform, status, level, employment_type, years), never
the description, and writes with one UPDATE per set of rows that share the
same new tags. The UPDATE re-checks that the row is still untagged, so a
crawl that tagged it in the meantime keeps its fresher tags.

What it cannot see: scraped_jobs has no department column and the
description is not read, so a family comes from the title alone (92.5% on
the labeled sample, against 96.5% with department and description) and stays
until the title changes.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict

STATUSES = ("active", "stale", "off_target")
DEFAULT_BATCH = 1000
ATS = "ats"
# The two levels the old entry-level crawler wrote, and the levels a row stored
# as one of them may be given: the student feed (Intern/New Grad plus Entry).
STUDENT_LABELS = ("internship", "new_grad")
FEED_LEVELS = ("internship", "new_grad", "entry")


def candidate_filter() -> tuple:
    """Rows to tag: still visible or recoverable, and never enriched."""
    from backend.db.models import ScrapedJob as SJ

    return (SJ.listing_status.in_(STATUSES), SJ.enrichment_version.is_(None))


def retag(row) -> tuple[dict, bool]:
    """(the columns to write for one row, whether its stored student label was
    kept). ``row`` carries id, title, source_platform, experience_level,
    employment_type and experience_years_required."""
    from backend.ingest import taxonomy

    family, secondary = taxonomy.classify_family(row.title or "")
    values = {
        "career_family": family,
        "career_family_secondary": secondary,
        "enrichment_version": taxonomy.ENRICHMENT_VERSION,
    }
    kept = False
    if row.source_platform == ATS:
        # The stored employment_type is the crawler's commitment field, or
        # else what the title or the description's opening said ("prior
        # internship experience" made a Financial Analyst read as an
        # internship). It counts only behind a label the old crawler already
        # gave as an internship, which it set from the real commitment.
        commitment = ""
        if row.experience_level == "internship" and row.employment_type == "internship":
            commitment = "internship"
        level = taxonomy.classify_experience(
            row.title or "", "", commitment, years_required=row.experience_years_required)
        if row.experience_level in STUDENT_LABELS and level not in FEED_LEVELS:
            # The old crawler kept this row, perhaps on a department or a
            # commitment that is not stored; the title alone saying mid or
            # above must not push it out of the student feed. The label is
            # left as it is (not written) and the next crawl, which has the
            # department, derives the level.
            kept = True
        else:
            values["experience_level"] = level
    return values, kept


def backfill(db, *, apply: bool = False, batch: int = DEFAULT_BATCH, progress=None) -> dict:
    """Classify (and with ``apply``, tag) the candidate rows in batches of
    ``batch`` ids taken in id order. Returns counts."""
    from backend.db.models import ScrapedJob as SJ

    stats = {
        "applied": apply, "rows": 0, "updated": 0, "batches": 0,
        "by_status": Counter(), "by_source": Counter(), "families": Counter(),
        "levels": Counter(), "ats_rows": 0, "kept": 0, "level_changes": Counter(),
    }
    last_id = 0
    while True:
        rows = (
            db.query(SJ.id, SJ.title, SJ.source_platform, SJ.listing_status,
                     SJ.experience_level, SJ.employment_type, SJ.experience_years_required)
            .filter(SJ.id > last_id, *candidate_filter())
            .order_by(SJ.id)
            .limit(batch)
            .all()
        )
        if not rows:
            break
        last_id = rows[-1].id

        groups: dict[tuple, list[int]] = defaultdict(list)
        for row in rows:
            values, kept = retag(row)
            groups[tuple(values.items())].append(row.id)
            stats["rows"] += 1
            stats["by_status"][row.listing_status] += 1
            stats["by_source"][row.source_platform or ""] += 1
            stats["families"][values["career_family"]] += 1
            if row.source_platform == ATS:
                stored = row.experience_level or ""
                level = stored if kept else values["experience_level"]
                stats["ats_rows"] += 1
                stats["levels"][level] += 1
                if kept:
                    stats["kept"] += 1
                elif level != stored:
                    stats["level_changes"][(stored, level)] += 1

        stats["batches"] += 1
        if apply:
            for values, ids in groups.items():
                stats["updated"] += (
                    db.query(SJ)
                    .filter(SJ.id.in_(ids), SJ.enrichment_version.is_(None))
                    .update(dict(values), synchronize_session=False)
                )
            db.commit()
        if progress:
            verb = "tagged" if apply else "classified"
            progress(f"batch {stats['batches']}: {len(rows)} rows {verb} (up to id {last_id})")
    db.rollback()
    return stats


def _counts(counter: Counter, order=None, indent: str = "  ") -> list[str]:
    keys = order if order is not None else [key for key, _ in counter.most_common()]
    return [f"{indent}{key:<32} {counter[key]:>8,}" for key in keys if counter[key]]


def format_report(stats: dict) -> str:
    """The counts a run prints: rows by status and source, the primary family
    of every row, and the recomputed level of the ATS rows with what changed."""
    from backend.ingest import taxonomy

    verb = "tagged" if stats["applied"] else "would tag"
    batches = stats["batches"]
    lines = [f"{stats['rows']:,} rows {verb} in {batches} batch{'' if batches == 1 else 'es'}"
             + (f" ({stats['updated']:,} written)" if stats["applied"] else "")]
    lines.append("by status:  " + ", ".join(f"{k} {v:,}" for k, v in stats["by_status"].most_common()))
    lines.append("by source:  " + ", ".join(f"{k or '(none)'} {v:,}" for k, v in stats["by_source"].most_common()))
    lines.append("career family:")
    lines += _counts(stats["families"])
    lines.append(f"experience level ({stats['ats_rows']:,} ATS rows, recomputed):")
    lines += _counts(stats["levels"], order=taxonomy.LEVELS)
    changed = sum(stats["level_changes"].values())
    lines.append(f"  level changed on {changed:,} of {stats['ats_rows']:,} ATS rows"
                 + (": " + ", ".join(f"{old or '(none)'} -> {new} {n:,}"
                                      for (old, new), n in stats["level_changes"].most_common(8))
                    if changed else ""))
    lines.append(f"  kept the stored internship/new_grad label on {stats['kept']:,} of "
                 f"{stats['ats_rows']:,} ATS rows (the title alone says mid or above)")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.scripts.backfill_v2_tags",
        description="Tag rows that predate scraper v2 with a career family, and recompute "
                    "ATS rows' experience level (dry run by default).")
    parser.add_argument("--apply", action="store_true", help="write the tags (default: count)")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="rows per transaction")
    args = parser.parse_args(argv)

    from backend.db.database import SessionLocal

    db = SessionLocal()
    try:
        url = db.get_bind().url
        print(f"database: {url.host or '(local file)'}/{url.database}")
        stats = backfill(db, apply=args.apply, batch=max(1, args.batch), progress=print)
        print(format_report(stats))
        if not args.apply:
            print("dry run: nothing changed (add --apply)")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
