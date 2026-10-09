"""Reject any commit that touches the holdout corpus (EVAL §0).

Used by the repository's pre-commit config with the staged paths as arguments. With no
arguments it checks `git diff --cached --name-only` itself, so it also works as a plain git
hook. Exit status 1 blocks the commit.
"""

from __future__ import annotations

import subprocess
import sys

HOLDOUT_PREFIX = "match-engine/eval/corpora/holdout/"


def holdout_paths(paths: list[str]) -> list[str]:
    hits = []
    for raw in paths:
        path = raw.replace("\\", "/").lstrip("./")
        if path.startswith(HOLDOUT_PREFIX) or f"/{HOLDOUT_PREFIX}" in f"/{path}":
            hits.append(raw)
    return hits


def staged_paths() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only"], capture_output=True, text=True, check=True
    )
    return [line for line in out.stdout.splitlines() if line.strip()]


def main(argv: list[str]) -> int:
    paths = argv if argv else staged_paths()
    hits = holdout_paths(paths)
    if hits:
        print("Blocked: commits may not touch the holdout corpus (match-engine EVAL.md §0):")
        for hit in hits:
            print(f"  {hit}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
