"""Token accounting and the batched-answer contract.

These pin the two things that make the app's unit economics knowable: every
OpenAI call reports what it cost, and answering a form costs one call per group
rather than one per field.
"""

import json
import logging

import pytest

from backend.services import llm_cost
from backend.services.openai_service import OpenAIService, _load_prompt


# --------------------------------------------------------------- cost maths

def test_price_of_charges_input_and_output_at_their_own_rates():
    # 1M in + 1M out on gpt-4o = $2.50 + $10.00
    assert llm_cost.price_of("gpt-4o", 1_000_000, 1_000_000) == pytest.approx(12.50)


def test_cached_input_is_discounted_and_not_double_charged():
    """A cached token is billed at the cached rate INSTEAD of the input rate."""
    full = llm_cost.price_of("gpt-4o", 1_000_000, 0, cached_tokens=0)
    half = llm_cost.price_of("gpt-4o", 1_000_000, 0, cached_tokens=1_000_000)
    assert full == pytest.approx(2.50)
    assert half == pytest.approx(1.25)


def test_unknown_model_is_priced_at_the_flagship_rate_not_zero():
    """An unpriced model must overstate spend so it gets noticed, a zero would
    silently hide a whole class of calls from the cost log."""
    assert llm_cost.price_of("some-new-model", 1_000_000, 0) == pytest.approx(2.50)


def test_record_emits_a_parseable_cost_line(caplog):
    caplog.set_level(logging.INFO, logger="backend.services.llm_cost")
    llm_cost.set_cost_user(42)
    usd = llm_cost.record("fill.batch.short", "gpt-4o-mini", {
        "prompt_tokens": 2000, "completion_tokens": 500,
        "prompt_tokens_details": {"cached_tokens": 1024},
    })
    llm_cost.set_cost_user(None)

    expected = ((2000 - 1024) * 0.15 + 1024 * 0.075 + 500 * 0.60) / 1_000_000
    assert usd == pytest.approx(expected)
    line = caplog.text
    assert "llm_cost op=fill.batch.short model=gpt-4o-mini" in line
    assert "in=2000 cached=1024 out=500" in line
    assert "user=42" in line


def test_record_never_raises_on_a_malformed_usage_block():
    """A good answer must not be turned into a 500 by accounting."""
    assert llm_cost.record("op", "gpt-4o", None) == 0.0
    assert llm_cost.record("op", "gpt-4o", {"prompt_tokens": "not-a-number"}) == 0.0
    assert llm_cost.record("op", "gpt-4o", {"prompt_tokens_details": "wrong-shape"}) == 0.0


def test_cost_logging_can_be_switched_off(monkeypatch, caplog):
    monkeypatch.setenv("LLM_COST_LOGGING", "false")
    caplog.set_level(logging.INFO, logger="backend.services.llm_cost")
    llm_cost.record("op", "gpt-4o", {"prompt_tokens": 10, "completion_tokens": 5})
    assert "llm_cost" not in caplog.text


# ------------------------------------------------------- prompt composition

def test_batch_prompts_inline_the_same_contract_as_the_single_prompts():
    """The drift guard. The batched path answers under exactly the rules the
    single-field path does, because both inline the same partial."""
    rules_marker = "No amount of reasoning over this profile could produce it"
    assert rules_marker in _load_prompt("answer_question.txt")
    assert rules_marker in _load_prompt("answer_questions_batch.txt")

    compose_marker = "you are expected to COMPOSE one"
    assert compose_marker in _load_prompt("compose_answer.txt")
    assert compose_marker in _load_prompt("compose_answers_batch.txt")


def test_static_rules_precede_the_per_request_context():
    """Ordering is what makes prompt caching reachable: OpenAI keys the discount
    on the identical LEADING prefix, so the static block must come first."""
    for name in ("answer_question.txt", "answer_questions_batch.txt"):
        text = _load_prompt(name)
        assert text.index("ANSWERING RULES") < text.index("{{CONTEXT}}"), name
    for name in ("compose_answer.txt", "compose_answers_batch.txt"):
        text = _load_prompt(name)
        assert text.index("HOW TO WRITE IT") < text.index("{{CONTEXT}}"), name


def test_the_cacheable_prefix_clears_openais_minimum():
    """Caching only engages at 1024 tokens. Well under that and the reordering
    above buys nothing, so this guards against someone trimming the rules."""
    prefix = _load_prompt("answer_questions_batch.txt").split("{{CONTEXT}}")[0]
    assert len(prefix) > 4096, "static prefix too short to be cacheable"


