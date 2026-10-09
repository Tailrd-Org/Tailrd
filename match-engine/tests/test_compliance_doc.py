"""docs/compliance.md says what the code does: its facts are pinned to the code (Task 7.2).

The document is prose for people, so these tests only guard the places where it repeats a fact the
code owns: the reason codes and the sentence shown for each, the fields of a trace, the drift alarms
and their limits, the outcomes, and the options of the audit script. If the code changes, the
document has to change with it.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date
from pathlib import Path

import pytest

from matcher.audit import build_trace, drift
from matcher.errors import FLAG_TEXT, NEEDS_REVIEW_REASONS, Flag
from matcher.models import GateStatus, MatchResult, Outcome

ENGINE_ROOT = Path(__file__).resolve().parents[1]
DOC = ENGINE_ROOT / "docs" / "compliance.md"


@pytest.fixture(scope="module")
def text() -> str:
    return DOC.read_text(encoding="utf-8")


def section(text: str, heading: str) -> str:
    """The body under one `## ` or `### ` heading, up to the next heading of any level."""
    start = text.index(heading + "\n") + len(heading)
    nxt = re.search(r"\n#{2,3} ", text[start:])
    return text[start : start + nxt.start()] if nxt else text[start:]


def table_rows(body: str) -> list[list[str]]:
    rows = []
    for line in body.splitlines():
        if line.startswith("|") and not set(line) <= set("|- "):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows.append(cells)
    return rows[1:]  # drop the header


def test_the_reason_code_table_is_exactly_the_needs_review_reasons_with_their_sentences(text) -> None:
    rows = table_rows(section(text, "### Reason codes that route to a person"))
    documented = {row[0].strip("`"): row[1] for row in rows}
    assert set(documented) == {flag.value for flag in NEEDS_REVIEW_REASONS}
    for flag in NEEDS_REVIEW_REASONS:
        assert documented[flag.value] == FLAG_TEXT[Flag(flag)], flag


def test_every_field_of_a_trace_is_described(text) -> None:
    empty = MatchResult(
        outcome=Outcome.NEEDS_REVIEW,
        gate_status=GateStatus.PASS,
        reasons=(Flag.NOTHING_SCORABLE,),
        engine_version="1.0.0",
        config_hash="0123456789abcdef",
        as_of=date(2026, 10, 8),
    )
    body = section(text, "## 3. What is persisted for every score")
    for key in build_trace(empty):
        assert f"`{key}`" in body, f"compliance.md does not describe the trace field {key!r}"


def test_the_four_outcomes_are_documented(text) -> None:
    body = section(text, "## 2. The human-review path")
    for outcome in Outcome:
        assert f"`{outcome.value}`" in body


def test_the_drift_table_lists_every_alarm_code_and_the_current_limits(text) -> None:
    body = section(text, "## 5. Continuous monitoring")
    codes = {
        drift.COVERAGE_LOW,
        drift.ABSTENTION_HIGH,
        drift.ABSTENTION_RISING,
        drift.SCORE_DRIFT,
        drift.FOUR_FIFTHS_VIOLATION,
        drift.LATENCY_HIGH,
    }
    documented = {row[0].strip("`") for row in table_rows(body)}
    assert documented == codes
    for limit in (
        f"{drift.COVERAGE_FLOOR:.0%}",
        f"{drift.ABSTENTION_CEILING:.0%}",
        f"{drift.PSI_CEILING:.2f}",
        f"{drift.FAIRNESS_FLOOR:.2f}",
        f"{drift.LATENCY_P95_CEILING_MS:.0f} ms",
        "twice the 30-day baseline",
    ):
        assert limit in body, limit


def test_the_audit_command_uses_options_the_script_has(text) -> None:
    path = ENGINE_ROOT / "scripts" / "bias_audit.py"
    spec = importlib.util.spec_from_file_location("bias_audit_for_doc", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["bias_audit_for_doc"] = module
    spec.loader.exec_module(module)
    options = set(module._parser()._option_string_actions)
    body = section(text, "## 6. The annual bias audit")
    for option in set(re.findall(r"--[a-z][a-z-]+", body)):
        assert option in options, f"compliance.md mentions {option}, which bias_audit.py does not accept"
    for output in ("bias_audit.json", "bias_audit.md"):
        assert output in body
    # the exit statuses it describes are the ones the script returns
    assert (module.EXIT_PASS, module.EXIT_ERROR, module.EXIT_FAIL, module.EXIT_NOT_ASSESSED) == (0, 1, 2, 3)
    assert re.search(r"exits 0 .*?2 on .*?3 when .*?1 on", " ".join(body.split()))


def test_the_document_keeps_its_commitments(text) -> None:
    flat = " ".join(text.split())
    for phrase in (
        "The score ranks; it never auto-rejects.",
        "We couldn't score this",
        "employment attorney",
        "It is not legal advice",
        "A fairness failure is terminal",
        "no user id column",
        "keyed by opaque id",
    ):
        assert phrase.lower() in flat.lower(), phrase


def test_the_document_has_no_em_dashes_and_no_emoji(text) -> None:
    assert chr(0x2014) not in text
    assert max(map(ord, text)) < 0x2000  # no dashes, quotes or emoji beyond Latin-1 letters such as e-acute


# --- review round 1: facts, requirements and proposals are told apart ---------------------------

MARK = "proposed, owner to confirm"

# Statements that are policy calls or premises, not facts about the code. Each is marked wherever
# it is made, so a reader can tell a proposal from a fact.
PROPOSALS = (
    "no employer, recruiter or agency sees",
    "must say that it was not scored",
    "must read the log at least weekly",
    "are not an audit source",
    "only the person who owns the audit",
    "set with counsel",
    "run these on a schedule",
    "a person to look at any score",
    "never overwritten by hand",
)


def flat(value: str) -> str:
    return " ".join(value.split())


def blocks(body: str) -> list[str]:
    """Markdown blocks: a paragraph, a list item with its continuation lines, or a table row."""
    out: list[str] = []
    current: list[str] = []
    for line in body.splitlines():
        if not line.strip():
            if current:
                out.append(" ".join(current))
                current = []
        elif re.match(r"(?:- |\d+\. |\||#)", line):
            if current:
                out.append(" ".join(current))
            current = [line.strip()]
        else:
            current.append(line.strip())
    if current:
        out.append(" ".join(current))
    return out


def test_a_status_block_separates_what_exists_what_is_missing_and_what_is_proposed(text) -> None:
    block = section(text, "## Status and owner decisions")
    assert block.index("**(a)") < block.index("**(b)") < block.index("**(c)")
    built, missing, proposed = (flat(part).lower() for part in re.split(r"\*\*\([abc]\)", block)[1:4])
    for name in ("build_trace", "four_fifths", "drift_alarms", "bias_audit.py", "opaque_id"):
        assert name in built, name
    for work in (
        "milestone 9",
        "review log",
        "demographics form",
        "salt",
        "export that feeds the audit",
        "four-fifths job",
        "none of it is scheduled in the current plan",
        "needs the owner's decision first",
    ):
        assert work in missing, work
    assert "9.x" not in missing  # work that is not scheduled must not be pinned to a task number
    items = [b for b in blocks(re.split(r"\*\*\(c\)", block)[1]) if re.match(r"\d+\. ", b)]
    assert len(items) >= 9
    assert all(MARK in item for item in items), [item[:60] for item in items if MARK not in item]
    assert "nothing" in proposed and "owner" in proposed


def test_every_policy_call_is_marked_wherever_it_is_made(text) -> None:
    assert text.count(MARK) >= 18
    for phrase in PROPOSALS:
        hits = [b for b in blocks(text) if phrase in flat(b).lower()]
        assert len(hits) >= 1, phrase
        for hit in hits:
            assert MARK in flat(hit), (phrase, hit[:80])


def test_every_policy_call_is_listed_in_the_status_block(text) -> None:
    block = section(text, "## Status and owner decisions")
    items = [flat(b).lower() for b in blocks(re.split(r"\*\*\(c\)", block)[1]) if re.match(r"\d+\. ", b)]
    for phrase in PROPOSALS:
        if phrase != "run these on a schedule":  # the cadence item words it as "runs at least weekly"
            assert any(phrase in item for item in items), phrase
    assert any("at least weekly" in item and "once a year" in item for item in items)


def test_the_document_quotes_spec_9_exactly(text) -> None:
    spec = flat((ENGINE_ROOT / "SPEC.md").read_text(encoding="utf-8"))
    for sentence in (
        "Get an employment attorney to review before launch.",
        "The score ranks; it never auto-rejects.",
    ):
        assert sentence in spec, sentence  # SPEC's own words
        assert sentence in flat(text), sentence
    assert "before any employer-facing use" not in flat(text)
    assert "It never rejects" not in flat(text)  # SPEC says auto-rejects; the document must not shorten it


def test_the_pre_launch_review_does_not_cover_an_audit_that_has_not_run(text) -> None:
    body = flat(section(text, "## 8. Before launch"))
    assert "and the first audit's results once one has run" in body
    assert "audit procedure and the first audit's results." not in body


def test_claims_about_the_product_are_premises_or_requirements_not_facts(text) -> None:
    for stale in (
        "no employer sees a score or a ranking",
        "shows nothing to employers",
        "Tailrd shows a candidate their own score",
        "The candidate sees",
        "Tailrd today shows",
        "Tailrd's rule is that the score is never the only thing",
        "Tailrd's rule is that a score is never overwritten by hand",
        "Task 9.x",
        "The persisted line also carries",
    ):
        assert stale not in flat(text), stale
    section_two = section(text, "## 2. The human-review path")
    assert "The integration must show" in section_two and "The integration must record" in section_two


def test_the_psi_minimum_sample_and_the_no_verdict_rule_are_documented(text) -> None:
    body = flat(section(text, "## 5. Continuous monitoring"))
    assert str(drift.PSI_MIN_SAMPLE) in body and "insufficient sample" in body and "evaluate_drift" in body
    assert "`assessed`" in body and "no verdict" in body and "never a pass" in body


def test_prefer_not_to_say_is_exported_as_blank_and_read_as_blank(text) -> None:
    for heading in (
        "## 4. Demographic data: where it lives and who can join it",
        "## 6. The annual bias audit",
    ):
        body = section(text, heading)
        assert "Prefer not to say" in body and "blank" in body, heading


def test_the_forbidden_features_test_is_planned_and_not_claimed_absent(text) -> None:
    body = flat(section(text, "## 7. Prohibited inputs"))
    assert "tests/test_forbidden_features.py" in body and "planned in Task 3.6" in body
    assert "does not exist" not in body  # true today, silently false once Task 3.6 lands


def test_the_persisted_line_is_a_requirement_not_a_fact(text) -> None:
    body = flat(section(text, "## 3. What is persisted for every score"))
    assert "The persisted line must also carry the person's `opaque_id`" in body
    assert "and must carry no user id, name, email or" in body


def test_retention_lists_everything_a_replay_needs(text) -> None:
    body = flat(section(text, "## 3. What is persisted for every score")).lower()
    for needed in (
        "engine version",
        "config",
        "skill-graph artifacts",
        "by hash",
        "the inputs",
        "same period",
    ):
        assert needed in body, needed


def test_the_decisions_log_records_the_audit_decisions() -> None:
    log = (ENGINE_ROOT / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
    assert "\n## Audit and monitoring\n" in log
    audit = log.split("\n## Audit and monitoring\n", 1)[1].split("\n## ", 1)[0]
    assert re.findall(r"^\*\*D-(\d+) ", audit, flags=re.M) == [str(n) for n in range(90, 98)]
    lowered = audit.lower()
    for needle in (
        "distinct opaque ids",
        "eval/metrics/fairness.py",
        "pairs",
        "16 bytes",
        "quantile",
        "200 scores",
        "trace_version",
        "calibration{",
        "<redacted>",
        "exploratory",
        "not assessed",
        "prefer not to say",
        "owner",
    ):
        assert needle in lowered, needle
    assert max(len(line) for line in audit.splitlines()) <= 100
    assert chr(0x2014) not in audit
    flat_audit = " ".join(audit.split())
    # every unsafe component name is numbered (the code does so), so the entry must not say "where
    # keys would collide": a lone one is <redacted:1>
    assert "where keys would collide" not in flat_audit
    assert "a lone one is `<redacted:1>`" in flat_audit
