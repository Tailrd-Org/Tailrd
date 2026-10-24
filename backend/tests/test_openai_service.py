"""Transport tests for OpenAIService (POST /v1/chat/completions).

The other public methods are transport-agnostic (build a prompt -> _generate ->
parse) and are covered by the endpoint tests; here we pin down the OpenAI HTTP
contract: payload shape, system/user message mapping, response parsing, and the
missing-key guard. httpx is exercised for real via a MockTransport.
"""
import json

import httpx
import pytest
from unittest.mock import patch

from backend.services.openai_service import OpenAIService

# Capture the real client before any test patches httpx.AsyncClient, so the
# factory can still build a real client (backed by a mock transport) without
# recursing into the patch.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_client_factory(handler):
    """Build a drop-in for httpx.AsyncClient that routes through MockTransport."""
    def factory(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))
    return factory


@pytest.mark.asyncio
async def test_generate_builds_chat_payload_and_parses_content(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "Hello world"}}]},
        )

    with patch("httpx.AsyncClient", _mock_client_factory(handler)):
        svc = OpenAIService()
        out = await svc._generate("the prompt", system="be terse")

    assert out == "Hello world"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"
    assert captured["body"]["model"] == "gpt-4o"
    assert captured["body"]["messages"] == [
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "the prompt"},
    ]
    # A GPT-4 model takes no reasoning_effort (it answers that with a 400).
    assert "reasoning_effort" not in captured["body"]
    assert "max_tokens" not in captured["body"]


@pytest.mark.asyncio
async def test_generate_without_system_sends_only_user_message(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with patch("httpx.AsyncClient", _mock_client_factory(handler)):
        svc = OpenAIService()
        out = await svc._generate("just the prompt")

    assert out == "ok"
    assert captured["body"]["messages"] == [{"role": "user", "content": "just the prompt"}]


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError):
        OpenAIService()


@pytest.mark.asyncio
async def test_generate_model_override_and_json_mode(monkeypatch):
    """Per-call model routing: cheap high-volume calls must not be forced onto
    the account-wide flagship, and json_mode must request JSON output."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with patch("httpx.AsyncClient", _mock_client_factory(handler)):
        svc = OpenAIService()
        await svc._generate("score this as JSON", model="gpt-4o-mini", json_mode=True)

    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_generate_defaults_do_not_send_response_format(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with patch("httpx.AsyncClient", _mock_client_factory(handler)):
        svc = OpenAIService()
        await svc._generate("plain prose call")

    assert "response_format" not in captured["body"]


@pytest.mark.asyncio
async def test_default_model_is_gpt_6_luna_answering_without_reasoning(monkeypatch):
    """gpt-4o and gpt-4o-mini shut down on 2027-01-14. Their replacement answers
    max_tokens with a 400 and, left alone, reasons at "medium" effort before every
    reply, so the default request caps max_completion_tokens and asks for none."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MAX_TOKENS", raising=False)
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with patch("httpx.AsyncClient", _mock_client_factory(handler)):
        svc = OpenAIService()
        await svc._generate("plain prose call")

    assert captured["body"]["model"] == "gpt-6-luna"
    assert captured["body"]["reasoning_effort"] == "none"
    assert captured["body"]["max_completion_tokens"] == 4096
    assert "max_tokens" not in captured["body"]