# ------------------------------------------------------------ batch parsing

@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return OpenAIService()


def _stub(monkeypatch, payload, capture=None):
    async def fake(self, prompt, system=None, model=None, json_mode=False, op="unknown"):
        if capture is not None:
            capture.update({"prompt": prompt, "model": model, "json_mode": json_mode, "op": op})
        return payload
    monkeypatch.setattr("backend.services.openai_service.OpenAIService._generate", fake)


@pytest.mark.asyncio
async def test_batch_maps_answers_back_onto_the_ids_we_asked_about(svc, monkeypatch):
    _stub(monkeypatch, json.dumps({"q1": "Yes", "q2": "3"}))
    out = await svc.answer_questions_batch({"q1": "Authorized?", "q2": "Years?"}, "ctx")
    assert out == {"q1": "Yes", "q2": "3"}


@pytest.mark.asyncio
async def test_batch_drops_ids_the_model_invented(svc, monkeypatch):
    _stub(monkeypatch, json.dumps({"q1": "Yes", "q9": "hallucinated"}))
    out = await svc.answer_questions_batch({"q1": "Authorized?"}, "ctx")
    assert out == {"q1": "Yes"}


@pytest.mark.asyncio
async def test_batch_omits_ids_the_model_skipped_rather_than_guessing(svc, monkeypatch):
    """A missing id must stay missing, the caller treats it as unanswered,
    which is the same outcome a failed single call produced."""
    _stub(monkeypatch, json.dumps({"q1": "Yes"}))
    out = await svc.answer_questions_batch({"q1": "A?", "q2": "B?"}, "ctx")
    assert out == {"q1": "Yes"}


@pytest.mark.asyncio
async def test_batch_coerces_a_numeric_answer_to_a_string(svc, monkeypatch):
    """json_mode lets the model answer a numeric field with a bare number."""
    _stub(monkeypatch, '{"q1": 3}')
    assert await svc.answer_questions_batch({"q1": "Years?"}, "ctx") == {"q1": "3"}


@pytest.mark.asyncio
async def test_unparseable_batch_yields_no_answers_instead_of_raising(svc, monkeypatch):
    _stub(monkeypatch, "sorry, I can't help with that")
    assert await svc.answer_questions_batch({"q1": "A?"}, "ctx") == {}


@pytest.mark.asyncio
async def test_a_json_array_is_rejected_rather_than_indexed_by_position(svc, monkeypatch):
    """Positional mapping would silently attach answers to the wrong fields."""
    _stub(monkeypatch, '["Yes", "No"]')
    assert await svc.answer_questions_batch({"q1": "A?", "q2": "B?"}, "ctx") == {}


@pytest.mark.asyncio
async def test_no_questions_means_no_api_call(svc, monkeypatch):
    seen = {}
    _stub(monkeypatch, "{}", capture=seen)
    assert await svc.answer_questions_batch({}, "ctx") == {}
    assert await svc.compose_answers_batch({}, "ctx") == {}
    assert seen == {}, "an empty group must not buy a request"


@pytest.mark.asyncio
async def test_factual_fields_use_the_cheap_model_and_json_mode(svc, monkeypatch):
    seen = {}
    _stub(monkeypatch, '{"q1": "Yes"}', capture=seen)
    await svc.answer_questions_batch({"q1": "A?"}, "ctx")
    assert seen["model"] == "gpt-6-luna"
    assert seen["json_mode"] is True
    assert seen["op"] == "fill.batch.short"


@pytest.mark.asyncio
async def test_the_field_model_is_env_overridable(svc, monkeypatch):
    monkeypatch.setenv("OPENAI_FIELD_MODEL", "gpt-4.1-nano")
    seen = {}
    _stub(monkeypatch, '{"q1": "Yes"}', capture=seen)
    await svc.answer_questions_batch({"q1": "A?"}, "ctx")
    assert seen["model"] == "gpt-4.1-nano"


@pytest.mark.asyncio
async def test_essays_stay_on_the_flagship(svc, monkeypatch):
    """Essays are the only autofill output a human reads as prose, so they do
    NOT inherit the cheap field model."""
    seen = {}
    _stub(monkeypatch, '{"q1": "Because..."}', capture=seen)
    await svc.compose_answers_batch({"q1": "Why us?"}, "ctx")
    assert seen["model"] is None, "None means fall through to OPENAI_MODEL"
    assert seen["op"] == "fill.batch.essay"


