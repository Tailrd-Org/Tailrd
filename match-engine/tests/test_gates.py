"""Gates: eligibility, evaluated before scoring, never part of the weighted sum (SPEC §4).

Tailrd policy (DECISIONS D-60, config/gates.yaml): unknown candidate status never fails a gate, it
warns. Work authorisation fails only when the candidate's stated status contradicts the posting.
A licence gate fails only for a professional licence (config `professional_credential_ids`) the
résumé does not show, and only CERTIFICATION or ROLE_TITLE evidence of the licence id (or a
descendant) shows one: never a bullet mention, never a related edge, never an in-progress
designation (taxonomy DECISIONS D-28; config `in_progress_credential_ids`). Clearance fails only for
an active clearance the résumé does not show. Location only ever warns. A disabled gate kind passes.
The overall status is the worst gate's.
"""

from __future__ import annotations

import json
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
from tests.builders import tiny_graph, with_gates

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


# ─── Work authorisation ─────────────────────────────────────────────────────────────────────

_CA = ("CA",)
_NO_SPONSOR_CA = WorkAuthGate(countries=_CA, sponsorship_available=False)
_CITIZEN_CA = WorkAuthGate(countries=_CA, citizenship_required=True)


@pytest.mark.parametrize("work_auth,gate,expected", [
    # Status unknown → WARN, whatever the posting asks (D-60).
    (None, WorkAuthGate(countries=_CA), (WARN, Flag.WORK_AUTH_UNKNOWN)),
    (None, _NO_SPONSOR_CA, (WARN, Flag.WORK_AUTH_UNKNOWN)),
    (WorkAuth(), _NO_SPONSOR_CA, (WARN, Flag.WORK_AUTH_UNKNOWN)),  # a status that states nothing
    (WorkAuth(clearances=("CA_SECRET",)), _NO_SPONSOR_CA, (WARN, Flag.WORK_AUTH_UNKNOWN)),
    # Needs sponsorship and the posting says none → FAIL.
    (WorkAuth(needs_sponsorship=True), _NO_SPONSOR_CA, (FAIL, Flag.SPONSORSHIP_UNAVAILABLE)),
    (WorkAuth(needs_sponsorship=True), WorkAuthGate(sponsorship_available=False), (FAIL, Flag.SPONSORSHIP_UNAVAILABLE)),
    (WorkAuth(needs_sponsorship=True), WorkAuthGate(countries=_CA, sponsorship_available=True), (PASS, None)),
    (WorkAuth(needs_sponsorship=True), WorkAuthGate(countries=_CA), (PASS, None)),  # posting silent on it
    (WorkAuth(needs_sponsorship=False), _NO_SPONSOR_CA, (PASS, None)),
    # "Canadian citizen; will need sponsorship for US roles" meets no contradiction in Canada.
    (WorkAuth(needs_sponsorship=True, citizenships=_CA), _NO_SPONSOR_CA, (PASS, None)),
    (WorkAuth(needs_sponsorship=True, authorized_countries=_CA), _NO_SPONSOR_CA, (PASS, None)),
    # Citizenship required.
    (WorkAuth(citizenships=_CA), _CITIZEN_CA, (PASS, None)),
    (WorkAuth(citizenships=("US",), authorized_countries=_CA), _CITIZEN_CA, (FAIL, Flag.CITIZENSHIP_REQUIRED)),
    (WorkAuth(authorized_countries=_CA), _CITIZEN_CA, (WARN, Flag.CITIZENSHIP_REQUIRED)),  # citizenship unknown
    (WorkAuth(citizenships=_CA), WorkAuthGate(citizenship_required=True), (WARN, Flag.CITIZENSHIP_REQUIRED)),  # whose?
    (WorkAuth(citizenships=("US",)), WorkAuthGate(countries=("CA", "US"), citizenship_required=True), (PASS, None)),
    # Known authorised countries (citizenship included) missing every posting country.
    (WorkAuth(authorized_countries=("US",)), _NO_SPONSOR_CA, (FAIL, Flag.WORK_AUTH_MISMATCH)),
    (WorkAuth(authorized_countries=("US",)), WorkAuthGate(countries=_CA), (WARN, Flag.WORK_AUTH_MISMATCH)),
    (WorkAuth(authorized_countries=("US",)), WorkAuthGate(countries=_CA, sponsorship_available=True),
     (WARN, Flag.WORK_AUTH_MISMATCH)),
    (WorkAuth(citizenships=_CA), WorkAuthGate(countries=("US",), sponsorship_available=False),
     (FAIL, Flag.WORK_AUTH_MISMATCH)),
    (WorkAuth(authorized_countries=("US",)), WorkAuthGate(countries=("CA", "US"), sponsorship_available=False),
     (PASS, None)),  # authorised in one of the posting's countries
    (WorkAuth(authorized_countries=("US",), citizenships=_CA), _NO_SPONSOR_CA, (PASS, None)),
    (WorkAuth(authorized_countries=("ca",)), WorkAuthGate(countries=("Ca",), sponsorship_available=False),
     (PASS, None)),  # country codes compare upper-case
    (WorkAuth(authorized_countries=_CA), WorkAuthGate(), (PASS, None)),  # nothing stated to contradict
    # One gate, several findings: the worst status, then sponsorship > citizenship > countries.
    (WorkAuth(needs_sponsorship=True, authorized_countries=("US",)), _NO_SPONSOR_CA,
     (FAIL, Flag.SPONSORSHIP_UNAVAILABLE)),
    (WorkAuth(authorized_countries=("US",)), WorkAuthGate(countries=_CA, sponsorship_available=False,
                                                         citizenship_required=True),
     (FAIL, Flag.WORK_AUTH_MISMATCH)),  # citizenship unknown (WARN) never hides the FAIL
    (WorkAuth(citizenships=("US",)), WorkAuthGate(countries=_CA, sponsorship_available=False,
                                                 citizenship_required=True),
     (FAIL, Flag.CITIZENSHIP_REQUIRED)),
])
def test_work_authorisation(small_graph, config, work_auth, gate, expected) -> None:
    assert _one(Resume(work_auth=work_auth), gate, small_graph, config) == expected


