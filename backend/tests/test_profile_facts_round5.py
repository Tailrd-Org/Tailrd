"""Decide B fix round 5 (decide-b-round5-brief.md, after decide-b-review4.md).

Mirrors chrome-extension/test/workAuthRound5.test.ts test for test:
- veto lexicon per segment: a segment that holds a third-party, negation, past, future/conditional
  or pending cue gets no positive status (citizen, permanent resident, authorized, visa held) for
  any country, and the veto never turns into a No. The cues live in backend/data/work_auth_veto.json,
  read by both languages;
- a sponsorship need stated for one country applies only to that country; "now" and "currently"
  start a new segment ("was X, now Y");
- the input is capped at 4,000 characters before parsing, and the review4 pathological inputs at
  100,000 characters each read in under 200 ms.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backend.services.profile_facts import (
    INPUT_CAP,
    authorized_in,
    needs_sponsorship_in,
    polarity_of,
    work_auth_facts,
)

VETO_FILE = Path(__file__).resolve().parents[1] / "data" / "work_auth_veto.json"


def _auth(wa: str, rs: str = ""):
    return work_auth_facts({"workAuthorization": wa, "requiresSponsorship": rs})


def _value(fact):
    return fact.value if fact else None


def _basis(a, code):
    c = a.by_country.get(code)
    return c.basis if c else None


def test_the_shared_veto_lexicon_holds_every_cue_family_of_the_ruling():
    lexicon = json.loads(VETO_FILE.read_text(encoding="utf-8"))
    ruling = {
        "third_party": ["wife", "husband", "spouse", "partner", "fiance", "fiancee", "parent", "parents", "mother",
                        "father", "mom", "dad", "family", "sister", "brother", "son", "daughter", "child", "children",
                        "sponsor"],
        "negation": ["not", "neither", "nor", "never", "no longer", "without", "lost", "revoked", "denied", "rejected",
                     "ineligible"],
        "past": ["was", "were", "used to", "previously", "formerly", "former", "expired", "lapsed", "abandoned", "until",
                 "had"],
        "future": ["will", "would", "once", "when", "after", "upon", "if", "planning", "plan", "hope", "expect",
                   "going to", "soon", "future"],
        "pending": ["pending", "applied", "applying", "application", "waiting", "awaiting", "in process", "processing",
                    "lottery", "eligible", "eligibility", "need", "needs", "require", "requires", "requiring",
                    "sponsorship request"],
    }
    for family, words in ruling.items():
        for w in words:
            assert w in lexicon[family], f"{family}: {w}"
    assert {"non-", "ex-"} <= set(lexicon["prefixes"])


VETOED = [
    # Third party.
    ("My husband is a US citizen and we recently married, so I am in the process of applying for a green card "
     "through him. Until that is approved I do not have permission to work in the US.", ["US"]),
    ("My wife is a US citizen", ["US"]),
    ("My father is Canadian citizen", ["CA"]),
    ("My spouse holds a green card; I am on H-4", ["US"]),
    ("My mother is a British citizen, I am applying for citizenship by descent", ["GB"]),
    ("Married to a Canadian citizen, sponsorship application in progress", ["CA"]),
    ("My partner is an American citizen. I am a Canadian citizen.", ["US"]),
    ("Both my parents are US citizens but I was born in Canada and only hold Canadian citizenship", ["US"]),
    ("My wife's employer sponsored her H-1B; I am her H-4 dependent without EAD", ["US"]),
    ("My son is a Canadian citizen", ["CA"]),
    ("Not me, my brother is the US citizen; I need sponsorship", ["US"]),
    ("My fiancee is a Canadian PR", ["CA"]),
    # Negation.
    ("Canadian citizen. Not a US citizen. Not a US PR.", ["US"]),
    ("Neither Canadian citizen nor PR", ["CA"]),
    ("Not yet a citizen", []),
    ("Used to be a US citizen but renounced it in 2019; now Canadian citizen only", ["US"]),
    ("Non-US citizen", ["US"]),
    ("US work permit revoked", ["US"]),
    ("Canadian PR status lost", ["CA"]),
    # Past.
    ("I was previously on an H-1B but my status expired when I left my job in March, and I'm now back in India. "
     "I would need a new H-1B petition to return to the US.", ["US"]),
    ("I had a green card but it was abandoned when I moved back to Canada; Canadian citizen", ["US"]),
    ("Formerly a US permanent resident", ["US"]),
    ("Ex-US citizen", ["US"]),
    ("Expired Canadian work permit", ["CA"]),
    ("Was a Canadian PR", ["CA"]),
    # Future, conditional, pending, needed.
    ("As a Canadian citizen I can work in Canada freely, and under USMCA I would be able to obtain TN status at the "
     "border for a qualifying US position, though I do not currently hold it.", ["US"]),
    ("Honestly, I'm not sure how to answer this. I'm a Canadian citizen but I've been working remotely for a US "
     "company as a contractor. I've never had a US work visa.", ["US"]),
    ("For work in USA I am requiring the sponsor of H1B", ["US"]),
    ("I would be authorized to work in the US once my green card is approved", ["US"]),
    ("Should I receive the H-1B in the lottery, I will need sponsorship", ["US"]),
    ("I will become a permanent resident when my application is finalized in the spring", []),
    ("Once I graduate in May I will be on OPT", ["US"]),
    ("Will be a US citizen next year", ["US"]),
    ("US citizenship application pending", ["US"]),
    ("Eligible to work in the US after my EAD arrives", ["US"]),
    ("Hope to get Canadian PR soon", ["CA"]),
]


@pytest.mark.parametrize("text,codes", VETOED, ids=[t[:60] for t, _ in VETOED])
def test_veto_a_segment_with_a_cue_gets_no_positive_status(text, codes):
    a = _auth(text)
    for code in codes:
        assert _value(authorized_in(a, code, None)) is not True, f"{code} authorized"
        assert _basis(a, code) not in ("citizen", "permanent_resident"), f"{code} basis"
    if not codes:
        assert _value(authorized_in(a, None, None)) is not True
        assert _value(authorized_in(a, "CA", "CA")) is not True


def test_someone_elses_citizenship_is_unknown_not_a_no():
    a = _auth("My wife is a US citizen")
    assert authorized_in(a, "US", None) is None
    assert needs_sponsorship_in(a, "US", None) is None


def test_an_explicit_no_is_still_a_no():
    assert _value(authorized_in(_auth("Canada: yes\nUS: No"), "US", None)) is False
    assert _value(authorized_in(_auth("Not authorized to work in the US"), "US", None)) is False


def test_the_applicants_own_clause_keeps_its_status():
    a = _auth("My partner is an American citizen. I am a Canadian citizen.")
    ca = a.by_country["CA"]
    assert (ca.authorized, ca.basis) == (True, "citizen")
    ca = _auth("Canadian citizen").by_country["CA"]
    assert (ca.authorized, ca.needs_sponsorship, ca.basis) == (True, False, "citizen")
    us = _auth("US permanent resident").by_country["US"]
    assert (us.authorized, us.basis) == (True, "permanent_resident")


def test_a_negated_sponsorship_need_is_not_a_veto_on_the_status_beside_it():
    for text in ("I am a Canadian citizen and do not require sponsorship", "Canadian citizen, no sponsorship needed",
                 "US citizen without sponsorship"):
        code = "CA" if "canadian" in text.lower() else "US"
        c = _auth(text).by_country[code]
        assert (c.authorized, c.basis) == (True, "citizen"), text


def test_was_x_now_y_reads_y_as_the_current_status():
    ca = _auth("Was on a study permit, now a Canadian PR").by_country["CA"]
    assert (ca.authorized, ca.basis) == (True, "permanent_resident")
    b = _auth("I was a US green card holder now I am a Canadian citizen")
    assert (b.by_country["CA"].authorized, b.by_country["CA"].basis) == (True, "citizen")
    assert _basis(b, "US") != "permanent_resident"


def test_not_currently_authorized_is_never_split_into_a_yes():
    assert _value(authorized_in(_auth("Not currently authorized to work in the US"), "US", None)) is False
    assert _value(authorized_in(_auth("I don't currently have US work authorization"), "US", None)) is False


FIVE = ("I hold dual citizenship with the United Kingdom and Canada. I grew up in London and moved to Calgary for "
        "university, so I can work in either country without restrictions. I am not authorized to work in the United "
        "States and would need a visa.")


def test_sponsorship_scope_review4_five():
    a = _auth(FIVE)
    assert _value(needs_sponsorship_in(a, "CA", None)) is not True
    assert _value(needs_sponsorship_in(a, "GB", None)) is not True
    assert _value(authorized_in(a, "US", None)) is False
    assert _value(needs_sponsorship_in(a, "US", None)) is True


def test_a_need_in_a_sentence_that_names_no_country_still_applies_to_the_countries_named():
    a = _auth("I live in the US on OPT. I will need sponsorship.")
    assert _value(needs_sponsorship_in(a, "US", None)) is True


def test_the_input_cap_is_4000_characters():
    assert INPUT_CAP == 4000


def test_text_after_the_cap_is_not_read():
    pad = "x" * INPUT_CAP
    assert "CA" not in _auth(f"{pad} Canadian citizen").by_country
    assert _basis(_auth(f"Canadian citizen {pad}"), "CA") == "citizen"
    assert polarity_of(f"{pad} yes") is None


def test_the_cap_counts_characters_not_utf16_units():
    astral = "\U0001F600" * (INPUT_CAP - 20)
    assert _basis(_auth(f"{astral} Canadian citizen"), "CA") == "citizen"


# --- timing: the review4 pathological inputs, truncated to 100,000 characters -----------------
def _lcg(seed: int):
    s = seed & 0xFFFFFFFF

    def nxt() -> int:
        nonlocal s
        s = (s * 1103515245 + 12345) & 0xFFFFFFFF
        return s >> 8

    return nxt


C = ["Canada", "US", "UK", "India", "Mexico", "Germany", "the United States", "U.S.", "USA", "America"]


def _cycle(xs, n):
    return [xs[i % len(xs)] for i in range(n)]


def _pick(xs, n, seed):
    r = _lcg(seed)
    return [xs[r() % len(xs)] for _ in range(n)]


PATHOLOGICAL = {
    "nested_parens_50k": "(" * 25000 + "Canadian citizen" + ")" * 25000,
    "nested_parens_country": "".join(f"({c} " for c in _cycle(C, 25000)) + ")" * 25000,
    "US_space_100k": "US " * 33334,
    "US_nospace_100k": "US" * 50000,
    "US_comma_100k": "US, " * 25000,
    "US_dots_100k": "U.S. " * 20000,
    "not_country_alt": " ".join(f"not {c}" for c in _cycle(C, 12000)),
    "not_citizen_alt": " ".join(f"not {c} citizen" for c in _cycle(C, 8000)),
    "not_not_country": "not " * 20000 + "Canada " + "not " * 20000,
    "country_slash": "/".join(_cycle(C, 50000)),
    "country_and": " and ".join(_cycle(C, 40000)),
    "colon_lines": "\n".join(f"{c}: citizen" for c in _cycle(C, 10000)),
    "colon_need": "\n".join(f"{c}: needs sponsorship" for c in _cycle(C, 8000)),
    "dash_pending": "green card " + "- " * 50000 + "pending",
    "paren_pending": "green card " + "( " * 50000 + "pending",
    "hyphen_chain": "TN-" * 33000 + "eligible",
    "H1B_variants": " ".join(_pick(["H-1B", "H1B", "H 1 B", "h1-b", "OPT", "STEM OPT", "EAD", "TN", "L-1", "PR", "GC"],
                                   25000, 4)),
    "mixed_punct_100k": "".join(_pick(list(".,;:!?-()[]{}/\\|'\"_*&^%$#@~` \n\t"), 100000, 5)),
    "spaces_100k": " " * 100000,
    "tabs_nl_100k": "Canadian" + " \t\n\r" * 25000 + "citizen",
    "unicode_spaces": "US" + "  　" * 33000 + "citizen",
    "realistic_50k": ("I am a Canadian citizen living in Toronto, and I was on OPT in the U.S. previously, now I need "
                      "H-1B sponsorship to work in the United States; not authorized in the UK. ") * 300,
    "citizen_of_list": "citizen of " + ", ".join(_cycle(C, 50000)),
    "permanent_resident_of": "permanent resident of " * 5000,
    "need_spons_for": "need sponsorship for " * 5000 + "Canada",
    "emdash_100k": "Canadian citizen " + (chr(0x2014) + " ") * 50000 + "US",
    "yes_no_alt": " ".join(_cycle(["yes", "no", "not", "Canada", "US"], 25000)),
}


@pytest.mark.parametrize("name", list(PATHOLOGICAL))
def test_timing_the_review4_pathological_inputs_read_in_under_200_ms(name):
    text = PATHOLOGICAL[name][:100_000]
    _auth("Canadian citizen; US: no", "No")
    started = time.perf_counter()
    a = _auth(text, text)
    authorized_in(a, "US", None)
    needs_sponsorship_in(a, "CA", None)
    polarity_of(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.2, f"{name}: {elapsed * 1000:.0f} ms"
