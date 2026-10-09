"""Gates: eligibility, evaluated before scoring, never part of the weighted sum (SPEC §4).

Tailrd policy (DECISIONS D-60..D-64, config/gates.yaml): unknown candidate status never fails a
gate, it warns. Work authorisation follows the D-61 decision table: the posting's countries are the
gate's, else those of the posting's locations; a country missing from the candidate's list is no
evidence they can't work there, so a FAIL needs a stated sponsorship need or a stated, non-matching
citizenship. A licence gate fails only for a professional licence (config
`professional_credential_ids`) the résumé does not show, and only CERTIFICATION or ROLE_TITLE
evidence of the licence id (or anything below it on the is-a chain) shows one: never a bullet
mention, never a related edge, never through an in-progress designation (taxonomy DECISIONS D-28;
config `in_progress_credential_ids`). Clearance climbs the configured ladders and fails only for an
active clearance the résumé does not show. Location only ever warns. A disabled gate kind passes.
The overall status is the worst gate's.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from matcher.config import ConfigError, default_config_dir, load_config
from matcher.errors import Flag
from matcher.models import (
    ClearanceGate,
    Evidence,
    GateKind,
    GateStatus,
    JobPosting,
    LicenseGate,
    Location,
    LocationGate,
    Polarity,
    Project,
    Resume,
    Role,
    SkillRef,
    SkillSource,
    VerbClass,
    WorkAuth,
    WorkAuthGate,
)
from matcher.score.gates import evaluate_gates
from tests.builders import tiny_graph, with_gates, with_scoring

PASS, WARN, FAIL = GateStatus.PASS, GateStatus.WARN, GateStatus.FAIL
RN = "cred.registered_nurse"
TAXONOMY_RESOLVER = Path(__file__).resolve().parents[1] / "data" / "taxonomy" / "resolver.json"


def _one(resume: Resume, gate, graph, config, **posting) -> tuple[GateStatus, Flag | None]:
    """(status, reason) of a posting holding just `gate`; the overall status is that gate's."""
    status, results = evaluate_gates(resume, JobPosting(gates=(gate,), **posting), graph, config)
    assert len(results) == 1 and results[0].kind is GateKind(gate.kind)
    assert status is results[0].status
    return results[0].status, results[0].reason


def cert(credential_id: str, *, polarity: Polarity = Polarity.AFFIRMED) -> SkillRef:
    return SkillRef(canonical_id=credential_id, evidence=Evidence.CERTIFICATION, polarity=polarity,
                    source=SkillSource.CERTIFICATION)


def titled(credential_id: str) -> Role:
    """A role whose title resolved to the credential (ROLE_TITLE evidence)."""
    return Role(title_raw="Registered Nurse",
                skills=(SkillRef(canonical_id=credential_id, evidence=Evidence.ROLE_TITLE, source=SkillSource.ROLE),))


# ─── Work authorisation (D-61 decision table) ───────────────────────────────────────────────

TORONTO = Location(country="CA", region="ON", city="Toronto")
OTTAWA = Location(country="CA", region="ON", city="Ottawa")
MONTREAL = Location(country="CA", region="QC", city="Montréal")
SEATTLE = Location(country="US", region="WA", city="Seattle")
LONDON_ON = Location(country="CA", region="ON", city="London")
LONDON_UK = Location(country="GB", region="ENG", city="London")
CANADA = Location(country="CA")

_CA = ("CA",)
_NO_SPONSOR = WorkAuthGate(sponsorship_available=False)  # "We are unable to sponsor": no country named
_NO_SPONSOR_CA = WorkAuthGate(countries=_CA, sponsorship_available=False)
_CITIZEN_CA = WorkAuthGate(countries=_CA, citizenship_required=True)
_CANADIAN_NEEDS_US_SPONSOR = WorkAuth(needs_sponsorship=True, citizenships=_CA)


