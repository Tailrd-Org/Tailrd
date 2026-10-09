"""The calibrator protocol and the test-only FixedCalibrator (SPEC §7; plan Task 7.1 part A).

Also guards the package rule from the task brief: calibration is plain Python (standard library
plus pydantic), deterministic, and only `load_panel` ever touches a file.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

import matcher.calibrate as calibrate_package
from matcher.calibrate import Calibrator, FixedCalibrator
from matcher.models import CalibrationInfo

TEST_INFO = CalibrationInfo(method="identity", scope="test", n=0, version="test")


# ─── FixedCalibrator ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        (0.0, 0),
        (0.004, 0),  # 0.4 rounds down
        (0.006, 1),  # 0.6 rounds up
        (0.25, 25),
        (0.5, 50),
        (0.734, 73),  # 73.4
        (0.736, 74),  # 73.6
        (1.0, 100),
    ],
)
def test_fixed_calibrator_is_round_of_100_times_raw(raw: float, expected: int) -> None:
    score, info = FixedCalibrator().calibrate(raw)
    assert score == expected
    assert info == TEST_INFO


def test_fixed_calibrator_reports_the_identity_test_calibration() -> None:
    _, info = FixedCalibrator().calibrate(0.5)
    assert (info.method, info.scope, info.n, info.version) == ("identity", "test", 0, "test")


@pytest.mark.parametrize("raw, expected", [(-0.4, 0), (-1e9, 0), (1.2, 100), (1e9, 100)])
def test_fixed_calibrator_clamps_to_0_100(raw: float, expected: int) -> None:
    assert FixedCalibrator().calibrate(raw)[0] == expected


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_fixed_calibrator_refuses_a_non_finite_score(bad: float) -> None:
    with pytest.raises(ValueError):
        FixedCalibrator().calibrate(bad)


def test_fixed_calibrator_refuses_things_that_are_not_numbers() -> None:
    for bad in ("0.5", None, [0.5], True):
        with pytest.raises(TypeError):
            FixedCalibrator().calibrate(bad)  # type: ignore[arg-type]


def test_fixed_calibrator_scope_has_a_basis_sentence(config) -> None:
    # bands.basis[info.scope] is how the explanation names the reference group (D-50).
    assert config.bands.basis[TEST_INFO.scope] == "Test calibration"


@given(a=st.floats(-2.0, 3.0), b=st.floats(-2.0, 3.0))
def test_fixed_calibrator_is_monotone_and_bounded(a: float, b: float) -> None:
    low, high = sorted((a, b))
    calibrator = FixedCalibrator()
    low_score, high_score = calibrator.calibrate(low)[0], calibrator.calibrate(high)[0]
    assert 0 <= low_score <= high_score <= 100


# ─── The protocol ──────────────────────────────────────────────────────────────────────────


def test_protocol_is_checkable_at_run_time() -> None:
    assert isinstance(FixedCalibrator(), Calibrator)

    class NotACalibrator:
        def score(self, raw: float) -> int:
            return 0

    assert not isinstance(NotACalibrator(), Calibrator)


# ─── Package rule: pure Python, one reader of files ────────────────────────────────────────

CALIBRATE_DIR = Path(calibrate_package.__file__).resolve().parent
MODULES = sorted(CALIBRATE_DIR.glob("*.py"))
# Imports that bring I/O, a clock, randomness or the network into a module.
IMPURE = set(
    "os sys io time datetime random secrets socket subprocess shutil tempfile urllib http ssl "
    "threading multiprocessing sqlite3".split()
)
FILE_CALLS = {"open", "read_text", "read_bytes", "write_text", "write_bytes", "mkdir", "unlink", "touch"}


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_package_is_not_empty() -> None:
    names = {path.name for path in MODULES}
    assert {"__init__.py", "base.py", "reference.py", "percentile.py", "isotonic.py"} <= names


def test_calibration_imports_only_the_standard_library_and_pydantic() -> None:
    allowed = set(sys.stdlib_module_names) | {"pydantic", "matcher"}
    for path in MODULES:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                root = module.split(".")[0]
                assert root in allowed, f"{path.name} imports {root}: not standard library or pydantic"
                assert root not in IMPURE, f"{path.name} imports {root}: no I/O, clock or randomness"
                if root == "matcher":
                    # models (the contract) and this package only: not config (PyYAML), parse or ingest.
                    ok = module == "matcher.models" or module.startswith("matcher.calibrate")
                    assert ok, f"{path.name} imports {module}"


def test_only_reference_py_reads_a_file() -> None:
    for path in MODULES:
        if path.name == "reference.py":
            continue
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            assert name not in FILE_CALLS, f"{path.name} calls {name}(): only load_panel does file I/O"
