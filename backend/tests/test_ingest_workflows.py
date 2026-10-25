"""What the v2 ingest runner is installed and scheduled with.

.github/workflows/ingest.yml holds a database credential in a public
repository, so the guard rails are tested here the way scrape-jobs.yml's are
in test_scrape_jobs_workflow.py: static checks on the workflow files, and the
shell steps run for real under bash.

requirements-ingest.txt is the only thing that workflow installs.
"""

import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REQUIREMENTS = REPO / "requirements-ingest.txt"

# --- requirements-ingest.txt ---------------------------------------------


def _requirements():
    """{normalized name: (version, [sha256, ...])} for every requirement in
    requirements-ingest.txt. A requirement is one logical line: the pin, an
    optional environment marker, then its --hash options, joined across the
    trailing backslashes. Anything else that is not a comment fails the test
    that calls this."""
    parsed = {}
    logical = ""
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not logical and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            logical += " " + line[:-1].strip()
            continue
        logical = (logical + " " + line).strip()
        match = re.fullmatch(
            r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9.!+_-]+)"
            r"(?:\s*;\s*(?P<marker>.+?))?"
            r"(?P<hashes>(?:\s+--hash=sha256:[0-9a-f]{64})+)",
            logical,
        )
        assert match, f"not a pinned, hashed requirement: {logical[:120]!r}"
        key = _normalize(match["name"])
        assert key not in parsed, f"{key} is listed twice"
        parsed[key] = (match["version"], re.findall(r"[0-9a-f]{64}", match["hashes"]))
        logical = ""
    assert not logical, "the file ends inside a continued line"
    return parsed


def _normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _pins(path):
    """{normalized name: version} for the exact pins in a plain requirements
    file (extras and trailing comments dropped)."""
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([A-Za-z0-9.]+)", line)
        if match:
            pins[_normalize(match[1])] = match[2]
    return pins


def test_every_requirement_is_pinned_and_carries_hashes():
    requirements = _requirements()
    assert requirements
    for name, (version, hashes) in requirements.items():
        assert version and hashes, name


def test_the_file_takes_nothing_from_another_index_or_a_url():
    # Only comments, blank lines and pinned, hashed requirements: no -r, -e,
    # --index-url, --extra-index-url, --find-links or "name @ url".
    # _requirements() rejects every such line as "not a pinned, hashed
    # requirement"; a URL inside a marker would slip past it, so look for those.
    _requirements()
    code = [ln for ln in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if "://" in ln or " @ " in ln]


def test_python_jobspy_is_never_installed_with_the_database_credential():
    # scrape-jobs.yml runs python-jobspy and deliberately holds no database
    # credential. This workflow holds one, so that package must never be here.
    jobspy = {name for name in _requirements() if "jobspy" in name}
    assert not jobspy, f"{sorted(jobspy)} must not be installed next to the database credential"


def test_the_driver_the_app_builds_its_engine_with_is_listed():
    # backend/db/database.py (and the runner's own lock engine) rewrite every
    # Postgres URL to postgresql+pg8000, so pg8000 is the driver; psycopg2 is
    # in backend/requirements.txt but nothing imports it.
    database = (REPO / "backend" / "db" / "database.py").read_text(encoding="utf-8")
    assert "postgresql+pg8000" in database
    requirements = _requirements()
    assert "pg8000" in requirements, "the engine the runner builds needs pg8000"
    psycopg = {name for name in requirements if name.startswith("psycopg")}
    assert not psycopg, f"{sorted(psycopg)}: nothing imports psycopg2, so it only widens the install"


def test_the_runner_runs_the_versions_the_tests_and_the_app_pin():
    # Where backend/requirements.txt pins a package the runner needs, the
    # runner gets the same version: what CI tested is what ingests. A bump
    # there fails this test until requirements-ingest.txt is regenerated.
    # pydantic is the exception (the web tests pin 2.8.0, Vercel 2.11.7), so it
    # follows api/requirements.txt, the production manifest; pg8000 is pinned
    # only in the root requirements.txt.
    requirements = _requirements()
    backend = _pins(REPO / "backend" / "requirements.txt")
    expected = {name: backend[name] for name in ("sqlalchemy", "httpx", "python-dotenv", "pillow", "nh3")}
    expected["pydantic"] = _pins(REPO / "api" / "requirements.txt")["pydantic"]
    expected["pg8000"] = _pins(REPO / "requirements.txt")["pg8000"]
    pinned = {name: requirements[name][0] for name in expected}
    assert pinned == expected, "regenerate requirements-ingest.txt (steps in its header)"


# The third-party packages the runner reaches by import. Read from the source,
# not by importing it, so a lazy import inside a function counts too.

# The import name of a package whose distribution is named differently.
_DISTRIBUTION = {"PIL": "pillow", "dotenv": "python-dotenv", "yaml": "pyyaml"}
# Imported inside functions of services/resume_parser.py. backend/services/
# __init__.py imports that module, so the runner loads it, but only résumé
# parsing calls those functions and the runner never does.
_NEVER_CALLED_BY_THE_RUNNER = {"docx", "fitz", "pdfplumber"}


def _module_file(name):
    base = REPO.joinpath(*name.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _imports_of(path, module):
    """Every module name a file imports, anywhere in it: at the top, inside
    functions, and importlib.import_module("literal")."""
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                base = ".".join(parts[: len(parts) - (node.level - 1)])
                target = f"{base}.{node.module}" if node.module else base
            else:
                target = node.module or ""
            found.append(target)
            found.extend(f"{target}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in ("import_module", "__import__") and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append(arg.value)
    return found


def _runner_third_party_imports():
    """The top-level names of the third-party packages the runner can import:
    everything reachable from backend.ingest.run, every adapter module the
    registry discovers, and the boot migrations the runner applies. Importing
    a module runs its parent packages' __init__ first, so those count."""
    from backend.ingest import run as runner

    roots = ["backend.ingest.run", *runner.boot_migrations()]
    roots += [f"backend.ingest.adapters.{p.stem}"
              for p in (REPO / "backend" / "ingest" / "adapters").glob("*.py")]
    seen, third_party, queue = set(), set(), list(roots)
    while queue:
        module = queue.pop()
        path = _module_file(module) if module not in seen else None
        if path is None:
            continue
        seen.add(module)
        parts = module.split(".")
        queue.extend(".".join(parts[:i]) for i in range(1, len(parts)))
        for imported in _imports_of(path, module):
            top = imported.split(".")[0]
            if top == "backend":
                queue.append(imported)
            elif top not in sys.stdlib_module_names:
                third_party.add(top)
    return third_party


def test_the_runner_imports_nothing_that_is_not_installed_with_it():
    listed = set(_requirements())
    needed = {_normalize(_DISTRIBUTION.get(top, top))
              for top in _runner_third_party_imports() - _NEVER_CALLED_BY_THE_RUNNER}
    assert needed
    assert needed <= listed, (
        f"the runner imports {sorted(needed - listed)}: add them to requirements-ingest.txt "
        "(see the regeneration steps in its header), or the daily ingest fails at import"
    )
