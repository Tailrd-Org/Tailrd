"""What the v2 ingest runner is installed and scheduled with.

.github/workflows/ingest.yml holds a database credential in a public
repository, so the guard rails are tested here the way scrape-jobs.yml's are
in test_scrape_jobs_workflow.py: static checks on the workflow files, and the
shell steps run for real under bash.

requirements-ingest.txt is the only thing that workflow installs.
"""

import ast
import os
import re
import shlex
import sys
from pathlib import Path

import yaml

# The bash fixture (a stub `gh` first on PATH, scripts run as the runner runs
# them: bash -eo pipefail) and the ${{ }} renderer live with scrape-jobs.yml's
# tests; the ingest workflow's inactivity step is the same step.
from backend.tests import test_scrape_jobs_workflow as scrape_tests
from backend.tests.test_scrape_jobs_workflow import bash  # noqa: F401  (a fixture)

REPO = Path(__file__).resolve().parents[2]
REQUIREMENTS = REPO / "requirements-ingest.txt"
WORKFLOWS = REPO / ".github" / "workflows"
INGEST = WORKFLOWS / "ingest.yml"
SCRAPE = WORKFLOWS / "scrape-jobs.yml"

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


# --- ingest.yml -----------------------------------------------------------


def _workflow(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow):
    # PyYAML (YAML 1.1) reads the key `on` as the boolean True.
    return workflow["on"] if "on" in workflow else workflow[True]


def _ingest_steps():
    return _workflow(INGEST)["jobs"]["ingest"]["steps"]


def _ingest_step(step_id):
    for step in _ingest_steps():
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"ingest.yml has no step with id {step_id!r}")


def _adapter_secret_names():
    """The env vars the adapters need before they switch on."""
    from backend.ingest import adapters

    names = set()
    for platform in adapters.adapter_platforms():
        names.update(getattr(adapters.get_adapter(platform), "REQUIRED_ENV", ()) or ())
    return names


def test_ingest_runs_daily_at_an_off_peak_minute_and_can_be_started_by_hand():
    triggers = _triggers(_workflow(INGEST))
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert len(crons) == 1, "one fire a day: a second one would crawl every board twice"
    minute, hour, day, month, weekday = crons[0].split()

    assert hour.isdigit() and (day, month, weekday) == ("*", "*", "*")
    # GitHub's queue is most contended at the top of the hour, where fires are
    # delayed or dropped; and scrape-jobs.yml already fires on its own minutes.
    assert minute.isdigit() and minute != "0"
    scrape_minutes = {entry["cron"].split()[0] for entry in _triggers(_workflow(SCRAPE))["schedule"]}
    assert minute not in scrape_minutes
    assert "workflow_dispatch" in triggers


def test_ingest_token_is_read_only_and_no_job_widens_it():
    workflow = _workflow(INGEST)

    assert workflow["permissions"] == {"contents": "read"}
    assert all("permissions" not in job for job in workflow["jobs"].values())


def test_ingest_actions_are_pinned_by_commit_sha():
    refs = [step["uses"] for step in _ingest_steps() if "uses" in step]

    assert refs
    for ref in refs:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_ingest_checkout_does_not_leave_the_token_on_disk():
    checkouts = [s for s in _ingest_steps() if str(s.get("uses", "")).startswith("actions/checkout@")]

    assert checkouts
    for step in checkouts:
        assert (step.get("with") or {}).get("persist-credentials") is False


def test_ingest_runs_on_a_pinned_image_and_cannot_hang_the_queue():
    job = _workflow(INGEST)["jobs"]["ingest"]
    budget = int(re.search(r"--budget-minutes (\d+)", _ingest_step("ingest")["run"]).group(1))

    assert re.fullmatch(r"ubuntu-\d\d\.\d\d", job["runs-on"])
    assert budget <= 180, "a full run has to finish in under 3 hours"
    # Room above the runner's own budget for the install, the sweeps and the summary.
    assert budget + 20 <= job["timeout-minutes"] <= 360


def test_ingest_has_a_concurrency_group_of_its_own_that_never_cancels_a_run():
    group = _workflow(INGEST)["concurrency"]

    assert group == {"group": "ingest", "cancel-in-progress": False}
    assert group["group"] != _workflow(SCRAPE)["concurrency"]["group"]


