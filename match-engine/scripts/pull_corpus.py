"""Pull Tailrd's scraped job postings into data/raw/corpus/ for the corpus statistics (Task 2.3).

SPEC §3.1-§3.2: IDF and PMI edges are "computed offline from our own job corpus". This script reads
ONLY `DATABASE_URL`, from `--env-file` when the file has it, else from the environment, connects
over TLS with pg8000 and runs one SELECT inside a read-only transaction:

    SELECT id, title, description, role_category, experience_level, first_seen_at, scraped_at
    FROM scraped_jobs WHERE length(coalesce(description,'')) > 200 AND duplicate_of IS NULL
    ORDER BY id [LIMIT n]

It writes, under data/raw/ (git-ignored, DECISIONS D-04: scraped text never leaves it):

    data/raw/corpus/postings.jsonl.gz   one JSON object per posting, in id order
    data/raw/corpus/pull_record.json    row count, date ranges, the file's sha256, the query, a label

The connection string is never printed, logged or written; an error names the failure, with the
URL's parts masked. Run from match-engine/:

    .venv/Scripts/python.exe scripts/pull_corpus.py --env-file <path to .env> [--limit N]
        [--out DIR] [--source "label for SOURCES.md"]

Then scripts/compute_idf.py and scripts/compute_pmi.py turn the file into the committed statistics.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "data" / "raw" / "corpus"
POSTINGS = "postings.jsonl.gz"
RECORD = "pull_record.json"
COLUMNS = ("id", "title", "description", "role_category", "experience_level", "first_seen_at", "scraped_at")
QUERY = (
    "SELECT id, title, description, role_category, experience_level, first_seen_at, scraped_at "
    "FROM scraped_jobs WHERE length(coalesce(description,'')) > 200 AND duplicate_of IS NULL ORDER BY id"
)
DEFAULT_SOURCE = "Tailrd scraped postings (scraped_jobs)"


class PullError(RuntimeError):
    """The pull cannot run; the message never contains the connection string."""


def database_url(env_file: Optional[Path]) -> str:
    """`DATABASE_URL` from `env_file` (a KEY=VALUE file; no other key is kept), else the environment."""
    if env_file is not None:
        try:
            text = env_file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise PullError(f"cannot read the env file ({type(exc).__name__})") from None
        for line in text.splitlines():
            key, sep, value = line.strip().partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if key != "DATABASE_URL" or not sep:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                return value
    value = os.environ.get("DATABASE_URL", "").strip()
    if not value:
        raise PullError("DATABASE_URL is neither in the env file nor in the environment")
    return value


def connection_parts(url: str) -> dict[str, Any]:
    """pg8000 keyword arguments for a postgres:// URL (TLS is added by `connect`)."""
    try:
        parts = urlsplit(url)
        port = parts.port or 5432
    except ValueError:
        raise PullError("DATABASE_URL is not a valid URL") from None
    if parts.scheme not in ("postgres", "postgresql") or not parts.hostname or not parts.username:
        raise PullError("DATABASE_URL is not a postgres://user@host/database URL")
    return {
        "user": unquote(parts.username),
        "password": unquote(parts.password) if parts.password is not None else None,
        "host": parts.hostname,
        "port": port,
        "database": unquote(parts.path.lstrip("/")) or None,
    }


def _masked(message: str, secrets: Iterable[Optional[str]]) -> str:
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        message = message.replace(secret, "***")
    return message


def fetch(url: str, limit: Optional[int]) -> tuple[list[str], list[Sequence[Any]]]:
    """Run the one SELECT in a read-only transaction; (column names, rows in id order)."""
    import pg8000.native  # the eval extra (pyproject); imported here so --help needs no driver

    kwargs = connection_parts(url)
    secrets = (url, kwargs["password"], kwargs["user"], kwargs["host"], kwargs["database"])
    query = QUERY if limit is None else f"{QUERY} LIMIT :limit"
    params = {} if limit is None else {"limit": limit}
    try:
        connection = pg8000.native.Connection(
            **kwargs, ssl_context=ssl.create_default_context(), timeout=120,
            application_name="tailrd-match-engine-corpus-pull",
        )
    except Exception as exc:  # the driver's message can carry the host or the user: mask them
        raise PullError(f"cannot connect: {type(exc).__name__}: {_masked(str(exc), secrets)}") from None
    try:
        connection.run("START TRANSACTION READ ONLY")  # nothing in this session can write
        rows = connection.run(query, **params)
        columns = [column["name"] for column in connection.columns]
        connection.run("ROLLBACK")
    except Exception as exc:
        raise PullError(f"query failed: {type(exc).__name__}: {_masked(str(exc), secrets)}") from None
    finally:
        try:
            connection.close()
        except Exception:
            pass
    if tuple(columns) != COLUMNS:
        raise PullError(f"unexpected columns {columns}")
    return columns, rows


def _value(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


def _span(values: Iterable[Optional[str]]) -> Optional[list[str]]:
    present = sorted(v for v in values if v)
    return [present[0], present[-1]] if present else None


def write_corpus(out_dir: Path, columns: Sequence[str], rows: Sequence[Sequence[Any]], *, source: str,
                 limit: Optional[int]) -> dict[str, Any]:
    """Write the postings (gzip without a timestamp: the bytes depend on the rows only) and the
    pull record; return the record."""
    out_dir.mkdir(parents=True, exist_ok=True)
    records = [{name: _value(value) for name, value in zip(columns, row)} for row in rows]
    temporary = out_dir / (POSTINGS + ".tmp")
    with open(temporary, "wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as fh:
        for record in records:
            fh.write((json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
    os.replace(temporary, out_dir / POSTINGS)
    digest = hashlib.sha256((out_dir / POSTINGS).read_bytes()).hexdigest()
    record = {
        "format": 1,
        "source": source,
        "query": QUERY + ("" if limit is None else f" LIMIT {limit}"),
        "limit": limit,
        "rows": len(records),
        "first_seen_at": _span(r["first_seen_at"] for r in records),
        "scraped_at": _span(r["scraped_at"] for r in records),
        "file": POSTINGS,
        "sha256": digest,
        "pulled_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
    }
    (out_dir / RECORD).write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return record


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env-file", type=Path, default=None,
                        help="a KEY=VALUE file holding DATABASE_URL (else the environment's)")
    parser.add_argument("--limit", type=int, default=None, help="pull only the first N postings by id")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="what the corpus is, for SOURCES.md")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    try:
        columns, rows = fetch(database_url(args.env_file), args.limit)
    except PullError as exc:
        print(f"pull_corpus: {exc}", file=sys.stderr)
        return 1
    record = write_corpus(args.out, columns, rows, source=args.source, limit=args.limit)
    print(f"pulled {record['rows']:,} postings into {args.out / POSTINGS} (sha256 {record['sha256'][:12]}...); "
          f"first_seen_at {record['first_seen_at']}, scraped_at {record['scraped_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
