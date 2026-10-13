"""The engine entry point: any input in, exactly one `MatchResult` out.

SPEC §13, §17, §4, §6, §14.4, §14.5, §0.4; DECISIONS D-42..D-46, D-50, D-60.

`score(resume, posting, *, as_of, config, graph, calibration, options)` is a total function: every
input ends in one of the four outcomes of SPEC §13, never an exception, a None or a NaN. In order:

1. Validate. `resume` and `posting` may be the models, plain dicts or JSON (str or bytes); a model
   is validated again (one built with `model_construct` skipped validation, and one that does not
   serialise is not valid). `as_of` must be a `datetime.date`; a `datetime` is refused (a
   timestamp's date depends on its time zone). Anything else → NEEDS_REVIEW INVALID_INPUT with no
   input digest; an `as_of` that is not a date is recorded as `UNKNOWN_AS_OF` (date.min), a date no
   real score carries.
2. A flag in `NEEDS_REVIEW_REASONS` on the résumé or the posting → NEEDS_REVIEW with those flags,
   the résumé's first.
3. A posting with no requirements → NEEDS_REVIEW NO_REQUIREMENTS_RESOLVED (§14.4); one flagged
   POSTING_UNSCORABLE has already left at step 2 with that flag. One abstaining dimension is not
   this: it redistributes (D-42).
4. `as_of` before every date on the résumé → NEEDS_REVIEW INVALID_AS_OF (§14.5). Role and project
   dates count at their earliest bound, skill dates as they are: a date whose earliest bound is not
   after `as_of` may lie in the past, so it never triggers this. A résumé with no date never does.
5. Gates (§4; D-60..D-64): a FAIL → GATE_FAIL with the failing gates' reasons and no score. Nothing
   is prepared, scored or calibrated for it.
6. Prepare the résumé, score the seven dimensions, aggregate (§6, §14.5; D-42, D-43); nothing
   scored → NEEDS_REVIEW NOTHING_SCORABLE, with the gates and the abstaining dimensions.
7. Calibrate (`calibration.calibrate(raw)`, the band from `bands.band_for`), then confidence and
   the outcome (§17.1, §17.2): SCORED shows the number and the band (and POSTING_TRUNCATED as its
   only reason when the posting was cut short); SCORED_LOW_CONFIDENCE the band
   and the confidence reasons; NEEDS_REVIEW neither, with LOW_CONFIDENCE and the confidence
   reasons. The raw and calibrated scores of a non-SCORED result stay on the record for audit and
   for the person who reviews it (SPEC §0.4): adapters key on `outcome` and never rank, sort or show
   a suppressed pair by `calibrated_score`; `display_score` is set only when SCORED.
8. On request (`options.with_interval`) and for a SCORED result only, the §17.3 interval (D-45):
   the other outcomes suppress the percentage, and an interval is a range of percentages.

Every result carries `engine_version`, `config_hash` = `combined_hash(config, graph.version)`,
`as_of` and `input_digest`: the first 16 hex of sha256 over canonical résumé JSON + "|" + canonical
posting JSON + "|" + `as_of` ISO, so a dict, JSON or a model of the same input digest alike.
`flags` are informational, each once: the inputs' own flags (the résumé's, then the posting's) that
are not reasons, then the informational facts of the scoring (MIN_YEARS_DEFAULTED,
NO_WORK_EXPERIENCE, OVERQUALIFIED: the trace keeps flags but not facts).

Then the explanation layer (Task 4.1, `matcher.explain.attach_explanation`, imported late because it
re-runs this function for counterfactuals): every outcome gets an `explanation` when
`options.with_explanation` (the default), and a SCORED result gets `improvements` (and
`explanation.to_improve`) when `options.with_counterfactual`. Options that are not `ScoreOptions`
explain with the defaults. `explain/` is pure like `score/`.

A NEEDS_REVIEW returned before the gates run (steps 1 to 4, and faults) reports `gate_status` PASS
with no gate results: "not evaluated", since the model has no such status. Consumers key on
`outcome`.

Faults (SPEC §13, D-46): non-strict mode turns an exception raised after validation into
NEEDS_REVIEW, NUMERIC_FAULT for `EngineFault(NUMERIC_FAULT)` and INTERNAL_ERROR for anything else;
`options.strict` (tests) re-raises it. Invalid input is an outcome, not a fault: INVALID_INPUT in
both modes. The calibrator's answer must be an int in [0, 100] (not a bool) and a
`CalibrationInfo`; anything else is an INTERNAL_ERROR fault. No exception class is kept on the
result: the engine is deterministic, so a caller replays the pair in strict mode to get it.

Wiring: `calibration` is any object with `calibrate(raw) -> (int, CalibrationInfo)`, the
`matcher.calibrate.Calibrator` protocol, used by its shape (`score/` imports nothing outside
`matcher.{models, errors, config, graph}`, SPEC §1). A calibration without `calibrate` is an
INTERNAL_ERROR fault (raised as TypeError in strict mode); `options` that are not `ScoreOptions`
cannot say the mode, so the default, non-strict, applies and the result is NEEDS_REVIEW
INTERNAL_ERROR. Both are reported before the input is read. The one exemption from totality: an
unusable `config` or `graph` raises TypeError in both modes, since no result could carry an engine
version or a config hash.

Pure: no I/O, no clock (`as_of` is an argument), no randomness; identical inputs give
byte-identical canonical JSON.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime
from typing import Any, Optional, TypeVar, Union

from matcher.config import EngineConfig, combined_hash
from matcher.errors import NEEDS_REVIEW_REASONS, EngineFault, Flag
from matcher.graph.model import SkillGraph
from matcher.models import (
    CalibrationInfo,
    DimensionName,
    DimensionScore,
    GateResult,
    GateStatus,
    JobPosting,
    MatchResult,
    Outcome,
    Resume,
    ScoreOptions,
    canonical_json,
)
from matcher.score.aggregate import aggregate
from matcher.score.confidence import compute_confidence, outcome_for
from matcher.score.coverage import match_requirements
from matcher.score.dimensions import score_dimensions
from matcher.score.gates import evaluate_gates
from matcher.score.intervals import score_interval
from matcher.score.prepare import prepare_resume

__all__ = ["DIGEST_HEX", "UNKNOWN_AS_OF", "input_digest", "review_result", "score"]

UNKNOWN_AS_OF = date.min  # the as_of of a result whose as_of was not a date
DIGEST_HEX = 16

# Fact codes that are informational flags (errors.py "Scoring: informational"). CRITICAL_MISS is not
# one: when it lowers the score it is a cap (`caps`), and otherwise it is the requirement's own fact.
_FACT_FLAGS = frozenset(
    {Flag.MIN_YEARS_DEFAULTED.value, Flag.NO_WORK_EXPERIENCE.value, Flag.OVERQUALIFIED.value}
)

_Model = TypeVar("_Model", Resume, JobPosting)


# ─── The entry point ───────────────────────────────────────────────────────────────────────


def score(
    resume: Any,
    posting: Any,
    *,
    as_of: Any,
    config: EngineConfig,
    graph: SkillGraph,
    calibration: Any,
    options: ScoreOptions = ScoreOptions(),
) -> MatchResult:
    """Score one résumé against one posting as of `as_of` (see the module docstring)."""
    result = _bare_score(resume, posting, as_of=as_of, config=config, graph=graph, calibration=calibration,
                         options=options)
    wanted = options if isinstance(options, ScoreOptions) else ScoreOptions()
    if not (wanted.with_explanation or wanted.with_counterfactual):
        return result
    from matcher.explain import attach_explanation  # explain/ re-runs this function: imported late

    return attach_explanation(result, resume, posting, as_of=result.as_of, config=config, graph=graph,
                              calibration=calibration, options=wanted)


def _bare_score(
    resume: Any,
    posting: Any,
    *,
    as_of: Any,
    config: EngineConfig,
    graph: SkillGraph,
    calibration: Any,
    options: ScoreOptions,
) -> MatchResult:
    """`score` before the explanation layer (steps 1 to 8 of the module docstring)."""
    _check_wiring(config, graph)
    day = _valid_as_of(as_of)
    if not isinstance(options, ScoreOptions) or not _calibrates(calibration):
        if isinstance(options, ScoreOptions) and options.strict:
            raise TypeError(
                f"calibration needs calibrate(raw) -> (int, CalibrationInfo): {type(calibration).__name__}"
            )
        broken = [Flag.INTERNAL_ERROR]  # wiring, not input: reported before the input is read
        return review_result(broken, config=config, graph=graph, as_of=day or UNKNOWN_AS_OF)
    if day is None:
        return review_result([Flag.INVALID_INPUT], config=config, graph=graph, as_of=UNKNOWN_AS_OF)
    checked = _checked_inputs(resume, posting, day)
    if checked is None:
        return review_result([Flag.INVALID_INPUT], config=config, graph=graph, as_of=day)
    valid_resume, valid_posting, digest = checked
    try:
        return _score(valid_resume, valid_posting, day, digest, config, graph, calibration, options)
    except Exception as exc:  # SPEC §13: nothing escapes in production (D-46)
        if options.strict:
            raise
        numeric = isinstance(exc, EngineFault) and exc.flag is Flag.NUMERIC_FAULT
        fault = Flag.NUMERIC_FAULT if numeric else Flag.INTERNAL_ERROR
        return review_result([fault], config=config, graph=graph, as_of=day, input_digest=digest)


def review_result(
    reasons: Sequence[Flag],
    *,
    config: EngineConfig,
    graph: SkillGraph,
    as_of: date,
    input_digest: str = "",
    flags: Sequence[Flag] = (),
) -> MatchResult:
    """A NEEDS_REVIEW result with `reasons` (each once, in order), no score and no gate evaluated
    (`gate_status` PASS, no gate results). `flags` are informational; a flag that is also a reason
    is not repeated. An empty `reasons` is a ValueError: NEEDS_REVIEW always says why (SPEC §13)."""
    _check_wiring(config, graph)
    codes = _unique(Flag(reason) for reason in reasons)
    if not codes:
        raise ValueError("a NEEDS_REVIEW result needs at least one reason (SPEC §13)")
    return MatchResult(
        outcome=Outcome.NEEDS_REVIEW,
        gate_status=GateStatus.PASS,
        reasons=codes,
        flags=tuple(flag for flag in _unique(Flag(flag) for flag in flags) if flag not in codes),
        **_stamp(config, graph, as_of, input_digest),
    )


def input_digest(resume: Resume, posting: JobPosting, as_of: date) -> str:
    """The first 16 hex of sha256 over canonical résumé JSON | canonical posting JSON | as_of ISO."""
    return _digest(canonical_json(resume), canonical_json(posting), as_of)


# ─── Wiring and validation ─────────────────────────────────────────────────────────────────


def _check_wiring(config: Any, graph: Any) -> None:
    if not isinstance(config, EngineConfig):
        raise TypeError(f"config must be an EngineConfig, got {type(config).__name__}")
    if not isinstance(graph, SkillGraph):
        raise TypeError(f"graph must be a SkillGraph, got {type(graph).__name__}")


def _calibrates(calibration: Any) -> bool:
    """Does `calibration` have a callable `calibrate` (whatever reading it raises, it does not)?"""
    try:
        return callable(getattr(calibration, "calibrate", None))
    except Exception:
        return False


def _valid_as_of(value: Any) -> Optional[date]:
    """A plain `date`, or None for anything else (a `datetime` included, and a date subclass whose
    fields cannot be read)."""
    if isinstance(value, datetime) or not isinstance(value, date):
        return None
    try:
        return date(value.year, value.month, value.day)
    except Exception:  # whatever reading the input raises, it is invalid input, never a fault
        return None


def _validated(model_type: type[_Model], value: Any) -> tuple[_Model, str]:
    """`value` validated as `model_type`, with its canonical JSON. Raises (pydantic's ValueError
    subclasses, TypeError, or whatever the input itself raises while it is read) when it is not one."""
    if isinstance(value, model_type):
        # Validated again from its dump: a model built with model_construct was never validated,
        # and one whose fields do not serialise raises instead of warning.
        model = model_type.model_validate(value.model_dump(warnings="error"))
    elif isinstance(value, (str, bytes, bytearray)):
        model = model_type.model_validate_json(value)
    elif isinstance(value, dict):
        model = model_type.model_validate(value)
    else:
        raise TypeError(f"not a {model_type.__name__}, a dict or JSON: {type(value).__name__}")
    return model, canonical_json(model)


def _checked_inputs(resume: Any, posting: Any, as_of: date) -> Optional[tuple[Resume, JobPosting, str]]:
    """The valid résumé, posting and their input digest; None when either is not valid."""
    try:
        valid_resume, resume_json = _validated(Resume, resume)
        valid_posting, posting_json = _validated(JobPosting, posting)
        return valid_resume, valid_posting, _digest(resume_json, posting_json, as_of)
    except Exception:  # whatever refuses to be read as a Resume / JobPosting is invalid input (D-46)
        return None


def _digest(resume_json: str, posting_json: str, as_of: date) -> str:
    text = f"{resume_json}|{posting_json}|{as_of.isoformat()}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:DIGEST_HEX]


# ─── The pipeline ──────────────────────────────────────────────────────────────────────────


def _score(
    resume: Resume,
    posting: JobPosting,
    as_of: date,
    digest: str,
    config: EngineConfig,
    graph: SkillGraph,
    calibration: Any,
    options: ScoreOptions,
) -> MatchResult:
    stamp = _stamp(config, graph, as_of, digest)
    flags = _input_flags(resume, posting)

    def review(*reasons: Flag) -> MatchResult:
        return review_result(
            reasons, config=config, graph=graph, as_of=as_of, input_digest=digest, flags=flags
        )

    flagged = [flag for flag in (*resume.flags, *posting.flags) if flag in NEEDS_REVIEW_REASONS]
    if flagged:
        return review(*flagged)
    if not posting.requirements:  # one flagged POSTING_UNSCORABLE left above, with that flag
        return review(Flag.NO_REQUIREMENTS_RESOLVED)
    if _precedes_every_date(as_of, resume):
        return review(Flag.INVALID_AS_OF)

    gate_status, gates = evaluate_gates(resume, posting, graph, config)
    if gate_status is GateStatus.FAIL:
        return _gate_fail(gates, flags, stamp)

    prepared = prepare_resume(resume, graph, config, as_of)
    dimensions = score_dimensions(prepared, posting, graph, config)
    aggregated = aggregate(dimensions, config)
    if aggregated is None:
        return MatchResult(
            outcome=Outcome.NEEDS_REVIEW,
            gate_status=gate_status,
            gates=gates,
            dimensions=_keyed(dimensions),
            reasons=(Flag.NOTHING_SCORABLE,),
            flags=flags,
            **stamp,
        )

    calibrated, calibration_info = _calibrate(calibration, aggregated.raw)
    matches = match_requirements(prepared, posting.requirements, graph, config)
    confidence = compute_confidence(resume, posting, prepared, aggregated, matches, config)
    outcome = outcome_for(confidence, config)
    # A SCORED result names only a truncated posting (Task 6.5 fix round 1, D-104): its penalty is
    # always said, at every outcome.
    reasons: tuple[Flag, ...] = tuple(r for r in confidence.reasons if r is Flag.POSTING_TRUNCATED)
    if outcome is Outcome.SCORED_LOW_CONFIDENCE:
        reasons = confidence.reasons
    elif outcome is Outcome.NEEDS_REVIEW:
        reasons = _unique((Flag.LOW_CONFIDENCE, *confidence.reasons))
    interval = None
    if options.with_interval and outcome is Outcome.SCORED:
        interval = score_interval(
            calibrated, resume, posting, as_of=as_of, config=config, graph=graph,
            calibrate=lambda raw: _calibrate(calibration, raw)[0],
        )
    return MatchResult(
        outcome=outcome,
        gate_status=gate_status,
        gates=gates,
        raw_score=aggregated.raw,
        calibrated_score=calibrated,
        display_score=calibrated if outcome is Outcome.SCORED else None,
        band=None if outcome is Outcome.NEEDS_REVIEW else config.bands.band_for(calibrated),
        dimensions=aggregated.dimensions,
        caps=aggregated.caps,
        confidence=confidence,
        interval=interval,
        calibration=calibration_info,
        reasons=reasons,
        flags=_unique((*flags, *_fact_flags(aggregated.dimensions.values()))),
        **stamp,
    )


def _stamp(config: EngineConfig, graph: SkillGraph, as_of: date, digest: str) -> dict[str, Union[str, date]]:
    """What every result carries to be replayed and audited (SPEC §0.4)."""
    return {
        "engine_version": config.engine_version,
        "config_hash": combined_hash(config, graph.version),
        "as_of": as_of,
        "input_digest": digest,
    }


def _gate_fail(gates: tuple[GateResult, ...], flags: tuple[Flag, ...], stamp: dict) -> MatchResult:
    """GATE_FAIL: the failing gates' reasons, every gate result, no score (SPEC §4)."""
    failed = (gate.reason for gate in gates if gate.status is GateStatus.FAIL and gate.reason is not None)
    reasons = _unique(failed)
    if not reasons:
        raise EngineFault(Flag.INTERNAL_ERROR, "a gate failed without a reason")
    return MatchResult(
        outcome=Outcome.GATE_FAIL, gate_status=GateStatus.FAIL, gates=gates, reasons=reasons, flags=flags,
        **stamp,
    )


