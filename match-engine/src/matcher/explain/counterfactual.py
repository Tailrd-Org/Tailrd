"""`to_improve`: the cheapest skills that would move a score into the next band (SPEC §8; Task 4.1).

Minimise Σ learning_cost(s) over a set S of missing skills subject to score(résumé ⊕ S) reaching the
next band's minimum, |S| ≤ `max_steps` (at most 4). `score()` is pure, so every candidate set is
simply re-scored with the engine itself (`ScoreOptions(with_explanation=False,
with_counterfactual=False)`): the number a suggestion states is the number the engine gives.

* Only a SCORED result gets suggestions: the other outcomes show no number to move. The target is
  the smallest band minimum above the current calibrated score; an already-Strong match gets none.
* Candidates: every member of a REQUIRED, PREFERRED or IMPLIED requirement group with `m < 0.5`.
* Adding a skill (`add_skills`): one new AFFIRMED mention with `counterfactual.added_evidence` and
  `added_verb` in the most recent role (a current role first, the latest start among current ones;
  else the latest end; a role with neither counts only when no role is dated, the first listed
  then), or, with no role at all, in a new undated project.
* `learning_cost(s) = weeks_by_kind[kind] × (1 + weeks_per_hop × hops)`, hops the distance from the
  nearest résumé skill (`traversal.nearest_distance`, i.e. min over `traversal.distance`), capped at
  `unreachable_hops`, which is also what a skill out of reach or absent from the graph counts.
  A skill the graph does not hold is costed as TECHNOLOGY.
* Search: every candidate is re-scored once alone; the best `max_candidates` by raw gain per week
  (ties: the smaller id) form the pool. Greedy over the pool, by raw gain per week, until the target
  is reached or `max_steps` skills are chosen; then one swap pass (`swap_pass`): each chosen skill in
  turn is dropped when the rest still reach the target, else replaced by the cheapest pool skill
  with a lower cost that still reaches it. Total weeks never rise.
* Output: the chosen set when it reaches the target, then up to three single skills that each move
  the integer score up (higher new score first, then the lower cost, then the id), without
  repeating a one-skill set. Text: "Adding demonstrated Terraform experience would move this to
  roughly 84", with the band in parentheses when it changes; no percent sign (SPEC §7 naming).

Deterministic: ties are broken by skill id, gains compared at 12 decimals. Pure (no I/O, no clock).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import date
from typing import Any, Optional

from matcher.config import EngineConfig
from matcher.graph.model import SkillGraph
from matcher.graph.traversal import nearest_distance
from matcher.models import (
    Band,
    DimensionName,
    Improvement,
    JobPosting,
    MatchResult,
    Outcome,
    Polarity,
    Project,
    Resume,
    ScoreOptions,
    SkillKind,
    SkillRef,
    SkillSource,
    model_from_json,
)
from matcher.explain.gaps import group_facts
from matcher.explain.labels import join_labels, skill_label
from matcher.score.coverage import MATCH_FROM

__all__ = ["add_skills", "improve", "learning_cost", "most_recent_role", "swap_pass", "target_score"]

MAX_SINGLES = 3
_DECIMALS = 12
_CANDIDATE_DIMENSIONS = (DimensionName.REQUIRED_SKILLS, DimensionName.PREFERRED_SKILLS, DimensionName.TOOLS)


# ─── Pieces with their own tests ──────────────────────────────────────────────────────────


def target_score(current: int, config: EngineConfig) -> Optional[int]:
    """The next band's minimum above `current`; None when no band starts above it (Strong)."""
    above = [rule.min for rule in config.bands.bands if rule.min > current]
    return min(above) if above else None


def most_recent_role(resume: Resume, as_of: date) -> Optional[int]:
    """The index of the role a new skill mention goes into (see the module docstring)."""
    if not resume.roles:
        return None

    def rank(index: int) -> tuple:
        role = resume.roles[index]
        start = role.start.value if role.start is not None else date.min
        if role.is_current:
            return (2, as_of, start, -index)
        if role.end is not None:
            return (1, role.end.value, start, -index)
        return (0, date.min, date.min, -index)  # neither: the first listed

    return max(range(len(resume.roles)), key=rank)


