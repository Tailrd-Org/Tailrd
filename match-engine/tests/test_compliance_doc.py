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
        "The score ranks. It never rejects.",
        "We couldn't score this",
        "employment attorney",
        "It is not legal advice",
        "A fairness failure is terminal",
        "no user id column",
        "keyed by opaque id",
    ):
        assert phrase.lower() in flat.lower(), phrase


def test_the_document_has_no_em_dashes_and_no_emoji(text) -> None:
    assert "—" not in text
    assert not any(ord(ch) > 0x2000 and ch not in "’" for ch in text)
