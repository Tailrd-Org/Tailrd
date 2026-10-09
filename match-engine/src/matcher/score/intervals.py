"""Score intervals: the score again under the plausible readings of what is ambiguous.

SPEC §17.3; DECISIONS D-45, D-11, D-21, D-31, D-43.

"Report a range, not a point, wherever uncertainty is material. Compute by re-running the scorer
under the plausible interpretations of each ambiguous input (imprecise dates at both range
endpoints, unresolved skills counted as matched and as missed)." Two extra runs (D-45):

* `resume_at_bounds`. Imprecise dates (`DateValue.imprecise`: season, quarter or year precision;
  a month or a day is precise, D-11) move inside their bounds. Pessimistic: roles as short and last
  use as old as the bounds allow, i.e. an end at its earliest and a start at its latest, but never
  after the end: `end = max(end lo, start lo)`, `start = min(start hi, end)`, where a precise date's
  lo and hi are its own value. When the two bounds cross ("2020 – 2020") the role shrinks to a point
  at its oldest. Optimistic: a start at its earliest, an end at its latest. Projects move the same
  way (their end dates their skills, D-34). Only `value` moves; bounds and precision stay, and
  nothing else of the résumé changes.
* `unresolved_requirement_ids`. Each distinct unresolved requirement token of the posting becomes a
  pseudo-requirement `unresolved:<normalized token>` (`graph.aliases.normalize_surface`; blanks
  skipped; cut to the 200-character id limit), REQUIRED, at IDF `intervals.unresolved_token_idf`
  (0.50). `required_with_unresolved` adds them to §5.1's groups as missed (m = 0, pessimistic) or
  matched (m = 1, optimistic) and recomputes the coverage with the §5.1 machinery: the IDF-weighted
  mean, the CRITICAL_MISS rule (D-43; at the default 0.50 a token is never critical). They enter
  §5.1 only: they have no graph node, so nothing else could relate them to a résumé skill, and in
  the experience requirement space (D-35) an unmatched pseudo-id would lower role relevance even in
  the optimistic run.
* `variant_raw` re-runs preparation, the seven dimensions (§5.1 as above) and the aggregate on one
  variant. `score_interval` calibrates both variants with the caller's calibration and
  `interval_of` takes `low`/`high` as the min/max over the base score and both variants (a variant
  can land on either side of the base: moving an end can change which role is the latest, §5.4);
  `display` when `high − low > intervals.display_spread` (6, "when the spread exceeds 6 points").

Gates are not re-run: no gate reads a date or a skill credit (§4). Intervals cost two extra runs, so
the engine computes them on request only (`ScoreOptions.with_interval`, D-45).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
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
from matcher.score._shared import abstain, scored
from matcher.score.aggregate import aggregate
from matcher.score.coverage import RequirementMatch, coverage_value, match_requirements, requirement_facts
from matcher.score.dimensions import SCORERS
from matcher.score.numeric import clamp01
from matcher.score.prepare import PreparedResume, prepare_resume

__all__ = [
    "UNRESOLVED_PREFIX",
    "interval_of",
    "required_with_unresolved",
    "resume_at_bounds",
    "score_interval",
    "unresolved_requirement_ids",
    "variant_raw",
]

UNRESOLVED_PREFIX = "unresolved:"
_MAX_ID = 200  # the models' id length limit
_REQUIRED = DimensionName.REQUIRED_SKILLS


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
    and oldest (pessimistic), never with the end before the start."""
    if optimistic:
        new_start = None if start is None else _moved(start, _span(start)[0])
        new_end = None if end is None else _moved(end, _span(end)[1])
        return new_start, new_end
    if end is None:
        return (None if start is None else _moved(start, _span(start)[1])), None
    end_day = _span(end)[0]
    if start is None:
        return None, _moved(end, end_day)
    start_lo, start_hi = _span(start)
    end_day = max(end_day, start_lo)
    return _moved(start, min(start_hi, end_day)), _moved(end, end_day)


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
    """`resume` with every imprecise role and project date at the bound the variant asks for."""
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
    ids = set()
    for token in posting.unresolved_requirement_tokens:
        normalized = normalize_surface(token)
        if normalized:
            ids.add((UNRESOLVED_PREFIX + normalized)[:_MAX_ID])
    return tuple(sorted(ids))


def _pseudo_matches(ids: Iterable[str], matched: bool, config: EngineConfig) -> tuple[RequirementMatch, ...]:
    idf = clamp01(config.scoring.intervals.unresolved_token_idf)
    critical_miss = config.scoring.critical_miss
    m = 1.0 if matched else 0.0
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
            necessity=Necessity.REQUIRED,
            critical=idf > critical_miss.idf_threshold and m < critical_miss.m_threshold,
        )
        for skill_id in ids
    )


def required_with_unresolved(
    prepared: PreparedResume, posting: JobPosting, graph: SkillGraph, config: EngineConfig, *, matched: bool
) -> DimensionScore:
    """§5.1 with the posting's unresolved tokens as extra REQUIRED groups, all missed or all matched.
    Without unresolved tokens it is §5.1 itself."""
    pseudo_ids = unresolved_requirement_ids(posting)
    if not pseudo_ids:
        return SCORERS[_REQUIRED](prepared, posting, graph, config)
    required = [item for item in posting.requirements if item.necessity == Necessity.REQUIRED]
    real = match_requirements(prepared, required, graph, config)
    matches = (*real, *_pseudo_matches(pseudo_ids, matched, config))
    value = coverage_value(matches)
    if value is None:
        return abstain(_REQUIRED, config, Flag.NO_REQUIREMENTS_RESOLVED)
    return scored(_REQUIRED, config, value, requirement_facts(matches))


# ─── The variants' scores and the interval ─────────────────────────────────────────────────


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
    dimensions = [required_with_unresolved(prepared, posting, graph, config, matched=optimistic)]
    others = (scorer for name, scorer in SCORERS.items() if name is not _REQUIRED)
    dimensions.extend(scorer(prepared, posting, graph, config) for scorer in others)
    aggregated = aggregate(dimensions, config)
    if aggregated is None:  # the variants only add requirements and move dates inside their bounds
        raise EngineFault(Flag.INTERNAL_ERROR, "an interval variant scored nothing although the pair did")
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
    variant's raw score into the calibrated integer with the pair's own calibration."""
    variants = [
        calibrate(variant_raw(resume, posting, as_of=as_of, config=config, graph=graph, optimistic=reading))
        for reading in (False, True)
    ]
    return interval_of((base, *variants), config)