def _calibrate(calibration: Any, raw: float) -> tuple[int, CalibrationInfo]:
    """The calibrator's (integer, CalibrationInfo), checked: a plug-in that answers anything else is
    an engine fault, never a number shown to someone."""
    answer = calibration.calibrate(raw)
    try:
        calibrated, info = answer
    except (TypeError, ValueError) as exc:
        raise EngineFault(Flag.INTERNAL_ERROR, "the calibrator answered no (score, CalibrationInfo)") from exc
    if isinstance(calibrated, bool) or not isinstance(calibrated, int) or not 0 <= calibrated <= 100:
        raise EngineFault(Flag.INTERNAL_ERROR, f"calibrated score {calibrated!r} is not an int in [0, 100]")
    if not isinstance(info, CalibrationInfo):
        raise EngineFault(Flag.INTERNAL_ERROR, "the calibrator did not say how it calibrated")
    return calibrated, info


def _resume_dates(resume: Resume) -> Iterator[date]:
    """Every date on the résumé: role and project dates at their earliest bound, skill dates."""
    for container in (*resume.roles, *resume.projects):
        for value in (container.start, container.end):
            if value is not None:
                yield value.earliest
        for ref in container.skills:
            yield from (day for day in (ref.first_used, ref.last_used) if day is not None)
    for ref in (*resume.skills_blob, *resume.certifications):
        yield from (day for day in (ref.first_used, ref.last_used) if day is not None)


def _precedes_every_date(as_of: date, resume: Resume) -> bool:
    earliest = min(_resume_dates(resume), default=None)
    return earliest is not None and as_of < earliest


def _input_flags(resume: Resume, posting: JobPosting) -> tuple[Flag, ...]:
    """The inputs' informational flags, each once: the résumé's, then the posting's."""
    return _unique(flag for flag in (*resume.flags, *posting.flags) if flag not in NEEDS_REVIEW_REASONS)


def _fact_flags(dimensions: Iterable[DimensionScore]) -> Iterator[Flag]:
    for dimension in dimensions:
        for fact in dimension.facts:
            if fact.code in _FACT_FLAGS:
                yield Flag(fact.code)


def _keyed(dimensions: Iterable[DimensionScore]) -> dict[str, DimensionScore]:
    """Dimension scores by dimension, in SPEC order (keyed by `DimensionScore.dimension`, as the
    aggregator does)."""
    by_name = {score.dimension: score for score in dimensions}
    return {name.value: by_name[name] for name in DimensionName if name in by_name}


def _unique(flags: Iterable[Flag]) -> tuple[Flag, ...]:
    """`flags` in order, each once."""
    return tuple(dict.fromkeys(flags))
