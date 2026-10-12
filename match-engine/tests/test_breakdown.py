"""The explanation breakdown (SPEC §8, §7 naming, §17.2, §18; compliance.md §2; Task 4.1).

Expected impacts are closed-form arithmetic from SPEC §5.1 and §14.5 (EVAL §2.2): a gap's impact is
−(effective weight of its dimension × idf×wm / Σ idf×wm × (1 − m)).
"""

from __future__ import annotations

import ast
import re
import sys
from datetime import date
from pathlib import Path

import pytest

import matcher.explain
from matcher.calibrate import FixedCalibrator
from matcher.errors import FLAG_TEXT, Flag
from matcher.explain import breakdown
from matcher.explain.breakdown import LIMITED_INFORMATION, NEEDS_REVIEW_HEADLINE, build_explanation, headline
from matcher.explain.gaps import find_gaps
from matcher.explain.labels import join_labels, signed, skill_label, years
from matcher.graph.model import SkillGraph, SkillNode
from matcher.models import (
    Band,
    JobPosting,
    MatchResult,
    Necessity,
    Outcome,
    Resume,
    Role,
    ScoreInterval,
    ScoreOptions,
    SkillKind,
)
from matcher.score.engine import score
from matcher.score.prepare import prepare_resume
from tests.builders import AS_OF, bullet, close, listed, on, req, tiny_graph
from tests.explain_pairs import PAIRS

FIXED = FixedCalibrator()


def run(resume, posting, graph, config, **options) -> MatchResult:
    options.setdefault("strict", True)
    return score(resume, posting, as_of=AS_OF, config=config, graph=graph, calibration=FIXED,
                 options=ScoreOptions(**options))


def pair(name: str, graph, config, **options) -> MatchResult:
    resume, posting = PAIRS[name]()
    return run(resume, posting, graph, config, **options)


# ─── Labels ───────────────────────────────────────────────────────────────────────────────


def _graph_with(*nodes: SkillNode) -> SkillGraph:
    return SkillGraph({node.id: node for node in nodes}, {}, {}, "labels")


def test_skill_label_keeps_curated_casing_and_capitalises_esco_phrases() -> None:
    graph = _graph_with(
        SkillNode(id="skill.pandas", label="pandas", kind=SkillKind.TECHNOLOGY, source="curated"),
        SkillNode(id="esco.acc", label="accounting techniques", kind=SkillKind.DOMAIN, source="esco"),
        SkillNode(id="esco.ph", label="pH meters", kind=SkillKind.TOOL, source="esco"),
    )
    assert skill_label(graph, "skill.pandas") == "pandas"
    assert skill_label(graph, "esco.acc") == "Accounting techniques"
    assert skill_label(graph, "esco.ph") == "pH meters"


def test_skill_label_of_an_unknown_id_reads_its_own_spelling(small_graph) -> None:
    assert skill_label(small_graph, "skill.event_sourcing") == "Event sourcing"
    assert skill_label(small_graph, "unresolved:kafka streams") == "Kafka streams"
    assert skill_label(small_graph, "skill.aws") == "Amazon Web Services"


def test_small_text_helpers() -> None:
    assert join_labels(["A"]) == "A" and join_labels(["A", "B"]) == "A and B"
    assert join_labels(["A", "B", "C"]) == "A, B and C" and join_labels([]) == ""
    assert (signed(0.314), signed(-0.0751), signed(0.0)) == ("+0.31", "-0.08", "+0.00")
    assert years(42.0) == "3.5" and years(0.0) == "0.0"


# ─── Gaps: the impact arithmetic ─────────────────────────────────────────────────────────


IDF = {"skill.alpha": 0.8, "skill.beta": 0.4, "skill.gamma": 0.6}


def closed_form_pair():
    """Three REQUIRED skills, the résumé shows alpha only. Only §5.1 and §5.6 (no education
    requirement: 1.0, it never abstains) score, so §5.1's effective weight is 0.35 / 0.45."""
    graph = tiny_graph(IDF)
    resume = Resume(roles=(Role(start=on(date(2024, 1, 15)), is_current=True, skills=(bullet("skill.alpha"),)),))
    posting = JobPosting(requirements=tuple(req(skill_id) for skill_id in IDF))
    return graph, resume, posting