def add_skills(resume: Resume, skill_ids: Sequence[str], config: EngineConfig, as_of: date) -> Resume:
    """`resume` with one new mention of each of `skill_ids` (see the module docstring)."""
    rules = config.scoring.counterfactual
    mentions = tuple(
        SkillRef(canonical_id=skill_id, evidence=rules.added_evidence, verb_class=rules.added_verb,
                 polarity=Polarity.AFFIRMED, source=SkillSource.ROLE if resume.roles else SkillSource.PROJECT)
        for skill_id in skill_ids
    )
    index = most_recent_role(resume, as_of)
    if index is None:
        return resume.model_copy(update={"projects": (*resume.projects, Project(skills=mentions))})
    role = resume.roles[index]
    roles = list(resume.roles)
    roles[index] = role.model_copy(update={"skills": (*role.skills, *mentions)})
    return resume.model_copy(update={"roles": tuple(roles)})


def learning_cost(skill_id: str, resume_skills: Iterable[str], graph: SkillGraph, config: EngineConfig) -> float:
    """Weeks to show `skill_id`: by kind, scaled by its distance from the résumé's skills."""
    rules = config.scoring.counterfactual
    node = graph.node(skill_id)
    kind = node.kind if node is not None else SkillKind.TECHNOLOGY
    hops = nearest_distance(graph, skill_id, resume_skills, rules.unreachable_hops)
    hops = rules.unreachable_hops if hops is None else min(hops, rules.unreachable_hops)
    return float(rules.weeks_by_kind[SkillKind(kind)]) * (1.0 + rules.weeks_per_hop * hops)


def swap_pass(
    chosen: Sequence[str],
    pool: Sequence[str],
    reaches: Callable[[tuple[str, ...]], bool],
    cost: Callable[[str], float],
) -> tuple[str, ...]:
    """One pass over `chosen` (which reaches the target): drop a skill the rest reach without, else
    swap it for the cheapest pool skill of lower cost that still reaches. Never raises the total."""
    current = list(chosen)
    for skill in list(chosen):
        rest = [other for other in current if other != skill]
        if rest and reaches(tuple(rest)):
            current = rest
            continue
        cheaper = sorted((c for c in pool if c not in current and cost(c) < cost(skill)), key=lambda c: (cost(c), c))
        for replacement in cheaper:
            trial = tuple(rest + [replacement])
            if reaches(trial):
                current = list(trial)
                break
    return tuple(current)


# ─── The search ───────────────────────────────────────────────────────────────────────────


def _candidates(result: MatchResult) -> list[str]:
    found: set[str] = set()
    for name in _CANDIDATE_DIMENSIONS:
        dimension = result.dimensions.get(name.value)
        if dimension is None or dimension.abstained:
            continue
        for fact in group_facts(dimension.facts):
            if fact.value is None or fact.value >= MATCH_FROM:
                continue
            raw = fact.detail.get("members")
            members = [part for part in raw.split(",") if part] if isinstance(raw, str) else []
            found.update(members or ([fact.subject] if fact.subject else []))
    return sorted(found)


def _text(skill_ids: Sequence[str], graph: SkillGraph, new_score: int, new_band: Band, old_band: Optional[Band]) -> str:
    if len(skill_ids) == 1:
        node = graph.node(skill_ids[0])
        label = skill_label(graph, skill_ids[0])
        what = label if node is not None and node.kind is SkillKind.CREDENTIAL else f"demonstrated {label} experience"
    else:
        what = join_labels([skill_label(graph, skill_id) for skill_id in skill_ids])
    band = f" ({Band(new_band).value})" if new_band != old_band else ""
    return f"Adding {what} would move this to roughly {new_score}{band}"