@pytest.mark.parametrize("work_auth,gate,located,expected", [
    # 0. No statement at all: unknown (D-60), whatever the gate says.
    (None, WorkAuthGate(countries=_CA), (), (WARN, Flag.WORK_AUTH_UNKNOWN)),
    (None, _NO_SPONSOR, (TORONTO,), (WARN, Flag.WORK_AUTH_UNKNOWN)),
    (None, WorkAuthGate(sponsorship_available=True), (), (WARN, Flag.WORK_AUTH_UNKNOWN)),
    (WorkAuth(), _NO_SPONSOR_CA, (), (WARN, Flag.WORK_AUTH_UNKNOWN)),  # a status that states nothing
    (WorkAuth(clearances=("CA_SECRET",)), _NO_SPONSOR_CA, (), (WARN, Flag.WORK_AUTH_UNKNOWN)),
    # The posting's countries: the gate's, else its locations' (review A1, A2, A4).
    (_CANADIAN_NEEDS_US_SPONSOR, _NO_SPONSOR, (TORONTO,), (PASS, None)),  # A1
    (_CANADIAN_NEEDS_US_SPONSOR, _NO_SPONSOR_CA, (TORONTO,), (PASS, None)),  # A2
    (WorkAuth(needs_sponsorship=True, authorized_countries=_CA), _NO_SPONSOR, (SEATTLE,),
     (FAIL, Flag.SPONSORSHIP_UNAVAILABLE)),  # A4: needs sponsorship, in Seattle, none offered
    (WorkAuth(authorized_countries=_CA), _NO_SPONSOR, (SEATTLE,),
     (WARN, Flag.WORK_AUTH_MISMATCH)),  # A4 as probed: the need is unknown
    (WorkAuth(authorized_countries=("US",)), _NO_SPONSOR, (TORONTO, SEATTLE), (PASS, None)),  # any posting country
    # 1. Citizenship required: a match passes, a stated mismatch fails, anything else is unknown.
    (WorkAuth(citizenships=_CA), _CITIZEN_CA, (), (PASS, None)),
    (WorkAuth(citizenships=_CA), WorkAuthGate(citizenship_required=True), (TORONTO,), (PASS, None)),
    (WorkAuth(citizenships=("US",)), WorkAuthGate(countries=("CA", "US"), citizenship_required=True), (), (PASS, None)),
    (WorkAuth(citizenships=("US",), authorized_countries=_CA), _CITIZEN_CA, (), (FAIL, Flag.CITIZENSHIP_REQUIRED)),
    (WorkAuth(authorized_countries=_CA, needs_sponsorship=False), _CITIZEN_CA, (),
     (WARN, Flag.CITIZENSHIP_REQUIRED)),  # citizenship unknown (a permanent resident, say)
    (WorkAuth(citizenships=_CA), WorkAuthGate(citizenship_required=True), (),
     (WARN, Flag.CITIZENSHIP_REQUIRED)),  # no country to compare with
    # 2. Authorised in a posting country: PASS, whatever they need elsewhere.
    (WorkAuth(authorized_countries=("ca",)), WorkAuthGate(countries=("Ca",), sponsorship_available=False), (),
     (PASS, None)),  # country codes compare upper-case
    (WorkAuth(authorized_countries=("US",), citizenships=_CA), _NO_SPONSOR_CA, (), (PASS, None)),
    # 3. Not established as authorised in a posting country.
    (WorkAuth(needs_sponsorship=True), WorkAuthGate(countries=_CA, sponsorship_available=True), (),
     (PASS, None)),  # the employer sponsors
    (WorkAuth(needs_sponsorship=True), _NO_SPONSOR_CA, (), (FAIL, Flag.SPONSORSHIP_UNAVAILABLE)),
    (WorkAuth(needs_sponsorship=True), WorkAuthGate(countries=_CA), (),
     (FAIL, Flag.WORK_AUTH_MISMATCH)),  # authorisation required there, and they need sponsorship
    (WorkAuth(needs_sponsorship=False), _NO_SPONSOR_CA, (), (PASS, None)),
    (WorkAuth(needs_sponsorship=False, authorized_countries=("US",)), _NO_SPONSOR_CA, (),
     (PASS, None)),  # they state no sponsorship is needed
    (WorkAuth(citizenships=_CA), WorkAuthGate(countries=("US",), sponsorship_available=False), (),
     (WARN, Flag.WORK_AUTH_MISMATCH)),  # review minor 4: a missing country is no evidence
    (WorkAuth(authorized_countries=("US",)), WorkAuthGate(countries=_CA), (), (WARN, Flag.WORK_AUTH_MISMATCH)),
    # No posting country at all (remote, no location).
    (WorkAuth(needs_sponsorship=True), _NO_SPONSOR, (), (FAIL, Flag.SPONSORSHIP_UNAVAILABLE)),
    (_CANADIAN_NEEDS_US_SPONSOR, _NO_SPONSOR, (),
     (WARN, Flag.SPONSORSHIP_UNAVAILABLE)),  # the job may be where they are authorised
    (WorkAuth(citizenships=_CA), _NO_SPONSOR, (), (WARN, Flag.WORK_AUTH_UNKNOWN)),
    # A gate that names no country and no sponsorship policy.
    (_CANADIAN_NEEDS_US_SPONSOR, WorkAuthGate(), (SEATTLE,), (PASS, None)),
    (WorkAuth(needs_sponsorship=True), WorkAuthGate(), (), (PASS, None)),
])
def test_work_authorisation(small_graph, config, work_auth, gate, located, expected) -> None:
    assert _one(Resume(work_auth=work_auth), gate, small_graph, config, locations=located) == expected


def test_split_work_authorisation_gates_read_the_postings_location(small_graph, config) -> None:
    # Review A3: "Must be authorised to work in Canada" and "We are unable to sponsor" as two gates.
    post = JobPosting(locations=(TORONTO,), gates=(WorkAuthGate(countries=_CA), _NO_SPONSOR))
    status, results = evaluate_gates(Resume(work_auth=_CANADIAN_NEEDS_US_SPONSOR), post, small_graph, config)
    assert status is PASS and [result.status for result in results] == [PASS, PASS]


