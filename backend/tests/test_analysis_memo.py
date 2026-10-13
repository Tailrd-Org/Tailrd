"""The tailoring analysis after Task 9.3 (D-83): no LLM, so nothing to memoise but the posting.

Prod 2026-08-11 showed one "tailor my résumé" journey buying the SAME LLM analysis three times
in 34 seconds. The analysis is now the deterministic engine's, so a repeat ask costs CPU only and
can never disagree with the first. The one cost that remains is parsing a pasted posting (the
extension has no job row to cache it on): these pin that a pasted posting is parsed once per
journey, and that a hit can never be the wrong posting.
"""

import datetime as dt

import pytest

from backend.services import fit_engine
from backend.services import match_engine

JD = "Requirements\n- 2+ years of Python\n- Kubernetes and AWS"


@pytest.fixture(autouse=True)
def _cold_cache(monkeypatch):
    monkeypatch.setattr(fit_engine, "fit_as_of", lambda today=None: dt.date(2026, 10, 1))
    fit_engine.reset_text_postings()
    yield
    fit_engine.reset_text_postings()


@pytest.fixture
def parses(monkeypatch):
    calls = {"n": 0}
    real = fit_engine.parse_posting

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(fit_engine, "parse_posting", counting)
    return calls


def test_the_same_pasted_posting_is_parsed_once(parses):
    first = fit_engine.posting_from_text("Backend Engineer", "Acme", JD)
    second = fit_engine.posting_from_text("Backend Engineer", "Acme", JD)
    assert parses["n"] == 1
    assert first == second


@pytest.mark.parametrize("title,company,jd", [
    ("Different Role", "Acme", JD),
    ("Backend Engineer", "Other Co", JD),
    ("Backend Engineer", "Acme", JD + "\n- Terraform"),
])
def test_any_changed_input_is_a_miss(parses, title, company, jd):
    fit_engine.posting_from_text("Backend Engineer", "Acme", JD)
    fit_engine.posting_from_text(title, company, jd)
    assert parses["n"] == 2


def test_a_new_posting_version_is_a_miss(parses, monkeypatch):
    """Keyed like the feed's posting cache (D-84): a new year, parse table or config re-parses."""
    fit_engine.posting_from_text("Backend Engineer", "Acme", JD)
    monkeypatch.setattr(fit_engine, "fit_as_of", lambda today=None: dt.date(2027, 1, 1))
    fit_engine.posting_from_text("Backend Engineer", "Acme", JD)
    assert parses["n"] == 2


def test_the_cache_is_bounded(parses, monkeypatch):
    monkeypatch.setattr(fit_engine, "_TEXT_POSTING_CACHE_SIZE", 4)
    for i in range(12):
        fit_engine.posting_from_text("Backend Engineer", "Acme", f"{JD}\n- skill {i}")
    assert len(fit_engine._TEXT_POSTINGS) <= 4


def test_the_llm_analysis_memo_is_gone():
    for gone in ("reset_analysis_memo", "_analysis_memo", "ANALYSIS_MEMO_TTL", "JOB_ANALYSIS_PROMPT"):
        assert not hasattr(match_engine, gone), gone
