"""The pre-commit guard blocks holdout paths (EVAL §0)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "guard_holdout.py"
_spec = importlib.util.spec_from_file_location("guard_holdout", _PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def test_blocks_holdout_paths() -> None:
    assert guard.main(["match-engine/eval/corpora/holdout/labels.enc"]) == 1
    assert guard.main(["README.md", "match-engine\\eval\\corpora\\holdout\\pairs\\p1.json"]) == 1


def test_allows_everything_else() -> None:
    assert guard.main(["match-engine/eval/corpora/golden/README.md", "backend/main.py"]) == 0
    assert guard.holdout_paths(["match-engine/eval/corpora/holdouts.md"]) == []