def test_unknown_work_authorisation_follows_the_config(small_graph, config) -> None:
    lenient = with_gates(config, "work_authorization", unknown_status="PASS")
    assert _one(Resume(), WorkAuthGate(countries=_CA), small_graph, lenient) == (PASS, Flag.WORK_AUTH_UNKNOWN)
    assert _one(Resume(work_auth=WorkAuth(authorized_countries=_CA)), _CITIZEN_CA, small_graph,
                lenient) == (PASS, Flag.CITIZENSHIP_REQUIRED)
    us_only = WorkAuthGate(countries=("US",), sponsorship_available=False)
    assert _one(Resume(work_auth=WorkAuth(citizenships=_CA)), us_only, small_graph, lenient) == (
        PASS, Flag.WORK_AUTH_MISMATCH)
    assert _one(Resume(work_auth=_CANADIAN_NEEDS_US_SPONSOR), _NO_SPONSOR, small_graph, lenient) == (
        PASS, Flag.SPONSORSHIP_UNAVAILABLE)
    # A stated contradiction still fails: only what is unknown follows `unknown_status`.
    assert _one(Resume(work_auth=WorkAuth(needs_sponsorship=True)), _NO_SPONSOR_CA, small_graph,
                lenient) == (FAIL, Flag.SPONSORSHIP_UNAVAILABLE)
    assert _one(Resume(work_auth=WorkAuth(citizenships=("US",))), _CITIZEN_CA, small_graph, lenient) == (
        FAIL, Flag.CITIZENSHIP_REQUIRED)


def test_a_posting_without_a_work_authorisation_gate_never_asks(small_graph, config) -> None:
    assert evaluate_gates(Resume(), JobPosting(), small_graph, config) == (PASS, ())


# ─── Licences ───────────────────────────────────────────────────────────────────────────────


def _licence_graph(**extra_parents: tuple[str, ...]):
    parents = {"cred.rn_ontario": (RN,), "cred.rn_ontario_extended_class": ("cred.rn_ontario",), **extra_parents}
    return tiny_graph(
        {RN: 0.9, "cred.licensed_practical_nurse": 0.8},
        edges=[("cred.licensed_practical_nurse", RN, 0.70), ("cred.nursing_student", RN, 0.60),
               ("cred.eit", "cred.professional_engineer", 0.60)],
        parents=parents,
    )


def _in_role(*refs: SkillRef) -> Resume:
    return Resume(roles=(Role(skills=refs),))


@pytest.mark.parametrize("cv,expected", [
    (Resume(certifications=(cert(RN),)), (PASS, None)),
    (Resume(roles=(titled(RN),)), (PASS, None)),
    # "Administered medications under RN supervision": a bullet mention is never a held licence.
    (_in_role(SkillRef(canonical_id=RN, evidence=Evidence.ROLE_BULLET, verb_class=VerbClass.ASSISTANCE,
                       source=SkillSource.ROLE, surface_form="RN")), (FAIL, Flag.LICENSE_MISSING)),
    (_in_role(SkillRef(canonical_id=RN, evidence=Evidence.ROLE_BULLET_QUANTIFIED, verb_class=VerbClass.OWNERSHIP,
                       source=SkillSource.ROLE)), (FAIL, Flag.LICENSE_MISSING)),
    (Resume(skills_blob=(SkillRef(canonical_id=RN, evidence=Evidence.SKILLS_BLOB),)), (FAIL, Flag.LICENSE_MISSING)),
    (Resume(projects=(Project(skills=(SkillRef(canonical_id=RN, evidence=Evidence.ROLE_BULLET,
                                               source=SkillSource.PROJECT),)),)), (FAIL, Flag.LICENSE_MISSING)),
    (Resume(certifications=(cert(RN, polarity=Polarity.NEGATED),)), (FAIL, Flag.LICENSE_MISSING)),
    (Resume(certifications=(cert(RN, polarity=Polarity.ASPIRATIONAL),)), (FAIL, Flag.LICENSE_MISSING)),
    (Resume(), (FAIL, Flag.LICENSE_MISSING)),
], ids=["certification", "role_title", "bullet", "quantified_bullet", "skills_blob", "project", "negated",
        "aspirational", "nothing"])
def test_only_certification_or_title_evidence_shows_a_licence(small_graph, config, cv, expected) -> None:
    assert _one(cv, LicenseGate(credential_ids=(RN,)), small_graph, config) == expected


def test_a_descendant_of_the_licence_shows_it(config) -> None:
    graph, gate = _licence_graph(), LicenseGate(credential_ids=(RN,))
    assert _one(Resume(certifications=(cert("cred.rn_ontario"),)), gate, graph, config) == (PASS, None)
    assert _one(Resume(certifications=(cert("cred.rn_ontario_extended_class"),)), gate, graph, config) == (PASS, None)
    assert _one(Resume(roles=(titled("cred.rn_ontario"),)), gate, graph, config) == (PASS, None)


def test_a_broader_credential_never_shows_a_narrower_one(config) -> None:
    gate = LicenseGate(credential_ids=("cred.rn_ontario",))  # not a listed professional licence: WARN
    assert _one(Resume(certifications=(cert(RN),)), gate, _licence_graph(), config) == (WARN, Flag.LICENSE_UNVERIFIED)


def test_a_related_edge_never_shows_a_licence(config) -> None:
    # LPN ~ RN (0.70): §5.1 would give a sibling credit; a licence gate never does (D-28).
    cv = Resume(certifications=(cert("cred.licensed_practical_nurse"),))
    assert _one(cv, LicenseGate(credential_ids=(RN,)), _licence_graph(), config) == (FAIL, Flag.LICENSE_MISSING)


