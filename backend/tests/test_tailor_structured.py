import json

import httpx
import pytest

from backend.schemas.resume_document import ResumeDocument, Section, SectionItem
from backend.services.openai_service import OpenAIService, RewriteFailedError, TailorStructuredResult


def _doc():
    return ResumeDocument(sections=[
        Section(id="exp", type="experience", title="WORK EXPERIENCE",
                items=[SectionItem(id="i1", title="Engineer", subtitle="Acme", bullets=["Responsible for APIs"])]),
        Section(id="prj", type="projects", title="PROJECTS",
                items=[SectionItem(id="p1", title="Proj", bullets=["Built a thing"])]),
    ])


@pytest.mark.asyncio
async def test_parses_wrapped_contract_and_reorders(monkeypatch):
    svc = OpenAIService.__new__(OpenAIService)  # skip __init__ (no API key needed)
    payload = {
        "resume": {"header": {}, "sections": [
            {"id": "exp", "type": "experience", "title": "WORK EXPERIENCE",
             "items": [{"id": "i1", "title": "Engineer", "subtitle": "Acme", "bullets": ["Built and scaled REST APIs"]}]},
            {"id": "prj", "type": "projects", "title": "PROJECTS",
             "items": [{"id": "p1", "title": "Proj", "bullets": ["Built a thing"]}]},
        ]},
        "section_order": ["prj", "exp"],
        "new_summary": {"title": "PROFESSIONAL SUMMARY", "text": "Backend engineer."},
        "gaps": ["Role wants Kubernetes; not found in your experience"],
    }

    async def fake_generate(prompt, system=None, **kwargs):
        return json.dumps(payload)
    monkeypatch.setattr(svc, "_generate", fake_generate)

    out = await svc.tailor_resume_structured(_doc(), "Kubernetes backend role")
    assert isinstance(out, TailorStructuredResult)

    sections = out.document.sections
    assert sections[0].type == "summary"                       # summary prepended
    non_summary = [s.id for s in sections if s.type != "summary"]
    assert non_summary == ["prj", "exp"]                       # reordered by section_order

    by_id = {s.id: s for s in sections}
    assert by_id["exp"].items[0].bullets == ["Built and scaled REST APIs"]  # bullets reworded
    assert by_id["exp"].items[0].subtitle == "Acme"            # employer locked
    assert by_id["prj"].items[0].bullets == ["Built a thing"]  # unchanged
    assert out.gaps == ["Role wants Kubernetes; not found in your experience"]


@pytest.mark.asyncio
async def test_accepts_bare_document_for_back_compat(monkeypatch):
    svc = OpenAIService.__new__(OpenAIService)
    bare = {"header": {}, "sections": [
        {"id": "exp", "type": "experience", "title": "WORK EXPERIENCE",
         "items": [{"id": "i1", "title": "Engineer", "subtitle": "Acme", "bullets": ["Shipped the API"]}]},
        {"id": "prj", "type": "projects", "title": "PROJECTS",
         "items": [{"id": "p1", "title": "Proj", "bullets": ["Built a thing"]}]},
    ]}

    async def fake_generate(prompt, system=None, **kwargs):
        return json.dumps(bare)
    monkeypatch.setattr(svc, "_generate", fake_generate)

    out = await svc.tailor_resume_structured(_doc(), "jd")
    by_id = {s.id: s for s in out.document.sections}
    assert by_id["exp"].items[0].bullets == ["Shipped the API"]
    assert out.gaps == []


def _request():
    return httpx.Request("POST", "https://api.openai.com/v1/chat/completions")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "not json",                                             # bad JSON
    '{"resume": {"sections": "not a list"}}',               # a document the schema refuses
    httpx.ReadTimeout("stalled", request=_request()),       # timeout
    httpx.HTTPStatusError("400", request=_request(), response=httpx.Response(400, request=_request())),
    httpx.ConnectError("down", request=_request()),         # connection
    ValueError("unexpected response format"),               # _generate's own refusal
])
async def test_any_llm_failure_raises_instead_of_returning_the_original(monkeypatch, failure):
    """Task 9.3 fix round 1 (Important 1): a failed rewrite is never a 200 "rewrite" that is the
    original. Every failure is a RewriteFailedError, a ConnectionError, so the routes' existing
    503 handler answers it and no version is saved."""
    svc = OpenAIService.__new__(OpenAIService)

    async def fake_generate(prompt, system=None, **kwargs):
        if isinstance(failure, BaseException):
            raise failure
        return failure
    monkeypatch.setattr(svc, "_generate", fake_generate)

    with pytest.raises(RewriteFailedError) as caught:
        await svc.tailor_resume_structured(_doc(), "jd")
    assert isinstance(caught.value, ConnectionError)
