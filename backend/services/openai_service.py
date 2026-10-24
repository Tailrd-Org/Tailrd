"""
OpenAIService, OpenAI Chat Completions client for AI-powered features.

Drop-in replacement for the former AnthropicService: same public interface
(analyze_resume, generate_cover_letter, answer_question, suggest_job_titles,
tailor_resume*, edit_snippet, generate_connection_message,
analyze_resume_quality) so every caller
that goes through get_llm_service() works unchanged. Only the transport
(_generate) and configuration (__init__) are OpenAI-specific; all prompt
building and parsing is provider-agnostic.

No job scoring happens here: ``match_job`` and ``extract_experience_years`` were removed with every
AI scoring path (DECISIONS D-83); job scores come from the deterministic fit engine.
"""

import os
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from backend.schemas.resume import AnalysisReport, ResumeProfile
from backend.schemas.resume_document import ResumeDocument
from backend.schemas.application import JobPosting
from backend.services.llm_cost import record as record_cost
from backend.services.resume_extraction import build_analysis_report, build_profile

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"

# OpenAI shuts gpt-4o and gpt-4o-mini down on 2027-01-14 and names gpt-6-luna as
# the replacement for both. It also answers on a Free-tier account, where both
# GPT-4o models refuse with credit_balance_exhausted (checked 2026-10-24).
DEFAULT_MODEL = "gpt-6-luna"

# Answering an application field is extraction from a context we supply, not
# open-ended reasoning. It shares the default model today but keeps its own
# override (OPENAI_FIELD_MODEL), so the two can be split again by env alone.
DEFAULT_FIELD_MODEL = DEFAULT_MODEL

# gpt-6-luna reasons before it answers unless told not to (its default effort is
# "medium"): hidden tokens that add latency, bill as output and eat into
# max_completion_tokens. "none" makes it answer straight away, like the GPT-4
# models the prompts were written for. Only these families accept "none": the
# GPT-4 models reject the parameter and GPT-6 Astra and GPT-6.1 Sol reject the
# value, so an env override onto any of those sends no effort at all.
_NO_REASONING_MODELS = ("gpt-6-luna", "gpt-6-sol")


def _field_model() -> str:
    return os.getenv("OPENAI_FIELD_MODEL", DEFAULT_FIELD_MODEL).strip().strip("﻿") or DEFAULT_FIELD_MODEL


# Substrings that mark a 429 as an ACCOUNT problem rather than a traffic one:
# out of credit, unpaid, deactivated, over a configured ceiling. Waiting cannot
# fix any of them, so retrying is pure latency on a request a user is watching.
#
# Matched as substrings, not an exact allowlist, because OpenAI uses several
# names from the same family and the exact one is not documented per case. The
# real 2026-08-12 outage returned `billing_not_active` ("Your account is not
# active, please check your billing details"), which an allowlist of the three
# codes one would think to guess did not contain.
_TERMINAL_429_MARKERS = ("billing", "quota", "deactivat", "suspend", "expired")


class LLMAccountError(ConnectionError):
    """The OpenAI ACCOUNT refused the call: billing inactive, quota spent, or a
    revoked key. Nothing clears it but a human fixing the account, so a batch
    caller (the match sweep) should stop instead of making the same doomed call
    for every remaining item: from 2026-08-12 the sweep made up to 75 refused
    calls a run and still reported "completed".

    Subclasses ConnectionError so every existing `except ConnectionError` ->
    503 handler keeps working unchanged."""


def _is_terminal_429(code: str) -> bool:
    """True when a 429 will never clear on its own.

    Unknown/blank codes are treated as transient, so an undocumented burst limit
    still gets its retries; only a recognizably account-level failure fails fast.
    """
    c = (code or "").lower()
    return any(marker in c for marker in _TERMINAL_429_MARKERS)


def _openai_error_code(response: httpx.Response) -> str:
    """OpenAI's machine-readable error code, or "" when the body is unreadable.

    The body is `{"error": {"code": "insufficient_quota", "type": ...}}`; `code`
    is preferred and `type` is the fallback, since some errors set only one.
    """
    try:
        err = response.json().get("error") or {}
    except Exception:
        return ""
    return str(err.get("code") or err.get("type") or "")


def _retry_after(response: httpx.Response) -> float | None:
    """The `Retry-After` header in seconds, when it is present and sane.

    The API knows when its own window reopens; a blind doubling does not.
    Capped at 30s so a long header value cannot stall a user-facing request.
    """
    raw = response.headers.get("retry-after", "").strip()
    if not raw:
        return None
    try:
        return min(max(float(raw), 0.0), 30.0)
    except ValueError:
        return None

