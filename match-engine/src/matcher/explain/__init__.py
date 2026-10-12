"""The explanation layer (SPEC §1 `explain/`, §8): breakdown, gap report and `to_improve`.

`attach_explanation` is what the engine calls after it has a result: it fills `explanation` when
`options.with_explanation` and `improvements` (with `explanation.to_improve`) when
`options.with_counterfactual`. Like `score/`, this package is pure (no I/O, no clock): it reads
the result, the inputs, the graph and the config, and re-runs the pure scorer for counterfactuals.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from matcher.config import EngineConfig
from matcher.explain.breakdown import build_explanation
from matcher.explain.counterfactual import improve
from matcher.explain.gaps import Gap, find_gaps
from matcher.explain.labels import skill_label
from matcher.graph.model import SkillGraph
from matcher.models import Improvement, JobPosting, MatchResult, Outcome, Resume, ScoreOptions, model_from_json

__all__ = ["Gap", "attach_explanation", "build_explanation", "find_gaps", "improve", "skill_label"]


def _model(model_type: type, value: Any) -> Optional[Any]:
    """`value` as a valid `model_type`, or None (the engine has already said whether it is valid)."""
    try:
        if isinstance(value, model_type):
            return value
        return model_from_json(model_type, value)
    except Exception:
        return None


def attach_explanation(
    result: MatchResult,
    resume: Any,
    posting: Any,
    *,
    as_of: date,
    config: EngineConfig,
    graph: SkillGraph,
    calibration: Any,
    options: ScoreOptions,
) -> MatchResult:
    """`result` with its explanation and improvements, as `options` asks. Never raises unless
    `options.strict`: a failed counterfactual search leaves no improvements, and the explanation
    falls back to the headline and the reasons (`build_explanation`)."""
    if not (options.with_explanation or options.with_counterfactual):
        return result
    scored = Outcome(result.outcome) in (Outcome.SCORED, Outcome.SCORED_LOW_CONFIDENCE)
    valid_resume = _model(Resume, resume) if scored else None
    valid_posting = _model(JobPosting, posting) if scored else None
    improvements: tuple[Improvement, ...] = ()
    if options.with_counterfactual and Outcome(result.outcome) is Outcome.SCORED and valid_resume and valid_posting:
        try:
            improvements = improve(valid_resume, valid_posting, as_of=as_of, config=config, graph=graph,
                                   calibration=calibration, max_steps=options.max_counterfactual_steps,
                                   strict=options.strict)
        except Exception:
            if options.strict:
                raise
            improvements = ()
    update: dict[str, Any] = {"improvements": improvements}
    if options.with_explanation:
        update["explanation"] = build_explanation(result, valid_resume, valid_posting, None, graph, config,
                                                  improvements, strict=options.strict)
    return result.model_copy(update=update)
