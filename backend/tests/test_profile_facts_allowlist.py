"""Decide B: the allow-list safety layer (decide-b-allowlist-brief.md, D-61 amendment "positives only
from a fully recognised answer").

Mirrors chrome-extension/test/workAuthAllowList.test.ts test for test:
- a positive status (citizen, permanent resident, authorized, visa held) is emitted only when every
  token of the answer is in the closed vocabulary of backend/data/work_auth_allowlist.json (plus the
  country names of the geo tables). One token outside it and no country gets a positive status.
  Negative and sponsorship facts are read as before;
- the answer is NFKC-normalised first; any non-ASCII letter left after that (a Cyrillic or Greek
  lookalike, an accent) is outside the vocabulary;
- a labelled multi-line answer ("Canada: citizen\\nUS: no") is checked line by line.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.services.profile_facts import (
    authorized_in,
    needs_sponsorship_in,
    out_of_vocabulary,
    work_auth_facts,
)

ALLOW_FILE = Path(__file__).resolve().parents[1] / "data" / "work_auth_allowlist.json"


@pytest.fixture(autouse=True)
def setup_db():
    """Override conftest's autouse fixture of the same name: pure functions need no tables."""
    yield


def _auth(wa: str, rs: str = ""):
    return work_auth_facts({"workAuthorization": wa, "requiresSponsorship": rs})


def _basis(wa: str, code: str):
    c = _auth(wa).by_country.get(code)
    return c.basis if c else None


def _authorized(wa: str, code):
    f = authorized_in(_auth(wa), code, None)
    return f.value if f else None


def test_the_shared_vocabulary_holds_the_rulings_words_and_nothing_it_excludes():
    vocab = json.loads(ALLOW_FILE.read_text(encoding="utf-8"))
    every = [w for k, v in vocab.items() if not k.startswith("_") and isinstance(v, list) for w in v]
    for w in ["yes", "no", "i", "i'm", "am", "a", "an", "the", "my", "have", "hold", "with", "citizen", "citizenship",
              "national", "permanent resident", "pr", "green card", "lpr", "authorized", "authorised",
              "eligible to work", "legally", "work authorization", "work permit", "open work permit", "pgwp",
              "study permit", "opt", "stem opt", "cpt", "h-1b", "h1b", "tn", "l-1", "e-3", "o-1", "visa", "status",
              "and", "plus", "both", "in", "of", "for", "to", "work", "working", "no sponsorship needed",
              "without sponsorship", "do not require sponsorship", "don't need sponsorship"]:
        assert w in every, w
    for w in ["wife", "husband", "hubby", "not", "never", "was", "former", "ex", "will", "pending", "soon", "eligible",
              "need"]:
        assert w not in every, w


# Common real answers keep their positives.

def test_yes():
    assert _authorized("Yes", None) is True


def test_canadian_citizen():
    assert _basis("Canadian citizen", "CA") == "citizen"


def test_us_citizen():
    assert _basis("US citizen", "US") == "citizen"


def test_pr():
    assert _authorized("PR", None) is True


def test_canadian_pr():
    assert _basis("Canadian PR", "CA") == "permanent_resident"


def test_green_card_holder():
    assert _basis("Green card holder", "US") == "permanent_resident"


def test_authorized_to_work_in_canada_and_the_us():
    assert _authorized("Authorized to work in Canada and the US", "CA") is True
    assert _authorized("Authorized to work in Canada and the US", "US") is True


def test_yes_canadian_citizen_no_sponsorship_needed():
    a = _auth("Yes, Canadian citizen, no sponsorship needed")
    assert a.by_country["CA"].basis == "citizen"
    assert needs_sponsorship_in(a, "CA", None).value is False


def test_i_am_a_us_citizen():
    assert _basis("I am a US citizen", "US") == "citizen"


def test_citizen_of_canada():
    assert _basis("Citizen of Canada", "CA") == "citizen"


def test_canada_yes_us_no_on_two_lines():
    assert _authorized("Canada: Yes\nUS: No", "CA") is True
    assert _authorized("Canada: Yes\nUS: No", "US") is False


def test_h1b():
    a = _auth("H-1B")
    assert authorized_in(a, "US", None).value is True
    assert needs_sponsorship_in(a, "US", None).value is True


def test_on_opt():
    assert _authorized("On OPT", "US") is True