STANDARDS_TOKEN = "{{RESUME_STANDARDS}}"
STANDARDS_FILE = "_standards.md"

# Shared partials, by the token that pulls them in. Each is a single canonical
# copy of a contract used by more than one prompt, so a prompt that answers one
# field and a prompt that answers a whole form cannot drift apart.
PARTIALS: dict[str, str] = {
    STANDARDS_TOKEN: STANDARDS_FILE,
    "{{ANSWER_RULES}}": "_answer_rules.md",
    "{{COMPOSE_RULES}}": "_compose_rules.md",
}


def _load_partial(filename: str) -> str:
    """A shared prompt partial, minus its developer header comment.

    Everything before the partial's first ``##`` heading is a note to us, not to the
    model, so it is stripped before the block reaches a prompt.
    """
    text = (PROMPTS_DIR / filename).read_text(encoding="utf-8")
    _, sep, body = text.partition("\n## ")
    return f"## {body}".strip() if sep else text.strip()


def _load_standards() -> str:
    """Back-compat alias for the résumé-standards partial."""
    return _load_partial(STANDARDS_FILE)


def _load_prompt(name: str) -> str:
    """Read a prompt template, inlining any shared partials it asks for.

    Partials are placed ahead of the per-request content in every template that
    uses one. That is not cosmetic: OpenAI's automatic prompt caching keys on the
    identical *leading* prefix, so static-first ordering is what makes the
    discount reachable at all.
    """
    path = PROMPTS_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    template = path.read_text(encoding="utf-8")
    for token, filename in PARTIALS.items():
        if token in template:
            template = template.replace(token, _load_partial(filename))
    return template


def _extract_json(response: str) -> str:
    """Extract JSON from a response that may contain markdown fences or preamble."""
    text = response.strip()
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("{") or part.startswith("["):
                return part
    if not text.startswith("{") and not text.startswith("["):
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return text[start:end + 1]
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            return text[start:end + 1]
    return text


class RewriteFailedError(ConnectionError):
    """The structured rewrite produced nothing usable: the LLM call failed (connection, timeout,
    HTTP error, an unexpected response) or its reply was not a résumé document.

    A ConnectionError on purpose: the tailoring routes already answer ConnectionError with a 503
    (LLM_503_DETAIL), and a failed rewrite must never be a 200 that returns, or saves, the
    original as if it were the rewrite (Task 9.3 fix round 1)."""


@dataclass
class TailorStructuredResult:
    """Output of a structure-aware rewrite: merged document + honest gaps."""

    document: ResumeDocument
    gaps: list[str]


@dataclass
class ImproveStructuredResult:
    """Output of a job-agnostic improvement pass: merged document + the metrics
    the candidate still has to supply (the model is forbidden from guessing)."""

    document: ResumeDocument
    unresolved: list[str]


def _render_emphasis(sections: list[str] | None, keywords: list[str] | None) -> str:
    """The per-request focus block shared by both tailoring prompts.

    ``sections`` is what the user asked us to work on; ``keywords`` are the job's missing
    terms. Both are advisory, the honesty rule in the standards block still overrules them,
    which is why an unsupported term is dropped rather than inserted.
    """
    lines: list[str] = []
    if sections:
        lines.append(f"- Put extra effort into these sections: {', '.join(sections)}.")
    if keywords:
        lines.append(
            "- Where the candidate's REAL experience already supports them, use the job's own "
            f"words for these terms: {', '.join(keywords)}. If a term is not genuinely "
            "supported, leave it out."
        )
    return ("## THIS REQUEST\n\n" + "\n".join(lines)) if lines else ""


def _render_findings(report: AnalysisReport | None) -> str:
    """Flatten an analysis report into the instruction block for improve_resume."""
    if report is None or not report.categories:
        return (
            "(no analysis available, apply general best practice: achievement-led "
            "bullets, strong verbs, no filler, consistent tense)"
        )

    lines: list[str] = []
    for category in report.categories:
        if not category.issues:
            continue
        lines.append(f"## {category.name}")
        for issue in category.issues:
            where = f" in {issue.section}" if issue.section else ""
            lines.append(
                f"- [{issue.severity.upper()}] {issue.title}{where} "
                f"({issue.count} occurrence(s)): {issue.description}"
            )
            for snippet in issue.evidence:
                lines.append(f'    evidence: "{snippet}"')
            if issue.suggestion:
                lines.append(f"    fix: {issue.suggestion}")
    return "\n".join(lines) or "(no issues found)"


