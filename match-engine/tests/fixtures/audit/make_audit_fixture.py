"""Builds tests/fixtures/audit/: synthetic persisted traces and a synthetic demographics file.

Everything here is invented: users are "synthetic-user-NNN", the salt is a made-up constant, the
attributes are "attr_pass" and "attr_fail" and the groups are "p1".."p3" and "q1".."q2". Nothing
refers to a real person or a real demographic category (DECISIONS D-04: the repository is public).

The committed files are this module's output, written through `write_fixture`;
`tests/test_bias_audit.py::test_committed_fixture_is_current` fails when they drift from this
source. Regenerate from match-engine/ with:

    ./.venv/Scripts/python.exe tests/fixtures/audit/make_audit_fixture.py

The people (index -> calibrated score, or an outcome tag). S = SCORED, LC = SCORED_LOW_CONFIDENCE,
NR = NEEDS_REVIEW (UNREADABLE), GF = GATE_FAIL (WORK_AUTH_MISMATCH). Two people have a second trace.

    0-9    attr_pass p1, attr_fail q1: S92 S88 S85 S80 S74 S70 S66 S58 S40 NR
    10-19  attr_pass p2, attr_fail q2: S90 S87 S85 S78 S72 LC62 S58 S45 GF S30
    20     attr_pass p3, attr_fail q1: S95
    21     attr_pass p3, attr_fail q1: S52, S40
    22     attr_pass blank, attr_fail q2: S83
    23     attr_pass blank, attr_fail q2: S61, S41
    24     not in the demographics file: S77

then three unusable lines (not JSON; no `trace`; a user id where an opaque id belongs) and one line
whose digest does not match its trace.
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

if __name__ == "__main__":  # run as a script: make `matcher` importable without installing it
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from matcher.audit import build_trace, opaque_id, trace_digest  # noqa: E402
from matcher.errors import Flag  # noqa: E402
from matcher.models import (  # noqa: E402
    Band,
    GateKind,
    GateResult,
    GateStatus,
    MatchResult,
    Outcome,
)

FIXTURE_DIR = Path(__file__).resolve().parent
SALT = b"audit-fixture-salt-0123456789ab"  # synthetic; real salts are secret and never committed
CONFIG_HASH = "0f1e2d3c4b5a6978"
OTHER_CONFIG_HASH = "a1b2c3d4e5f60718"
FIRST_DAY = date(2026, 9, 1)

# index -> traces, each a calibrated score or an outcome tag
PLAN: dict[int, tuple] = {
    0: (92,),
    1: (88,),
    2: (85,),
    3: (80,),
    4: (74,),
    5: (70,),
    6: (66,),
    7: (58,),
    8: (40,),
    9: ("NR",),
    10: (90,),
    11: (87,),
    12: (85,),
    13: (78,),
    14: (72,),
    15: ("LC",),
    16: (58,),
    17: (45,),
    18: ("GF",),
    19: (30,),
    20: (95,),
    21: (52, 40),
    22: (83,),
    23: (61, 41),
    24: (77,),
}
LOW_CONFIDENCE_SCORE = 62

ATTR_PASS = {
    **{i: "p1" for i in range(0, 10)},
    **{i: "p2" for i in range(10, 20)},
    20: "p3",
    21: "p3",
    22: "",
    23: "",
}
ATTR_FAIL = {
    **{i: "q1" for i in range(0, 10)},
    20: "q1",
    21: "q1",
    **{i: "q2" for i in range(10, 20)},
    22: "q2",
    23: "q2",
}


def user_key(index: int) -> str:
    return f"synthetic-user-{index:03d}"


def person_id(index: int) -> str:
    return opaque_id(user_key(index), SALT)


def band_for(score: int) -> Band:
    if score >= 85:
        return Band.STRONG
    if score >= 70:
        return Band.GOOD
    if score >= 55:
        return Band.PARTIAL
    return Band.LIMITED


def make_result(index: int, slot: int, item) -> MatchResult:
    common = dict(
        gate_status=GateStatus.PASS,
        engine_version="1.0.1" if index == 12 else "1.0.0",
        config_hash=OTHER_CONFIG_HASH if index == 12 else CONFIG_HASH,
        as_of=FIRST_DAY + timedelta(days=index),
    )
    if item == "NR":
        return MatchResult(outcome=Outcome.NEEDS_REVIEW, reasons=(Flag.UNREADABLE,), **common)
    if item == "GF":
        gate = GateResult(
            kind=GateKind.WORK_AUTHORIZATION, status=GateStatus.FAIL, reason=Flag.WORK_AUTH_MISMATCH
        )
        return MatchResult(
            outcome=Outcome.GATE_FAIL,
            reasons=(Flag.WORK_AUTH_MISMATCH,),
            gates=(gate,),
            **{**common, "gate_status": GateStatus.FAIL},
        )
    if item == "LC":
        score = LOW_CONFIDENCE_SCORE
        return MatchResult(
            outcome=Outcome.SCORED_LOW_CONFIDENCE,
            raw_score=score / 100,
            calibrated_score=score,
            band=band_for(score),
            reasons=(Flag.LOW_EVIDENCE_DENSITY,),
            **common,
        )
    return MatchResult(
        outcome=Outcome.SCORED,
        raw_score=item / 100,
        calibrated_score=item,
        display_score=item,
        band=band_for(item),
        **common,
    )


def trace_lines() -> list[str]:
    lines = []
    for index, items in PLAN.items():
        for slot, item in enumerate(items):
            trace = build_trace(make_result(index, slot, item))
            record = {"opaque_id": person_id(index), "digest": trace_digest(trace), "trace": trace}
            lines.append(json.dumps(record, sort_keys=True))
    good_trace = build_trace(make_result(3, 0, 80))
    tampered = {**good_trace, "calibrated": 99, "band": Band.STRONG.value}
    lines.append("{this line is not json")
    lines.append(json.dumps({"opaque_id": person_id(3), "digest": trace_digest(good_trace)}, sort_keys=True))
    lines.append(json.dumps({"opaque_id": "user-17", "trace": good_trace}, sort_keys=True))
    lines.append(
        json.dumps(
            {"opaque_id": person_id(3), "digest": trace_digest(good_trace), "trace": tampered}, sort_keys=True
        )
    )
    return lines


def demographic_rows() -> list[tuple[str, str, str]]:
    rows = [(person_id(i), ATTR_PASS[i], ATTR_FAIL[i]) for i in sorted(ATTR_FAIL)]
    return sorted(rows)  # by opaque id: the file's order says nothing about the people


def write_fixture(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "traces.jsonl").write_text("\n".join(trace_lines()) + "\n", encoding="utf-8", newline="\n")
    with (directory / "demographics.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["opaque_id", "attr_pass", "attr_fail"])
        writer.writerows(demographic_rows())


if __name__ == "__main__":
    write_fixture(FIXTURE_DIR)
    print(f"wrote {FIXTURE_DIR / 'traces.jsonl'} and {FIXTURE_DIR / 'demographics.csv'}")
