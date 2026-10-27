"""scrape-jobs.yml guard rails: a read-only token, no automated re-enable of
the schedule, pinned third-party code, and a check that turns the run red
before GitHub's 60-day inactivity rule silently switches the schedule off.

The inactivity check and the final outcome check are real shell. They run
here under bash with a stub `gh` first on PATH: no network, no token."""

import datetime
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

WORKFLOW = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scrape-jobs.yml"
)
REPO = "owner/repo"

# Every ${{ }} expression the tested steps may use, rendered the way the
# runner would. An expression missing here fails the test on purpose.
EXPRESSIONS = {
    "github.token": "stub-token",
    "github.repository": REPO,
}

# Stub GitHub CLI. It answers the two reads the inactivity check makes and
# exits non-zero on anything else, and logs every call so the test can show
# the step never writes.
STUB_GH = """#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
if [ -n "$STUB_FAIL" ]; then echo "HTTP 502: Bad Gateway" >&2; exit 1; fi
[ "$1" = api ] || { echo "unexpected: $*" >&2; exit 2; }
case "$2" in
  "repos/$STUB_REPO") echo "$STUB_BRANCH" ;;
  "repos/$STUB_REPO/commits/$STUB_BRANCH") echo "$STUB_DATE" ;;
  *) echo "unexpected: $*" >&2; exit 2 ;;
esac
"""


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps():
    return _workflow()["jobs"]["scrape"]["steps"]


def _step(step_id):
    for step in _steps():
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"scrape-jobs.yml has no step with id {step_id!r}")


def _render(value, extra=None):
    table = {**EXPRESSIONS, **(extra or {})}
    return re.sub(
        r"\$\{\{\s*(.+?)\s*\}\}", lambda m: table[m.group(1)], str(value)
    )


# --- static shape --------------------------------------------------------


def test_token_is_read_only():
    wf = _workflow()
    assert wf["permissions"] == {"contents": "read"}
    for name, job in wf["jobs"].items():
        perms = job.get("permissions", {})
        assert isinstance(perms, dict), name
        assert set(perms.values()) <= {"read", "none"}, (name, perms)


def test_runner_image_is_pinned():
    # ubuntu-latest moves to Ubuntu 26.04 from 2026-10-19. An unattended
    # hourly job should change OS when someone decides to, not silently: a
    # versioned image, never a floating -latest label.
    for name, job in _workflow()["jobs"].items():
        assert re.fullmatch(r"ubuntu-\d\d\.\d\d", job["runs-on"]), (name, job["runs-on"])


def test_no_step_re_enables_the_workflow():
    for step in _steps():
        run = step.get("run", "")
        assert "/enable" not in run, step.get("name")
        assert "actions/workflows" not in run, step.get("name")