# Formal designations on the way to a licence (taxonomy DECISIONS D-28) and the licence they lead to.
D28_DESIGNATIONS = [
    ("cred.cpa_in_progress", "cred.cpa", FAIL),
    ("cred.nursing_student", RN, FAIL),
    ("cred.eit", "cred.professional_engineer", FAIL),
    ("cred.articling_student", "cred.bar_admission", FAIL),
    ("cred.cfa_candidate", "cred.cfa", WARN),  # the CFA charter is no gated licence
    ("cred.cdl_trainee", "cred.commercial_drivers_license", FAIL),
    ("cred.medical_student", "cred.medical_license", FAIL),
    ("cred.pharmacy_student", "cred.pharmacist_license", FAIL),
    ("cred.teacher_candidate", "cred.teaching_license", FAIL),
    ("cred.electrician_apprentice", "cred.electrician_license", FAIL),
    ("cred.paramedic_student", "cred.paramedic_license", FAIL),
]


@pytest.mark.parametrize("designation,licence,status", D28_DESIGNATIONS, ids=[d for d, _, _ in D28_DESIGNATIONS])
def test_an_in_progress_designation_never_shows_its_licence(config, designation, licence, status) -> None:
    assert designation in config.gates.license.in_progress_credential_ids
    # Worst case: a taxonomy that placed the designation BELOW its licence (the real one only relates them).
    graph = tiny_graph({licence: 0.9}, edges=[(designation, licence, 0.60)], parents={designation: (licence,)})
    reason = Flag.LICENSE_MISSING if status is FAIL else Flag.LICENSE_UNVERIFIED
    for cv in (Resume(certifications=(cert(designation),)), Resume(roles=(titled(designation),))):
        assert _one(cv, LicenseGate(credential_ids=(licence,)), graph, config) == (status, reason)


def test_nothing_below_an_in_progress_designation_shows_the_licence(config) -> None:
    # Review minor 1: cred.eit_ontario is-a cred.eit is-a cred.professional_engineer. The walk up
    # stops at the designation, which still matches a gate that names it.
    graph = tiny_graph({"cred.professional_engineer": 0.9},
                       parents={"cred.eit_ontario": ("cred.eit",), "cred.eit": ("cred.professional_engineer",)})
    ontario_eit = Resume(certifications=(cert("cred.eit_ontario"),))
    assert _one(ontario_eit, LicenseGate(credential_ids=("cred.professional_engineer",)), graph, config) == (
        FAIL, Flag.LICENSE_MISSING)
    either = LicenseGate(credential_ids=("cred.professional_engineer", "cred.eit"))
    assert _one(ontario_eit, either, graph, config) == (PASS, None)  # an Ontario EIT is an EIT
    # A credential that is also directly a kind of the licence still shows it.
    both = tiny_graph({"cred.professional_engineer": 0.9},
                      parents={"cred.x": ("cred.eit", "cred.professional_engineer"),
                               "cred.eit": ("cred.professional_engineer",)})
    assert _one(Resume(certifications=(cert("cred.x"),)), LicenseGate(credential_ids=("cred.professional_engineer",)),
                both, config) == (PASS, None)


def test_licence_descent_follows_the_whole_is_a_chain(config) -> None:
    # Review minor 2: is-a links are eligibility facts; `relations.max_depth` is a scoring knob.
    chain = {f"cred.l{i}": (f"cred.l{i - 1}",) for i in range(2, 7)}
    graph = tiny_graph({RN: 0.9}, parents={"cred.l1": (RN,), **chain})
    gate = LicenseGate(credential_ids=(RN,))
    for depth in range(1, 7):
        assert _one(Resume(certifications=(cert(f"cred.l{depth}"),)), gate, graph, config) == (PASS, None), depth
    shallow = with_scoring(config, "relations", max_depth=1)
    assert _one(Resume(certifications=(cert("cred.l6"),)), gate, graph, shallow) == (PASS, None)


def test_a_parent_cycle_never_hangs_the_licence_walk(config) -> None:
    graph = tiny_graph({RN: 0.9}, parents={"cred.a": ("cred.b",), "cred.b": ("cred.a",)})
    assert _one(Resume(certifications=(cert("cred.a"),)), LicenseGate(credential_ids=(RN,)), graph, config) == (
        FAIL, Flag.LICENSE_MISSING)


def test_a_gate_that_names_the_designation_itself_accepts_it(config) -> None:
    # "The ideal candidate: P.Eng. or EIT with five years of design experience." (D-28 licence probe):
    # the posting accepts an EIT, so holding the EIT designation meets it; it never stands in for P.Eng.
    eit, graph = Resume(certifications=(cert("cred.eit"),)), _licence_graph()
    either = LicenseGate(credential_ids=("cred.professional_engineer", "cred.eit"))
    assert _one(eit, either, graph, config) == (PASS, None)
    assert _one(eit, LicenseGate(credential_ids=("cred.professional_engineer",)), graph, config) == (
        FAIL, Flag.LICENSE_MISSING)


