"""Score intervals: the score again under the plausible readings of what is ambiguous.

SPEC §17.3; DECISIONS D-45, D-11, D-21, D-30, D-31, D-42, D-43.

"Report a range, not a point, wherever uncertainty is material. Compute by re-running the scorer
under the plausible interpretations of each ambiguous input (imprecise dates at both range
endpoints, unresolved skills counted as matched and as missed)." Two extra runs (D-45):

* `resume_at_bounds`. Imprecise dates (`DateValue.imprecise`: season, quarter or year precision;
  a month or a day is precise, D-11) move inside their bounds. Pessimistic: roles as short and last
  use as old as the bounds allow, i.e. an end at its earliest and a start at its latest, but never
  after the end: `end = min(max(end lo, start lo), end hi)`, `start = max(start lo, min(start hi,
  end))`, where a precise date's lo and hi are its own value. When the two bounds cross ("2020 –
  2020") the role shrinks to a point at its oldest. Every moved value stays inside its own bounds:
  a project, which unlike a role may end before it starts, keeps each date within its own range.
  Optimistic: a start at its earliest, an end at its latest. Projects move the same way (their end
  dates their skills, D-34). Only `value` moves; bounds and precision stay, and nothing else of the
  résumé changes.
* `unresolved_requirement_ids`. Each distinct unresolved requirement token of the posting becomes a
  pseudo-requirement `unresolved:<normalized token>` (`graph.aliases.normalize_surface`; blanks
  skipped; cut to the 200-character id limit) at IDF `intervals.unresolved_token_idf` (0.50), missed
  (m = 0, pessimistic) or matched (m = 1, optimistic). `unresolved_necessity`: the pseudo-requirements
  are REQUIRED when the posting lists a REQUIRED requirement; else PREFERRED when it lists one, since
  D-42 then moves §5.1's weight to §5.2 and that is where the weight lives (making §5.1 score would
  undo the transfer the base run applied); else IMPLIED. They join that necessity's coverage
  dimension (§5.1, §5.2 or §5.7, D-30) through `score_coverage`'s extra groups, so the readings run
  the same path as the base run: the IDF-weighted mean and, for REQUIRED only, the CRITICAL_MISS
  rule (D-43; at the default 0.50 a token is never critical). They enter no other dimension: they
  have no graph node, so nothing could relate them to a résumé skill, and in the experience
  requirement space (D-35) an unmatched pseudo-id would lower role relevance even optimistically.
* `variant_dimensions` re-scores the seven dimensions of one reading, `variant_raw` aggregates them.
  `score_interval` calibrates both readings with the caller's calibration and `interval_of` takes
  `low`/`high` as the min/max over the base score and both readings (a reading can land on either
  side of the base: moving an end can change which role is the latest, §5.4); `display` when
  `high − low > intervals.display_spread` (6, "when the spread exceeds 6 points").

Gates are not re-run: no gate reads a date or a skill credit (§4). Intervals cost two extra runs, so
the engine computes them on request only (`ScoreOptions.with_interval`), for SCORED results (D-45).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date
from types import MappingProxyType
from typing import Optional

from matcher.config import EngineConfig
from matcher.errors import EngineFault, Flag
from matcher.graph.aliases import normalize_surface
from matcher.graph.model import SkillGraph
from matcher.models import (
    DateValue,
    DimensionName,
    DimensionScore,
    JobPosting,
    Necessity,
    Project,
    RelationKind,
    Resume,
    Role,
    ScoreInterval,
)
from matcher.score.aggregate import aggregate
from matcher.score.coverage import RequirementMatch
from matcher.score.dimensions import SCORERS, score_preferred_skills, score_required_skills, score_tools
from matcher.score.numeric import clamp01
from matcher.score.prepare import PreparedResume, prepare_resume

__all__ = [
    "UNRESOLVED_PREFIX",
    "interval_of",
    "resume_at_bounds",
    "score_interval",
    "unresolved_matches",
    "unresolved_necessity",
    "unresolved_requirement_ids",
    "variant_dimensions",
    "variant_raw",
]

UNRESOLVED_PREFIX = "unresolved:"
_MAX_ID = 200  # the models' id length limit
# The coverage dimension of each necessity and its scorer (D-30), in the order the posting's
# strongest stated necessity is looked for.
_COVERAGE: Mapping[Necessity, tuple[DimensionName, Callable[..., DimensionScore]]] = MappingProxyType({
    Necessity.REQUIRED: (DimensionName.REQUIRED_SKILLS, score_required_skills),
    Necessity.PREFERRED: (DimensionName.PREFERRED_SKILLS, score_preferred_skills),
    Necessity.IMPLIED: (DimensionName.TOOLS, score_tools),
})


# ─── Résumé variants (imprecise dates at their bounds) ─────────────────────────────────────


def _span(value: DateValue) -> tuple[date, date]:
    """(lo, hi): an imprecise date's bounds, a precise date's own value twice (D-11)."""
    if value.imprecise:
        return value.earliest, value.latest
    return value.value, value.value


def _moved(value: DateValue, day: date) -> DateValue:
    if day == value.value:
        return value
    return DateValue(value=day, earliest=value.earliest, latest=value.latest, precision=value.precision)


def _at_bounds(
    start: Optional[DateValue], end: Optional[DateValue], *, optimistic: bool
) -> tuple[Optional[DateValue], Optional[DateValue]]:
    """A start and an end moved to the bounds that make the span longest (optimistic) or shortest
    and oldest (pessimistic), never with the end before the start when the bounds allow it, and
    never outside a date's own bounds."""
    if optimistic:
        new_start = None if start is None else _moved(start, _span(start)[0])
        new_end = None if end is None else _moved(end, _span(end)[1])
        return new_start, new_end
    if end is None:
        return (None if start is None else _moved(start, _span(start)[1])), None
    end_lo, end_hi = _span(end)
    if start is None:
        return None, _moved(end, end_lo)
    start_lo, start_hi = _span(start)
    end_day = min(max(end_lo, start_lo), end_hi)
    start_day = max(start_lo, min(start_hi, end_day))
    return _moved(start, start_day), _moved(end, end_day)


