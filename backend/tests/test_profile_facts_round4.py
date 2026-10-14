"""Decide B fix round 4 (decide-b-round4-brief.md, after decide-b-review3.md).

Mirrors chrome-extension/test/workAuthRound4.test.ts test for test:
- a pending, applied-for, lottery, waiting or eligible status is never held;
- on a labelled multi-line answer, one country's line never changes another's;
- every pattern on user text is linear: whitespace runs are collapsed first, and 100,000 spaces,
  100,000 punctuation characters and a 50,000-character answer each read in under 200 ms.
"""

from __future__ import annotations

import re
import time

import pytest

from backend.services.profile_facts import authorized_in, needs_sponsorship_in, polarity_of, work_auth_facts


def _auth(wa: str, rs: str = ""):
    return work_auth_facts({"workAuthorization": wa, "requiresSponsorship": rs})


def _value(fact):
    return fact.value if fact else None


def _basis(a, code):
    c = a.by_country.get(code)
    return c.basis if c else None


PENDING = [
    "I-485 pending",
    "I485 pending",
    "green card pending",
    "Green card - pending",
    "green card (pending)",
    "Green card in process",
    "green card application in progress",
    "Waiting on my green card",
    "GC pending",
    "H-1B lottery pending",
    "H-1B lottery",
    "In the H1B lottery",
    "Entered the H-1B lottery this year",
    "Applying for PR",
    "Applied for PR",
    "PR - pending",
    "PR (pending)",
    "PR application submitted",
    "Eligible for PR next year",
    "Canadian citizen, TN-eligible",
    "Canadian citizen, can get a TN",
    "Canadian citizen; would relocate to the US on a TN",
    "Canadian citizen. Plan to get TN for US jobs.",
    "Canadian citizen; open to relocating to the US with TN",
    "Planning to apply for a TN",
    "Could get an H-1B",
    "Can obtain TN status",
]


@pytest.mark.parametrize("text", PENDING)
def test_a_pending_status_is_never_held(text):
    a = _auth(text)
    assert _value(authorized_in(a, "US", None)) is not True
    assert _basis(a, "US") != "permanent_resident"
    assert _basis(a, "CA") != "permanent_resident"
    assert _value(authorized_in(a, None, None)) is not True
    if not re.search(r"canadian citizen", text, re.IGNORECASE):
        assert _value(authorized_in(a, "CA", "CA")) is not True


def test_a_held_status_next_to_a_pending_one_is_unknown_under_the_allow_list_never_a_pending_one_held():
    # "pending", "I-485", "EAD", "hand": outside the closed vocabulary (D-61 amendment).
    a = _auth("On H-1B, green card pending").by_country.get("US")
    assert a is None or (a.basis != "permanent_resident" and a.authorized is None)
    b = _auth("I-485 pending, EAD in hand").by_country.get("US")
    assert b is None or b.authorized is None


LINES = [
    ("Canada: citizen\nUSA: needs sponsorship", "citizen"),
    ("Canada: Citizen\nUS: Requires sponsorship", "citizen"),
    ("Canada: PR\nUS: need visa", "permanent_resident"),
    ("Canada: permanent resident\nUS: would need a visa", "permanent_resident"),
]


@pytest.mark.parametrize("text,ca_basis", LINES)
def test_one_countrys_line_never_changes_anothers(text, ca_basis):
    a = _auth(text)
    ca = a.by_country["CA"]
    assert (ca.authorized, ca.needs_sponsorship, ca.basis) == (True, False, ca_basis)
    assert _value(needs_sponsorship_in(a, "CA", None)) is False
    assert _value(needs_sponsorship_in(a, "US", None)) is True
    assert _value(authorized_in(a, "US", None)) is not True


def test_a_us_lines_pending_status_never_reaches_the_canada_line():
    a = _auth("Canada: open work permit\nUS: green card pending")
    ca = a.by_country["CA"]
    assert (ca.authorized, ca.basis) == (True, "work_permit")
    assert _value(authorized_in(a, "US", None)) is not True


def test_informal_yes_and_no_and_a_question_echoed_before_the_answer():
    assert polarity_of("Nope") is False
    assert polarity_of("yep") is True
    assert polarity_of("Do you require sponsorship? No I do not") is False
    assert polarity_of("Do you now or in the future require sponsorship? No.") is False


def test_a_leading_no_that_a_sponsorship_need_confirms_is_a_no():
    assert _value(authorized_in(_auth("No, I need sponsorship"), None, None)) is False
    assert _value(authorized_in(_auth("No - I will require sponsorship"), "CA", "CA")) is False
    assert authorized_in(_auth("No, but I will need sponsorship later"), None, None) is None


def test_na_citizen_does_not_block_the_single_country_inference():
    assert _value(needs_sponsorship_in(_auth("US citizen", "N/A - citizen"), None, None)) is False


# --- timing ------------------------------------------------------------------------------------
PUNCT = ",.;:-/()&+!?|[]'\"*#@%=_~<>"


def _punctuation(n: int) -> str:
    return (PUNCT * (n // len(PUNCT) + 1))[:n]


MIXED = "US: - / ( ) , ; . Canada ? | & + "
REALISTIC = (
    "Hi! I'm a 4th year CS student at UofT.\nCitizenship: Canadian\nCanada: citizen\nUSA: needs sponsorship\n"
    "I am a Canadian citizen, not authorized in the U.S. (would need TN). Green card - pending. "
    "H-1B lottery pending; open to relocating to the US with TN. Canada - yes / US - no.\n"
)
TIMING_INPUTS = {
    "100,000 spaces": " " * 100_000,
    "100,000 spaces inside a status": "Canadian" + " " * 100_000 + "citizen",
    "100,000 spaces after a country": "US" + " " * 100_000 + "x",
    "100,000 mixed whitespace": ("Canada \t\n " * 10_000)[:100_000],
    "100,000 punctuation characters": _punctuation(100_000),
    "100,000 punctuation characters between countries": "US" + _punctuation(100_000) + "Canada",
    "100,000 punctuation and countries": (MIXED * (100_000 // len(MIXED) + 1))[:100_000],
    "a 50,000-character realistic answer": (REALISTIC * (50_000 // len(REALISTIC) + 1))[:50_000],
}


@pytest.mark.parametrize("name", list(TIMING_INPUTS))
def test_timing_every_pattern_on_user_text_is_linear(name):
    text = TIMING_INPUTS[name]
    _auth("Canadian citizen; US: no", "No")
    started = time.perf_counter()
    a = _auth(text, text)
    authorized_in(a, "US", None)
    needs_sponsorship_in(a, "CA", None)
    polarity_of(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2, f"{name}: {elapsed * 1000:.0f} ms"