def test_in_progress_designations_come_from_the_config_never_from_their_ids(config) -> None:
    graph = tiny_graph({"cred.cpa": 0.9}, parents={"cred.cpa_in_progress": ("cred.cpa",),
                                                   "cred.cpa_ontario": ("cred.cpa",)})
    gate = LicenseGate(credential_ids=("cred.cpa",))
    candidate, regional = Resume(certifications=(cert("cred.cpa_in_progress"),)), Resume(
        certifications=(cert("cred.cpa_ontario"),))
    assert _one(candidate, gate, graph, config) == (FAIL, Flag.LICENSE_MISSING)
    assert _one(regional, gate, graph, config) == (PASS, None)
    swapped = with_gates(config, "license", in_progress_credential_ids=("cred.cpa_ontario",))
    assert _one(candidate, gate, graph, swapped) == (PASS, None)  # "_in_progress" in an id decides nothing
    assert _one(regional, gate, graph, swapped) == (FAIL, Flag.LICENSE_MISSING)


def test_the_gated_licences_come_from_the_config(small_graph, config) -> None:
    rn_gate, licence_gate = LicenseGate(credential_ids=(RN,)), LicenseGate(credential_ids=("cred.drivers_license",))
    assert _one(Resume(), rn_gate, small_graph, config) == (FAIL, Flag.LICENSE_MISSING)
    assert _one(Resume(), licence_gate, small_graph, config) == (WARN, Flag.LICENSE_UNVERIFIED)  # D-60
    flipped = with_gates(config, "license", professional_credential_ids=("cred.drivers_license",))
    assert _one(Resume(), rn_gate, small_graph, flipped) == (WARN, Flag.LICENSE_UNVERIFIED)
    assert _one(Resume(), licence_gate, small_graph, flipped) == (FAIL, Flag.LICENSE_MISSING)


def test_any_listed_credential_satisfies_and_any_listed_licence_makes_a_miss_fail(small_graph, config) -> None:
    either = LicenseGate(credential_ids=("cred.aws_saa", RN))
    assert _one(Resume(), either, small_graph, config) == (FAIL, Flag.LICENSE_MISSING)
    assert _one(Resume(certifications=(cert("cred.aws_saa"),)), either, small_graph, config) == (PASS, None)
    assert _one(Resume(), LicenseGate(credential_ids=("cred.aws_saa",)), small_graph, config) == (
        WARN, Flag.LICENSE_UNVERIFIED)


def test_the_shipped_config_lists_every_in_progress_designation(config) -> None:
    listed = set(config.gates.license.in_progress_credential_ids)
    named_by_the_brief = {"cred.cpa_in_progress", "cred.nursing_student", "cred.eit", "cred.articling_student",
                          "cred.cfa_candidate"}
    assert named_by_the_brief <= listed
    assert {designation for designation, _, _ in D28_DESIGNATIONS} <= listed
    assert not listed & set(config.gates.license.professional_credential_ids)


def test_a_credential_is_never_both_a_licence_and_an_in_progress_designation(tmp_path: Path) -> None:
    root = tmp_path / "config"
    shutil.copytree(default_config_dir(), root)
    path = root / "gates.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["license"]["in_progress_credential_ids"].append(RN)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigError, match=RN):
        load_config(root)


def test_every_in_progress_node_the_taxonomy_marks_is_listed(config) -> None:
    """The taxonomy marks its formal designations in resolver.json's `in_progress` map (D-28). Only
    the map's VALUES are designations; its keys are the credentials they lead to (cred.cfa among
    them), never the gated licence set, which is `professional_credential_ids` alone."""
    if not TAXONOMY_RESOLVER.is_file():
        pytest.skip("the taxonomy artifacts (Task 2.2) are not on this branch yet")
    marked = set(json.loads(TAXONOMY_RESOLVER.read_text(encoding="utf-8")).get("in_progress", {}).values())
    assert marked <= set(config.gates.license.in_progress_credential_ids)


# ─── Security clearance (D-63: ladders, the unknown-level token) ────────────────────────────

_SECRET_ACTIVE = ClearanceGate(level="US_SECRET", active_required=True)
_SECRET_OBTAINABLE = ClearanceGate(level="US_SECRET")  # "able to obtain"
_RELIABILITY = ClearanceGate(level="CA_RELIABILITY")
_ANY_ACTIVE = ClearanceGate(level="", active_required=True)  # "active security clearance", no level
_UNKNOWN_LEVEL = "ACTIVE_UNKNOWN_LEVEL"  # what Tailrd stores for "Active clearance: yes"
SHIPPED_LADDERS = (
    ("CA_RELIABILITY", "CA_ENHANCED_RELIABILITY", "CA_SECRET", "CA_TOP_SECRET"),
    ("US_PUBLIC_TRUST", "US_CONFIDENTIAL", "US_SECRET", "US_TOP_SECRET", "US_TS_SCI"),
)
LADDER_TOKEN = re.compile(r"[A-Z]{2}(?:_[A-Z0-9]+)+")  # <CC>_<LEVEL>
RESERVED_TOKEN = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*")


def _clearance(held, gate, graph, config) -> tuple[GateStatus, str | None]:
    """(status, reason code) for a candidate holding `held` clearances (None: no work_auth at all)."""
    work_auth = None if held is None else WorkAuth(clearances=held)
    status, reason = _one(Resume(work_auth=work_auth), gate, graph, config)
    return status, (reason.value if reason is not None else None)