def test_ingest_uses_the_python_vercel_runs_and_the_requirements_were_resolved_for():
    setup = next(s for s in _ingest_steps() if str(s.get("uses", "")).startswith("actions/setup-python@"))
    pinned = (REPO / ".python-version").read_text(encoding="utf-8").strip()

    assert setup["with"]["python-version"] == pinned == "3.12"


def test_ingest_installs_only_the_hashed_requirements():
    runs = [step["run"] for step in _ingest_steps() if "run" in step]
    installs = [line.split() for run in runs for line in run.splitlines()
                if re.search(r"\bpip install\b", line)]

    assert len(installs) == 1, installs
    args = installs[0]
    assert "--require-hashes" in args
    assert "--only-binary=:all:" in args
    assert args[args.index("-r") + 1] == "requirements-ingest.txt"
    assert "jobspy" not in " ".join(runs).lower()


def test_the_database_secret_reaches_one_step_only():
    workflow = _workflow(INGEST)
    # A workflow- or job-level env reaches every step, third-party actions included.
    assert "env" not in workflow and "env" not in workflow["jobs"]["ingest"]

    holders = [s.get("id") or s.get("name") or s.get("uses")
               for s in _ingest_steps() if "INGEST_DATABASE_URL" in yaml.safe_dump(s)]
    assert holders == ["secret", "ingest"]
    # The first only learns whether it is set: GitHub hands it "true" or "false".
    assert _ingest_step("secret")["env"] == {"CONFIGURED": "${{ secrets.INGEST_DATABASE_URL != '' }}"}
    assert _ingest_step("ingest")["env"]["DATABASE_URL"] == "${{ secrets.INGEST_DATABASE_URL }}"


def test_ingest_passes_each_adapter_key_and_the_alert_webhook_and_no_other_secret():
    wanted = _adapter_secret_names() | {"ALERT_WEBHOOK_URL"}
    env = _ingest_step("ingest")["env"]

    for name in sorted(wanted):
        assert env.get(name) == "${{ secrets." + name + " }}", name
    used = set(re.findall(r"secrets\.([A-Za-z0-9_]+)", yaml.safe_dump(_workflow(INGEST))))
    assert used == wanted | {"INGEST_DATABASE_URL"}


def test_the_off_target_kill_switch_reaches_the_runner_and_an_unset_one_means_on(monkeypatch):
    # The switch's twin on Vercel covers cron-ats only. A repository variable of
    # the same name covers the runner, and a variable nobody set arrives as the
    # empty string, which has to leave the retirement on.
    from backend.services import listing_freshness

    env = _ingest_step("ingest")["env"]
    assert env["CRON_ATS_RETIRE_OFF_TARGET"] == "${{ vars.CRON_ATS_RETIRE_OFF_TARGET }}"
    monkeypatch.setenv("CRON_ATS_RETIRE_OFF_TARGET", "")
    assert listing_freshness.retire_off_target_enabled() is True
    monkeypatch.setenv("CRON_ATS_RETIRE_OFF_TARGET", "0")
    assert listing_freshness.retire_off_target_enabled() is False


def test_every_step_after_the_secret_check_waits_for_it():
    steps = _ingest_steps()
    ids = [step.get("id") for step in steps]
    gated = steps[ids.index("secret") + 1:-1]

    assert len(gated) == 4, "checkout, setup-python, install and the run"
    for step in gated:
        assert step.get("if") == "steps.secret.outputs.configured == 'true'", step
    # The closing check is the one step that still runs without the secret.
    assert "!cancelled()" in steps[-1]["if"]


def test_the_inactivity_check_is_the_one_scrape_jobs_uses():
    mine, theirs = _ingest_step("inactivity"), scrape_tests._step("inactivity")

    for key in ("name", "run", "env", "continue-on-error", "timeout-minutes"):
        assert mine[key] == theirs[key], key
    assert "${{" not in mine["run"], "pass expressions through env, not the script"


def test_the_run_prints_the_connection_string_only_to_mask_it():
    run = _ingest_step("ingest")["run"]

    for line in run.splitlines():
        if "DATABASE_URL" in line and re.match(r"\s*(echo|printf)\b", line):
            assert 'echo "::add-mask::$DATABASE_URL_DIRECT"' in line, line


