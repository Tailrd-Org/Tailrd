"""`matcher.audit` is runtime code: standard library and pydantic only (DECISIONS D-02; Task 7.2).

No numpy, no `eval`, no I/O and no clock: the audit functions are pure, so they can run inside the
backend next to the scorer and be tested without files.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

import matcher.audit

ENGINE_ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = Path(matcher.audit.__file__).parent
ALLOWED_MATCHER = ("matcher.models", "matcher.errors", "matcher.audit")
IMPURE_STDLIB = {
    "os",
    "io",
    "pathlib",
    "shutil",
    "tempfile",
    "glob",
    "socket",
    "subprocess",
    "urllib",
    "http",
    "time",
    "random",
    "secrets",
    "sqlite3",
    "datetime",
}
CLOCK_METHODS = {"today", "now", "utcnow"}
IO_BUILTINS = {"open", "input", "print"}


def audit_files() -> list[Path]:
    files = sorted(AUDIT_DIR.glob("*.py"))
    assert {f.name for f in files} >= {"__init__.py", "trace.py", "fairness.py", "drift.py"}
    return files


@pytest.mark.parametrize("path", audit_files(), ids=lambda p: p.name)
def test_audit_modules_import_only_the_standard_library_pydantic_and_the_contracts(path: Path) -> None:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names = [node.module or ""]
        else:
            continue
        for name in names:
            top = name.split(".")[0]
            if top == "matcher":
                assert name.startswith(ALLOWED_MATCHER), f"{path.name} imports {name}"
            else:
                assert top == "pydantic" or top in sys.stdlib_module_names, f"{path.name} imports {name}"
                assert top not in IMPURE_STDLIB, f"{path.name} imports {name}"
                assert top != "eval"


@pytest.mark.parametrize("path", audit_files(), ids=lambda p: p.name)
def test_audit_modules_do_no_io_and_read_no_clock(path: Path) -> None:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            assert func.id not in IO_BUILTINS, f"{path.name}:{node.lineno} calls {func.id}()"
        elif isinstance(func, ast.Attribute):
            assert func.attr not in CLOCK_METHODS, f"{path.name}:{node.lineno} reads the clock"


def test_importing_audit_loads_no_numerical_library_and_not_the_eval_harness() -> None:
    code = (
        "import sys\n"
        "import matcher.audit\n"
        "banned = ('numpy', 'scipy', 'sklearn', 'pandas', 'eval', 'optuna', 'yaml')\n"
        "print(sorted(name for name in banned if name in sys.modules))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(ENGINE_ROOT / "src"))
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ENGINE_ROOT, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


def test_the_package_exports_the_task_interfaces() -> None:
    assert set(matcher.audit.__all__) >= {
        "DriftAlarm",
        "FairnessReport",
        "build_trace",
        "drift_alarms",
        "four_fifths",
        "opaque_id",
        "psi",
        "trace_digest",
    }
    for name in matcher.audit.__all__:
        assert hasattr(matcher.audit, name)
