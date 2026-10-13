import pytest

from backend.schemas.ai import JobAnalysisOut
from backend.schemas.resume_document import ResumeDocument, Section, SectionItem
from backend.services.openai_service import TailorStructuredResult
import backend.services.resume_tailor as rt


def _analysis(score, missing):
    return JobAnalysisOut(overall_score=score, ats_score=score, match_label="Good match",
                          keyword_coverage=50, matched_keywords=["Python"], missing_keywords=[*missing, "Docker"],
                          suggested_keywords=missing, outcome="SCORED")


@pytest.mark.asyncio
async def test_tailor_document_threads_changes_gaps_figures(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")  # tailor.llm constructs the service

    original = ResumeDocument(sections=[
        Section(id="exp", type="experience", title="WORK EXPERIENCE",
                items=[SectionItem(id="i1", title="Engineer", subtitle="Acme", bullets=["Handled billing"])]),
    ])
    rewritten = ResumeDocument(sections=[
        Section(id="exp", type="experience", title="WORK EXPERIENCE",
                items=[SectionItem(id="i1", title="Engineer", subtitle="Acme", bullets=["Cut billing errors 30%"])]),
    ])
    scored: list[ResumeDocument] = []
    seen_keywords: list = []

    def fake_posting(self, *, job=None, job_title="", company="", job_description=""):
        assert (job_title, company, job_description) == ("Eng", "X", "AWS role")
        return "the posting"

    def fake_analyze(self, document, posting, *, user_id=None, raw_text=None, with_suggestions=False):
        assert posting == "the posting"  # before and after are scored against the SAME posting
        scored.append(document)
        return _analysis(40, ["AWS"]) if len(scored) == 1 else _analysis(55, [])

    async def fake_structured(self, doc, jd, sections=None, keywords=None):
        seen_keywords.append(keywords)
        return TailorStructuredResult(document=rewritten, gaps=["Role wants AWS; not shown"])

    monkeypatch.setattr("backend.services.match_engine.MatchEngine.posting", fake_posting)
    monkeypatch.setattr("backend.services.match_engine.MatchEngine.analyze", fake_analyze)
    monkeypatch.setattr("backend.services.openai_service.OpenAIService.tailor_resume_structured", fake_structured)

    result = await rt.tailor_document(db=None, original_document=original, job_title="Eng",
                                      company="X", job_description="AWS role")
    assert scored == [original, result.document]          # the original, then the rewrite
    assert seen_keywords == [["AWS"]]  # the rewrite is fed the engine's suggested gaps, not every chip
    assert (result.before.overall_score, result.after.overall_score) == (40, 55)
    assert result.gaps == ["Role wants AWS; not shown"]
    assert result.figures_to_verify == ["30%"]           # 30% not in source → flagged
    assert any("entr" in c.lower() or "bullet" in c.lower() for c in result.changes)


def test_score_fields_explain_a_number_that_went_away():
    """Fix round 2: the reasons are exactly the engine's sentences for the tailored result's reason
    codes (compliance.md section 2), whatever the before was. No sentence of our own is added:
    LOW_EVIDENCE_DENSITY already says "only listed" when that is the cause."""
    before = _analysis(48, ["AWS"])
    after = JobAnalysisOut(overall_score=None, ats_score=None, match_label="Partial match", keyword_coverage=60,
                           outcome="SCORED_LOW_CONFIDENCE",
                           reasons=["Several parts of the match couldn't be assessed."])
    doc = ResumeDocument()
    result = rt.TailorResult(document=doc, original_text="", tailored_text="", before=before, after=after,
                             diff_summary="", changes=[], gaps=[], figures_to_verify=[])
    fields = result.score_fields()
    assert fields["reasons"] == ["Several parts of the match couldn't be assessed."]
    assert not hasattr(rt, "LISTED_ONLY_SENTENCE")

    scored = rt.TailorResult(document=doc, original_text="", tailored_text="", before=before,
                             after=_analysis(55, []), diff_summary="", changes=[], gaps=[], figures_to_verify=[])
    assert scored.score_fields()["reasons"] == []
