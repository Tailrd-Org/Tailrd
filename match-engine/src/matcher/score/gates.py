"""Gates: eligibility, evaluated before scoring and never part of the weighted sum (SPEC §4).

Policy: DECISIONS D-60..D-64 and config/gates.yaml; licence evidence: taxonomy DECISIONS D-28.
`evaluate_gates` returns the overall status (FAIL if any gate fails, else WARN if any warns, else
PASS; PASS for a posting without gates) and one `GateResult` per posting gate, grouped by kind in
SPEC §4's order (work authorisation, licence, clearance, location), the posting's order within a
kind. A gate of a disabled kind passes. `reason` names the rule that fired, also when a configured
status made it a PASS; `detail` says what the posting asks, never anything about the candidate.

Work authorisation: the D-61 decision table (D-60: an unknown status never fails). C, the posting's
countries, is the gate's `countries`, else the countries of the posting's `locations` ("We are
unable to sponsor" names none); it may be empty. A, where the candidate may work, is
`authorized_countries` and `citizenships` (a citizenship implies authorisation, never the reverse).
A country missing from A is no evidence on its own: a FAIL needs a stated sponsorship need or a
stated citizenship that misses a citizenship requirement. One row decides; every WARN of the table
is an unknown and takes `unknown_status`.
0. Nothing stated (no `work_auth`, or no country, citizenship or sponsorship need) → WARN
   WORK_AUTH_UNKNOWN.
1. Citizenship required: a citizenship in C → PASS; known citizenships missing C → FAIL
   CITIZENSHIP_REQUIRED; no known citizenship, or C empty → WARN CITIZENSHIP_REQUIRED.
2. A meets C → PASS ("Canadian citizen; needs sponsorship for US roles" passes in Canada).
3. Otherwise: the posting sponsors → PASS. The posting refuses sponsorship, or its gate names
   countries: needs sponsorship → FAIL (SPONSORSHIP_UNAVAILABLE when sponsorship is refused, else
   WORK_AUTH_MISMATCH), but WARN SPONSORSHIP_UNAVAILABLE when C is empty and A is not (the job may be
   where they are authorised); no sponsorship needed → PASS; need unknown → WARN WORK_AUTH_MISMATCH
   (WORK_AUTH_UNKNOWN when C is empty). A gate with no country and no sponsorship policy → PASS.

Licence (D-60, D-62; D-28). The candidate holds the credentials of their AFFIRMED mentions with
CERTIFICATION or ROLE_TITLE evidence, in any section: never a bullet ("administered medications
under RN supervision"), a skills-list entry, a negated or an aspirational mention. A gate passes
when a held credential is one it lists or sits below one on the is-a chain, at any depth (the
graph's parent links: eligibility facts, not the scoring knob `relations.max_depth`); never through
a related edge. The walk up never passes an in-progress designation
(`license.in_progress_credential_ids`, an explicit list): a designation, or anything below it,
meets only a gate that lists the designation itself ("P.Eng. or EIT"). Otherwise FAIL
LICENSE_MISSING when the gate lists a professional licence (`license.professional_credential_ids`:
the config alone decides which credentials are gated licences, never the graph), else WARN
LICENSE_UNVERIFIED ("valid driver's licence").

Security clearance (D-60, D-63). Tokens are compared upper-case with each run of other characters
read as one underscore. A held clearance meets the gate's level when it is the same token or at or
above it on the same configured ladder (`security_clearance.ladders`, lowest first: "Top Secret"
meets "Secret"); a token on no ladder matches only itself; a gate that states no level takes any
clearance. Not met: the configured unknown-level token (an active clearance of unknown country and
level, all Tailrd stores) meets an "able to obtain" gate and gives WARN CLEARANCE_UNVERIFIED against
an active one, never a FAIL; otherwise an active clearance required → `active_missing_status` (FAIL)
CLEARANCE_MISSING, "able to obtain" → `obtainable_status` (WARN) CLEARANCE_OBTAINABLE.

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

import re
from collections.abc import Iterable, Sequence
from itertools import chain
from typing import NamedTuple, Optional, Union

from matcher.config import EngineConfig
from matcher.errors import Flag
from matcher.graph.model import SkillGraph
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
_PASS = GateStatus.PASS


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
        return _work_authorization(gate, posting, resume.work_auth, config)
    if isinstance(gate, LicenseGate):
        return _licence(gate, resume, graph, config)
    if isinstance(gate, ClearanceGate):
        return _clearance(gate, resume.work_auth, config)
    return _location(gate, resume, posting, config)


def _result(kind: GateKind, detail: str, status: GateStatus = _PASS, reason: Optional[Flag] = None) -> GateResult:
    """One gate's result; a PASS by evidence carries no reason. A long detail is cut, never refused."""
    if len(detail) > _MAX_DETAIL:
        detail = detail[: _MAX_DETAIL - 1] + "…"
    return GateResult(kind=kind, status=status, reason=reason, detail=detail)


# ─── Work authorisation ─────────────────────────────────────────────────────────────────────