def test_pgwp():
    assert _authorized("PGWP", "CA") is True


def test_curly_apostrophes_and_nfkc_spaces_are_not_outside_the_vocabulary():
    assert _basis("I’m a Canadian citizen", "CA") == "citizen"


# One unknown token removes every positive.

@pytest.mark.parametrize("wa,code", [
    ("My better half is a US citizen.", "US"),
    ("Hubby is a Canadian citizen.", "CA"),
    ("US citizen. NOT.", "US"),
    ("Canadian PR (lapsed in 2022).", "CA"),
    ("Green card in the works.", "US"),
    ("Canadian citizen-to-be.", "CA"),
    ("Wed to a US citizen.", "US"),
    ("Canadian citizen, US citizen except me", "CA"),
])
def test_one_unknown_token_removes_every_positive(wa, code):
    assert _authorized(wa, code) is not True
    assert _basis(wa, code) not in ("citizen", "permanent_resident")


@pytest.mark.parametrize("wa,code", [
    ("US citizen? No.", "US"),
    ("Am I a US citizen? No", "US"),
    ("Canadian PR: false", "CA"),
    ("green card? no", "US"),
    ("Canadian citizen n", "CA"),
    ("Working on my green card", "US"),
])
def test_a_negative_answer_word_beside_a_status_or_on_before_no_visa(wa, code):
    assert _authorized(wa, code) is not True
    assert _basis(wa, code) not in ("citizen", "permanent_resident")


def test_on_counts_before_a_visa_a_or_an_aside():
    assert out_of_vocabulary("Working on OPT") is False
    assert out_of_vocabulary("On a work permit") is False
    assert out_of_vocabulary("Working on PR") is True
    assert _authorized("citizen? no", None) is not True


def test_a_bare_yes_beside_an_unknown_token_is_unknown_never_a_no():
    assert _authorized("Yes, hubby", None) is None


def test_negative_facts_are_still_read():
    assert _authorized("Canadian citizen, not authorized in the US", "US") is False
    assert _authorized("Canadian citizen, not authorized in the US", "CA") is None


def test_my_is_in_the_vocabulary_only_before_a_status_noun_about_the_applicant():
    assert _basis("My Canadian citizenship", "CA") == "citizen"
    assert _basis("Canadian citizen, my", "CA") is None
    assert out_of_vocabulary("my green card") is False
    assert out_of_vocabulary("my PR card") is False
    assert out_of_vocabulary("US citizen, my") is True


# Normalisation.

@pytest.mark.parametrize("name,wa,code", [
    ("fullwidth negation", "Ｎｏｔ a Canadian PR.", "CA"),
    ("fullwidth third party", "My ｈｕｓｂａｎｄ is a US citizen.", "US"),
    ("Cyrillic o in not", "I am nоt a US citizen.", "US"),
    ("Cyrillic a in husband", "My husbаnd is a US citizen.", "US"),
    ("accented letter", "My wifé is a Canadian citizen.", "CA"),
    ("Cyrillic i in citizen", "Canadian citіzen", "CA"),
    ("Chinese words", "我的丈夫是 US citizen.", "US"),
    ("combining accent", "US citizén", "US"),
    ("zero-width space", "US citi​zen", "US"),
])
def test_normalisation(name, wa, code):
    assert _authorized(wa, code) is not True
    assert _basis(wa, code) not in ("citizen", "permanent_resident")


def test_nfkc_folds_fullwidth_letters_that_are_in_the_vocabulary():
    assert out_of_vocabulary("ＵＳ citizen") is False


# Labelled multi-line answers are checked line by line.

def test_an_unknown_token_on_one_labelled_line_leaves_the_other_lines_positive():
    wa = "Canada: citizen\nUS: citizen through my wife"
    assert _basis(wa, "CA") == "citizen"
    assert _authorized(wa, "US") is not True
    assert _basis(wa, "US") != "citizen"


def test_a_dash_labels_a_line_too():
    assert _authorized("Canada - Yes\nUS - No", "CA") is True
    assert _authorized("Canada - Yes\nUS - No", "US") is False


def test_lines_without_labels_are_one_answer():
    assert _basis("Canadian citizen\nMy wife is a US citizen", "CA") is None
    assert _basis("Canadian citizen\nMy wife is a US citizen", "US") is None