def test_gap_impacts_are_the_closed_form_shares(config) -> None:
    graph, resume, posting = closed_form_pair()
    result = run(resume, posting, graph, config, with_explanation=False)
    prepared = prepare_resume(resume, graph, config, AS_OF)
    gaps = find_gaps(result, posting, prepared, graph, config)
    w_required = 0.35 / (0.35 + 0.10)
    total_idf = 0.8 + 0.4 + 0.6
    expected = {"skill.gamma": -(w_required * 0.6 / total_idf), "skill.beta": -(w_required * 0.4 / total_idf)}
    assert [gap.requirement_id for gap in gaps] == ["skill.gamma", "skill.beta"]  # most negative first
    for gap in gaps:
        assert gap.necessity is Necessity.REQUIRED and gap.m == 0.0
        assert close(gap.impact, expected[gap.requirement_id])
    # The impacts sum to what §5.1 lost: effective weight × (1 − value).
    required = result.dimensions["required_skills"]
    assert close(sum(gap.impact for gap in gaps), -(required.effective_weight * (1.0 - required.value)))


def test_a_partial_gap_costs_its_share_times_one_minus_m(config) -> None:
    """delta is related (edge 0.6) to epsilon, listed in the skills section (E 0.65, R 1.0, nothing
    dates it): m = 0.6 × 0.65 = 0.39."""
    idf = {"skill.alpha": 0.8, "skill.beta": 0.4, "skill.delta": 0.5}
    graph = tiny_graph({**idf, "skill.epsilon": 0.3}, edges=[("skill.delta", "skill.epsilon", 0.6)])
    resume = Resume(roles=(Role(start=on(date(2024, 1, 15)), is_current=True, skills=(bullet("skill.alpha"),)),),
                    skills_blob=listed("skill.epsilon"))
    posting = JobPosting(requirements=tuple(req(skill_id) for skill_id in idf))
    result = run(resume, posting, graph, config, with_explanation=False)
    gaps = {gap.requirement_id: gap for gap in find_gaps(result, posting, prepare_resume(resume, graph, config, AS_OF), graph, config)}
    w_required, m = 0.35 / 0.45, 0.6 * 0.65
    assert close(gaps["skill.delta"].m, m) and gaps["skill.delta"].covered_by == ("skill.epsilon",)
    assert close(gaps["skill.delta"].impact, -(w_required * 0.5 / 1.7 * (1.0 - m)))
    assert close(gaps["skill.beta"].impact, -(w_required * 0.4 / 1.7))


def test_gap_items_show_the_signed_impacts_in_order(config) -> None:
    graph, resume, posting = closed_form_pair()
    result = run(resume, posting, graph, config)
    explanation = build_explanation(result, resume, posting, None, graph, config, strict=True)
    labels = [(item.label, item.impact) for item in explanation.gaps]
    if result.outcome is Outcome.SCORED:
        assert labels == [("Missing: skill.gamma (required)", "-0.26"), ("Missing: skill.beta (required)", "-0.17")]
    else:  # a suppressed score shows no raw contributions either
        assert labels == [("Missing: skill.gamma (required)", ""), ("Missing: skill.beta (required)", "")]


def test_a_partial_requirement_names_what_covers_it(small_graph, config) -> None:
    result = pair("good_match", small_graph, config)
    gap = next(item for item in result.explanation.gaps if "Terraform" in item.label)
    assert gap.label == "Partly covered: Terraform (required)"
    assert gap.note == "Your Amazon Web Services experience partially covers this"
    assert gap.impact.startswith("-")


def test_covered_by_lists_children_siblings_and_parents_never_the_requirement(small_graph, config) -> None:
    # Wants React; has Next.js (child and related), Vue (related) and JavaScript (an ancestor).
    resume = Resume(skills_blob=listed("skill.nextjs", "skill.vue", "skill.javascript", "skill.python"))
    posting = JobPosting(requirements=(req("skill.react"), req("skill.go")))
    result = run(resume, posting, small_graph, config, with_explanation=False)
    prepared = prepare_resume(resume, small_graph, config, AS_OF)
    gaps = {gap.requirement_id: gap for gap in find_gaps(result, posting, prepared, small_graph, config)}
    assert "skill.react" not in gaps  # Next.js covers React as a child: 0.85 × 0.65 ≥ 0.5
    assert gaps["skill.go"].covered_by == ()
    posting = JobPosting(requirements=(req("skill.js_framework"),))
    resume = Resume(skills_blob=listed("skill.javascript"))
    result = run(resume, posting, small_graph, config, with_explanation=False)
    gaps = find_gaps(result, posting, prepare_resume(resume, small_graph, config, AS_OF), small_graph, config)
    assert [gap.covered_by for gap in gaps] == [("skill.javascript",)]


