"""Download the skill taxonomy's public sources into data/raw/ and record their sha256 (Task 2.2).

Run from match-engine/ (network required; never run by the tests):

    .venv/Scripts/python.exe scripts/fetch_sources.py [--force] [--only NAME ...]

Every download lands under `data/raw/` (git-ignored: DECISIONS D-04). Archives are unpacked next
to their download, member by member, refusing any member path that would leave its directory;
the NAICS workbook is converted to a tab-separated `code<TAB>title` file the same way (an .xlsx
file is a zip of XML), so every input of `scripts/build_taxonomy.py` is plain text.
`data/raw/fetch_record.json` records, per source, the URL, the file, its byte size and sha256,
and the date it was fetched; `scripts/build_taxonomy.py` copies the hashes into
`data/taxonomy/SOURCES.md` and MANIFEST.json so every artifact names the exact inputs it came
from. A source that cannot be fetched is recorded with its error and the build goes on without
it (SOURCES.md then says so).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
RECORD = "fetch_record.json"
USER_AGENT = "tailrd-match-engine-taxonomy-build/1.0 (+https://www.tailrd.ca)"
PAUSE_SECONDS = 2.0  # between requests to the same publishers: be polite
RETRIES = 3


@dataclass(frozen=True)
class Download:
    name: str
    title: str
    url: str
    file: str  # relative to data/raw/
    extract_to: Optional[str]  # relative to data/raw/; None keeps the file as is
    licence: str
    convert_to: Optional[str] = None  # .xlsx only: first sheet's first two columns as a TSV


ESCO_VERSION = "v1.2.1"
_ESCO = "https://ec.europa.eu/esco/download/ESCO%20dataset%20-%20{v}%20-%20classification%20-%20{lang}%20-%20csv.zip"
_ESCO_LICENCE = (
    "European Commission reuse policy (Decision 2011/833/EU), equivalent to CC BY 4.0; "
    "attribution: ESCO, European Commission"
)

DOWNLOADS: tuple[Download, ...] = (
    Download(
        "esco_en",
        f"ESCO {ESCO_VERSION} classification, English, CSV",
        _ESCO.format(v=ESCO_VERSION, lang="en"),
        f"esco/esco-{ESCO_VERSION}-en-csv.zip",
        f"esco/{ESCO_VERSION}/en",
        _ESCO_LICENCE,
    ),
    Download(
        "esco_fr",
        f"ESCO {ESCO_VERSION} classification, French, CSV",
        _ESCO.format(v=ESCO_VERSION, lang="fr"),
        f"esco/esco-{ESCO_VERSION}-fr-csv.zip",
        f"esco/{ESCO_VERSION}/fr",
        _ESCO_LICENCE,
    ),
    Download(
        "onet_db",
        "O*NET 30.0 database, text files",
        "https://www.onetcenter.org/dl_files/database/db_30_0_text.zip",
        "onet/db_30_0_text.zip",
        "onet",
        "CC BY 4.0; attribution: O*NET 30.0 Database by the U.S. Department of Labor, Employment and "
        "Training Administration (USDOL/ETA)",
    ),
    Download(
        "onet_military",
        "O*NET Military Occupational Classification (MOC) crosswalk, updated August 2024",
        "https://www.onetcenter.org/dl_files/2019/military_crosswalk.zip",
        "onet/military_crosswalk.zip",
        "onet/military_crosswalk",
        "CC BY 4.0; attribution: O*NET Resource Center, USDOL/ETA (MOC data from the Defense Manpower "
        "Data Center)",
    ),
    Download(
        "naics_2022",
        "NAICS 2022, 2-6 digit codes and titles (U.S. Census Bureau)",
        "https://www.census.gov/naics/2022NAICS/2-6%20digit_2022_Codes.xlsx",
        "naics/2-6 digit_2022_Codes.xlsx",
        None,
        "U.S. Government work (public domain)",
        convert_to="naics/naics_2022_codes.tsv",
    ),
)

_XLSX_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fetch(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    last_error: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as out:
                shutil.copyfileobj(response, out, 1 << 20)
            partial.replace(target)
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(PAUSE_SECONDS * attempt)
    raise RuntimeError(f"giving up on {url}: {last_error}")


def _extract(archive: Path, destination: Path) -> list[str]:
    """Unpack every member of `archive` under `destination`; refuses paths that escape it."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    written = []
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            if member.is_dir():
                continue
            target = (destination / member.filename).resolve()
            if root not in target.parents:
                raise RuntimeError(f"{archive.name}: member {member.filename!r} escapes {destination}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
            written.append(target.relative_to(root).as_posix())
    return sorted(written)


def xlsx_rows(path: Path) -> list[list[str]]:
    """The first worksheet of an .xlsx file as rows of cell texts (stdlib only).

    Handles shared strings, inline strings and plain values; a cell missing from a row (an
    empty cell) becomes "" so columns keep their positions.
    """
    with zipfile.ZipFile(path) as book:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in book.namelist():
            for item in ET.fromstring(book.read("xl/sharedStrings.xml")).iter(f"{_XLSX_NS}si"):
                shared.append("".join(text.text or "" for text in item.iter(f"{_XLSX_NS}t")))
        sheet = ET.fromstring(book.read("xl/worksheets/sheet1.xml"))
    rows = []
    for row in sheet.iter(f"{_XLSX_NS}row"):
        cells: dict[int, str] = {}
        for cell in row.iter(f"{_XLSX_NS}c"):
            column = _column_index(cell.get("r", ""))
            kind, value = cell.get("t"), cell.find(f"{_XLSX_NS}v")
            if kind == "s" and value is not None:
                text = shared[int(value.text)]
            elif kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(f"{_XLSX_NS}t"))
            else:
                text = value.text if value is not None and value.text is not None else ""
            cells[column] = text
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