def improve(
    resume: Any,
    posting: Any,
    *,
    as_of: date,
    config: EngineConfig,
    graph: SkillGraph,
    calibration: Any,
    max_steps: int = 4,
    strict: bool = False,
) -> tuple[Improvement, ...]:
    """The suggestions for one pair (see the module docstring); () when there are none."""
    from matcher.score.engine import score  # the engine imports this package lazily too

    resume = resume if isinstance(resume, Resume) else model_from_json(Resume, resume)
    posting = posting if isinstance(posting, JobPosting) else model_from_json(JobPosting, posting)
    options = ScoreOptions(strict=strict, with_explanation=False, with_counterfactual=False)
    steps = max(1, min(4, int(max_steps)))

    def run(candidate: Resume) -> MatchResult:
        return score(candidate, posting, as_of=as_of, config=config, graph=graph, calibration=calibration,
                     options=options)

    base = run(resume)
    if base.outcome is not Outcome.SCORED or base.calibrated_score is None or base.raw_score is None:
        return ()
    target = target_score(base.calibrated_score, config)
    candidates = _candidates(base)
    if target is None or not candidates:
        return ()

    held = sorted(
        {ref.canonical_id for role in resume.roles for ref in role.skills}
        | {ref.canonical_id for project in resume.projects for ref in project.skills}
        | {ref.canonical_id for ref in (*resume.skills_blob, *resume.certifications)}
    )
    costs = {skill_id: learning_cost(skill_id, held, graph, config) for skill_id in candidates}
    memo: dict[frozenset[str], Optional[MatchResult]] = {}

    def rescored(skill_ids: Iterable[str]) -> Optional[MatchResult]:
        key = frozenset(skill_ids)
        if key not in memo:
            # model_copy skips validation; the engine validates again, so a role pushed past its
            # mention limit comes back INVALID_INPUT and is simply not a usable suggestion.
            outcome = run(add_skills(resume, sorted(key), config, as_of))
            usable = outcome.outcome is Outcome.SCORED and outcome.raw_score is not None
            memo[key] = outcome if usable else None
        return memo[key]

    def raw(skill_ids: Iterable[str]) -> float:
        found = rescored(skill_ids)
        return base.raw_score if found is None else found.raw_score

    def reaches(skill_ids: Iterable[str]) -> bool:
        found = rescored(skill_ids)
        return found is not None and found.calibrated_score is not None and found.calibrated_score >= target

    def per_week(gain: float, skill_id: str) -> float:
        return round(gain / costs[skill_id], _DECIMALS)

    singles = {skill_id: round(raw((skill_id,)) - base.raw_score, _DECIMALS) for skill_id in candidates}
    useful = [skill_id for skill_id in candidates if singles[skill_id] > 0.0]
    pool = sorted(useful, key=lambda s: (-per_week(singles[s], s), s))[: config.scoring.counterfactual.max_candidates]

    chosen: list[str] = []
    current = base.raw_score
    while len(chosen) < steps and not (chosen and reaches(chosen)):
        options_left = []
        for skill_id in pool:
            if skill_id in chosen:
                continue
            gain = round(raw((*chosen, skill_id)) - current, _DECIMALS)
            if gain > 0.0:
                options_left.append((-per_week(gain, skill_id), skill_id))
        if not options_left:
            break
        chosen.append(min(options_left)[1])
        current = raw(chosen)

    improvements: list[Improvement] = []
    if chosen and reaches(chosen):
        final = swap_pass(chosen, pool, lambda skills: reaches(skills), costs.__getitem__)
        ordered = tuple(sorted(final, key=lambda s: (costs[s], s)))
        found = rescored(ordered)  # reached, so SCORED with a score and a band
        improvements.append(Improvement(
            skill_ids=ordered, new_raw_score=found.raw_score, new_score=found.calibrated_score,
            new_band=found.band, learning_cost=sum(costs[s] for s in ordered),
            text=_text(ordered, graph, found.calibrated_score, found.band, base.band),
        ))

    single_options = []
    for skill_id in useful:
        found = rescored((skill_id,))
        if found is None or found.calibrated_score is None or found.calibrated_score <= base.calibrated_score:
            continue
        if improvements and improvements[0].skill_ids == (skill_id,):
            continue
        single_options.append((-found.calibrated_score, costs[skill_id], skill_id, found))
    for _, cost, skill_id, found in sorted(single_options, key=lambda item: item[:3])[:MAX_SINGLES]:
        improvements.append(Improvement(
            skill_ids=(skill_id,), new_raw_score=found.raw_score, new_score=found.calibrated_score,
            new_band=found.band, learning_cost=cost,
            text=_text((skill_id,), graph, found.calibrated_score, found.band, base.band),
        ))
    return tuple(improvements)
