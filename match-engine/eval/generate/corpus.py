"""Generate the synthetic corpus (EVAL.md section 2.1):

    python -m eval.generate.corpus --profiles 500 --seed 7 --out eval/corpora/synthetic

writes, under the output directory,

    <profile_id>/<template>__<format>.<ext>     8 templates x 6 formats for every profile
    profiles.jsonl                              one CandidateProfile (the ground truth) per line
    postings.jsonl                              {"posting_id", "posting"} per line: the targets
    manifest.json                               seed, counts and the sha256 of every file

The same arguments always give byte-identical files and manifest: nothing reads the clock, and a
file's randomness comes only from (seed, profile, template, format). `eval/corpora/synthetic/` is
git-ignored; the corpus is generated, never committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from eval.generate.postings import generate_postings, posting_id
from eval.generate.profiles import GENERATION_AS_OF, CandidateProfile, generate_profiles
from eval.generate.render import (
    FORMAT_EXTENSIONS,
    FORMATS,
    TEMPLATES,
    build_document,
    render_document,
)
from eval.generate.vocabulary import Vocabulary, default_vocabulary
from matcher.models import JobPosting

DEFAULT_OUT = "eval/corpora/synthetic"


def render_seed(corpus_seed: int, profile_id: str, template: str) -> int:
    """The render seed of one (profile, template): shared by its six formats, so they differ only
    in layout, while every profile and template still gets its own date style and colours."""
    digest = hashlib.sha256(f"{corpus_seed}:{profile_id}:{template}".encode()).digest()
    return int.from_bytes(digest[:6], "big")


def read_profiles(path: Path | str) -> tuple[CandidateProfile, ...]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return tuple(CandidateProfile.model_validate_json(line) for line in lines if line.strip())


def read_postings(path: Path | str) -> dict[str, JobPosting]:
    rows = (json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip())
    return {row["posting_id"]: JobPosting.model_validate(row["posting"]) for row in rows}


# --------------------------------------------------------------------------------------------
# rendering one profile (runs in a worker process when --jobs > 1)
# --------------------------------------------------------------------------------------------

_WORKER_VOCAB: Vocabulary | None = None


def _init_worker(vocab: Vocabulary) -> None:
    global _WORKER_VOCAB
    _WORKER_VOCAB = vocab


def _render_profile(
    task: tuple[CandidateProfile, str, int, tuple[str, ...], tuple[str, ...]],
) -> list[tuple[str, str, int]]:
    """Write every requested rendering of one profile; return (relative path, sha256, bytes)."""
    profile, out_dir, seed, templates, formats = task
    vocab = _WORKER_VOCAB or default_vocabulary()
    folder = Path(out_dir) / profile.profile_id
    folder.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, str, int]] = []
    for template in templates:
        s = render_seed(seed, profile.profile_id, template)
        document = build_document(profile, template, vocab, seed=s)
        for fmt in formats:
            data = render_document(document, fmt, seed=s, template=template, profile_id=profile.profile_id)
            name = f"{template}__{fmt}.{FORMAT_EXTENSIONS[fmt]}"
            (folder / name).write_bytes(data)
            rows.append((f"{profile.profile_id}/{name}", hashlib.sha256(data).hexdigest(), len(data)))
    return rows


# --------------------------------------------------------------------------------------------
# the corpus
# --------------------------------------------------------------------------------------------


def _write(path: Path, text: str) -> str:
    data = text.encode("utf-8")
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def generate_corpus(
    out: Path | str,
    *,
    profiles: int = 500,
    seed: int = 7,
    postings_per_family: int = 5,
    templates: Sequence[str] = TEMPLATES,
    formats: Sequence[str] = FORMATS,
    jobs: int = 1,
    vocab: Vocabulary | None = None,
    log=None,
) -> dict:
    """Write the corpus under `out` and return the manifest (also written as manifest.json)."""
    vocab = vocab or default_vocabulary()
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    postings = generate_postings(vocab, postings_per_family, seed)
    people = generate_profiles(vocab, postings, profiles, seed)

    profiles_sha = _write(out / "profiles.jsonl", "".join(p.model_dump_json() + "\n" for p in people))
    posting_lines = (
        json.dumps(
            {"posting_id": posting_id(p), "posting": p.model_dump(mode="json")},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for p in postings
    )
    postings_sha = _write(out / "postings.jsonl", "".join(line + "\n" for line in posting_lines))

    tasks = [(p, str(out), seed, tuple(templates), tuple(formats)) for p in people]
    files: dict[str, str] = {}
    total_bytes = 0
    started = time.perf_counter()

    def collect(done: int, rows: list[tuple[str, str, int]]) -> None:
        nonlocal total_bytes
        for rel, digest, size in rows:
            files[rel] = digest
            total_bytes += size
        if log and (done % 25 == 0 or done == len(tasks)):
            log(
                f"{done}/{len(tasks)} profiles, {len(files)} files, {total_bytes / 1e6:.0f} MB, "
                f"{time.perf_counter() - started:.0f} s"
            )

    if jobs <= 1:
        _init_worker(vocab)
        for done, task in enumerate(tasks, start=1):
            collect(done, _render_profile(task))
    else:
        with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker, initargs=(vocab,)) as pool:
            for done, rows in enumerate(pool.map(_render_profile, tasks, chunksize=1), start=1):
                collect(done, rows)

    manifest = {
        "seed": seed,
        "as_of": GENERATION_AS_OF.isoformat(),
        "templates": list(templates),
        "formats": list(formats),
        "postings_per_family": postings_per_family,
        "counts": {"profiles": len(people), "postings": len(postings), "files": len(files)},
        "bytes": total_bytes,
        "profiles_sha256": profiles_sha,
        "postings_sha256": postings_sha,
        "files": dict(sorted(files.items())),
    }
    # bytes, never text mode: Windows would turn "\n" into "\r\n" and the manifest would differ per platform
    (out / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    return manifest


# --------------------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------------------


def _choices(parser: argparse.ArgumentParser, flag: str, raw: str, allowed: Sequence[str]) -> tuple[str, ...]:
    picked = tuple(part.strip() for part in raw.split(",") if part.strip())
    unknown = [p for p in picked if p not in allowed]
    if unknown or not picked:
        parser.error(f"{flag}: expected a comma-separated subset of {', '.join(allowed)} (got {raw!r})")
    return picked


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.generate.corpus",
        description="Generate the synthetic evaluation corpus: profiles, postings and 8 x 6 renderings each.",
    )
    parser.add_argument(
        "--profiles", type=int, default=500, help="number of candidate profiles (default 500)"
    )
    parser.add_argument("--seed", type=int, default=7, help="corpus seed (default 7)")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"output directory (default {DEFAULT_OUT})")
    parser.add_argument("--postings-per-family", type=int, default=5, help="postings per family (default 5)")
    parser.add_argument(
        "--jobs", type=int, default=0, help="worker processes (default 0 = up to 8, by core count)"
    )
    parser.add_argument(
        "--templates", default=",".join(TEMPLATES), help="comma-separated subset of templates"
    )
    parser.add_argument("--formats", default=",".join(FORMATS), help="comma-separated subset of formats")
    parser.add_argument("--quiet", action="store_true", help="no progress output")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.profiles < 1 or args.postings_per_family < 1:
        parser.error("--profiles and --postings-per-family must be at least 1")
    templates = _choices(parser, "--templates", args.templates, TEMPLATES)
    formats = _choices(parser, "--formats", args.formats, FORMATS)
    jobs = args.jobs if args.jobs > 0 else max(1, min(os.cpu_count() or 1, 8))
    log = None if args.quiet else (lambda line: print(line, file=sys.stderr, flush=True))
    started = time.perf_counter()
    manifest = generate_corpus(
        args.out,
        profiles=args.profiles,
        seed=args.seed,
        postings_per_family=args.postings_per_family,
        templates=templates,
        formats=formats,
        jobs=jobs,
        log=log,
    )
    if log:
        counts = manifest["counts"]
        log(
            f"done: {counts['profiles']} profiles, {counts['postings']} postings, {counts['files']} files, "
            f"{manifest['bytes'] / 1e6:.0f} MB in {time.perf_counter() - started:.0f} s with {jobs} workers "
            f"-> {args.out}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
