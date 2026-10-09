"""§5.6 Education & certifications (Task 3.3).

SPEC §5.6, §9, §14.3; DECISIONS D-13, D-14, D-41. Degree level: meets or exceeds 1.00, one level
below 0.55, two or more below 0.20, no requirement 1.00 (never penalise an unrequested
credential); "or equivalent" (or a preferred requirement) is one tier more lenient. Field of
study: × (0.7 + 0.3 × the best relation credit between the candidate's fields and the required
ones). Relevant credentials: +0.1 each, at most +0.3. Value = min(1, level × field + bonus).
"""

from __future__ import annotations

import math
import random
from typing import Optional

import pytest

from matcher.config import load_config
from matcher.errors import EngineFault, Flag
from matcher.models import (
    DegreeLevel,
    DimensionName,
    Education,
    EducationRequirement,
    Evidence,
    JobPosting,
    Necessity,
    Polarity,
    Resume,
    SkillSource,
)
from matcher.score.dimensions import score_education
from matcher.score.prepare import prepare_resume
from tests.builders import (
    AS_OF,
    CHILD_CREDIT,
    PARENT_CREDIT,
    ref,
    req,
    tiny_graph,
    with_scoring,
)

MEETS, ONE_BELOW, TWO_OR_MORE_BELOW, NO_REQUIREMENT = 1.00, 0.55, 0.20, 1.00  # SPEC §5.6
FIELD_MIN = 0.70  # SPEC §5.6: field multiplier 0.7 – 1.0
CERT_EACH, CERT_CAP = 0.10, 0.30  # SPEC §5.6
W_EDUCATION = 0.10  # SPEC §5.6
NONE, HIGH_SCHOOL, ASSOCIATE, BACHELOR, MASTER, DOCTORATE = (
    DegreeLevel.NONE, DegreeLevel.HIGH_SCHOOL, DegreeLevel.ASSOCIATE, DegreeLevel.BACHELOR, DegreeLevel.MASTER,
    DegreeLevel.DOCTORATE)
CS, SOFTWARE, MATH, ENGINEERING, HISTORY = ("field.computer_science", "field.software_engineering",
                                           "field.mathematics", "field.engineering", "field.history")
FIELD_EDGE = 0.60  # computer science – mathematics
AWS_EDGE = 0.70  # cred.aws_saa – skill.aws


@pytest.fixture(scope="module")
def graph():
    """Fields of study and credentials are graph nodes (D-13): software engineering sits below
    computer science, which sits below engineering and shares an edge with mathematics; the AWS
    certification is related to AWS, the Kubernetes administrator credential sits below Kubernetes
    and the application-developer one below that. History and nursing relate to nothing here."""
    return tiny_graph(
        {"skill.aws": 0.6, "skill.kubernetes": 0.8, "skill.python": 0.4},
        edges=[(CS, MATH, FIELD_EDGE), ("cred.aws_saa", "skill.aws", AWS_EDGE)],
        parents={SOFTWARE: (CS,), CS: (ENGINEERING,), "cred.cka": ("skill.kubernetes",), "cred.ckad": ("cred.cka",),
                 "cred.registered_nurse": (), "cred.azure_fundamentals": ()},
    )


def degree(level: DegreeLevel, *fields: str, in_progress: bool = False) -> Education:
    return Education(level=level, field_ids=fields, in_progress=in_progress)


def cert(credential_id: str, polarity: Polarity = Polarity.AFFIRMED):
    return ref(credential_id, Evidence.CERTIFICATION, source=SkillSource.CERTIFICATION, polarity=polarity)


def wants(level: Optional[DegreeLevel] = None, *fields: str, or_equivalent: bool = False,
          necessity: Necessity = Necessity.REQUIRED, skills=("skill.aws", "skill.kubernetes"),
          certs_required=(), certs_preferred=()) -> JobPosting:
    education = None
    if level is not None:
        education = EducationRequirement(min_level=level, field_ids=fields, or_equivalent=or_equivalent,
                                         necessity=necessity)
    return JobPosting(requirements=tuple(req(s) for s in skills), education_requirement=education,
                      certifications_required=tuple(certs_required), certifications_preferred=tuple(certs_preferred))


