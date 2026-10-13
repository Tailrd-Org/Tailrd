"""Determinism (SPEC §10, §13; DECISIONS D-10): the same pair gives the same bytes, every time.

* 1,000 runs of every hand-built pair (tests/fixtures/profiles) give identical canonical JSON
  (`canonical_json`, D-10), and 100 runs with the §17.3 interval, whose two extra readings take the
  same path (slow, about a minute; a 25-run version of both always runs).
* The same pairs scored in two fresh interpreters with different `PYTHONHASHSEED` values give the
  same bytes as each other and as this process: nothing depends on set or dict iteration order of
  hashed strings, on id(), on the clock or on the environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from matcher.models import canonical_json
from tests.test_fixture_pairs import FIXTURES, load, run_fixture

ENGINE_ROOT = Path(__file__).resolve().parents[1]
SLOW_RUNS = 1000
SLOW_INTERVAL_RUNS = 100
FAST_RUNS = 25

# Run in a fresh interpreter: score every fixture (with and without the interval) and print one
# sha256 per result, so the comparison is byte for byte without moving megabytes through a pipe.
_CHILD = """
import hashlib, sys
from matcher.models import canonical_json
from tests.test_fixture_pairs import FIXTURES, load, run_fixture
for path in FIXTURES:
    fixture = load(path)
    for with_interval in (False, True):
        result = run_fixture(fixture, with_interval=with_interval)
        digest = hashlib.sha256(canonical_json(result).encode("utf-8")).hexdigest()
        print(path.stem, with_interval, digest)
"""


def _bytes(fixture: dict, with_interval: bool) -> bytes:
    return canonical_json(run_fixture(fixture, with_interval=with_interval)).encode("utf-8")


def _repeat(runs: int, *, with_interval: bool) -> None:
    for path in FIXTURES:
        fixture = load(path)
        first = _bytes(fixture, with_interval)
        for _ in range(runs - 1):
            assert _bytes(fixture, with_interval) == first, (path.stem, with_interval)


@pytest.mark.parametrize("with_interval", (False, True))
def test_repeated_runs_give_identical_bytes(with_interval: bool) -> None:
    _repeat(FAST_RUNS, with_interval=with_interval)


@pytest.mark.slow
def test_a_thousand_runs_give_identical_bytes() -> None:
    _repeat(SLOW_RUNS, with_interval=False)


@pytest.mark.slow
def test_repeated_interval_runs_give_identical_bytes() -> None:
    _repeat(SLOW_INTERVAL_RUNS, with_interval=True)


def _in_child(hash_seed: str) -> list[str]:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = hash_seed
    env["PYTHONPATH"] = os.pathsep.join((str(ENGINE_ROOT / "src"), str(ENGINE_ROOT)))
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD], cwd=ENGINE_ROOT, env=env, capture_output=True, text=True, timeout=600,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.splitlines()


def test_two_hash_seeds_give_identical_bytes() -> None:
    first, second = _in_child("1"), _in_child("4242")
    assert len(first) == 2 * len(FIXTURES)
    assert first == second
    here = [
        f"{path.stem} {with_interval} {hashlib.sha256(_bytes(load(path), with_interval)).hexdigest()}"
        for path in FIXTURES
        for with_interval in (False, True)
    ]
    assert first == here


def test_canonical_bytes_are_the_d10_serialisation() -> None:
    # D-10: canonical bytes are sorted-key, compact, non-ASCII-preserving JSON of the mode="json" dump.
    result = run_fixture(load(FIXTURES[0]))
    expected = json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert canonical_json(result) == expected