def _role_at_bounds(role: Role, optimistic: bool) -> Role:
    start, end = _at_bounds(role.start, role.end, optimistic=optimistic)
    if start is role.start and end is role.end:
        return role
    return Role(**{**dict(role), "start": start, "end": end})


def _project_at_bounds(project: Project, optimistic: bool) -> Project:
    start, end = _at_bounds(project.start, project.end, optimistic=optimistic)
    if start is project.start and end is project.end:
        return project
    return Project(**{**dict(project), "start": start, "end": end})


def resume_at_bounds(resume: Resume, *, optimistic: bool) -> Resume:
    """`resume` with every imprecise role and project date at the bound the reading asks for."""
    roles = tuple(_role_at_bounds(role, optimistic) for role in resume.roles)
    projects = tuple(_project_at_bounds(project, optimistic) for project in resume.projects)
    unchanged = all(a is b for a, b in zip(roles, resume.roles)) and all(
        a is b for a, b in zip(projects, resume.projects)
    )
    if unchanged:
        return resume
    return Resume(**{**dict(resume), "roles": roles, "projects": projects})


# ─── Posting variants (unresolved tokens as pseudo-requirements) ───────────────────────────


def unresolved_requirement_ids(posting: JobPosting) -> tuple[str, ...]:
    """The distinct pseudo-requirement ids of the posting's unresolved tokens, sorted."""
    ids: set[str] = set()
    for token in posting.unresolved_requirement_tokens:
        normalized = normalize_surface(token)
        if normalized:
            ids.add((UNRESOLVED_PREFIX + normalized)[:_MAX_ID])
    return tuple(sorted(ids))