# --- the shell steps, run for real ----------------------------------------


def _run_secret_check(bash, tmp_path, configured):
    output = tmp_path / "github_output"
    result = bash(_ingest_step("secret")["run"],
                  {"CONFIGURED": configured, "GITHUB_OUTPUT": output.as_posix()})
    return result, output.read_text(encoding="utf-8") if output.exists() else ""


def test_without_the_secret_the_run_ends_green_with_a_notice(bash, tmp_path):
    result, output = _run_secret_check(bash, tmp_path, "false")

    assert result.returncode == 0, result.stdout + result.stderr
    assert output.strip() == "configured=false"
    assert result.stdout.count("::notice") == 1 and "INGEST_DATABASE_URL" in result.stdout
    assert "::error" not in result.stdout and "::warning" not in result.stdout


def test_with_the_secret_set_the_run_goes_on_without_comment(bash, tmp_path):
    result, output = _run_secret_check(bash, tmp_path, "true")

    assert result.returncode == 0, result.stdout + result.stderr
    assert output.strip() == "configured=true"
    assert "::notice" not in result.stdout


# A stand-in for `python`: it reports how the runner would have been started.
STUB_PYTHON = """#!/usr/bin/env bash
echo "argv: $*"
echo "DATABASE_URL=$DATABASE_URL"
echo "DATABASE_URL_DIRECT=$DATABASE_URL_DIRECT"
"""
POOLED = "postgresql://ingest:s3cret@ep-cool-name-123456-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require"
DIRECT = "postgresql://ingest:s3cret@ep-cool-name-123456.us-east-2.aws.neon.tech/neondb?sslmode=require"


def _run_ingest_step(bash, tmp_path, database_url, event="schedule"):
    stub_dir = tmp_path / "pybin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "python"
    stub.write_bytes(STUB_PYTHON.encode())
    stub.chmod(0o755)
    result = bash(_ingest_step("ingest")["run"], {
        "DATABASE_URL": database_url,
        "EVENT_NAME": event,
        "PATH": str(stub_dir) + os.pathsep + os.environ.get("PATH", ""),
    })
    assert result.returncode == 0, result.stdout + result.stderr
    seen = {}
    for line in result.stdout.splitlines():
        for key, separator in (("argv", ": "), ("DATABASE_URL", "="), ("DATABASE_URL_DIRECT", "=")):
            if line.startswith(key + separator):
                seen[key] = line[len(key + separator):]
    return seen


def test_a_scheduled_run_is_labelled_cron_and_a_manual_one_manual(bash, tmp_path):
    scheduled = _run_ingest_step(bash, tmp_path, DIRECT, event="schedule")
    manual = _run_ingest_step(bash, tmp_path, DIRECT, event="workflow_dispatch")

    assert scheduled["argv"] == "-m backend.ingest.run --trigger cron --budget-minutes 170"
    assert manual["argv"] == "-m backend.ingest.run --trigger manual --budget-minutes 170"


def test_the_advisory_lock_gets_a_direct_connection_from_a_pooled_secret(bash, tmp_path):
    seen = _run_ingest_step(bash, tmp_path, POOLED)

    assert seen["DATABASE_URL"] == POOLED, "the run's own queries keep the secret as given"
    assert seen["DATABASE_URL_DIRECT"] == DIRECT


def test_a_secret_that_is_already_direct_is_left_alone(bash, tmp_path):
    seen = _run_ingest_step(bash, tmp_path, DIRECT)

    assert seen["DATABASE_URL"] == seen["DATABASE_URL_DIRECT"] == DIRECT


def test_only_the_host_of_the_connection_string_is_rewritten(bash, tmp_path):
    url = "postgresql://ing-pooler.x:pw-pooler.y@ep-a-pooler.eu.neon.tech:5432/db-pooler.v?options=-c%20x-pooler.z"

    seen = _run_ingest_step(bash, tmp_path, url)

    assert seen["DATABASE_URL_DIRECT"] == (
        "postgresql://ing-pooler.x:pw-pooler.y@ep-a.eu.neon.tech:5432/db-pooler.v?options=-c%20x-pooler.z")