def test_unknown_work_authorisation_follows_the_config(small_graph, config) -> None:
    lenient = with_gates(config, "work_authorization", unknown_status="PASS")
    assert _one(Resume(), WorkAuthGate(countries=_CA), small_graph, lenient) == (PASS, Flag.WORK_AUTH_UNKNOWN)
    assert _one(Resume(work_auth=WorkAuth(authorized_countries=_CA)), _CITIZEN_CA, small_graph,
                lenient) == (PASS, Flag.CITIZENSHIP_REQUIRED)
    # A contradiction still fails: only unknown status follows `unknown_status`.
    assert _one(Resume(work_auth=WorkAuth(needs_sponsorship=True)), _NO_SPONSOR_CA, small_graph,
                lenient) == (FAIL, Flag.SPONSORSHIP_UNAVAILABLE)


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


# ─── Security clearance ─────────────────────────────────────────────────────────────────────

_SECRET_ACTIVE = ClearanceGate(level="US_SECRET", active_required=True)
_RELIABILITY = ClearanceGate(level="CA_RELIABILITY")  # "able to obtain"


@pytest.mark.parametrize("work_auth,gate,expected", [
    (WorkAuth(clearances=("US_SECRET",)), _SECRET_ACTIVE, (PASS, None)),
    (WorkAuth(clearances=(" us_secret",)), _SECRET_ACTIVE, (PASS, None)),  # case and edge spaces ignored
    (WorkAuth(clearances=("CA_RELIABILITY",)), _SECRET_ACTIVE, (FAIL, Flag.CLEARANCE_MISSING)),
    (WorkAuth(), _SECRET_ACTIVE, (FAIL, Flag.CLEARANCE_MISSING)),
    (None, _SECRET_ACTIVE, (FAIL, Flag.CLEARANCE_MISSING)),
    (WorkAuth(clearances=("CA_RELIABILITY",)), _RELIABILITY, (PASS, None)),
    (WorkAuth(), _RELIABILITY, (WARN, Flag.CLEARANCE_OBTAINABLE)),
    (None, _RELIABILITY, (WARN, Flag.CLEARANCE_OBTAINABLE)),
    # No level stated ("active security clearance required"): any clearance on the résumé shows one.
    (WorkAuth(clearances=("CA_SECRET",)), ClearanceGate(level="", active_required=True), (PASS, None)),
    (WorkAuth(), ClearanceGate(level=" ", active_required=True), (FAIL, Flag.CLEARANCE_MISSING)),
])
def test_security_clearance(small_graph, config, work_auth, gate, expected) -> None:
    assert _one(Resume(work_auth=work_auth), gate, small_graph, config) == expected


def test_clearance_statuses_come_from_the_config(small_graph, config) -> None:
    soft = with_gates(config, "security_clearance", active_missing_status="WARN", obtainable_status="PASS")
    assert _one(Resume(), _SECRET_ACTIVE, small_graph, soft) == (WARN, Flag.CLEARANCE_MISSING)
    assert _one(Resume(), _RELIABILITY, small_graph, soft) == (PASS, Flag.CLEARANCE_OBTAINABLE)
    hard = with_gates(config, "security_clearance", obtainable_status="FAIL")
    assert _one(Resume(), _RELIABILITY, small_graph, hard) == (FAIL, Flag.CLEARANCE_OBTAINABLE)


# ─── Location ───────────────────────────────────────────────────────────────────────────────

TORONTO = Location(country="CA", region="ON", city="Toronto")
OTTAWA = Location(country="CA", region="ON", city="Ottawa")
MONTREAL = Location(country="CA", region="QC", city="Montréal")
SEATTLE = Location(country="US", region="WA", city="Seattle")
LONDON_ON = Location(country="CA", region="ON", city="London")
LONDON_UK = Location(country="GB", region="ENG", city="London")
CANADA = Location(country="CA")


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
