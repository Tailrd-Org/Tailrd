"""match-engine-eval.yml: the owner shipped with the M6 eval gate red (D-154).

`make eval-fast` stays red and honest (never loosened or skipped) but no longer blocks a pull
request: only its step continues on error. `make test` still blocks, and a release candidate with
a failing guardrail still does not spend a holdout look.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "match-engine-eval.yml"


def _jobs():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def _step(job, run):
    return [step for step in job["steps"] if step.get("run", "").strip() == run]


def test_only_the_eval_fast_step_continues_on_error():
    jobs = _jobs()
    for name, job in jobs.items():
        assert "continue-on-error" not in job, name
        for step in job["steps"]:
            if step.get("run", "").strip() != "make eval-fast":
                assert "continue-on-error" not in step, (name, step.get("name"))
    (eval_step,) = _step(jobs["eval-fast"], "make eval-fast")
    assert eval_step["continue-on-error"] is True


def test_the_decision_is_cited_next_to_the_step():
    text = WORKFLOW.read_text(encoding="utf-8")
    before = text[:text.index("run: make eval-fast")]
    assert "D-154" in before[before.rindex("- name:") - 400:]


def test_make_test_still_blocks():
    (test_step,) = _step(_jobs()["test"], "make test")
    assert "continue-on-error" not in test_step


def test_a_red_gate_still_stops_a_release_holdout_look():
    job = _jobs()["eval-fast"]
    guards = [step for step in job["steps"]
              if "steps.eval.outcome" in str(step.get("if", "")) and "exit 1" in step.get("run", "")]
    assert len(guards) == 1
    assert "startsWith(github.ref, 'refs/heads/release/')" in guards[0]["if"]
    assert "eval-fast" in _jobs()["holdout"]["needs"]


def test_a_red_gate_on_a_pr_is_annotated_not_silent():
    """Review M-8: with continue-on-error the check is green, so a red gate must say so in a
    ::warning:: annotation and the step summary, naming D-154."""
    job = _jobs()["eval-fast"]
    notes = [step for step in job["steps"] if "::warning" in step.get("run", "")]
    assert len(notes) == 1
    assert notes[0]["if"] == "steps.eval.outcome == 'failure'"
    run = notes[0]["run"]
    assert "::warning" in run and "D-154" in run
    assert "GITHUB_STEP_SUMMARY" in run
    assert "exit 1" not in run