class OpenAIService:
    """Async client for the OpenAI Chat Completions API."""

    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY", "").strip().strip("﻿")
        self.model = os.getenv("OPENAI_MODEL", DEFAULT_MODEL).strip().strip("﻿")
        self.timeout = float(os.getenv("OPENAI_TIMEOUT", "60"))
        self.max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "4096"))
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not set in environment")

    async def _generate(
        self,
        prompt: str,
        system: str = None,
        model: str = None,
        json_mode: bool = False,
        op: str = "unknown",
    ) -> str:
        """Run one chat completion.

        ``model`` overrides the account-wide default for calls that don't need
        the flagship (e.g. high-volume match scoring). ``json_mode`` turns on
        OpenAI's JSON response format for callers that parse the output as
        JSON, the prompt must still mention JSON, per the API contract.
        ``op`` labels the call in the cost log so spend can be attributed to a
        user-facing action rather than to "OpenAI" in aggregate.
        """
        import asyncio

        url = "https://api.openai.com/v1/chat/completions"

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict = {
            "model": model or self.model,
            # Not max_tokens: gpt-6-luna answers that with a 400
            # unsupported_parameter, and the GPT-4 models accept either.
            "max_completion_tokens": self.max_tokens,
            "messages": messages,
        }
        if body["model"].startswith(_NO_REASONING_MODELS):
            body["reasoning_effort"] = "none"
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        max_retries = 4
        for attempt in range(max_retries):
            async with httpx.AsyncClient() as client:
                r = await client.post(url, json=body, headers=headers, timeout=self.timeout)
                if r.status_code == 429:
                    code = _openai_error_code(r)
                    # 429 covers two conditions that want opposite handling. A
                    # spent quota / hard billing limit will NEVER clear by
                    # waiting, so retrying it burns 45s (3+6+12+24) of a
                    # user-facing request and fails anyway, which is exactly
                    # what blanked 12 fields of a real Lyft application on
                    # 2026-08-12 while the log said only "rate limited".
                    if _is_terminal_429(code):
                        logger.error(
                            "OpenAI refused the call (429 %s) for op=%s, this is an "
                            "ACCOUNT/BILLING problem and will not clear by retrying",
                            code, op,
                        )
                        raise LLMAccountError(
                            f"OpenAI rejected the request ({code}). This is an account "
                            "billing/quota problem, not a transient rate limit."
                        )
                    # A genuine rate limit: obey Retry-After when the API sends
                    # one, since it knows when the window actually reopens.
                    wait_time = _retry_after(r) or (2 ** attempt) * 3
                    logger.warning(
                        f"OpenAI rate limited (429 {code or 'no-code'}), retrying in "
                        f"{wait_time}s (attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                if r.status_code in (500, 502, 503):
                    wait_time = (2 ** attempt) * 5
                    logger.warning(
                        f"OpenAI server error ({r.status_code}), retrying in {wait_time}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(wait_time)
                    continue
                # A revoked or mistyped key is the same class of failure as an
                # unpaid account: no retry fixes it. It used to fall through to
                # raise_for_status, an HTTPStatusError no handler catches (a 500).
                if r.status_code == 401:
                    code = _openai_error_code(r) or "401"
                    logger.error("OpenAI rejected the API key (%s) for op=%s", code, op)
                    raise LLMAccountError(f"OpenAI rejected the API key ({code}).")
                r.raise_for_status()
                data = r.json()
                break
        else:
            raise ConnectionError(
                "OpenAI API rate limited after retries. Please try again in a minute."
            )

        record_cost(op, body["model"], data.get("usage"))

        try:
            # OpenAI response: { "choices": [{ "message": { "content": "..." } }] }
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            logger.error("Unexpected OpenAI response: %s", json.dumps(data)[:500])
            raise ValueError("OpenAI returned an unexpected response format")

    async def analyze_resume(self, raw_text: str) -> ResumeProfile:
        template = _load_prompt("analyze_resume.txt")
        prompt = template.replace("{{RESUME_TEXT}}", raw_text)
        response = await self._generate(prompt, op="resume.analyze")
        data = json.loads(_extract_json(response))
        return build_profile(data)

    async def analyze_resume_quality(
        self, raw_text: str, metrics_digest: str = ""
    ) -> AnalysisReport:
        template = _load_prompt("analyze_resume_quality.txt")
        prompt = (
            template
            .replace("{{METRICS}}", metrics_digest or "(not available)")
            .replace("{{RESUME_TEXT}}", raw_text)
        )
        response = await self._generate(prompt, op="resume.quality")
        data = json.loads(_extract_json(response))
        return build_analysis_report(data)

    async def improve_resume_structured(
        self, document: ResumeDocument, report: AnalysisReport | None
    ) -> "ImproveStructuredResult":
        """Apply an analysis report's findings to a document, facts locked.

        The model rewrites only wording; ``merge_rewrite`` re-imposes every
        factual field from the original, so a bad rewrite can degrade the prose
        but can never invent an employer, a date, or a degree.
        """
        from backend.services.resume_document import merge_rewrite

        prompt = (
            _load_prompt("improve_resume.txt")
            .replace("{{FINDINGS}}", _render_findings(report))
            .replace("{{RESUME_JSON}}", document.model_dump_json())
        )

        try:
            response = await self._generate(prompt, op="resume.improve")
            data = json.loads(_extract_json(response))
        except Exception as e:  # noqa: BLE001
            logger.warning("Resume improve failed to parse (%s); returning original", e)
            return ImproveStructuredResult(document=document, unresolved=[])

        if not isinstance(data, dict):
            return ImproveStructuredResult(document=document, unresolved=[])

        resume_data = data.get("resume")
        section_order = data.get("section_order")
        new_summary = data.get("new_summary")
        unresolved_raw = data.get("unresolved", [])

        unresolved = (
            [str(u).strip() for u in unresolved_raw if str(u).strip()]
            if isinstance(unresolved_raw, list)
            else []
        )
        if not isinstance(section_order, list):
            section_order = None
        if not isinstance(new_summary, dict):
            new_summary = None

        try:
            edited = ResumeDocument(**resume_data)
        except Exception as e:  # noqa: BLE001
            logger.warning("Resume improve bad shape (%s); returning original", e)
            return ImproveStructuredResult(document=document, unresolved=unresolved)

        merged = merge_rewrite(
            document, edited, section_order=section_order, new_summary=new_summary
        )
        return ImproveStructuredResult(document=merged, unresolved=unresolved)

    async def generate_cover_letter(self, profile: ResumeProfile, job: JobPosting) -> str:
        template = _load_prompt("cover_letter.txt")
        prompt = (
            template
            .replace("{{RESUME_JSON}}", profile.model_dump_json(indent=2))
            .replace("{{JOB_TITLE}}", job.title)
            .replace("{{JOB_COMPANY}}", job.company)
            .replace("{{JOB_DESCRIPTION}}", job.description)
        )
        return await self._generate(prompt, op="cover_letter")

    ANSWER_SYSTEM = (
        "You are a job applicant filling out an application form. "
        "You write in first person. You give direct answers only. "
        "Reason over the profile you are given rather than leaving a question "
        "blank, but never state a fact the profile does not support. "
        "Never start with conversational phrases. Never explain yourself. "
        "Just answer the question."
    )

    COMPOSE_SYSTEM = (
        "You are a job applicant writing a short, genuine answer to an "
        "open-ended application question in first person. Ground it in the "
        "applicant's real experience and the job posting. Output only the answer."
    )

    async def answer_question(self, question: str, context: str) -> str:
        template = _load_prompt("answer_question.txt")
        prompt = template.replace("{{QUESTION}}", question).replace("{{CONTEXT}}", context)
        return await self._generate(prompt, system=self.ANSWER_SYSTEM, op="fill.answer")

    @staticmethod
    def _render_questions(questions: dict[str, str]) -> str:
        """The numbered question block shared by both batch prompts."""
        return "\n\n".join(f"[{qid}]\n{text}" for qid, text in questions.items())

    def _parse_batch(self, response: str, questions: dict[str, str]) -> dict[str, str]:
        """Map the model's JSON object back onto the ids we asked about.

        Anything the model omitted, nested, or invented is dropped rather than
        guessed at, a missing id simply leaves that field for the caller to
        treat as unanswered, which is the same outcome a failed single call had.
        """
        try:
            data = json.loads(_extract_json(response))
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning("Batch answer failed to parse (%s): %s", e, response[:300])
            return {}
        if not isinstance(data, dict):
            logger.warning("Batch answer was not a JSON object: %s", response[:200])
            return {}
        out: dict[str, str] = {}
        for qid in questions:
            value = data.get(qid)
            if isinstance(value, (int, float)):
                value = str(value)
            if isinstance(value, str) and value.strip():
                out[qid] = value.strip()
        missing = set(questions) - set(out)
        if missing:
            logger.info("Batch answer omitted %d/%d ids: %s",
                        len(missing), len(questions), sorted(missing)[:10])
        return out

    async def answer_questions_batch(
        self, questions: dict[str, str], context: str, model: str | None = None
    ) -> dict[str, str]:
        """Answer every factual field on one form in a single call.

        The per-field loop this replaces re-sent the whole applicant context,
        profile, résumé, job description, once per field, so a 22-field Workday
        form paid for that context 22 times to produce ~20 tokens of answer each.
        Sending the form as one request drops that to once, and lets the model
        see the form as a coherent whole rather than 22 blind questions.

        Returns ``{question_id: answer}``; ids the model did not answer are
        absent, and the caller decides what an unanswered field means.
        """
        if not questions:
            return {}
        prompt = (
            _load_prompt("answer_questions_batch.txt")
            .replace("{{CONTEXT}}", context)
            .replace("{{QUESTIONS}}", self._render_questions(questions))
        )
        response = await self._generate(
            prompt,
            system=self.ANSWER_SYSTEM,
            model=model or _field_model(),
            json_mode=True,
            op="fill.batch.short",
        )
        return self._parse_batch(response, questions)

    async def compose_answers_batch(
        self, questions: dict[str, str], context: str, model: str | None = None
    ) -> dict[str, str]:
        """Compose every open-ended essay field on one form in a single call.

        Kept separate from ``answer_questions_batch`` because the two obey
        different contracts (an essay may compose prose that is not stated
        verbatim; a factual field may not) and run on different models, essays
        are the only autofill output a human reads as prose, so they stay on the
        flagship while factual extraction does not.
        """
        if not questions:
            return {}
        prompt = (
            _load_prompt("compose_answers_batch.txt")
            .replace("{{CONTEXT}}", context)
            .replace("{{QUESTIONS}}", self._render_questions(questions))
        )
        response = await self._generate(
            prompt,
            system=self.COMPOSE_SYSTEM,
            model=model,
            json_mode=True,
            op="fill.batch.essay",
        )
        return self._parse_batch(response, questions)

    async def compose_answer(self, question: str, context: str) -> str:
        """Compose a grounded answer to an open-ended essay question (why-us,
        behavioral, self-intro, company-knowledge). Unlike answer_question, this
        is allowed to write prose that is not stated verbatim in the context,
        but it still may not invent hard facts (see prompts/compose_answer.txt)."""
        template = _load_prompt("compose_answer.txt")
        prompt = template.replace("{{QUESTION}}", question).replace("{{CONTEXT}}", context)
        return await self._generate(prompt, system=self.COMPOSE_SYSTEM, op="fill.compose")

    async def suggest_job_titles(self, profile: ResumeProfile) -> list[str]:
        template = _load_prompt("suggest_titles.txt")
        prompt = template.replace("{{RESUME_JSON}}", profile.model_dump_json(indent=2))
        response = await self._generate(prompt, op="suggest_titles")
        return json.loads(_extract_json(response))

    async def tailor_resume(self, resume_text: str, job_description: str) -> str:
        template = _load_prompt("tailor_resume.txt")
        prompt = (
            template
            .replace("{{RESUME_TEXT}}", resume_text[:4000])
            .replace("{{JOB_DESCRIPTION}}", job_description[:3000])
        )
        return await self._generate(prompt, op="resume.tailor")

    async def tailor_resume_guided(
        self,
        resume_text: str,
        job_description: str,
        sections: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> str:
        """Rewrite the candidate's COMPLETE resume, tailored to the target job."""
        prompt = (
            _load_prompt("tailor_resume_guided.txt")
            .replace("{{FOCUS}}", _render_emphasis(sections, keywords))
            .replace("{{RESUME_TEXT}}", resume_text[:6000])
            .replace("{{JOB_DESCRIPTION}}", job_description[:3000])
        )
        return await self._generate(prompt, op="resume.tailor_guided")

    async def tailor_resume_structured(
        self,
        document: ResumeDocument,
        job_description: str,
        sections: list[str] | None = None,
        keywords: list[str] | None = None,
    ) -> "TailorStructuredResult":
        """Structure-aware rewrite. Returns the merged document + honest gaps.

        The model does a genuine rewrite (structure review, achievement-based
        bullets, holistic alignment) and returns a wrapped object
        ``{resume, section_order, new_summary, gaps}``. ``merge_rewrite`` then
        applies the reorder/summary while keeping every factual field locked. A
        bare résumé document is still accepted for back-compat.
        """
        from backend.services.resume_document import merge_rewrite

        prompt = (
            _load_prompt("tailor_resume_structured.txt")
            .replace("{{EMPHASIS}}", _render_emphasis(sections, keywords))
            .replace("{{JOB_DESCRIPTION}}", job_description[:3500])
            .replace("{{RESUME_JSON}}", document.model_dump_json())
        )

        # Every failure is a RewriteFailedError (a ConnectionError, so the routes' 503 handler
        # answers it): never a 200 "rewrite" that is the original, and never a saved version
        # (Task 9.3 fix round 1). Before Task 9.3 the LLM analysis ran first and raised instead.
        try:
            response = await self._generate(prompt, op="resume.tailor_structured")
        except RewriteFailedError:
            raise
        except (ConnectionError, httpx.HTTPError, ValueError) as e:
            logger.warning("Structured tailor: the LLM call failed (%s)", type(e).__name__)
            raise RewriteFailedError("the rewrite call failed") from e
        try:
            data = json.loads(_extract_json(response))
        except (ValueError, TypeError) as e:  # json.JSONDecodeError is a ValueError
            logger.warning("Structured tailor: unparseable reply (%s)", type(e).__name__)
            raise RewriteFailedError("the rewrite reply was not JSON") from e

        if isinstance(data, dict) and "resume" in data:
            resume_data = data.get("resume")
            section_order = data.get("section_order")
            new_summary = data.get("new_summary")
            gaps_raw = data.get("gaps", [])
        else:
            # Back-compat: a bare résumé document (no wrapper).
            resume_data, section_order, new_summary, gaps_raw = data, None, None, []

        gaps = (
            [str(g).strip() for g in gaps_raw if str(g).strip()]
            if isinstance(gaps_raw, list)
            else []
        )
        if not isinstance(section_order, list):
            section_order = None
        if not isinstance(new_summary, dict):
            new_summary = None

        try:
            edited = ResumeDocument(**resume_data)
        except Exception as e:  # noqa: BLE001
            logger.warning("Structured tailor: bad resume shape (%s)", type(e).__name__)
            raise RewriteFailedError("the rewrite reply was not a resume document") from e

        merged = merge_rewrite(document, edited, section_order=section_order, new_summary=new_summary)
        return TailorStructuredResult(document=merged, gaps=gaps)

    async def edit_snippet(self, text: str, action: str, job_description: str = "") -> str:
        """Apply a single AI editing action to a selected snippet of resume text."""
        instructions = {
            "rewrite": "Rewrite the text to be clearer and stronger while keeping the meaning.",
            "shorten": "Make the text more concise without losing key information.",
            "expand": "Expand the text with relevant, truthful detail.",
            "professional": "Rewrite in a more professional, polished tone.",
            "ats": "Rewrite to be ATS-friendly: clear, keyword-rich phrasing aligned to the job, plain text only.",
            "impact": "Rewrite to emphasize measurable impact with strong action verbs.",
            "grammar": "Fix spelling and grammar only; keep the wording and meaning intact.",
        }
        instruction = instructions.get(action, instructions["rewrite"])
        ctx = ""
        if job_description and action in ("ats", "impact", "rewrite"):
            ctx = f"\n\nTarget job (for context only):\n{job_description[:1500]}"
        prompt = (
            "You are editing a snippet of a resume. "
            f"{instruction} "
            "Return ONLY the edited text, no preamble, quotes, labels, or explanation. "
            "Do not invent employers, job titles, dates, or metrics that are not already implied."
            f"{ctx}\n\nText:\n{text}"
        )
        result = await self._generate(prompt, op="edit_snippet")
        return result.strip().strip('"').strip()

    async def generate_connection_message(
        self, profile_name: str, profile_title: str, job_title: str, company: str
    ) -> str:
        template = _load_prompt("connection_message.txt")
        prompt = (
            template
            .replace("{{PROFILE_NAME}}", profile_name)
            .replace("{{PROFILE_TITLE}}", profile_title)
            .replace("{{JOB_TITLE}}", job_title)
            .replace("{{COMPANY}}", company)
        )
        message = await self._generate(prompt, op="connection_message")
        return message.strip()[:300]