@pytest.mark.parametrize("held,gate,expected", [
    (("US_SECRET",), _SECRET_ACTIVE, (PASS, None)),
    ((" us_secret",), _SECRET_ACTIVE, (PASS, None)),  # case and edge spaces ignored
    (("US_TOP_SECRET",), _SECRET_ACTIVE, (PASS, None)),  # review C1: higher on the same ladder
    (("US_TOP_SECRET",), _SECRET_OBTAINABLE, (PASS, None)),  # review C2
    (("us ts/sci",), _SECRET_ACTIVE, (PASS, None)),  # separators normalised: US_TS_SCI
    (("US_CONFIDENTIAL",), _SECRET_ACTIVE, (FAIL, "CLEARANCE_MISSING")),  # lower on the ladder
    (("CA_TOP_SECRET",), _SECRET_ACTIVE, (FAIL, "CLEARANCE_MISSING")),  # another country's ladder
    ((), _SECRET_ACTIVE, (FAIL, "CLEARANCE_MISSING")),
    (None, _SECRET_ACTIVE, (FAIL, "CLEARANCE_MISSING")),
    (("CA_RELIABILITY",), _RELIABILITY, (PASS, None)),
    (("CA_ENHANCED_RELIABILITY",), _RELIABILITY, (PASS, None)),
    (("CA_RELIABILITY",), ClearanceGate(level="CA_SECRET"), (WARN, "CLEARANCE_OBTAINABLE")),  # still to obtain
    ((), _RELIABILITY, (WARN, "CLEARANCE_OBTAINABLE")),
    (None, _RELIABILITY, (WARN, "CLEARANCE_OBTAINABLE")),
    # A token on no ladder matches exactly or not at all.
    (("XX_COSMIC",), ClearanceGate(level="XX_COSMIC", active_required=True), (PASS, None)),
    (("XX_COSMIC",), ClearanceGate(level="XX_ATOMAL", active_required=True), (FAIL, "CLEARANCE_MISSING")),
    (("TOP_SECRET",), _SECRET_ACTIVE, (FAIL, "CLEARANCE_MISSING")),  # no country: no ladder
    # Tailrd's "Active clearance" (country and level unknown): unknown against a stated active level.
    ((_UNKNOWN_LEVEL,), _SECRET_ACTIVE, (WARN, "CLEARANCE_UNVERIFIED")),
    ((_UNKNOWN_LEVEL,), _SECRET_OBTAINABLE, (PASS, None)),
    ((_UNKNOWN_LEVEL,), _ANY_ACTIVE, (PASS, None)),
    ((_UNKNOWN_LEVEL, "US_TOP_SECRET"), _SECRET_ACTIVE, (PASS, None)),  # a known level that meets it
    ((_UNKNOWN_LEVEL, "US_CONFIDENTIAL"), _SECRET_ACTIVE, (WARN, "CLEARANCE_UNVERIFIED")),
    # No level stated: any clearance on the résumé shows one.
    (("CA_SECRET",), _ANY_ACTIVE, (PASS, None)),
    ((), ClearanceGate(level=" ", active_required=True), (FAIL, "CLEARANCE_MISSING")),
])
def test_security_clearance(small_graph, config, held, gate, expected) -> None:
    assert _clearance(held, gate, small_graph, config) == expected


def test_the_ladders_come_from_the_config(small_graph, config) -> None:
    flat = with_gates(config, "security_clearance", ladders=())
    assert _clearance(("US_TOP_SECRET",), _SECRET_ACTIVE, small_graph, flat) == (FAIL, "CLEARANCE_MISSING")
    assert _clearance(("US_SECRET",), _SECRET_ACTIVE, small_graph, flat) == (PASS, None)


def test_the_unknown_level_token_is_named_by_the_config(small_graph, config) -> None:
    renamed = with_gates(config, "security_clearance", unknown_level_token="CLEARANCE_HELD")
    assert _clearance(("CLEARANCE_HELD",), _SECRET_ACTIVE, small_graph, renamed) == (WARN, "CLEARANCE_UNVERIFIED")
    # The old spelling is now an ordinary token on no ladder.
    assert _clearance((_UNKNOWN_LEVEL,), _SECRET_ACTIVE, small_graph, renamed) == (FAIL, "CLEARANCE_MISSING")


def test_the_shipped_clearance_config(config) -> None:
    rules = config.gates.security_clearance
    assert rules.ladders == SHIPPED_LADDERS
    assert rules.unknown_level_token == _UNKNOWN_LEVEL


def test_every_clearance_token_named_in_the_config_is_well_formed(config) -> None:
    rules = config.gates.security_clearance
    tokens = [token for ladder in rules.ladders for token in ladder]
    assert tokens and len(tokens) == len(set(tokens))  # no token twice, on one ladder or two
    for ladder in rules.ladders:
        assert ladder and all(LADDER_TOKEN.fullmatch(token) for token in ladder), ladder
        assert len({token[:2] for token in ladder}) == 1, ladder  # one country per ladder
    assert RESERVED_TOKEN.fullmatch(rules.unknown_level_token)
    assert rules.unknown_level_token not in tokens