def _run_final(bash, inactivity):
    final = _ingest_steps()[-1]
    env = {"OUTCOMES": scrape_tests._render(final["env"]["OUTCOMES"],
                                            {"steps.inactivity.outcome": inactivity})}
    return bash(final["run"], env)


def test_the_closing_step_covers_every_step_that_may_fail_quietly():
    final = _ingest_steps()[-1]
    quiet = {step["id"] for step in _ingest_steps() if step.get("continue-on-error")}

    assert quiet == set(re.findall(r"steps\.(\w+)\.outcome", final["env"]["OUTCOMES"]))


def test_the_closing_step_is_green_when_the_inactivity_check_passed(bash):
    result = _run_final(bash, "success")

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_closing_step_turns_the_run_red_when_the_inactivity_check_failed(bash):
    result = _run_final(bash, "failure")

    assert result.returncode == 1
    assert "inactivity=failure" in result.stdout


# --- ingest-network-tests.yml ----------------------------------------------

NETWORK = WORKFLOWS / "ingest-network-tests.yml"


def _network_steps():
    return _workflow(NETWORK)["jobs"]["network"]["steps"]


def _minute_of_day(cron):
    minute, hour = cron.split()[:2]
    return int(hour) * 60 + int(minute)


def test_the_network_tests_run_nightly_off_peak_and_never_alongside_the_ingest():
    triggers = _triggers(_workflow(NETWORK))
    crons = [entry["cron"] for entry in triggers["schedule"]]
    assert len(crons) == 1
    minute, hour, day, month, weekday = crons[0].split()

    assert hour.isdigit() and (day, month, weekday) == ("*", "*", "*")
    assert minute.isdigit() and minute != "0", "the top of the hour is GitHub's most contended slot"
    assert "workflow_dispatch" in triggers
    # Neither workflow's run may overlap the other's: both page the same job boards.
    network_start = _minute_of_day(crons[0])
    ingest = _workflow(INGEST)
    ingest_start = _minute_of_day(_triggers(ingest)["schedule"][0]["cron"])
    ingest_cap = ingest["jobs"]["ingest"]["timeout-minutes"]
    network_cap = _workflow(NETWORK)["jobs"]["network"]["timeout-minutes"]
    assert (network_start - ingest_start) % 1440 >= ingest_cap
    assert (ingest_start - network_start) % 1440 >= network_cap


def test_the_network_tests_keep_the_same_guard_rails_as_the_other_workflows():
    workflow = _workflow(NETWORK)
    job = workflow["jobs"]["network"]

    assert workflow["permissions"] == {"contents": "read"} and "permissions" not in job
    assert re.fullmatch(r"ubuntu-\d\d\.\d\d", job["runs-on"])
    assert 0 < job["timeout-minutes"] <= 60
    assert workflow["concurrency"] == {"group": "ingest-network-tests", "cancel-in-progress": False}
    refs = [step["uses"] for step in _network_steps() if "uses" in step]
    assert refs
    for ref in refs:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref
    checkout = next(s for s in _network_steps() if str(s.get("uses", "")).startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False


def test_the_network_tests_hold_no_database_credential_and_only_the_adapter_keys():
    workflow = _workflow(NETWORK)
    dumped = yaml.safe_dump(workflow)
    run = next(s for s in _network_steps() if s.get("name") == "Run the network tests")

    assert "INGEST_DATABASE_URL" not in dumped
    assert set(re.findall(r"secrets\.([A-Za-z0-9_]+)", dumped)) == _adapter_secret_names()
    for name in _adapter_secret_names():
        assert run["env"][name] == "${{ secrets." + name + " }}", name
    assert run["env"]["DATABASE_URL"].startswith("sqlite:")


def test_the_network_tests_run_only_the_marked_tests_with_the_variable_that_unlocks_them():
    run = next(s for s in _network_steps() if s.get("name") == "Run the network tests")
    args = shlex.split(run["run"])

    assert run["env"]["RUN_NETWORK_TESTS"] == "1"
    assert args[:3] == ["python", "-m", "pytest"]
    assert args[args.index("-m", 3) + 1] == "network", "the marker expression, after python -m pytest"
    assert "-rs" in args, "the log lists the keyed adapters' tests that skipped for want of a key"