def test_checkout_does_not_leave_the_token_on_disk():
    checkouts = [
        s for s in _steps() if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    assert checkouts
    for step in checkouts:
        assert (step.get("with") or {}).get("persist-credentials") is False


def test_every_gitlink_has_a_gitmodules_entry():
    # persist-credentials: false (above) makes checkout run `git submodule
    # foreach` inside its main step, and git dies on any gitlink .gitmodules
    # doesn't map ("No url found for submodule path", exit 128): checkout
    # fails and both scrapers are skipped. Two orphaned gitlinks did exactly
    # that to every scheduled run after f26fa4c.
    root = WORKFLOW.parents[2]
    if shutil.which("git") is None or not (root / ".git").exists():
        pytest.skip("needs a git checkout")
    # -z on both: raw NUL-separated paths, never C-quoted, so a mapped path
    # with non-ASCII characters still compares equal.
    staged = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    gitlinks = {
        entry.split("\t", 1)[1]
        for entry in staged.split("\0")
        if entry.startswith("160000 ")
    }
    mapped = subprocess.run(
        ["git", "config", "--file", ".gitmodules", "-z",
         "--get-regexp", r"^submodule\..*\.path$"],
        cwd=root, capture_output=True,
    ).stdout.decode("utf-8")
    paths = {entry.split("\n", 1)[1] for entry in mapped.split("\0") if entry}
    assert gitlinks <= paths, sorted(gitlinks - paths)


def test_pip_installs_are_pinned():
    installs = [
        m.group(1).split()
        for step in _steps()
        for line in step.get("run", "").splitlines()
        if (m := re.search(r"\bpip install\b(.*)", line))
    ]
    assert installs
    for args in installs:
        packages = [a for a in args if not a.startswith("-")]
        assert packages
        for pkg in packages:
            assert re.fullmatch(r"[A-Za-z0-9._\[\],-]+==[A-Za-z0-9.]+", pkg), pkg


def test_inactivity_check_feeds_the_final_outcome_check():
    step = _step("inactivity")
    assert step.get("continue-on-error") is True
    assert "${{" not in step["run"], "pass expressions through env, not the script"
    final = _steps()[-1]
    assert "steps.inactivity.outcome" in final["env"]["OUTCOMES"]


# --- the shell itself ----------------------------------------------------


def _bash_candidates():
    found = shutil.which("bash")
    if found:
        yield found
    # On Windows PATH often finds WSL's launcher first, which can't see a
    # Windows PATH; Git for Windows ships a bash that can.
    git = shutil.which("git") if os.name == "nt" else None
    if git:
        root = Path(git).resolve().parents[1]
        for exe in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
            if exe.is_file():
                yield str(exe)


@pytest.fixture
def bash(tmp_path):
    """Run a step's script the way the runner does (bash -eo pipefail) with
    the stub `gh` first on PATH. Skips only where no bash can run the stub."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "gh"
    stub.write_bytes(STUB_GH.encode())
    stub.chmod(0o755)
    log = tmp_path / "gh.log"
    base_env = {
        **os.environ,
        "PATH": str(stub_dir) + os.pathsep + os.environ.get("PATH", ""),
        "STUB_LOG": log.as_posix(),
        "STUB_REPO": REPO,
        "STUB_BRANCH": "main",
    }
    base_env.pop("STUB_FAIL", None)

    def runner(exe):
        def run(script, env=None):
            path = tmp_path / "step.sh"
            path.write_bytes(script.encode())
            return subprocess.run(
                [exe, "--noprofile", "--norc", "-eo", "pipefail", path.as_posix()],
                env={**base_env, **(env or {})},
                capture_output=True,
                text=True,
                timeout=60,
            )

        run.log = log
        return run

    for exe in _bash_candidates():
        run = runner(exe)
        probe = run("gh api repos/owner/repo")
        log.unlink(missing_ok=True)
        if probe.returncode == 0 and probe.stdout.strip() == "main":
            return run
    pytest.skip("no bash on this machine can run the stub gh")


def _run_inactivity(bash, days_old=None, **stub):
    step = _step("inactivity")
    env = {k: _render(v) for k, v in step.get("env", {}).items()}
    if days_old is not None:
        when = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=days_old, hours=1
        )
        stub.setdefault("STUB_DATE", when.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return bash(step["run"], {**env, **stub})


def test_inactivity_check_passes_on_a_recent_commit(bash):
    result = _run_inactivity(bash, days_old=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "10 days" in result.stdout
    assert "::error" not in result.stdout


def test_inactivity_check_passes_one_day_under_the_threshold(bash):
    result = _run_inactivity(bash, days_old=49)
    assert result.returncode == 0, result.stdout + result.stderr


def test_inactivity_check_fails_the_step_at_50_days(bash):
    result = _run_inactivity(bash, days_old=50)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "::error::" in result.stdout
    assert "50 days" in result.stdout
    assert "Push any commit" in result.stdout
    assert "external cron" in result.stdout


def test_inactivity_check_only_reads(bash):
    _run_inactivity(bash, days_old=55)
    calls = bash.log.read_text().splitlines()
    assert calls == [
        f"api repos/{REPO} --jq .default_branch",
        f"api repos/{REPO}/commits/main --jq .commit.committer.date",
    ]


def test_inactivity_check_fails_loudly_when_github_is_unreadable(bash):
    result = _run_inactivity(bash, days_old=1, STUB_FAIL="1")
    assert result.returncode == 1
    assert "::error::" in result.stdout


def test_inactivity_check_does_not_pass_on_an_empty_date(bash):
    result = _run_inactivity(bash, STUB_DATE="")
    assert result.returncode == 1
    assert "::error::" in result.stdout


def _run_final(bash, failed=()):
    final = _steps()[-1]
    step_ids = re.findall(r"steps\.(\w+)\.outcome", final["env"]["OUTCOMES"])
    outcomes = {
        f"steps.{sid}.outcome": ("failure" if sid in failed else "success")
        for sid in step_ids
    }
    env = {"OUTCOMES": _render(final["env"]["OUTCOMES"], outcomes)}
    return bash(final["run"], env)


def test_final_step_is_green_when_everything_succeeded(bash):
    result = _run_final(bash)
    assert result.returncode == 0, result.stdout + result.stderr


def test_final_step_turns_the_run_red_on_an_inactivity_failure(bash):
    result = _run_final(bash, failed={"inactivity"})
    assert result.returncode == 1
    assert "inactivity=failure" in result.stdout


# --- cron-poll: a sweep that cannot score is a warning, not a red run -----

# Stub curl: writes $STUB_BODY to the -o file and prints "$STUB_CODE <time>"
# for -w, the two things the endpoint steps read.
STUB_CURL = """#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$out" ] && printf '%s' "$STUB_BODY" > "$out"
printf '%s 1.23s' "${STUB_CODE:-200}"
"""

# The match-alert sweep's summary since Task 9.2 (fit engine, no AI): the keys the
# workflow greps for are pinned by test_sweep_summary_carries_the_keys_the_workflow_greps_for.
SALT_MISSING_BODY = (
    '{"status":"completed","sources_polled":5,"new_jobs":3,'
    '"match_alerts":{"status":"fit_trace_salt_missing","scoring":"fit-engine",'
    '"alert_band":"Strong match","users_scanned":5,"users_notified":0,"jobs_notified":0,'
    '"jobs_scored":0,"scoring_errors":0,"chunks_skipped":0,"scoring_budget_spent":false,'
    '"fit_trace_salt_missing":true,"calibration":"skipped: no scores",'
    '"drift":"skipped: no scores"}}'
)
ENGINE_DOWN_BODY = (
    '{"status":"completed","match_alerts":{"status":"engine_unavailable","scoring":"fit-engine",'
    '"alert_band":"Strong match","users_scanned":1,"jobs_scored":0,"scoring_errors":0,'
    '"chunks_skipped":0,"fit_trace_salt_missing":false,"error":"FitEngineError"}}'
)
HEALTHY_BODY = (
    '{"status":"completed","match_alerts":{"status":"completed","scoring":"fit-engine",'
    '"alert_band":"Strong match","users_scanned":5,"jobs_scored":12,"scoring_errors":0,'
    '"chunks_skipped":0,"fit_trace_salt_missing":false}}'
)


def _run_cron_poll(bash, tmp_path, body, code="200"):
    step = _step("cron_poll")
    stub_dir = tmp_path / "curlbin"
    stub_dir.mkdir(exist_ok=True)
    curl = stub_dir / "curl"
    curl.write_bytes(STUB_CURL.encode())
    curl.chmod(0o755)
    env = {k: _render(v, {"secrets.CRON_SECRET": "stub-secret"})
           for k, v in step.get("env", {}).items()}
    env.update({
        "API_BASE": "https://example.invalid",
        "PATH": str(stub_dir) + os.pathsep + os.environ.get("PATH", ""),
        "STUB_BODY": body,
        "STUB_CODE": code,
    })
    # The step writes response.json into its working directory.
    return bash(f'cd "{tmp_path.as_posix()}"\n' + step["run"], env)


def _warnings(result):
    return [line for line in result.stdout.splitlines() if line.startswith("::warning")]


def test_cron_poll_warns_but_stays_green_when_the_trace_salt_is_missing(bash, tmp_path):
    result = _run_cron_poll(bash, tmp_path, SALT_MISSING_BODY)

    assert result.returncode == 0, result.stdout + result.stderr
    warnings = _warnings(result)
    assert len(warnings) == 1, result.stdout
    assert warnings[0].startswith("::warning title=Match scoring stopped::")
    assert "FIT_TRACE_SALT" in warnings[0]
    assert "::error" not in result.stdout


def test_cron_poll_warns_but_stays_green_when_the_engine_cannot_be_built(bash, tmp_path):
    result = _run_cron_poll(bash, tmp_path, ENGINE_DOWN_BODY)

    assert result.returncode == 0, result.stdout + result.stderr
    warnings = _warnings(result)
    assert len(warnings) == 1, result.stdout
    assert warnings[0].startswith("::warning title=Match scoring stopped::")
    assert "FitEngineError" in warnings[0]


def test_the_retired_llm_outage_check_is_gone():
    assert "llm_unavailable" not in _step("cron_poll")["run"]


def test_sweep_summary_carries_the_keys_the_workflow_greps_for():
    """The strings the workflow greps for are exactly what the sweep's summary serialises to."""
    import json

    run = _step("cron_poll")["run"]
    for status in ("engine_unavailable",):
        assert json.dumps({"status": status}, separators=(",", ":"))[1:-1] in run
    assert json.dumps({"fit_trace_salt_missing": True}, separators=(",", ":"))[1:-1] in run


def test_cron_poll_says_nothing_extra_when_scoring_works(bash, tmp_path):
    result = _run_cron_poll(bash, tmp_path, HEALTHY_BODY)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "::warning" not in result.stdout


def test_cron_poll_still_fails_the_step_on_an_http_error(bash, tmp_path):
    result = _run_cron_poll(bash, tmp_path, '{"detail":"Internal server error"}', code="500")

    assert result.returncode == 1
    assert "HTTP 500" in result.stdout


# --- cut-over to the v2 runner ------------------------------------------------
#
# ingest.yml is the new path. Until the owner adds its database secret, this
# workflow's cron-ats call is what feeds production, and once INGEST_V2_RUNNER
# is on (Vercel's env, not this file) cron-ats stands aside board by board.


def test_cron_ats_still_feeds_production_and_is_still_checked():
    step = _step("cron_ats")

    assert "/github-sources/cron-ats" in step["run"]
    assert "steps.cron_ats.outcome" in _steps()[-1]["env"]["OUTCOMES"]


def test_the_cut_over_flag_is_set_on_vercel_never_in_this_workflow():
    # cron-ats reads INGEST_V2_RUNNER in its Vercel function. Setting it in
    # this workflow's env would change nothing there and look as if it had.
    # (safe_dump keeps the values and drops the comments, which name it.)
    assert "INGEST_V2_RUNNER" not in yaml.safe_dump(_workflow())


# --- ci.yml: network tests deselected, Postgres-only ingest tests run -------
#
# Scraper v2 added tests that call live job boards (marker `network`, run
# nightly by ingest-network-tests.yml) and tests that need a real Postgres
# (skipped unless INGEST_TEST_POSTGRES_URL names one). CI keeps its main suite
# on SQLite and runs the second kind in a job with a postgres:17 service.

REPO_ROOT = WORKFLOW.parents[2]
CI = WORKFLOW.with_name("ci.yml")
POSTGRES_JOB = "backend-postgres"
POSTGRES_TESTS_STEP = "Run the Postgres-only ingest tests"
SKIP_GUARD_STEP = "Fail if a Postgres-only test was skipped"


def _ci():
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def _ci_step(job, name):
    for step in _ci()["jobs"][job]["steps"]:
        if step.get("name") == name:
            return step
    raise AssertionError(f"ci.yml job {job!r} has no step named {name!r}")


def test_ci_runner_images_are_pinned():
    for name, job in _ci()["jobs"].items():
        assert re.fullmatch(r"ubuntu-\d\d\.\d\d", job["runs-on"]), (name, job["runs-on"])


def test_ci_main_suite_stays_on_sqlite_and_leaves_out_only_the_network_tests():
    step = _ci_step("backend", "Run backend tests")

    assert step["env"]["DATABASE_URL"].startswith("sqlite:")
    assert "INGEST_TEST_POSTGRES_URL" not in step["env"]
    assert shlex.split(step["run"]) == [
        "python", "-m", "pytest", "backend/tests", "-q", "-m", "not network",
    ]


def test_ci_postgres_job_runs_the_ingest_tests_on_a_postgres_17_service():
    job = _ci()["jobs"][POSTGRES_JOB]
    service = job["services"]["postgres"]
    step = _ci_step(POSTGRES_JOB, POSTGRES_TESTS_STEP)
    url = urlparse(step["env"]["INGEST_TEST_POSTGRES_URL"])

    assert service["image"] == "postgres:17"
    assert "5432:5432" in [str(port) for port in service["ports"]]
    # The service is the only database in reach: a scratch one on the runner.
    assert url.scheme == "postgresql+pg8000"
    assert (url.hostname, url.port) == ("localhost", 5432)
    assert url.username == "postgres"
    assert url.password == service["env"]["POSTGRES_PASSWORD"]
    assert url.path == "/" + service["env"]["POSTGRES_DB"]
    assert "neon" not in step["env"]["INGEST_TEST_POSTGRES_URL"].lower()
    # Everything else the app touches is still SQLite.
    assert step["env"]["DATABASE_URL"].startswith("sqlite:")
    # pipefail (shell: bash) so a failing pytest still fails the step behind `| tee`.
    assert step["shell"] == "bash"
    assert '-m "not network"' in step["run"]
    assert "-rs" in step["run"] and "tee pytest.out" in step["run"]


def test_ci_postgres_job_installs_the_driver_the_app_uses():
    # The version the repo already pins for Vercel (requirements.txt) and for
    # the runner (requirements-ingest.txt).
    run = _ci_step(POSTGRES_JOB, "Install dependencies")["run"]
    pinned = re.search(r"^pg8000==(\S+)", (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8"), re.M)

    assert pinned
    assert re.search(r"\bpg8000==" + re.escape(pinned.group(1)) + r"\b", run)


def test_ci_runs_every_test_that_waits_for_the_postgres_url():
    # The job names the test files it runs (the other 500 ingest tests run in
    # the `backend` job). A file that reads the variable but is not named there
    # would skip in CI without anyone noticing.
    tests = Path(__file__).resolve().parent
    reads_it = re.compile(r"environ(?:\.get\(|\[)\s*[\"']INGEST_TEST_POSTGRES_URL[\"']")
    readers = sorted(
        "backend/tests/" + path.relative_to(tests).as_posix()
        for path in tests.rglob("test_*.py")
        if reads_it.search(path.read_text(encoding="utf-8"))
    )
    named = sorted(
        arg for arg in shlex.split(_ci_step(POSTGRES_JOB, POSTGRES_TESTS_STEP)["run"].split("|")[0])
        if arg.startswith("backend/tests/")
    )

    assert readers, "the tests the Postgres job exists for are gone"
    assert named == readers


def _run_skip_guard(bash, tmp_path, pytest_output):
    (tmp_path / "pytest.out").write_text(pytest_output, encoding="utf-8")
    step = _ci_step(POSTGRES_JOB, SKIP_GUARD_STEP)
    return bash(f'cd "{tmp_path.as_posix()}"\n' + step["run"])


def test_ci_fails_the_job_when_a_postgres_only_test_was_skipped(bash, tmp_path):
    result = _run_skip_guard(
        bash, tmp_path,
        "..s.\n=== short test summary info ===\n"
        "SKIPPED [1] backend/tests/ingest/test_queue.py:152: "
        "set INGEST_TEST_POSTGRES_URL to a scratch Postgres\n"
        "3 passed, 1 skipped in 2.10s\n",
    )

    assert result.returncode == 1
    assert "::error::" in result.stdout
    assert "test_queue.py:152" in result.stdout, "the log should name the skipped test"


def test_ci_lets_other_skips_through(bash, tmp_path):
    result = _run_skip_guard(
        bash, tmp_path,
        "SKIPPED [2] backend/tests/ingest/test_adapters_tier1.py:1024: "
        "makes real network requests; set RUN_NETWORK_TESTS=1 to run\n"
        "40 passed, 2 skipped in 9.00s\n",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error" not in result.stdout


def test_the_network_marker_is_registered_for_the_whole_repo(tmp_path):
    # `-m "not network"` in ci.yml deselects by this marker. Registered in
    # pyproject.toml rather than a conftest, so a marked test anywhere is known
    # (--strict-markers fails on an unregistered one) and is deselected. The
    # probe lives outside the repo, so no conftest of its own registers it.
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import pytest\n\n\n"
        "@pytest.mark.network\n"
        "def test_live_board():\n"
        "    raise AssertionError('called a live job board')\n\n\n"
        "def test_offline():\n"
        "    pass\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-c", str(REPO_ROOT / "pyproject.toml"),
         "--rootdir", str(REPO_ROOT), "--strict-markers", "-p", "no:cacheprovider",
         "-m", "not network", "-q", str(probe)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 deselected" in result.stdout