# ─── Headline, score and basis ────────────────────────────────────────────────────────────


def test_scored_headline_score_and_basis(small_graph, config) -> None:
    result = pair("good_match", small_graph, config)
    explanation = result.explanation
    assert explanation.headline == f"Fit score {result.display_score} · Good match"
    assert (explanation.score, explanation.band) == (result.display_score, Band.GOOD)
    assert explanation.basis == config.bands.basis["test"].format(score=result.display_score)


def test_a_displayed_interval_replaces_the_point_in_the_headline(small_graph, config) -> None:
    result = pair("good_match", small_graph, config)
    shown = result.model_copy(update={"interval": ScoreInterval(low=71, high=79, display=True)})
    hidden = result.model_copy(update={"interval": ScoreInterval(low=74, high=78, display=False)})
    assert headline(shown, config) == "Fit score 71–79 · Good match"
    assert headline(hidden, config) == f"Fit score {result.display_score} · Good match"


def test_low_confidence_shows_the_band_and_says_why(small_graph, config) -> None:
    result = pair("low_confidence", small_graph, config)
    assert result.outcome is Outcome.SCORED_LOW_CONFIDENCE
    explanation = result.explanation
    assert explanation.headline == result.band.value and explanation.score is None and explanation.basis == ""
    assert explanation.notes[0] == LIMITED_INFORMATION
    for reason in result.reasons:
        assert FLAG_TEXT[reason] in explanation.notes
    assert all(item.impact == "" for item in (*explanation.strengths, *explanation.gaps))


def test_gate_fail_headline_names_the_gate(small_graph, config) -> None:
    result = pair("gate_fail_sponsorship", small_graph, config)
    assert result.outcome is Outcome.GATE_FAIL
    explanation = result.explanation
    assert explanation.headline == "Not eligible: " + FLAG_TEXT[Flag.SPONSORSHIP_UNAVAILABLE]
    assert explanation.score is None and explanation.band is None and explanation.basis == ""
    assert explanation.strengths == () and explanation.gaps == () and explanation.to_improve == ()
    assert breakdown.GATE_FACTS in explanation.notes


def test_needs_review_headline_and_one_sentence_per_reason(small_graph, config) -> None:
    result = pair("needs_review_flag", small_graph, config)
    assert result.explanation.headline == NEEDS_REVIEW_HEADLINE
    assert result.explanation.notes == (FLAG_TEXT[Flag.NOT_A_RESUME],)
    junk = score("not a résumé", b"{}", as_of=AS_OF, config=config, graph=small_graph, calibration=FIXED)
    assert junk.explanation.headline == NEEDS_REVIEW_HEADLINE
    assert junk.explanation.notes == (FLAG_TEXT[Flag.INVALID_INPUT],)


# ─── Strengths ────────────────────────────────────────────────────────────────────────────


def test_strengths_are_at_most_four_by_contribution(small_graph, config) -> None:
    result = pair("good_match", small_graph, config)
    strengths = result.explanation.strengths
    assert 1 <= len(strengths) <= 4
    impacts = [float(item.impact) for item in strengths]
    assert impacts == sorted(impacts, reverse=True)
    required = strengths[0]
    assert required.label == "4 of 5 required skills" and required.note == "Docker, Python, React and SQL"
    assert required.impact == signed(result.dimensions["required_skills"].contribution)
    assert "1.2 yrs relevant vs 1.0 required" in [item.label for item in strengths]


def test_the_required_line_is_kept_when_other_dimensions_outscore_it(small_graph, config) -> None:
    result = pair("critical_miss_binding", small_graph, config)
    labels = [item.label for item in result.explanation.strengths]
    assert "3 of 5 required skills" in labels and len(labels) <= 4


