"""structure-description: mini-model routing + description_sections caching.

No AI in job scoring or match signals (final review I-4): the structuring pass may lay out the
posting's headings and bullets, but it never produces a skill list, and a ``skills`` key in a
cached row (written while the old prompt asked for one) is never served."""

import json

import pytest

from backend.db.models import ScrapedJob


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    # The route builds OpenAIService, whose constructor refuses an empty key,
    # before the patched _generate runs. Tests run keyless (see conftest).
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    # The LLM parse is opt-in now; these tests pin its behaviour.
    monkeypatch.setenv("JOB_STRUCTURE_AI", "1")


def _mk_job(db_session, url="https://x.test/sd-1", description=None):
    row = ScrapedJob(
        title="EPM Consultant", company="Acme", url=url,
        location="Ottawa, ON, CA", country="CA", work_type="onsite",
        source_platform="ats", experience_level="new_grad", easy_apply=0,
        match_score=0,
        description=description or (
            "Responsibilities:\n- Support EPM applications\n- Build planning models\n"
            "Requirements:\n- 3-5 years experience\n- Excel proficiency\n"
        ),
    )
    db_session.add(row)
    db_session.commit()
    return row


STRUCT = {
    "sections": [{"title": "Responsibilities", "icon": "clipboard-list",
                  "items": ["Support EPM applications"]}],
    "skills": ["Excel", "EPM"],
    "experience_years": "3-5",
    "education": "",
}


def test_structure_calls_field_model_json_mode_and_caches(client, db_session, monkeypatch):
    job = _mk_job(db_session)
    seen = {}

    async def fake_generate(self, prompt, model=None, json_mode=False, **kwargs):
        seen["model"] = model
        seen["json_mode"] = json_mode
        seen["prompt"] = prompt
        return json.dumps(STRUCT)  # a model that still answers with skills

    monkeypatch.setattr(
        "backend.services.openai_service.OpenAIService._generate", fake_generate
    )
    res = client.post(f"/jobs/{job.id}/structure-description")
    assert res.status_code == 200, res.text
    assert (seen["model"], seen["json_mode"]) == ("gpt-6-luna", True)
    assert '"skills"' not in seen["prompt"] and "skill tags" not in seen["prompt"]
    assert "skills" not in res.json()
    assert res.json()["sections"][0]["title"] == "Responsibilities"
    db_session.refresh(job)
    assert "skills" not in job.description_sections


def test_structure_served_from_cache_without_llm(client, db_session, monkeypatch):
    job = _mk_job(db_session, url="https://x.test/sd-2")
    job.description_sections = STRUCT
    db_session.commit()

    async def boom(self, *args, **kwargs):
        raise AssertionError("LLM must not be called on cache hit")

    monkeypatch.setattr(
        "backend.services.openai_service.OpenAIService._generate", boom
    )
    res = client.post(f"/jobs/{job.id}/structure-description")
    assert res.status_code == 200, res.text
    assert res.json()["sections"][0]["title"] == "Responsibilities"
    assert "skills" not in res.json()  # a row cached with LLM skills never serves them


def test_structure_migrates_legacy_company_description_cache(client, db_session, monkeypatch):
    job = _mk_job(db_session, url="https://x.test/sd-3")
    job.company_description = json.dumps(STRUCT)
    db_session.commit()

    async def boom(self, *args, **kwargs):
        raise AssertionError("LLM must not be called on legacy cache hit")

    monkeypatch.setattr(
        "backend.services.openai_service.OpenAIService._generate", boom
    )
    res = client.post(f"/jobs/{job.id}/structure-description")
    assert res.status_code == 200, res.text
    assert "skills" not in res.json()
    db_session.refresh(job)
    assert job.description_sections["sections"][0]["title"] == "Responsibilities"
    assert "skills" not in job.description_sections


def test_structure_off_serves_no_skill_list(client, db_session, monkeypatch):
    monkeypatch.delenv("JOB_STRUCTURE_AI")
    job = _mk_job(db_session, url="https://x.test/sd-4")
    res = client.post(f"/jobs/{job.id}/structure-description")
    assert res.status_code == 200, res.text
    assert res.json()["sections"] == [] and "skills" not in res.json()