def _close(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-12)


def _score(cv: Resume, post: JobPosting, graph, config):
    return score_education(prepare_resume(cv, graph, config, AS_OF), post, graph, config)


def _value(cv: Resume, post: JobPosting, graph, config) -> float:
    return _score(cv, post, graph, config).value


# ─── Degree level ────────────────────────────────────────────────────────────────────────


def test_no_requirement_is_neutral_whatever_the_candidate_lists(graph, config) -> None:
    # SPEC §14.3: bootcamp, self-taught, no degree → 1.00 when nothing is required.
    for cv in (Resume(), Resume(education=(degree(HIGH_SCHOOL),)), Resume(education=(degree(DOCTORATE, HISTORY),))):
        dim = _score(cv, wants(), graph, config)
        assert dim.name is DimensionName.EDUCATION and dim.abstained is False
        assert _close(dim.value, NO_REQUIREMENT)
        assert _close(dim.weight, W_EDUCATION) and _close(dim.effective_weight, W_EDUCATION)
        assert _close(dim.contribution, W_EDUCATION * NO_REQUIREMENT)
        assert [f.code for f in dim.facts] == ["NO_EDUCATION_REQUIREMENT"]


@pytest.mark.parametrize("held,expected", [
    ((BACHELOR,), MEETS),
    ((MASTER,), MEETS),  # exceeds
    ((HIGH_SCHOOL, MASTER, ASSOCIATE), MEETS),  # the highest listed degree counts
    ((ASSOCIATE,), ONE_BELOW),  # one level below → 0.55
    ((HIGH_SCHOOL,), TWO_OR_MORE_BELOW),  # two below → 0.20
    ((NONE,), TWO_OR_MORE_BELOW),
    ((), TWO_OR_MORE_BELOW),  # nothing listed is NONE: three below
])
def test_degree_level_tiers(graph, config, held, expected) -> None:
    cv = Resume(education=tuple(degree(level) for level in held))
    assert _close(_value(cv, wants(BACHELOR), graph, config), expected)


def test_a_degree_in_progress_counts_at_its_level(graph, config) -> None:
    # D-41: completion timing is never inferred (graduation dates are prohibited inputs, SPEC §9).
    cv = Resume(education=(degree(BACHELOR, in_progress=True),))
    assert _close(_value(cv, wants(BACHELOR), graph, config), MEETS)


@pytest.mark.parametrize("held,expected", [
    (ASSOCIATE, MEETS),  # one below → 1.00
    (HIGH_SCHOOL, ONE_BELOW),  # two below → 0.55
    (NONE, ONE_BELOW),  # three below: the two-or-more tier, one tier more lenient
    (BACHELOR, MEETS),
])
@pytest.mark.parametrize("lenient", [{"or_equivalent": True}, {"necessity": Necessity.PREFERRED},
                                     {"or_equivalent": True, "necessity": Necessity.PREFERRED}])
def test_or_equivalent_and_preferred_are_one_tier_more_lenient(graph, config, held, expected, lenient) -> None:
    cv = Resume(education=(degree(held),))
    assert _close(_value(cv, wants(BACHELOR, **lenient), graph, config), expected)


def test_level_facts(graph, config) -> None:
    dim = _score(Resume(education=(degree(HIGH_SCHOOL),)), wants(BACHELOR, or_equivalent=True), graph, config)
    (fact,) = dim.facts
    assert (fact.code, fact.subject) == ("DEGREE_LEVEL", "BACHELOR") and _close(fact.value, ONE_BELOW)
    assert fact.detail == {"candidate": "HIGH_SCHOOL", "shortfall": 2, "lenient": True}