def _work_authorization(
    gate: WorkAuthGate, posting: JobPosting, work_auth: Optional[WorkAuth], config: EngineConfig
) -> GateResult:
    """DECISIONS D-61: one row of the decision table decides; every WARN is an unknown (D-60)."""
    kind = GateKind.WORK_AUTHORIZATION
    unknown = GateStatus(config.gates.work_authorization.unknown_status)
    named = frozenset(gate.countries)
    located = frozenset(country for country in (_part(place.country) for place in posting.locations) if country)
    countries = named or located  # C: the gate's countries, else the posting's locations'
    parts = []
    if named:
        parts.append("countries=" + ",".join(sorted(named)))
    elif located:
        parts.append("location countries=" + ",".join(sorted(located)))
    if gate.sponsorship_available is not None:
        parts.append("sponsorship=" + ("yes" if gate.sponsorship_available else "no"))
    if gate.citizenship_required:
        parts.append("citizenship=required")
    detail = "; ".join(parts) or "work authorisation required"
    if work_auth is None or not (
        work_auth.authorized_countries or work_auth.citizenships or work_auth.needs_sponsorship is not None
    ):
        return _result(kind, detail, unknown, Flag.WORK_AUTH_UNKNOWN)  # 0. nothing stated
    citizenships = frozenset(work_auth.citizenships)
    authorised = frozenset(work_auth.authorized_countries) | citizenships  # A
    if gate.citizenship_required:  # 1.
        if not countries or not citizenships:
            return _result(kind, detail, unknown, Flag.CITIZENSHIP_REQUIRED)
        if not citizenships.isdisjoint(countries):
            return _result(kind, detail)
        return _result(kind, detail, GateStatus.FAIL, Flag.CITIZENSHIP_REQUIRED)  # stated, non-matching
    if not authorised.isdisjoint(countries):  # 2. authorised in a posting country
        return _result(kind, detail)
    # 3. Not established as authorised there: a country missing from A is no evidence on its own.
    if gate.sponsorship_available is True:
        return _result(kind, detail)  # the employer sponsors
    if gate.sponsorship_available is False or named:
        needs = work_auth.needs_sponsorship
        if needs is True:
            if not countries and authorised:  # the job may be where they are authorised
                return _result(kind, detail, unknown, Flag.SPONSORSHIP_UNAVAILABLE)
            refused = gate.sponsorship_available is False
            return _result(kind, detail, GateStatus.FAIL,
                           Flag.SPONSORSHIP_UNAVAILABLE if refused else Flag.WORK_AUTH_MISMATCH)
        if needs is False:
            return _result(kind, detail)  # they state no sponsorship is needed
        return _result(kind, detail, unknown, Flag.WORK_AUTH_MISMATCH if countries else Flag.WORK_AUTH_UNKNOWN)
    return _result(kind, detail)  # no country named and no sponsorship policy stated


# ─── Licences ───────────────────────────────────────────────────────────────────────────────


def _licence(gate: LicenseGate, resume: Resume, graph: SkillGraph, config: EngineConfig) -> GateResult:
    kind = GateKind.LICENSE
    wanted = frozenset(gate.credential_ids)
    detail = "credentials=" + ",".join(sorted(wanted))
    if _shows_any(wanted, resume, graph, config):
        return _result(kind, detail)
    if not wanted.isdisjoint(config.gates.license.professional_credential_ids):
        return _result(kind, detail, GateStatus.FAIL, Flag.LICENSE_MISSING)
    return _result(kind, detail, GateStatus.WARN, Flag.LICENSE_UNVERIFIED)


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
    in_progress = frozenset(config.gates.license.in_progress_credential_ids)
    return any(_reaches(credential, wanted, in_progress, graph) for credential in sorted(_held_credentials(resume)))


def _reaches(credential: str, wanted: frozenset[str], in_progress: frozenset[str], graph: SkillGraph) -> bool:
    """`credential` is a listed id, or sits below one on the is-a chain (any depth: is-a links are
    eligibility facts, not the scoring knob `relations.max_depth`). The walk never climbs past an
    in-progress designation, which matches only a gate that lists it (D-28, D-62). Related edges are
    never followed; `seen` makes a parent cycle harmless."""
    seen = {credential}
    stack = [credential]
    while stack:
        node_id = stack.pop()
        if node_id in wanted:
            return True
        node = graph.node(node_id)
        if node is None or node_id in in_progress:
            continue
        for parent in node.parents:
            if parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return False


# ─── Security clearance ─────────────────────────────────────────────────────────────────────


def _clearance_token(text: str) -> str:
    """Upper case, each run of other characters one underscore: "us ts/sci" reads "US_TS_SCI"."""
    return re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")


def _meets(held: str, required: str, ladders: Sequence[Sequence[str]]) -> bool:
    """The same token, or one at or above it on the same ladder (lowest first, D-63)."""
    if held == required:
        return True
    for ladder in ladders:
        if held in ladder and required in ladder:
            return ladder.index(held) >= ladder.index(required)
    return False


def _clearance(gate: ClearanceGate, work_auth: Optional[WorkAuth], config: EngineConfig) -> GateResult:
    kind = GateKind.SECURITY_CLEARANCE
    rules = config.gates.security_clearance
    level = _clearance_token(gate.level)
    held = frozenset(filter(None, map(_clearance_token, work_auth.clearances))) if work_auth is not None else frozenset()
    detail = f"level={level or 'unstated'}; " + ("active" if gate.active_required else "obtainable")
    if held and (not level or any(_meets(token, level, rules.ladders) for token in held)):
        return _result(kind, detail)
    if rules.unknown_level_token in held:  # an active clearance of unknown country and level
        if not gate.active_required:
            return _result(kind, detail)
        return _result(kind, detail, GateStatus.WARN, Flag.CLEARANCE_UNVERIFIED)  # unknown: never FAIL
    if gate.active_required:
        return _result(kind, detail, GateStatus(rules.active_missing_status), Flag.CLEARANCE_MISSING)
    return _result(kind, detail, GateStatus(rules.obtainable_status), Flag.CLEARANCE_OBTAINABLE)


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
        return _result(kind, detail)  # nothing to compare: unknown → PASS
    if all(_same_area(own, wanted) is False for own in mine for wanted in theirs):
        return _result(kind, detail, GateStatus(config.gates.location.mismatch_status), Flag.LOCATION_MISMATCH)
    return _result(kind, detail)