def _write_clearance(tmp_path: Path, **changes: object) -> Path:
    root = tmp_path / "config"
    shutil.copytree(default_config_dir(), root)
    path = root / "gates.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for key, value in changes.items():
        if value is None:
            data["security_clearance"].pop(key, None)
        else:
            data["security_clearance"][key] = value
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return root


@pytest.mark.parametrize("changes,pattern", [
    ({"ladders": [["US_SECRET", "US_SECRET"]]}, r"listed twice: US_SECRET"),  # twice on one ladder
    ({"ladders": [["US_SECRET"], ["US_SECRET", "US_TOP_SECRET"]]}, r"listed twice: US_SECRET"),  # on two
    ({"ladders": [["US-SECRET"]]}, r"not <CC>_<LEVEL>: 'US-SECRET'"),
    ({"ladders": [["SECRET"]]}, r"not <CC>_<LEVEL>: 'SECRET'"),
    ({"ladders": [["USA_SECRET"]]}, r"not <CC>_<LEVEL>: 'USA_SECRET'"),
    ({"ladders": [["us_secret"]]}, r"not <CC>_<LEVEL>: 'us_secret'"),
    ({"ladders": [["US_SECRET", "CA_SECRET"]]}, r"spans more than one country"),
    ({"ladders": [[]]}, r"a clearance ladder is empty"),
    ({"unknown_level_token": "US_SECRET"}, r"unknown-level token is on a ladder: US_SECRET"),
    ({"unknown_level_token": "active unknown"}, r"unknown-level token is not an upper-case token: 'active unknown'"),
    ({"unknown_level_token": None}, r"unknown_level_token\s+Field required"),  # named in config, never in code
])
def test_the_clearance_config_is_validated(tmp_path: Path, changes: dict, pattern: str) -> None:
    with pytest.raises(ConfigError, match=pattern):
        load_config(_write_clearance(tmp_path, **changes))


def test_clearance_statuses_come_from_the_config(small_graph, config) -> None:
    soft = with_gates(config, "security_clearance", active_missing_status="WARN", obtainable_status="PASS")
    assert _one(Resume(), _SECRET_ACTIVE, small_graph, soft) == (WARN, Flag.CLEARANCE_MISSING)
    assert _one(Resume(), _RELIABILITY, small_graph, soft) == (PASS, Flag.CLEARANCE_OBTAINABLE)
    hard = with_gates(config, "security_clearance", obtainable_status="FAIL")
    assert _one(Resume(), _RELIABILITY, small_graph, hard) == (FAIL, Flag.CLEARANCE_OBTAINABLE)


# ─── Location ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mine,theirs,expected", [
    ((TORONTO,), (TORONTO,), (PASS, None)),
    ((Location(city="toronto"),), (TORONTO,), (PASS, None)),  # the same city
    ((OTTAWA,), (TORONTO,), (PASS, None)),  # the same country and region
    ((MONTREAL,), (TORONTO,), (WARN, Flag.LOCATION_MISMATCH)),  # another region
    ((SEATTLE,), (TORONTO,), (WARN, Flag.LOCATION_MISMATCH)),  # another country
    ((LONDON_UK,), (LONDON_ON,), (WARN, Flag.LOCATION_MISMATCH)),  # one city name, two countries
    ((TORONTO,), (CANADA,), (PASS, None)),  # a country-wide posting location
    ((SEATTLE,), (CANADA,), (WARN, Flag.LOCATION_MISMATCH)),
    ((CANADA,), (TORONTO,), (PASS, None)),  # only the country is known: nothing shows they're elsewhere
    ((Location(country="CA", city="Ottawa"),), (TORONTO,), (PASS, None)),  # region unknown: can't tell
    ((SEATTLE, TORONTO), (TORONTO,), (PASS, None)),  # any of the candidate's places
    ((MONTREAL,), (SEATTLE, OTTAWA, MONTREAL), (PASS, None)),  # any of the posting's places
    ((), (TORONTO,), (PASS, None)),  # unknown → PASS
    ((Location(),), (TORONTO,), (PASS, None)),
    ((TORONTO,), (), (PASS, None)),
])
def test_location(small_graph, config, mine, theirs, expected) -> None:
    assert _one(Resume(locations=mine), LocationGate(locations=theirs), small_graph, config) == expected


def test_a_location_gate_without_places_uses_the_postings(small_graph, config) -> None:
    seattle = Resume(locations=(SEATTLE,))
    assert _one(seattle, LocationGate(), small_graph, config, locations=(TORONTO,)) == (WARN, Flag.LOCATION_MISMATCH)
    assert _one(seattle, LocationGate(), small_graph, config) == (PASS, None)


def test_location_matters_only_when_onsite_is_required(small_graph, config) -> None:
    gate = LocationGate(locations=(TORONTO,), onsite_required=False)
    assert _one(Resume(locations=(SEATTLE,)), gate, small_graph, config) == (PASS, None)


def test_location_mismatch_status_follows_the_config(small_graph, config) -> None:
    lenient = with_gates(config, "location", mismatch_status="PASS")
    assert _one(Resume(locations=(SEATTLE,)), LocationGate(locations=(TORONTO,)), small_graph, lenient) == (
        PASS, Flag.LOCATION_MISMATCH)