# ─── Field of study ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("held,credit", [
    ((CS,), 1.00),  # exact
    ((SOFTWARE,), CHILD_CREDIT),  # 0.7 + 0.3 × 0.85 = 0.955
    ((MATH,), FIELD_EDGE),  # 0.7 + 0.3 × 0.60 = 0.88
    ((ENGINEERING,), PARENT_CREDIT),  # 0.7 + 0.3 × 0.40 = 0.82
    ((HISTORY,), 0.0),  # unrelated → 0.70
    ((), 0.0),  # required but none listed → 0.70
    ((HISTORY, MATH, SOFTWARE), CHILD_CREDIT),  # the best credit
])
def test_field_multiplier_is_seventy_percent_plus_thirty_times_the_best_credit(graph, config, held, credit) -> None:
    cv = Resume(education=(degree(MASTER, *held),))
    expected = MEETS * (FIELD_MIN + (1.0 - FIELD_MIN) * credit)
    assert _close(_value(cv, wants(BACHELOR, CS), graph, config), expected)


def test_fields_from_every_listed_degree_count_and_every_required_field_is_tried(graph, config) -> None:
    cv = Resume(education=(degree(ASSOCIATE, SOFTWARE), degree(BACHELOR, HISTORY)))
    expected = MEETS * (FIELD_MIN + (1.0 - FIELD_MIN) * CHILD_CREDIT)
    assert _close(_value(cv, wants(BACHELOR, HISTORY + "_unlisted", CS), graph, config), expected)


def test_no_required_field_means_no_field_multiplier(graph, config) -> None:
    cv = Resume(education=(degree(ASSOCIATE, HISTORY),))
    assert _close(_value(cv, wants(BACHELOR), graph, config), ONE_BELOW * 1.0)


def test_level_and_field_multiply(graph, config) -> None:
    cv = Resume(education=(degree(ASSOCIATE, MATH),))
    expected = ONE_BELOW * (FIELD_MIN + (1.0 - FIELD_MIN) * FIELD_EDGE)  # 0.55 × 0.88 = 0.484
    assert _close(_value(cv, wants(BACHELOR, CS), graph, config), expected)
    dim = _score(cv, wants(BACHELOR, CS), graph, config)
    field = [f for f in dim.facts if f.code == "FIELD_MATCH"][0]
    assert field.subject == CS and _close(field.value, FIELD_EDGE)
    assert field.detail["relation"] == "SIBLING" and field.detail["via"] == MATH
    assert _close(field.detail["multiplier"], FIELD_MIN + (1.0 - FIELD_MIN) * FIELD_EDGE)


# ─── Certifications ──────────────────────────────────────────────────────────────────────


def test_two_relevant_certifications_on_a_one_below_candidate(graph, config) -> None:
    # aws_saa is related to the required AWS skill (edge), cka sits below the required Kubernetes.
    cv = Resume(education=(degree(ASSOCIATE),), certifications=(cert("cred.aws_saa"), cert("cred.cka")))
    expected = ONE_BELOW * 1.0 + 2 * CERT_EACH  # 0.55 + 0.2
    dim = _score(cv, wants(BACHELOR), graph, config)
    assert _close(dim.value, expected) and _close(expected, 0.75)
    bonus = [f for f in dim.facts if f.code == "CERT_BONUS"]
    assert [(f.subject, f.value) for f in bonus] == [("cred.aws_saa", CERT_EACH), ("cred.cka", CERT_EACH)]
    assert bonus[0].detail == {"relation": "SIBLING", "to": "skill.aws"}
    assert bonus[1].detail == {"relation": "CHILD", "to": "skill.kubernetes"}


def test_the_certification_bonus_is_capped(graph, config) -> None:
    # Four relevant credentials: aws_saa, cka, ckad (two levels below Kubernetes) and azure, listed
    # by the posting itself → 4 × 0.1, capped at 0.3.
    certs = (cert("cred.aws_saa"), cert("cred.cka"), cert("cred.ckad"), cert("cred.azure_fundamentals"))
    cv = Resume(education=(degree(HIGH_SCHOOL),), certifications=certs)
    post = wants(BACHELOR, certs_preferred=("cred.azure_fundamentals",))
    assert _close(_value(cv, post, graph, config), TWO_OR_MORE_BELOW + CERT_CAP)  # 0.20 + 0.30
    assert len([f for f in _score(cv, post, graph, config).facts if f.code == "CERT_BONUS"]) == 4