def unresolved_necessity(posting: JobPosting) -> Necessity:
    """The necessity the pseudo-requirements take: the strongest the posting states (REQUIRED, then
    PREFERRED, where D-42 moves §5.1's weight, then IMPLIED). A posting without requirements never
    reaches scoring; REQUIRED, the brief's default, is returned for it."""
    stated = {Necessity(requirement.necessity) for requirement in posting.requirements}
    return next((necessity for necessity in _COVERAGE if necessity in stated), Necessity.REQUIRED)


def unresolved_matches(
    posting: JobPosting, *, matched: bool, config: EngineConfig
) -> tuple[RequirementMatch, ...]:
    """The pseudo-requirement groups of one reading: all missed or all matched (D-45)."""
    idf = clamp01(config.scoring.intervals.unresolved_token_idf)
    critical_miss = config.scoring.critical_miss
    necessity = unresolved_necessity(posting)
    m = 1.0 if matched else 0.0
    critical = (necessity is Necessity.REQUIRED and idf > critical_miss.idf_threshold
                and m < critical_miss.m_threshold)
    return tuple(
        RequirementMatch(
            group=skill_id,
            members=(skill_id,),
            best_member=skill_id,
            m=m,
            relation=RelationKind.EXACT if matched else RelationKind.NONE,
            via=None,  # hypothetical: no résumé skill earned it
            idf=idf,
            weight_multiplier=1.0,
            necessity=necessity,
            critical=critical,
        )
        for skill_id in unresolved_requirement_ids(posting)
    )


# ─── The readings' scores and the interval ─────────────────────────────────────────────────


def variant_dimensions(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig, *, matched: bool
) -> tuple[DimensionScore, ...]:
    """The seven dimensions of one reading, in SPEC order: the pseudo-requirements in the coverage
    dimension of their necessity, every other dimension as the base run scores it."""
    pseudo = unresolved_matches(posting, matched=matched, config=config)
    target, scorer = _COVERAGE[unresolved_necessity(posting)]
    return tuple(
        scorer(prepared, posting, graph, config, extra=pseudo) if pseudo and name is target
        else default(prepared, posting, graph, config)
        for name, default in SCORERS.items()
    )


def variant_raw(
    resume: Resume,
    posting: JobPosting,
    *,
    as_of: date,
    config: EngineConfig,
    graph: SkillGraph,
    optimistic: bool,
) -> float:
    """The raw score of the pessimistic or optimistic reading of a pair that scored (D-45)."""
    prepared = prepare_resume(resume_at_bounds(resume, optimistic=optimistic), graph, config, as_of)
    aggregated = aggregate(variant_dimensions(prepared, posting, graph, config, matched=optimistic), config)
    if aggregated is None:  # the readings only add requirements and move dates inside their bounds
        raise EngineFault(Flag.INTERNAL_ERROR, "an interval reading scored nothing although the pair did")
    return aggregated.raw


def interval_of(scores: Iterable[int], config: EngineConfig) -> ScoreInterval:
    """low/high over the calibrated scores; displayed when the spread exceeds `display_spread`."""
    values = list(scores)
    low, high = min(values), max(values)
    return ScoreInterval(low=low, high=high, display=high - low > config.scoring.intervals.display_spread)


def score_interval(
    base: int,
    resume: Resume,
    posting: JobPosting,
    *,
    as_of: date,
    config: EngineConfig,
    graph: SkillGraph,
    calibrate: Callable[[float], int],
) -> ScoreInterval:
    """The interval around a pair's calibrated score `base` (SPEC §17.3): `calibrate` turns each
    reading's raw score into the calibrated integer with the pair's own calibration."""
    readings = [
        calibrate(variant_raw(resume, posting, as_of=as_of, config=config, graph=graph, optimistic=reading))
        for reading in (False, True)
    ]
    return interval_of((base, *readings), config)
