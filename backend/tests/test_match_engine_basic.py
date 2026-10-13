"""MatchEngine after Task 9.3: the tailoring flow's analysis, on the deterministic engine only."""

from backend.services import match_engine
from backend.services.match_engine import MatchEngine


def test_no_job_scoring_is_left_in_the_llm_match_engine():
    # Job scores come from the deterministic fit engine (Task 9.2, D-83); MatchEngine keeps only
    # the tailoring flow's analysis, which is the engine's too (Task 9.3).
    for gone in ("compute_breakdown", "analyze_fit", "queue_analysis", "_parse_json_response", "llm"):
        assert not hasattr(MatchEngine, gone), gone
    for kept in ("analyze_job", "analyze", "posting"):
        assert hasattr(MatchEngine, kept), kept


def test_the_80_60_labels_are_gone():
    # D-85: every hard-coded 80/60 is replaced by the engine's band.
    assert not hasattr(match_engine, "score_to_label")
    assert not hasattr(match_engine, "DEFAULT_MATCH_MODEL")