# ─── All gates together ─────────────────────────────────────────────────────────────────────


def test_the_overall_status_is_the_worst_gate(small_graph, config) -> None:
    unknown = WorkAuthGate(countries=_CA)  # WARN: status unknown
    missing = LicenseGate(credential_ids=(RN,))  # FAIL: not shown
    nowhere = LocationGate(locations=(TORONTO,))  # PASS: the candidate's place is unknown
    assert evaluate_gates(Resume(), JobPosting(gates=(nowhere,)), small_graph, config)[0] is PASS
    assert evaluate_gates(Resume(), JobPosting(gates=(nowhere, unknown)), small_graph, config)[0] is WARN
    assert evaluate_gates(Resume(), JobPosting(gates=(unknown, missing, nowhere)), small_graph, config)[0] is FAIL


def test_results_come_in_the_spec_order_of_gate_kinds(small_graph, config) -> None:
    gates = (LocationGate(locations=(TORONTO,)), LicenseGate(credential_ids=("cred.aws_saa",)),
             WorkAuthGate(countries=_CA), ClearanceGate(level="CA_SECRET"), LicenseGate(credential_ids=(RN,)))
    status, results = evaluate_gates(Resume(), JobPosting(gates=gates), small_graph, config)
    assert [result.kind for result in results] == [GateKind.WORK_AUTHORIZATION, GateKind.LICENSE, GateKind.LICENSE,
                                                   GateKind.SECURITY_CLEARANCE, GateKind.LOCATION]
    licences = [result.reason for result in results if result.kind is GateKind.LICENSE]
    assert licences == [Flag.LICENSE_UNVERIFIED, Flag.LICENSE_MISSING]  # the posting's order within a kind
    assert status is FAIL


@pytest.mark.parametrize("section,gate,cv", [
    ("work_authorization", WorkAuthGate(countries=_CA, sponsorship_available=False),
     Resume(work_auth=WorkAuth(needs_sponsorship=True))),
    ("license", LicenseGate(credential_ids=(RN,)), Resume()),
    ("security_clearance", _SECRET_ACTIVE, Resume()),
    ("location", LocationGate(locations=(TORONTO,)), Resume(locations=(SEATTLE,))),
])
def test_a_disabled_gate_kind_passes(small_graph, config, section, gate, cv) -> None:
    assert _one(cv, gate, small_graph, config)[0] is not PASS  # it would bite when enabled
    off = with_gates(config, section, enabled=False)
    assert _one(cv, gate, small_graph, off) == (PASS, None)
    # The other kinds still apply.
    other = WorkAuthGate(countries=_CA) if section != "work_authorization" else LicenseGate(credential_ids=(RN,))
    status, results = evaluate_gates(cv, JobPosting(gates=(gate, other)), small_graph, off)
    assert status is not PASS and len(results) == 2


def test_gate_details_describe_the_posting_never_the_candidate(small_graph, config) -> None:
    cv = Resume(
        work_auth=WorkAuth(authorized_countries=("FR",), citizenships=("BR",), needs_sponsorship=True,
                           clearances=("ZZ_COSMIC",)),
        locations=(Location(country="FR", region="IDF", city="Springfield"),),
        certifications=(cert("cred.aws_saa"),),
    )
    gates = (WorkAuthGate(countries=_CA, sponsorship_available=False, citizenship_required=True),
             LicenseGate(credential_ids=(RN,)), _SECRET_ACTIVE, LocationGate(locations=(TORONTO,)))
    status, results = evaluate_gates(cv, JobPosting(gates=gates), small_graph, config)
    assert status is FAIL and [result.status for result in results] == [FAIL, FAIL, FAIL, WARN]
    for result in results:
        assert result.detail  # says what the posting asks
        for private in ("FR", "BR", "IDF", "springfield", "ZZ_COSMIC", "aws_saa"):
            assert private not in result.detail


def test_details_stay_within_the_model_limit(small_graph, config) -> None:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    gates = (
        WorkAuthGate(countries=tuple(a + b for a in letters for b in letters)),
        LicenseGate(credential_ids=tuple(f"cred.{'x' * 180}{i:03d}" for i in range(20))),
        ClearanceGate(level="z" * 200, active_required=True),
        LocationGate(locations=tuple(Location(country="CA", region="ON", city=f"{'y' * 150}{i}") for i in range(30))),
    )
    status, results = evaluate_gates(Resume(), JobPosting(gates=gates), small_graph, config)
    assert len(results) == 4 and all(len(result.detail) <= 2000 for result in results)


def test_deterministic(small_graph, config) -> None:
    cv = Resume(work_auth=WorkAuth(authorized_countries=("US",)), locations=(MONTREAL,))
    post = JobPosting(gates=(_NO_SPONSOR_CA, LicenseGate(credential_ids=(RN, "cred.aws_saa")), _RELIABILITY,
                             LocationGate(locations=(TORONTO, SEATTLE))))
    first = evaluate_gates(cv, post, small_graph, config)
    assert all(evaluate_gates(cv, post, small_graph, config) == first for _ in range(20))