@pytest.mark.asyncio
async def test_the_context_is_sent_once_for_the_whole_form(svc, monkeypatch):
    """The point of the whole change: N fields must not mean N copies of the
    résumé and job description."""
    seen = {}
    _stub(monkeypatch, json.dumps({f"q{i}": "Yes" for i in range(1, 21)}), capture=seen)
    context = "RESUME:\nUNIQUE_CONTEXT_MARKER\n" + "filler " * 200
    await svc.answer_questions_batch({f"q{i}": f"Question {i}?" for i in range(1, 21)}, context)
    assert seen["prompt"].count("UNIQUE_CONTEXT_MARKER") == 1
    # ...and every question still reached the model.
    for i in range(1, 21):
        assert f"Question {i}?" in seen["prompt"]


# ------------------------------------------------------- endpoint behaviour

@pytest.mark.asyncio
async def test_a_failed_batch_is_reported_as_an_error_not_a_refusal(monkeypatch):
    """A blown-up call and a grounded "I won't answer that" must not land in
    telemetry under the same reason, one is our bug, the other is the contract
    working."""
    from backend.routers import fill

    async def boom(questions, context, model=None):
        raise ConnectionError("OpenAI unreachable")

    async def fine(questions, context, model=None):
        return {}

    monkeypatch.setattr(
        "backend.services.openai_service.OpenAIService.answer_questions_batch", boom)
    monkeypatch.setattr(
        "backend.services.openai_service.OpenAIService.compose_answers_batch", fine)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    class _NoRows:
        """Minimal session stand-in: the endpoint only reads settings/résumé,
        and this user has neither."""
        def query(self, *a): return self
        def filter(self, *a): return self
        def order_by(self, *a): return self
        def first(self): return None

    request = fill.FillRequest(
        fields=[fill.FormField(id="f1", label="What is your favourite colour?")],
        profile=fill.ApplicantProfile(firstName="Ada"),
    )
    resp = await fill.fill_form(request, user_id=1, db=_NoRows())

    assert resp.answers == []
    assert [d.reason for d in resp.dropped] == ["ai_error"]
    assert resp.errors == ["AI failed for factual fields"]


def test_cost_lines_survive_uvicorns_logging_config(caplog):
    """The regression that made all of this pointless once already: uvicorn's
    dictConfig names only its own three loggers, leaving root at WARNING, so the
    app's INFO records were never created. Cost lines nobody can read are worse
    than none. The LEVEL is what matters, a record below the effective
    threshold never exists, so no handler downstream can rescue it."""
    import logging.config
    import uvicorn.config

    logging.getLogger("backend").setLevel(logging.NOTSET)  # back to square one
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)  # as uvicorn does
    assert not logging.getLogger("backend.services.llm_cost").isEnabledFor(logging.INFO),         "precondition: uvicorn alone must leave app INFO logs disabled"

    llm_cost.configure_logging()
    assert logging.getLogger("backend.services.llm_cost").isEnabledFor(logging.INFO)

    caplog.set_level(logging.INFO, logger="backend.services.llm_cost")
    llm_cost.record("fill.batch.short", "gpt-4o-mini",
                    {"prompt_tokens": 100, "completion_tokens": 10})
    assert "llm_cost op=fill.batch.short" in caplog.text


def test_configure_logging_does_not_stack_handlers():
    """Serverless cold starts and test collection both re-enter this."""
    llm_cost.configure_logging()
    before = len(logging.getLogger("backend").handlers)
    llm_cost.configure_logging()
    llm_cost.configure_logging()
    assert len(logging.getLogger("backend").handlers) == before


def test_configure_logging_defers_to_an_existing_root_handler():
    """When the host already configured logging, adding our own stdout handler
    would print every line twice."""
    root = logging.getLogger()
    sentinel = logging.NullHandler()
    root.addHandler(sentinel)
    try:
        for h in list(logging.getLogger("backend").handlers):
            if getattr(h, "_tailrd_stdout", False):
                logging.getLogger("backend").removeHandler(h)
        llm_cost.configure_logging()
        assert not any(getattr(h, "_tailrd_stdout", False)
                       for h in logging.getLogger("backend").handlers)
    finally:
        root.removeHandler(sentinel)