def _column_index(reference: str) -> int:
    letters = "".join(ch for ch in reference if ch.isalpha())
    index = 0
    for ch in letters.upper():
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index - 1


def _convert_xlsx(source: Path, target: Path) -> int:
    """Write `code<TAB>title` for every row whose first two columns after the sequence number
    hold a code and a title (the census layout: Seq. No., Code, Title)."""
    lines = ["code\ttitle"]
    for row in xlsx_rows(source)[1:]:
        if len(row) < 3 or not row[1].strip() or not row[2].strip():
            continue
        code, title = row[1].strip(), " ".join(row[2].split())
        lines.append(f"{code}\t{title}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return len(lines) - 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="download again even if the file exists")
    parser.add_argument("--only", nargs="*", default=None, help="source names to fetch (default: all)")
    args = parser.parse_args(argv)

    record_path = RAW_DIR / RECORD
    record: dict = {"format": 1, "downloads": {}}
    if record_path.exists():
        record = json.loads(record_path.read_text(encoding="utf-8"))

    failures = 0
    for index, item in enumerate(DOWNLOADS):
        if args.only is not None and item.name not in args.only:
            continue
        target = RAW_DIR / item.file
        entry = {"title": item.title, "url": item.url, "file": item.file, "licence": item.licence}
        try:
            if args.force or not target.exists():
                if index:
                    time.sleep(PAUSE_SECONDS)
                print(f"fetching {item.name}: {item.url}")
                _fetch(item.url, target)
                entry["fetched"] = dt.date.today().isoformat()
            else:
                entry["fetched"] = record["downloads"].get(item.name, {}).get("fetched", "unknown")
            entry["bytes"] = target.stat().st_size
            entry["sha256"] = _sha256(target)
            if item.extract_to is not None:
                entry["extracted_to"] = item.extract_to
                entry["members"] = len(_extract(target, RAW_DIR / item.extract_to))
            if item.convert_to is not None:
                entry["converted_to"] = item.convert_to
                entry["rows"] = _convert_xlsx(target, RAW_DIR / item.convert_to)
            print(f"  ok {item.name}: {entry['bytes']:,} bytes sha256={entry['sha256'][:16]}...")
        except Exception as exc:  # noqa: BLE001 - recorded, reported, and the build goes on without it
            failures += 1
            entry["error"] = f"{type(exc).__name__}: {exc}"
            print(f"  FAILED {item.name}: {entry['error']}", file=sys.stderr)
        record["downloads"][item.name] = entry

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    record_path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {record_path.relative_to(ROOT)} ({failures} failed)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
