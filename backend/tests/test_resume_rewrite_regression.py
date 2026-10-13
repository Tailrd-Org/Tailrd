"""Golden baseline: a real rewrite (structure changed), not a keyword append.

Future prompt/pipeline changes are compared against these assertions, if a
change turns the rewriter back into a keyword-patcher (no reorder, no summary,
duty-phrasing left intact, fabrications unflagged), this test fails.
"""
import pytest

from backend.schemas.ai import JobAnalysisOut
from backend.schemas.resume_document import ResumeDocument, Section, SectionItem
from backend.services.openai_service import TailorStructuredResult
from backend.services.resume_document import merge_rewrite
import backend.services.resume_tailor as rt


ORIGINAL = ResumeDocument(sections=[
    Section(id="exp", type="experience", title="WORK EXPERIENCE", items=[
        SectionItem(id="i1", title="Software Engineer", subtitle="Acme",
                    bullets=["Responsible for maintaining the billing service",
                             "Worked on internal tools"])]),
    Section(id="prj", type="projects", title="PROJECTS", items=[
        SectionItem(id="p1", title="Realtime Dashboard",
                    bullets=["Built a dashboard with websockets for 500 users"])]),
])

# A structure-aware rewrite: projects lifted above experience, a summary added,
# duty-phrasing turned into impact, one fabricated metric ("60%") slipped in.
REWRITE = TailorStructuredResult(
    document=merge_rewrite(
        ORIGINAL,
        ResumeDocument(sections=[
            Section(id="exp", type="experience", title="WORK EXPERIENCE", items=[
                SectionItem(id="i1", title="Software Engineer", subtitle="Acme",
                            bullets=["Owned the billing service, cutting incidents 60%",
                                     "Shipped internal tooling adopted org-wide"])]),
            Section(id="prj", type="projects", title="PROJECTS", items=[
                SectionItem(id="p1", title="Realtime Dashboard",
                            bullets=["Built a websocket dashboard serving 500 users"])]),
        ]),
        section_order=["prj", "exp"],
        new_summary={"title": "PROFESSIONAL SUMMARY", "text": "Backend engineer focused on reliability."},
    ),
    gaps=["Role requires Kubernetes; no evidence in your experience"],
)


@pytest.mark.asyncio
async def test_pipeline_reorders_adds_summary_rewrites_and_flags_fabrication(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fake_posting(self, *, job=None, job_title="", company="", job_description=""):
        return None

    def fake_analyze(self, document, posting, *, user_id=None, raw_text=None, with_suggestions=False):
        return JobAnalysisOut(overall_score=72, ats_score=72, match_label="Good match",
                              keyword_coverage=50, matched_keywords=["Python"], missing_keywords=["Kubernetes"],
                              outcome="SCORED")

    async def fake_structured(self, doc, jd, sections=None, keywords=None):
        return REWRITE

    monkeypatch.setattr("backend.services.match_engine.MatchEngine.posting", fake_posting)
    monkeypatch.setattr("backend.services.match_engine.MatchEngine.analyze", fake_analyze)
    monkeypatch.setattr("backend.services.openai_service.OpenAIService.tailor_resume_structured", fake_structured)

    result = await rt.tailor_document(None, ORIGINAL, "Backend Engineer", "Globex",
                                      "Kubernetes-heavy backend role")

    types = [s.type for s in result.document.sections]
    assert types[0] == "summary"                                 # summary added, first
    assert types.index("projects") < types.index("experience")  # projects lifted
    assert result.gaps == ["Role requires Kubernetes; no evidence in your experience"]
    assert "60%" in result.figures_to_verify                     # fabricated metric flagged
    assert any("reorder" in c.lower() for c in result.changes)
    assert any("summary" in c.lower() for c in result.changes)
    # It is a rewrite, not an append: the duty phrasing is gone.
    all_bullets = [b for s in result.document.sections for it in s.items for b in it.bullets]
    assert not any(b.lower().startswith("responsible for") for b in all_bullets)