def test_a_credential_the_posting_lists_counts_even_unrelated_to_its_skills(graph, config) -> None:
    cv = Resume(education=(degree(ASSOCIATE),), certifications=(cert("cred.azure_fundamentals"),))
    for post in (wants(BACHELOR, certs_required=("cred.azure_fundamentals",)),
                 wants(BACHELOR, certs_preferred=("cred.azure_fundamentals",))):
        assert _close(_value(cv, post, graph, config), ONE_BELOW + CERT_EACH)
    assert _close(_value(cv, wants(BACHELOR), graph, config), ONE_BELOW)


def test_unrelated_negated_aspirational_and_repeated_credentials(graph, config) -> None:
    cv = Resume(education=(degree(ASSOCIATE),), certifications=(
        cert("cred.registered_nurse"),  # unrelated to every requirement
        cert("cred.cka", Polarity.ASPIRATIONAL),  # "studying for the CKA"
        cert("cred.ckad", Polarity.NEGATED),
        cert("cred.aws_saa"), cert("cred.aws_saa"),  # listed twice counts once
    ))
    assert _close(_value(cv, wants(BACHELOR), graph, config), ONE_BELOW + CERT_EACH)


def test_the_value_never_exceeds_one(graph, config) -> None:
    cv = Resume(education=(degree(MASTER, CS),), certifications=(cert("cred.aws_saa"), cert("cred.cka")))
    for post in (wants(BACHELOR, CS), wants()):
        assert _value(cv, post, graph, config) == 1.0


def test_values_follow_the_config(graph, config) -> None:
    tuned = with_scoring(config, "education", one_below=0.5, field_multiplier_min=0.6, cert_bonus_each=0.05,
                         cert_bonus_cap=0.05)
    cv = Resume(education=(degree(ASSOCIATE, MATH),), certifications=(cert("cred.aws_saa"), cert("cred.cka")))
    expected = 0.5 * (0.6 + 0.4 * FIELD_EDGE) + 0.05  # two relevant credentials, capped at one's worth
    assert _close(_value(cv, wants(BACHELOR, CS), graph, tuned), expected)


# ─── Determinism, binding ────────────────────────────────────────────────────────────────


def test_order_never_changes_the_result(graph, config) -> None:
    education = [degree(ASSOCIATE, MATH), degree(HIGH_SCHOOL), degree(BACHELOR, SOFTWARE, HISTORY)]
    certs = [cert("cred.cka"), cert("cred.aws_saa"), cert("cred.registered_nurse")]
    fields = [CS, ENGINEERING, HISTORY]
    reference = _score(Resume(education=tuple(education), certifications=tuple(certs)),
                       wants(MASTER, *fields), graph, config)
    rng = random.Random(9)
    for _ in range(10):
        for items in (education, certs, fields):
            rng.shuffle(items)
        again = _score(Resume(education=tuple(education), certifications=tuple(certs)), wants(MASTER, *fields),
                       graph, config)
        assert again == reference


def test_a_prepared_resume_is_bound_to_its_graph_and_config(graph, small_graph, config) -> None:
    prepared = prepare_resume(Resume(education=(degree(BACHELOR, CS),)), graph, config, AS_OF)
    other_config = with_scoring(config, "education", cert_bonus_cap=0.2)
    for post in (wants(BACHELOR, CS), wants()):  # even where no requirement is stated
        score_education(prepared, post, graph, config)
        score_education(prepared, post, graph, load_config())
        for other_graph, cfg in ((small_graph, config), (graph, other_config)):
            with pytest.raises(EngineFault) as err:
                score_education(prepared, post, other_graph, cfg)
            assert err.value.flag is Flag.INTERNAL_ERROR
