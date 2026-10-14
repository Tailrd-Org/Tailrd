"""The shared work-authorization fixture, graded against the Python port.

chrome-extension/test/fixtures/work-auth-cases.json holds every phrasing from the three
decide-b review probe sets with its expected reading; chrome-extension/test/workAuthFixture.test.ts
grades the extension against the SAME file with the same rules (see that file's docstring).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.profile_facts import authorized_in, needs_sponsorship_in, work_auth_facts

FIXTURE = Path(__file__).resolve().parents[2] / "chrome-extension" / "test" / "fixtures" / "work-auth-cases.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))
NON_CITIZEN = {"permanent_resident", "work_permit", "student", "denied"}
NON_PR = {"work_permit", "student", "denied"}
#: Lost value on the whole fixture (567 phrasings) after the allow-list layer (D-61 amendment:
#: positives only from a fully recognised answer): 57 before it on the same 567 (49 on the first 467
#: after round 5). The ruling accepts the rise for zero wrong yeses on phrasings no blocklist could
#: enumerate.
LOST_VALUE_CEILING = 262


def _judge(exp, val, kind):
    if exp is None:
        return None
    if val is None:
        return "L" if exp is True or exp is False else None
    favourable = kind == "a"
    if exp == "nf":
        return ("N" if kind == "a" else "Y") if val is False else None
    if exp == "nt":
        return ("Y" if kind == "a" else "N") if val is True else None
    if exp == "unk":
        return "Y" if val == favourable else "N"
    if val == exp:
        return None
    return "Y" if val == favourable else "N"


def grade_case(c: dict) -> list[str]:
    a = work_auth_facts({"workAuthorization": c["wa"], "requiresSponsorship": c.get("rs", "")})
    errs: list[str] = []
    for x, e in c["exp"].items():
        code = None if x == "null" else x
        af = authorized_in(a, code, c.get("res"))
        av = af.value if af else None
        basis = a.by_country[code].basis if code and code in a.by_country else None
        sf = needs_sponsorship_in(a, code, c.get("res"))
        sp = sf.value if sf else None
        ja = _judge(e.get("a"), av, "a")
        if ja:
            errs.append(f"{ja} {x} authorized={av}")
        if e.get("cit") is True and basis != "citizen":
            errs.append(f"{'N' if basis in NON_CITIZEN else 'L'} {x} basis {basis} (citizen expected)")
        if e.get("cit") is False and basis == "citizen":
            errs.append(f"Y {x} basis citizen")
        if e.get("pr") is False and basis == "permanent_resident":
            errs.append(f"Y {x} basis permanent_resident")
        if e.get("pr") is True and basis not in ("permanent_resident", "citizen"):
            errs.append(f"{'N' if basis in NON_PR else 'L'} {x} basis {basis} (permanent resident expected)")
        js = _judge(e.get("sp"), sp, "sp")
        if js:
            errs.append(f"{js} {x} needsSponsorship={sp}")
    return errs


def test_the_fixture_holds_the_567_reviewer_phrasings():
    assert len(CASES) == 567


@pytest.mark.parametrize("case", CASES, ids=[f"{i}-{c['source']}" for i, c in enumerate(CASES)])
def test_each_case(case):
    assert [e for e in grade_case(case) if not e.startswith("L")] == []


def test_totals_zero_wrong_yes_zero_wrong_no_lost_value_within_ceiling():
    graded = [grade_case(c) for c in CASES]
    wrong_yes = sum(1 for errs in graded if any(e.startswith("Y") for e in errs))
    wrong_no = sum(1 for errs in graded if any(e.startswith("N") for e in errs))
    lost = sum(1 for errs in graded if errs and all(e.startswith("L") for e in errs))
    assert wrong_yes == 0
    assert wrong_no == 0
    assert lost <= LOST_VALUE_CEILING
