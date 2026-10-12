"""
The Python port against the REAL TypeScript (opt-in: needs Node >= 22.6, skips without it).

backend/services/profile_facts.py is a port of chrome-extension/src/content/profileFacts.ts and
geo.ts, and the two have to keep agreeing. This loads those two files IN PLACE under Node (type
stripping, no copy, no build step), feeds the TypeScript and the port the same generated corpus
and compares every result: facts, confidences and the `source` rule that produced each one.

When it fails, the extension and the port have diverged: decide which one is right and change it
(and its pinned tests) in the same commit. How to re-check more deeply, how to ask the TypeScript
a question, and how it works: backend/tests/tools/profile_facts_parity.py.
"""

from __future__ import annotations

import os

import pytest

from backend.tests.tools import profile_facts_parity as parity

_UNAVAILABLE = parity.unavailable_reason()

pytestmark = pytest.mark.skipif(_UNAVAILABLE is not None, reason=_UNAVAILABLE or "")


@pytest.fixture(autouse=True)
def setup_db():
    """Override conftest's autouse fixture of the same name: a pure comparison needs no tables."""
    yield


SEED = int(os.environ.get("PROFILE_FACTS_PARITY_SEED", parity.DEFAULT_SEED))
PER_KIND = int(os.environ.get("PROFILE_FACTS_PARITY_CASES", parity.DEFAULT_PER_KIND))


@pytest.fixture(scope="module")
def corpus_and_reference():
    """One Node run for the whole module: the generated corpus and what the TypeScript made of it."""
    cases = parity.generate_corpus(SEED, PER_KIND)
    return cases, parity.run_reference(cases)


def test_the_port_matches_the_typescript_on_a_generated_corpus(corpus_and_reference):
    cases, reference = corpus_and_reference
    mismatches = parity.compare(cases, reference)
    shown = "\n".join(mismatches[:8])
    assert not mismatches, (
        f"{len(mismatches)} of {len(cases)} cases differ (seed {SEED}, {PER_KIND} per kind):\n{shown}\n"
        "The port and the extension disagree: see backend/tests/tools/profile_facts_parity.py."
    )


def test_the_corpus_reaches_the_rules_it_is_meant_to_check(corpus_and_reference):
    """A comparison of two empty answers proves nothing: the corpus must exercise the rules."""
    cases, reference = corpus_and_reference
    missed = parity.EXPECTED_RULE_SOURCES - parity.rule_sources_reached(cases, reference)
    assert not missed, f"the generated corpus never reached: {sorted(missed)}"


def test_the_geo_tables_match_the_typescript():
    """COUNTRIES, CA_PROVINCES, US_STATES, the city hints and the free-movement group are row-for-row
    the extension's (the first three are imported from geo.ts, the last two read from its source)."""
    assert parity.table_mismatches() == []
