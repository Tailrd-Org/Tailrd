"""The explanation every result carries (SPEC §8, §7 naming, §17.2, §18; compliance.md §2).

`build_explanation(result, resume, posting, prepared, graph, config, improvements)`:

* Headline. SCORED: "Fit score 76 · Good match" (`bands.display.label`), or "Fit score 71–79 · Good
  match" when the interval is displayed. SCORED_LOW_CONFIDENCE: the band only. GATE_FAIL: "Not
  eligible: " + the sentence of the first failing gate. NEEDS_REVIEW: "We couldn't score this"
  (compliance.md §2). Never a percent sign after the number. `score` and `basis` (`bands.basis[scope]` with the
  score) only when a score is displayed, i.e. SCORED; `band` for SCORED and low confidence.
* Strengths (SCORED and low confidence; at most four, by contribution): "k of n required skills",
  "k of n preferred skills" (k ≥ 1; the note names them, a skill credited through a related one as
  "Terraform (through your AWS experience)", never as held), "x yrs relevant vs y required" ("vs a
  typical y" when the minimum came from the level) when the minimum is met, and seniority,
  education (when the posting states a requirement) and industry when they scored at least 0.8.
  No work experience against a stated minimum of zero is "This role doesn't require prior work
  experience". The required-skills line is always kept when there is one, so every REQUIRED group is named in a
  strength or a gap.
* Gaps (same outcomes): every REQUIRED and PREFERRED group with m < 0.5 (`gaps.find_gaps`):
  "Missing: Terraform (required)" or "Partly covered: …", with a note naming the résumé skills that
  partly cover it; then an experience shortfall, a seniority gap (the role is above the candidate's
  level) and an education shortfall. Ordered by impact, most negative first.
* Impacts ("+0.31", raw scale) are shown on SCORED results only: on the other outcomes the score is
  suppressed, and a column of raw contributions would be the number under another name.
* Notes: one plain sentence per low-confidence reason, gate warning, binding cap, résumé-side
  abstention (DATES_MISSING, FUNCTIONAL_RESUME) and informational flag (`FLAG_TEXT`; not MIN_YEARS_DEFAULTED, nor
  NO_WORK_EXPERIENCE when an experience item says it). LOW_EVIDENCE_DENSITY names the skills only
  listed in the skills section, LOW_DIMENSION_COVERAGE the parts not assessed (DECISIONS D-56). OVERQUALIFIED
  is a note and never a gap. A cap is only ever mentioned when it bound (`MatchResult.caps`).
  NEEDS_REVIEW lists its reasons; GATE_FAIL its other reasons and warnings, and that the facts can
  be updated.
* `to_improve`: the texts of `improvements`.

Total: with `strict=False` (production) a defect while building the detail falls back to the
headline (a scored result's band at worst) and the reason sentences, so every result still gets an explanation; `strict=True` (tests)
raises it. Deterministic and pure.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.explain.gaps import Gap, find_gaps, group_facts
from matcher.explain.labels import flag_sentence, join_labels, signed, skill_label, years
from matcher.graph.model import SkillGraph
from matcher.models import (
    Band,
    DegreeLevel,
    DimensionName,
    DimensionScore,
    Evidence,
    Explanation,
    ExplanationItem,
    Fact,
    GateStatus,
    Improvement,
    JobPosting,
    MatchResult,
    Necessity,
    Outcome,
    RelationKind,
    Resume,
)
from matcher.score.coverage import REQ_MATCH
from matcher.score.prepare import PreparedResume, prepare_resume

__all__ = ["MAX_STRENGTHS", "NEEDS_REVIEW_HEADLINE", "build_explanation", "headline"]

MAX_STRENGTHS = 4
STRONG_DIMENSION = 0.8  # seniority / education / industry count as a strength from here
NEEDS_REVIEW_HEADLINE = "We couldn't score this"  # compliance.md §2, verbatim
NOT_ELIGIBLE = "Not eligible: "
LIMITED_INFORMATION = "We're showing a band instead of a number because some information was limited."
GATE_FACTS = ("This is based on the details in your profile. If they've changed, update them "
              "and we'll check again.")
_MAX_NAMED = 6  # skills named in one note before "and N more"
_RESUME_ABSTENTIONS = (Flag.DATES_MISSING, Flag.FUNCTIONAL_RESUME)
_MAX_LISTED = 3  # skills named in the "only listed" note before "and N more"
# Informational flags never shown as notes: the posting's own business, already said by an item.
_SILENT_FLAGS = frozenset({Flag.MIN_YEARS_DEFAULTED})
_DIMENSION_TEXT = {
    DimensionName.REQUIRED_SKILLS: "required skills",
    DimensionName.PREFERRED_SKILLS: "preferred skills",
    DimensionName.EXPERIENCE: "experience",
    DimensionName.SENIORITY: "level",
    DimensionName.DOMAIN: "industry",
    DimensionName.EDUCATION: "education",
    DimensionName.TOOLS: "other keywords",
}
_DEGREE_TEXT = {
    DegreeLevel.HIGH_SCHOOL: "a high school diploma",
    DegreeLevel.ASSOCIATE: "a college diploma or associate degree",
    DegreeLevel.BACHELOR: "a bachelor's degree",
    DegreeLevel.MASTER: "a master's degree",
    DegreeLevel.DOCTORATE: "a doctorate",
}


# ─── Headline, score, basis ───────────────────────────────────────────────────────────────


def headline(result: MatchResult, config: EngineConfig) -> str:
    """The one line shown first (see the module docstring)."""
    outcome = Outcome(result.outcome)
    if outcome is Outcome.SCORED and result.display_score is not None and result.band is not None:
        label = config.bands.display.get("label", "Fit score")
        number = str(result.display_score)
        if result.interval is not None and result.interval.display:
            number = f"{result.interval.low}–{result.interval.high}"
        return f"{label} {number} · {Band(result.band).value}"
    if outcome is Outcome.SCORED_LOW_CONFIDENCE and result.band is not None:
        return Band(result.band).value
    if outcome is Outcome.GATE_FAIL:
        failed = [gate.reason for gate in result.gates if gate.status is GateStatus.FAIL and gate.reason]
        reason = failed[0] if failed else (result.reasons[0] if result.reasons else None)
        return NOT_ELIGIBLE + (flag_sentence(reason) if reason is not None else "this role has a requirement you don't meet.")
    return NEEDS_REVIEW_HEADLINE


def _basis(result: MatchResult, config: EngineConfig) -> str:
    if Outcome(result.outcome) is not Outcome.SCORED or result.display_score is None or result.calibration is None:
        return ""
    template = config.bands.basis.get(result.calibration.scope, "")
    try:
        return template.format(score=result.display_score)
    except (KeyError, IndexError, ValueError):
        return template


# ─── Notes ────────────────────────────────────────────────────────────────────────────────


def _cap_note(result: MatchResult, graph: SkillGraph) -> list[str]:
    notes = []
    for cap in result.caps:
        if Flag(cap.reason) is Flag.CRITICAL_MISS and cap.requirement_ids:
            names = join_labels([skill_label(graph, skill_id) for skill_id in cap.requirement_ids])
            if len(cap.requirement_ids) == 1:
                notes.append(f"Missing {names}, a skill this role centres on, limits this score.")
            else:
                notes.append(f"Missing {names}, skills this role centres on, limits this score.")
        else:
            notes.append(flag_sentence(cap.reason))
    return notes


def _only_listed(result: MatchResult, prepared: Optional[PreparedResume], graph: SkillGraph) -> str:
    """LOW_EVIDENCE_DENSITY, said from the data: the résumé skills behind a matched requirement
    whose only affirmed evidence is the skills section (what D-44's density counts as not shown)."""
    listed: list[str] = []
    if prepared is not None:
        for dimension in result.dimensions.values():
            for fact in group_facts(dimension.facts):
                via = fact.detail.get("via")
                if not (isinstance(via, str) and (fact.value or 0.0) > 0.0) or via in listed:
                    continue
                if prepared.affirmed_evidence.get(via) is Evidence.SKILLS_BLOB:
                    listed.append(via)
    if not listed:
        return ("Some matching skills are only listed in your skills section; showing them in a "
                "project or role would count for more.")
    labels = sorted(skill_label(graph, skill_id) for skill_id in listed)
    if len(labels) > _MAX_LISTED:
        labels = [*labels[:_MAX_LISTED], f"{len(labels) - _MAX_LISTED} more"]
    if len(listed) == 1:
        return (f"{labels[0]} is only listed in your skills section; showing it in a project or role "
                "would count for more.")
    return (f"{join_labels(labels)} are only listed in your skills section; showing them in a project "
            "or role would count for more.")


def _not_assessed(result: MatchResult) -> str:
    """LOW_DIMENSION_COVERAGE, naming the parts that abstained (§5.1's weight moved to §5.2 for a
    posting with no required skills counts as assessed, D-42)."""
    names = []
    for name in DimensionName:
        dimension = result.dimensions.get(name.value)
        if dimension is None or not dimension.abstained:
            continue
        if name is DimensionName.REQUIRED_SKILLS and dimension.abstain_reason == Flag.NO_REQUIRED_REQUIREMENTS:
            continue
        names.append(_DIMENSION_TEXT[name])
    if not names:
        return flag_sentence(Flag.LOW_DIMENSION_COVERAGE)
    return f"We couldn't assess {join_labels(names)} for this match."


def reason_sentence(reason: Flag, result: MatchResult, prepared: Optional[PreparedResume], graph: SkillGraph) -> str:
    """The note for one reason code: FLAG_TEXT, except the two said from the data."""
    if reason == Flag.LOW_EVIDENCE_DENSITY:
        return _only_listed(result, prepared, graph)
    if reason == Flag.LOW_DIMENSION_COVERAGE:
        return _not_assessed(result)
    return flag_sentence(reason)


def _notes(result: MatchResult, graph: SkillGraph, prepared: Optional[PreparedResume] = None,
           shown_codes: frozenset[str] = frozenset()) -> list[str]:
    outcome = Outcome(result.outcome)
    notes: list[str] = []
    if outcome is Outcome.NEEDS_REVIEW:
        notes.extend(reason_sentence(reason, result, prepared, graph) for reason in result.reasons)
        return _unique(notes)
    if outcome is Outcome.GATE_FAIL:
        failed = [gate.reason for gate in result.gates if gate.status is GateStatus.FAIL and gate.reason]
        notes.extend(flag_sentence(reason) for reason in failed[1:])
        notes.extend(flag_sentence(reason) for reason in result.reasons if reason not in failed)
    if outcome is Outcome.SCORED_LOW_CONFIDENCE:
        notes.append(LIMITED_INFORMATION)
        notes.extend(reason_sentence(reason, result, prepared, graph) for reason in result.reasons)
    notes.extend(flag_sentence(gate.reason) for gate in result.gates if gate.status is GateStatus.WARN and gate.reason)
    notes.extend(_cap_note(result, graph))
    for dimension in result.dimensions.values():
        if dimension.abstained and dimension.abstain_reason in _RESUME_ABSTENTIONS:
            notes.append(flag_sentence(dimension.abstain_reason))
    for flag in result.flags:
        if flag in _SILENT_FLAGS:
            continue
        if flag == Flag.NO_WORK_EXPERIENCE and shown_codes & {"EXPERIENCE", "EXPERIENCE_SHORT"}:
            continue  # the experience item already says so
        notes.append(flag_sentence(flag))
    if outcome is Outcome.GATE_FAIL:
        notes.append(GATE_FACTS)
    return _unique(notes)


# ─── Strengths and gaps ───────────────────────────────────────────────────────────────────


def _named(labels: Sequence[str]) -> str:
    if len(labels) <= _MAX_NAMED:
        return join_labels(labels)
    shown = list(labels[: _MAX_NAMED - 1])
    return join_labels([*shown, f"{len(labels) - len(shown)} more"])


def _group_label(graph: SkillGraph, fact: Fact) -> str:
    """One requirement group: its best member, or "A or B" for an OR-group."""
    raw = fact.detail.get("members")
    members = [part for part in raw.split(",") if part] if isinstance(raw, str) else []
    if len(members) > 1:
        return " or ".join(skill_label(graph, member) for member in members)
    return skill_label(graph, fact.subject)


def _held_label(graph: SkillGraph, fact: Fact) -> str:
    """A matched requirement as the candidate holds it: its own name when the résumé shows it (or
    one of its OR-group members), "Terraform (through your AWS experience)" when a related skill
    earned the credit. Never names an unheld skill as held."""
    label = skill_label(graph, fact.subject)
    via = fact.detail.get("via")
    raw = fact.detail.get("members")
    members = {part for part in raw.split(",") if part} if isinstance(raw, str) else set()
    if isinstance(via, str) and via and via != fact.subject and via not in members:
        return f"{label} (through your {skill_label(graph, via)} experience)"
    return label


def _coverage_strength(dimension: Optional[DimensionScore], graph: SkillGraph, word: str, code: str) -> Optional[tuple[float, ExplanationItem]]:
    if dimension is None or dimension.abstained:
        return None
    groups = group_facts(dimension.facts)
    met = [fact for fact in groups if fact.code == REQ_MATCH]
    if not met:
        return None
    note = _named([_held_label(graph, fact) for fact in met])
    item = ExplanationItem(label=f"{len(met)} of {len(groups)} {word} skills", impact=signed(dimension.contribution),
                           note=note, code=code)
    return dimension.contribution, item


def _first(dimension: Optional[DimensionScore], code: str) -> Optional[Fact]:
    if dimension is None:
        return None
    return next((fact for fact in dimension.facts if fact.code == code), None)


def _number(value: Any, default: float = 0.0) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def _experience(dimension: Optional[DimensionScore]) -> tuple[Optional[tuple[float, ExplanationItem]], Optional[tuple[float, ExplanationItem]]]:
    """(strength, gap) of §5.3: met → strength, short → gap."""
    summary = _first(dimension, "RELEVANT_MONTHS")
    if dimension is None or dimension.abstained or summary is None:
        return None, None
    months = _number(summary.value)
    minimum = _number(summary.detail.get("min_months"))
    typical = summary.subject == "SENIORITY_DEFAULT"
    if _first(dimension, Flag.NO_WORK_EXPERIENCE.value) is not None and minimum <= 0.0:
        item = ExplanationItem(label="This role doesn't require prior work experience",
                               impact=signed(dimension.contribution), code="EXPERIENCE")
        return (dimension.contribution, item), None
    if minimum <= 0.0:
        label = f"{years(months)} yrs relevant (none required)"
    elif _first(dimension, Flag.NO_WORK_EXPERIENCE.value) is not None:
        asks = f"typical for this level: {years(minimum)} yrs" if typical else f"this role asks for {years(minimum)} yrs"
        label = f"No work experience listed ({asks})"
    else:
        versus = f"vs a typical {years(minimum)}" if typical else f"vs {years(minimum)} required"
        label = f"{years(months)} yrs relevant {versus}"
    value = dimension.value or 0.0
    if months >= minimum:
        return (dimension.contribution, ExplanationItem(label=label, impact=signed(dimension.contribution), code="EXPERIENCE")), None
    impact = -(dimension.effective_weight * (1.0 - value))
    return None, (impact, ExplanationItem(label=label, impact=signed(impact), code="EXPERIENCE_SHORT"))


def _seniority(dimension: Optional[DimensionScore]) -> tuple[Optional[tuple[float, ExplanationItem]], Optional[tuple[float, ExplanationItem]]]:
    fact = _first(dimension, "SENIORITY_GAP")
    if dimension is None or dimension.abstained or fact is None:
        return None, None
    value = dimension.value or 0.0
    if value >= STRONG_DIMENSION:
        item = ExplanationItem(label="Your level fits this role", impact=signed(dimension.contribution), code="SENIORITY")
        return (dimension.contribution, item), None
    gap = int(round(_number(fact.value)))
    if gap > 0:
        levels = "one level" if gap == 1 else f"{gap} levels"
        label = f"This role is {levels} above your current one"
    else:
        label = "This role is on a different career track from your current one"
    impact = -(dimension.effective_weight * (1.0 - value))
    return None, (impact, ExplanationItem(label=label, impact=signed(impact), code="SENIORITY_GAP"))


def _education(dimension: Optional[DimensionScore], graph: SkillGraph) -> tuple[Optional[tuple[float, ExplanationItem]], Optional[tuple[float, ExplanationItem]]]:
    level = _first(dimension, "DEGREE_LEVEL")
    if dimension is None or dimension.abstained or level is None:
        return None, None  # no stated requirement: nothing earned, nothing missing
    value = dimension.value or 0.0
    certificates = [fact.subject for fact in dimension.facts if fact.code == "CERT_BONUS" and _number(fact.value) > 0]
    if value >= STRONG_DIMENSION:
        note = ""
        if certificates:
            note = "Relevant: " + join_labels([skill_label(graph, cert) for cert in certificates])
        item = ExplanationItem(label="Your education meets this role's requirement",
                               impact=signed(dimension.contribution), note=note, code="EDUCATION")
        return (dimension.contribution, item), None
    impact = -(dimension.effective_weight * (1.0 - value))
    shortfall = int(_number(level.detail.get("shortfall")))
    if shortfall > 0:
        try:
            wanted = _DEGREE_TEXT.get(DegreeLevel(level.subject), "a higher degree")
        except ValueError:
            wanted = "a higher degree"
        label = f"This role asks for {wanted}"
    else:
        label = "Your field of study differs from the one this role asks for"
    return None, (impact, ExplanationItem(label=label, impact=signed(impact), code="EDUCATION_SHORT"))


def _industry(dimension: Optional[DimensionScore]) -> Optional[tuple[float, ExplanationItem]]:
    if dimension is None or dimension.abstained or (dimension.value or 0.0) < STRONG_DIMENSION:
        return None
    item = ExplanationItem(label="Experience in this industry", impact=signed(dimension.contribution), code="INDUSTRY")
    return dimension.contribution, item


def _skill_gap(gap: Gap, graph: SkillGraph) -> tuple[float, ExplanationItem]:
    names = [skill_label(graph, member) for member in (gap.members or (gap.requirement_id,))]
    what = " or ".join(names) if len(names) > 1 else names[0]
    necessity = "required" if gap.necessity is Necessity.REQUIRED else "preferred"
    state = "Missing" if gap.m <= 0.0 else "Partly covered"
    note = ""
    if gap.covered_by:
        note = f"Your {join_labels([skill_label(graph, s) for s in gap.covered_by[:3]])} experience partially covers this"
    elif gap.m > 0.0 and gap.relation is RelationKind.EXACT:
        note = "It's on your résumé, but showing it in a recent role would count for more"
    code = "REQ_MISS" if gap.m <= 0.0 else "REQ_PARTIAL"
    if gap.necessity is Necessity.PREFERRED:
        code = "PREF_" + code.split("_", 1)[1]
    return gap.impact, ExplanationItem(label=f"{state}: {what} ({necessity})", impact=signed(gap.impact),
                                       note=note, code=code)


def _breakdown(
    result: MatchResult,
    posting: Optional[JobPosting],
    prepared: Optional[PreparedResume],
    graph: SkillGraph,
    config: EngineConfig,
) -> tuple[tuple[ExplanationItem, ...], tuple[ExplanationItem, ...]]:
    dims = result.dimensions
    get = lambda name: dims.get(name.value)  # noqa: E731
    strengths: list[tuple[float, int, ExplanationItem]] = []
    gaps: list[tuple[float, int, ExplanationItem]] = []

    def add(target: list, entry: Optional[tuple[float, ExplanationItem]]) -> None:
        if entry is not None:
            target.append((entry[0], len(target), entry[1]))

    required = _coverage_strength(get(DimensionName.REQUIRED_SKILLS), graph, "required", "REQUIRED_SKILLS")
    add(strengths, _coverage_strength(get(DimensionName.PREFERRED_SKILLS), graph, "preferred", "PREFERRED_SKILLS"))
    for strength, gap in (_experience(get(DimensionName.EXPERIENCE)), _seniority(get(DimensionName.SENIORITY)),
                          _education(get(DimensionName.EDUCATION), graph)):
        add(strengths, strength)
        add(gaps, gap)
    add(strengths, _industry(get(DimensionName.DOMAIN)))
    for gap in find_gaps(result, posting, prepared, graph, config):
        add(gaps, _skill_gap(gap, graph))

    ranked = sorted(strengths, key=lambda entry: (-round(entry[0], 12), entry[1]))
    if required is not None:
        ranked = [(required[0], -1, required[1]), *ranked][:MAX_STRENGTHS]
        ranked.sort(key=lambda entry: (-round(entry[0], 12), entry[1]))
    else:
        ranked = ranked[:MAX_STRENGTHS]
    ordered_gaps = sorted(gaps, key=lambda entry: (round(entry[0], 12), entry[1]))
    return tuple(entry[2] for entry in ranked), tuple(entry[2] for entry in ordered_gaps)


def _without_impact(items: Sequence[ExplanationItem]) -> tuple[ExplanationItem, ...]:
    return tuple(item.model_copy(update={"impact": ""}) for item in items)


# ─── Entry point ──────────────────────────────────────────────────────────────────────────


def build_explanation(
    result: MatchResult,
    resume: Optional[Resume],
    posting: Optional[JobPosting],
    prepared: Optional[PreparedResume],
    graph: SkillGraph,
    config: EngineConfig,
    improvements: Sequence[Improvement] = (),
    *,
    strict: bool = False,
) -> Explanation:
    """The SPEC §8 explanation of `result` (see the module docstring). Never raises unless `strict`."""
    try:
        return _build(result, resume, posting, prepared, graph, config, improvements)
    except Exception:
        if strict:
            raise
        return _fallback(result, config)


def _build(
    result: MatchResult,
    resume: Optional[Resume],
    posting: Optional[JobPosting],
    prepared: Optional[PreparedResume],
    graph: SkillGraph,
    config: EngineConfig,
    improvements: Sequence[Improvement],
) -> Explanation:
    outcome = Outcome(result.outcome)
    strengths: tuple[ExplanationItem, ...] = ()
    gaps: tuple[ExplanationItem, ...] = ()
    if prepared is None and resume is not None and result.dimensions:  # the notes read it too
        prepared = prepare_resume(resume, graph, config, result.as_of)
    if outcome in (Outcome.SCORED, Outcome.SCORED_LOW_CONFIDENCE) and result.dimensions:
        strengths, gaps = _breakdown(result, posting, prepared, graph, config)
        if outcome is not Outcome.SCORED:
            strengths, gaps = _without_impact(strengths), _without_impact(gaps)
    shown = outcome is Outcome.SCORED
    return Explanation(
        band=result.band if outcome in (Outcome.SCORED, Outcome.SCORED_LOW_CONFIDENCE) else None,
        score=result.display_score if shown else None,
        basis=_basis(result, config),
        headline=headline(result, config),
        strengths=strengths,
        gaps=gaps,
        to_improve=tuple(improvement.text for improvement in improvements) if shown else (),
        notes=tuple(_notes(result, graph, prepared, frozenset(item.code for item in (*strengths, *gaps)))),
    )


def _fallback(result: MatchResult, config: EngineConfig) -> Explanation:
    """The headline and the reason sentences: what is always safe to say."""
    outcome = Outcome(result.outcome)
    shown = outcome is Outcome.SCORED
    try:
        line = headline(result, config)
    except Exception:
        if outcome in (Outcome.SCORED, Outcome.SCORED_LOW_CONFIDENCE) and result.band is not None:
            line = Band(result.band).value
        elif outcome is Outcome.GATE_FAIL:
            line = NOT_ELIGIBLE.rstrip(": ")
        else:
            line = NEEDS_REVIEW_HEADLINE
    return Explanation(
        band=result.band if outcome in (Outcome.SCORED, Outcome.SCORED_LOW_CONFIDENCE) else None,
        score=result.display_score if shown else None,
        headline=line,
        notes=tuple(_unique(flag_sentence(reason) for reason in result.reasons)),
    )


def _unique(items) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))
