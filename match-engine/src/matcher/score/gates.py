"""Gates: eligibility, evaluated before scoring and never part of the weighted sum (SPEC §4).

Policy: DECISIONS D-60 and config/gates.yaml; licence evidence: taxonomy DECISIONS D-28.
`evaluate_gates` returns the overall status (FAIL if any gate fails, else WARN if any warns, else
PASS; PASS for a posting without gates) and one `GateResult` per posting gate, grouped by kind in
SPEC §4's order (work authorisation, licence, clearance, location), the posting's order within a
kind. A gate of a disabled kind passes. `reason` names the rule that fired, also when a configured
status made it a PASS; `detail` says what the posting asks, never anything about the candidate.

Work authorisation (D-60: an unknown status never fails, a stated one that contradicts the posting
does). The countries a candidate may work in are `authorized_countries` and `citizenships`.
* No statement (no `work_auth`, or one naming no country, no citizenship and no sponsorship need)
  → `unknown_status` (WARN) WORK_AUTH_UNKNOWN.
* Needs sponsorship, the posting says none, and not authorised in any posting country → FAIL
  SPONSORSHIP_UNAVAILABLE. Authorised in a posting country, the candidate needs no sponsorship for
  this job ("Canadian citizen; will need sponsorship for US roles" meets no contradiction in Canada).
* Citizenship required: a citizenship of a posting country passes; known citizenships that miss
  every posting country → FAIL CITIZENSHIP_REQUIRED; no known citizenship, or no posting country to
  compare with → `unknown_status` CITIZENSHIP_REQUIRED (never a vacuous FAIL).
* Known countries that miss every posting country → WORK_AUTH_MISMATCH: FAIL when the posting says
  no sponsorship, else WARN.
Several findings on one gate: the worst status wins; among equals sponsorship, then citizenship,
then countries.

Licence (D-60; D-28). The candidate holds the credentials of their AFFIRMED mentions with
CERTIFICATION or ROLE_TITLE evidence, in any section: never a bullet ("administered medications
under RN supervision"), a skills-list entry, a negated or an aspirational mention. A gate passes
when a held credential is one it lists, or a descendant of one (within `relations.max_depth`);
never through a related edge. A formal in-progress designation (`license.in_progress_credential_ids`,
an explicit list) never passes as a descendant of a licence; it counts only for a gate that lists
it itself ("P.Eng. or EIT"). Otherwise FAIL LICENSE_MISSING when the gate lists a professional
licence (`license.professional_credential_ids`: the config alone decides which credentials are
gated licences, never the graph), else WARN LICENSE_UNVERIFIED ("valid driver's licence").

Security clearance (D-60). A clearance on the résumé (`work_auth.clearances`) equal to the gate's
level (case and edge spaces ignored; an unstated level takes any clearance) → PASS. Otherwise an
active one required → `active_missing_status` (FAIL) CLEARANCE_MISSING; "able to obtain" →
`obtainable_status` (WARN) CLEARANCE_OBTAINABLE.

Location (SPEC §4: soft, people relocate; D-60: always a warning). Only an onsite gate counts,
against its places (the posting's when it lists none). The candidate is elsewhere only when each of
their known places is known to differ from each posting place: another country, another region, or
another country than a country-wide posting place. The same city (in a compatible country) or the
same country and region is the same area. What cannot be told (a place known only by its country,
a city without its region) is no mismatch. Elsewhere → `mismatch_status` (WARN) LOCATION_MISMATCH;
unknown → PASS.

This is the only `score/` module that reads `work_auth`, `locations` and certifications (the SPEC
§9 forbidden-features test exempts it); nothing it reads enters a score.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import chain
from typing import NamedTuple, Optional, Union

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
from matcher.graph.traversal import ancestors
from matcher.models import (
    ClearanceGate,
    Evidence,
    GateKind,
    GateResult,
    GateStatus,
    JobPosting,
    LicenseGate,
    Location,
    LocationGate,
    Polarity,
    Resume,
    SkillRef,
    WorkAuth,
    WorkAuthGate,
)

__all__ = ["evaluate_gates"]

_KIND_ORDER = {
    GateKind.WORK_AUTHORIZATION: 0,
    GateKind.LICENSE: 1,
    GateKind.SECURITY_CLEARANCE: 2,
    GateKind.LOCATION: 3,
}
_SEVERITY = {GateStatus.PASS: 0, GateStatus.WARN: 1, GateStatus.FAIL: 2}
_LICENCE_EVIDENCE = frozenset({Evidence.CERTIFICATION, Evidence.ROLE_TITLE})
_MAX_DETAIL = 2000  # GateResult.detail's limit; a long posting list is cut, never an error

_Gate = Union[WorkAuthGate, LicenseGate, ClearanceGate, LocationGate]
_Finding = tuple[GateStatus, Flag]


def evaluate_gates(
    resume: Resume, posting: JobPosting, graph: SkillGraph, config: EngineConfig
) -> tuple[GateStatus, tuple[GateResult, ...]]:
    """The overall gate status and one result per posting gate (SPEC §4; DECISIONS D-60)."""
    results = sorted(
        (_evaluate(gate, resume, posting, graph, config) for gate in posting.gates),
        key=lambda result: _KIND_ORDER[result.kind],  # stable: the posting's order within a kind
    )
    overall = max((result.status for result in results), key=_SEVERITY.__getitem__, default=GateStatus.PASS)
    return overall, tuple(results)


def _evaluate(gate: _Gate, resume: Resume, posting: JobPosting, graph: SkillGraph, config: EngineConfig) -> GateResult:
    kind = GateKind(gate.kind)
    sections = config.gates
    enabled = {
        GateKind.WORK_AUTHORIZATION: sections.work_authorization.enabled,
        GateKind.LICENSE: sections.license.enabled,
        GateKind.SECURITY_CLEARANCE: sections.security_clearance.enabled,
        GateKind.LOCATION: sections.location.enabled,
    }[kind]
    if not enabled:
        return GateResult(kind=kind, status=GateStatus.PASS, detail="gate disabled")
    if isinstance(gate, WorkAuthGate):
        return _work_authorization(gate, resume.work_auth, config)
    if isinstance(gate, LicenseGate):
        return _licence(gate, resume, graph, config)
    if isinstance(gate, ClearanceGate):
        return _clearance(gate, resume.work_auth, config)
    return _location(gate, resume, posting, config)


def _result(kind: GateKind, findings: Sequence[_Finding], detail: str) -> GateResult:
    """The worst finding, the first of equals in rule order; PASS without a reason when none fired."""
    if len(detail) > _MAX_DETAIL:
        detail = detail[: _MAX_DETAIL - 1] + "…"
    if not findings:
        return GateResult(kind=kind, status=GateStatus.PASS, detail=detail)
    worst = max(_SEVERITY[status] for status, _ in findings)
    status, reason = next(finding for finding in findings if _SEVERITY[finding[0]] == worst)
    return GateResult(kind=kind, status=status, reason=reason, detail=detail)


# ─── Work authorisation ─────────────────────────────────────────────────────────────────────


def _work_authorization(gate: WorkAuthGate, work_auth: Optional[WorkAuth], config: EngineConfig) -> GateResult:
    kind = GateKind.WORK_AUTHORIZATION
    unknown = GateStatus(config.gates.work_authorization.unknown_status)
    countries = frozenset(gate.countries)
    parts = []
    if countries:
        parts.append("countries=" + ",".join(sorted(countries)))
    if gate.sponsorship_available is not None:
        parts.append("sponsorship=" + ("yes" if gate.sponsorship_available else "no"))
    if gate.citizenship_required:
        parts.append("citizenship=required")
    detail = "; ".join(parts) or "work authorisation required"
    if work_auth is None or not (
        work_auth.authorized_countries or work_auth.citizenships or work_auth.needs_sponsorship is not None
    ):
        return _result(kind, [(unknown, Flag.WORK_AUTH_UNKNOWN)], detail)
    citizenships = frozenset(work_auth.citizenships)
    authorised = frozenset(work_auth.authorized_countries) | citizenships
    authorised_here = not countries.isdisjoint(authorised)
    findings: list[_Finding] = []
    if work_auth.needs_sponsorship is True and gate.sponsorship_available is False and not authorised_here:
        findings.append((GateStatus.FAIL, Flag.SPONSORSHIP_UNAVAILABLE))
    if gate.citizenship_required and countries.isdisjoint(citizenships):
        known = bool(countries) and bool(citizenships)  # known citizenships that miss every posting country
        findings.append((GateStatus.FAIL if known else unknown, Flag.CITIZENSHIP_REQUIRED))
    if countries and authorised and not authorised_here:
        no_sponsorship = gate.sponsorship_available is False
        findings.append((GateStatus.FAIL if no_sponsorship else GateStatus.WARN, Flag.WORK_AUTH_MISMATCH))
    return _result(kind, findings, detail)


# ─── Licences ───────────────────────────────────────────────────────────────────────────────


def _licence(gate: LicenseGate, resume: Resume, graph: SkillGraph, config: EngineConfig) -> GateResult:
    kind = GateKind.LICENSE
    wanted = frozenset(gate.credential_ids)
    detail = "credentials=" + ",".join(sorted(wanted))
    if _shows_any(wanted, resume, graph, config):
        return _result(kind, (), detail)
    if not wanted.isdisjoint(config.gates.license.professional_credential_ids):
        return _result(kind, [(GateStatus.FAIL, Flag.LICENSE_MISSING)], detail)
    return _result(kind, [(GateStatus.WARN, Flag.LICENSE_UNVERIFIED)], detail)


def _mentions(resume: Resume) -> Iterable[SkillRef]:
    return chain(
        resume.certifications,
        resume.skills_blob,
        (ref for role in resume.roles for ref in role.skills),
        (ref for project in resume.projects for ref in project.skills),
    )


def _held_credentials(resume: Resume) -> frozenset[str]:
    """Credentials the résumé shows as held: affirmed certification or title evidence only (D-28)."""
    return frozenset(
        ref.canonical_id
        for ref in _mentions(resume)
        if Evidence(ref.evidence) in _LICENCE_EVIDENCE and Polarity(ref.polarity) is Polarity.AFFIRMED
    )


def _shows_any(wanted: frozenset[str], resume: Resume, graph: SkillGraph, config: EngineConfig) -> bool:
    """A held credential is one the gate lists, or (unless it is an in-progress designation) a
    descendant of one. Related edges never count."""
    in_progress = frozenset(config.gates.license.in_progress_credential_ids)
    depth = config.scoring.relations.max_depth
    for credential in sorted(_held_credentials(resume)):
        if credential in wanted:
            return True
        if credential in in_progress:
            continue  # never a licence through the graph (D-28)
        if not wanted.isdisjoint(ancestors(graph, credential, depth)):
            return True
    return False


# ─── Security clearance ─────────────────────────────────────────────────────────────────────


def _fold(text: str) -> str:
    return text.strip().casefold()


def _clearance(gate: ClearanceGate, work_auth: Optional[WorkAuth], config: EngineConfig) -> GateResult:
    kind = GateKind.SECURITY_CLEARANCE
    rules = config.gates.security_clearance
    level = _fold(gate.level)
    held = frozenset(filter(None, map(_fold, work_auth.clearances))) if work_auth is not None else frozenset()
    detail = f"level={gate.level.strip() or 'unstated'}; " + ("active" if gate.active_required else "obtainable")
    if held and (not level or level in held):
        return _result(kind, (), detail)
    if gate.active_required:
        return _result(kind, [(GateStatus(rules.active_missing_status), Flag.CLEARANCE_MISSING)], detail)
    return _result(kind, [(GateStatus(rules.obtainable_status), Flag.CLEARANCE_OBTAINABLE)], detail)


# ─── Location ───────────────────────────────────────────────────────────────────────────────


class _Place(NamedTuple):
    country: Optional[str]
    region: Optional[str]
    city: Optional[str]


def _part(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    return text.strip() or None


def _place(location: Location) -> Optional[_Place]:
    """The known parts of a place; None when nothing about it is known."""
    place = _Place(_part(location.country), _part(location.region), _part(location.city))
    return place if any(place) else None


def _same_area(mine: _Place, theirs: _Place) -> Optional[bool]:
    """True: the same area at the precision both give; False: known to be elsewhere; None: can't tell."""
    if mine.country and theirs.country and mine.country != theirs.country:
        return False
    if mine.city and theirs.city and mine.city == theirs.city:
        return True
    if mine.region and theirs.region:
        return mine.region == theirs.region
    if theirs.region is None and theirs.city is None:  # a country-wide posting place
        return True if mine.country and theirs.country else None
    return None


def _location(gate: LocationGate, resume: Resume, posting: JobPosting, config: EngineConfig) -> GateResult:
    kind = GateKind.LOCATION
    places = gate.locations or posting.locations
    theirs = [place for place in map(_place, places) if place is not None]
    parts = ["onsite=" + ("required" if gate.onsite_required else "not required")]
    if theirs:
        parts.append("places=" + "|".join("/".join(part for part in place if part) for place in theirs))
    detail = "; ".join(parts)
    mine = [place for place in map(_place, resume.locations) if place is not None]
    if not gate.onsite_required or not theirs or not mine:
        return _result(kind, (), detail)  # nothing to compare: unknown → PASS
    if all(_same_area(own, wanted) is False for own in mine for wanted in theirs):
        return _result(kind, [(GateStatus(config.gates.location.mismatch_status), Flag.LOCATION_MISMATCH)], detail)
    return _result(kind, (), detail)
