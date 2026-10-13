"""Basic tests for MatchEngine service."""

from backend.services.match_engine import MatchEngine, score_to_label


class FakeDB:
    pass


def test_score_to_label_strong():
    assert score_to_label(80) == "STRONG MATCH"
    assert score_to_label(90) == "STRONG MATCH"
    assert score_to_label(100) == "STRONG MATCH"


def test_score_to_label_good():
    assert score_to_label(60) == "GOOD MATCH"
    assert score_to_label(79) == "GOOD MATCH"
    assert score_to_label(70) == "GOOD MATCH"


def test_score_to_label_fair():
    assert score_to_label(59) == "FAIR MATCH"
    assert score_to_label(0) == "FAIR MATCH"
    assert score_to_label(30) == "FAIR MATCH"


def test_parse_json_plain():
    engine = MatchEngine(FakeDB())
    result = engine._parse_json_response('{"overall_score": 75}')
    assert result == {"overall_score": 75}


def test_parse_json_with_code_fence():
    engine = MatchEngine(FakeDB())
    response = '```json\n{"overall_score": 85}\n```'
    result = engine._parse_json_response(response)
    assert result == {"overall_score": 85}


def test_parse_json_with_preamble():
    engine = MatchEngine(FakeDB())
    response = 'Here is the analysis:\n{"overall_score": 60}'
    result = engine._parse_json_response(response)
    assert result == {"overall_score": 60}


def test_parse_json_invalid_returns_empty():
    engine = MatchEngine(FakeDB())
    result = engine._parse_json_response("not json at all")
    assert result == {}


def test_parse_json_complex_response():
    engine = MatchEngine(FakeDB())
    response = '```json\n{"overall_score": 72, "experience_score": 65, "skill_score": 80, "industry_score": 70, "strengths": ["Python", "AWS"], "weaknesses": ["No Java"]}\n```'
    result = engine._parse_json_response(response)
    assert result["overall_score"] == 72
    assert result["experience_score"] == 65
    assert result["skill_score"] == 80
    assert result["industry_score"] == 70
    assert result["strengths"] == ["Python", "AWS"]
    assert result["weaknesses"] == ["No Java"]


def test_no_job_scoring_is_left_in_the_llm_match_engine():
    # Job scores come from the deterministic fit engine (Task 9.2, D-83); MatchEngine keeps only
    # the tailoring flow's analysis.
    for gone in ("compute_breakdown", "analyze_fit", "queue_analysis"):
        assert not hasattr(MatchEngine, gone), gone
    assert hasattr(MatchEngine, "analyze_job")