def test_typical_years_when_the_posting_states_none(small_graph, config) -> None:
    result = pair("low_confidence", small_graph, config)
    assert any(item.label.endswith("vs a typical 1.0") for item in result.explanation.strengths)


def test_shortfalls_become_gaps_ordered_by_impact(small_graph, config) -> None:
    result = pair("stretch_role", small_graph, config)
    labels = [item.label for item in result.explanation.gaps]
    assert "This role is 2 levels above your current one" in labels
    assert "This role asks for a master's degree" in labels
    assert any(re.fullmatch(r"\d+\.\d yrs relevant vs 5\.0 required", label) for label in labels)
    impacts = [float(item.impact) for item in result.explanation.gaps]
    assert impacts == sorted(impacts)


# ─── Notes ────────────────────────────────────────────────────────────────────────────────


def test_a_cap_is_named_only_when_it_bound(small_graph, config) -> None:
    capped = pair("critical_miss_binding", small_graph, config)
    assert capped.caps and "Missing Go and Terraform, skills this role centres on, limits this score." in capped.explanation.notes
    # Terraform is a critical miss below the cap here: its own gap, and never "limits".
    resume = Resume(roles=(Role(start=on(date(2024, 1, 15)), is_current=True, skills=(bullet("skill.python"),)),))
    posting = JobPosting(requirements=(req("skill.terraform"), req("skill.go"), req("skill.react"), req("skill.python")))
    result = run(resume, posting, small_graph, config)
    assert result.caps == () and any(f.code == "CRITICAL_MISS" for f in result.dimensions["required_skills"].facts)
    text = " ".join((*result.explanation.notes, *(item.note for item in result.explanation.gaps)))
    assert "limit" not in text and "cap" not in text


def test_overqualified_is_a_note_and_never_a_gap(small_graph, config) -> None:
    result = pair("overqualified", small_graph, config)
    assert Flag.OVERQUALIFIED in result.flags
    assert FLAG_TEXT[Flag.OVERQUALIFIED] in result.explanation.notes
    assert not any("level" in item.label.lower() for item in result.explanation.gaps)


def test_no_work_experience_reads_plainly(small_graph, config) -> None:
    result = pair("student_projects_only", small_graph, config)
    assert FLAG_TEXT[Flag.NO_WORK_EXPERIENCE] in result.explanation.notes
    assert "No work experience listed (typical for this level: 1.0 yrs)" in [i.label for i in result.explanation.gaps]


# ─── Totality ─────────────────────────────────────────────────────────────────────────────


def test_a_defect_falls_back_to_the_headline_unless_strict(small_graph, config, monkeypatch) -> None:
    resume, posting = PAIRS["good_match"]()
    result = run(resume, posting, small_graph, config, with_explanation=False)

    def broken(*args, **kwargs):
        raise RuntimeError("defect")

    monkeypatch.setattr(breakdown, "find_gaps", broken)
    with pytest.raises(RuntimeError):
        build_explanation(result, resume, posting, None, small_graph, config, strict=True)
    explanation = build_explanation(result, resume, posting, None, small_graph, config)
    assert explanation.headline == f"Fit score {result.display_score} · Good match"
    assert explanation.score == result.display_score and explanation.strengths == ()


# ─── The package is pure, like score/ (SPEC §1) ─────────────────────────────────────────────


_ALLOWED = ("matcher.models", "matcher.errors", "matcher.config", "matcher.graph", "matcher.score", "matcher.explain")
_IMPURE = {"os", "io", "pathlib", "shutil", "tempfile", "glob", "socket", "subprocess", "urllib", "http",
           "time", "random", "secrets", "sqlite3"}


def test_explain_package_imports_only_pure_modules_and_reads_no_clock() -> None:
    root = Path(matcher.explain.__file__).parent
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                func = node.func
                assert not (isinstance(func, ast.Attribute) and func.attr in {"today", "now", "utcnow"}), path.name
                assert not (isinstance(func, ast.Name) and func.id in {"open", "input", "print"}), path.name
                continue
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top == "matcher":
                    assert name.startswith(_ALLOWED), f"{path.name} imports {name}"
                else:
                    assert top == "pydantic" or top in sys.stdlib_module_names, f"{path.name} imports {name}"
                    assert top not in _IMPURE, f"{path.name} imports {name}"
