"""The audit trace, its digest and the opaque id (SPEC section 0.4 and 9; DECISIONS D-14).

Every expected value is written out by hand from the inputs in this file; none is read back from the
code under test (EVAL section 2.2). The free-text strings below stand in for everything a trace must
never carry: names, employers, locations, sentences and explanations.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from matcher.audit.trace import REDACTED, build_trace, opaque_id, trace_digest
from matcher.errors import Flag
from matcher.models import (
    Band,
    CalibrationInfo,
    CapApplied,
    Confidence,
    DimensionName,
    DimensionScore,
    Explanation,
    ExplanationItem,
    Fact,
    GateKind,
    GateResult,
    GateStatus,
    Improvement,
    MatchResult,
    Outcome,
    ScoreInterval,
)

SALT = b"unit-test-salt-0123456789abcdef"
OTHER_SALT = b"another-salt-0123456789abcdef!!"

# Everything in here is free text and must never reach a trace.
FREE_TEXT = (
    "Jane Doe",
    "Toronto",
    "Acme Corp",
    "great fit",
    "Learn Terraform",
    "Senior Engineer",
    "led the migration",
    "visa",
)


def req_fact(skill: str, m: float, relation: str, necessity: str = "REQUIRED") -> Fact:
    """The fact the coverage scorers write for one requirement group (score/coverage.py)."""
    code = "REQ_MATCH" if m >= 0.5 else ("REQ_PARTIAL" if m > 0 else "REQ_MISS")
    return Fact(
        code=code,
        subject=skill,
        value=m,
        detail={
            "relation": relation,
            "via": skill if relation == "EXACT" else None,
            "idf": 0.5,
            "weight_multiplier": 1.0,
            "necessity": necessity,
            "members": skill,
        },
    )


def full_result() -> MatchResult:
    required = DimensionScore(
        name=DimensionName.REQUIRED_SKILLS,
        value=0.8,
        weight=0.35,
        effective_weight=0.4,
        contribution=0.32,
        abstained=False,
        facts=(
            req_fact("skill.python", 1.0, "EXACT"),
            req_fact("skill.docker", 0.6, "SIBLING"),
            req_fact("skill.terraform", 0.0, "NONE"),
            Fact(code="CRITICAL_MISS", subject="skill.terraform", value=0.0, detail={"idf": 0.9}),
        ),
    )
    preferred = DimensionScore(
        name=DimensionName.PREFERRED_SKILLS,
        value=None,
        weight=0.1,
        effective_weight=0.0,
        contribution=0.0,
        abstained=True,
        abstain_reason=Flag.NO_PREFERRED_REQUIREMENTS,
    )
    tools = DimensionScore(
        name=DimensionName.TOOLS,
        value=0.5,
        weight=0.1,
        effective_weight=0.12,
        contribution=0.06,
        abstained=False,
        facts=(req_fact("skill.git", 0.5, "PARENT", necessity="IMPLIED"),),
    )
    experience = DimensionScore(
        name=DimensionName.EXPERIENCE,
        value=0.7,
        weight=0.15,
        effective_weight=0.18,
        contribution=0.126,
        abstained=False,
        facts=(
            Fact(
                code="EXPERIENCE_MONTHS",
                subject="Senior Engineer at Acme Corp",
                value=60.0,
                detail={"employer": "Acme Corp", "note": "Jane Doe led the migration"},
            ),
        ),
    )
    return MatchResult(
        outcome=Outcome.SCORED,
        gate_status=GateStatus.WARN,
        gates=(
            GateResult(
                kind=GateKind.WORK_AUTHORIZATION,
                status=GateStatus.WARN,
                reason=Flag.WORK_AUTH_UNKNOWN,
                detail="Jane Doe needs a visa for Toronto",
            ),
        ),
        raw_score=0.7312,
        calibrated_score=74,
        display_score=74,
        band=Band.GOOD,
        dimensions={
            "tools": tools,
            "required_skills": required,
            "experience": experience,
            "preferred_skills": preferred,
        },
        caps=(CapApplied(reason=Flag.CRITICAL_MISS, cap=0.7, requirement_ids=("skill.terraform",)),),
        confidence=Confidence(
            value=0.91,
            components={
                "parse_quality": 1.0,
                "taxonomy_coverage": 0.9,
                "evidence_density": 0.75,
                "temporal_completeness": None,
                "requirement_count": 0.85,
                "dimension_coverage": 1.0,
            },
            reasons=(Flag.LOW_EVIDENCE_DENSITY,),
        ),
        interval=ScoreInterval(low=70, high=78, display=True),
        calibration=CalibrationInfo(method="percentile", scope="panel", n=500, version="panel-2026-10"),
        explanation=Explanation(
            band=Band.GOOD,
            score=74,
            basis="Better fit than 74% of Jane Doe's peers",
            headline="Jane Doe is a great fit for Acme Corp",
            strengths=(
                ExplanationItem(
                    label="7 of 8 required skills at Acme Corp", note="Jane Doe led the migration"
                ),
            ),
            gaps=(ExplanationItem(label="Missing: Terraform", note="Senior Engineer track in Toronto"),),
            to_improve=("Learn Terraform at Acme Corp",),
            notes=("Jane Doe needs a visa",),
        ),
        improvements=(
            Improvement(
                skill_ids=("skill.terraform",),
                new_raw_score=0.8,
                learning_cost=1.0,
                text="Learn Terraform at Acme Corp",
            ),
        ),
        reasons=(),
        flags=(Flag.OVERQUALIFIED,),
        engine_version="1.0.0",
        config_hash="0123456789abcdef",
        as_of=date(2026, 10, 8),
        input_digest="fedcba9876543210",
    )


def result_of(outcome: Outcome, **changes) -> MatchResult:
    base = dict(
        outcome=outcome,
        gate_status=GateStatus.PASS,
        engine_version="1.0.0",
        config_hash="0123456789abcdef",
        as_of=date(2026, 10, 8),
    )
    base.update(changes)
    return MatchResult(**base)


EXPECTED_FULL_TRACE = {
    "trace_version": 1,
    "outcome": "SCORED",
    "gate_status": "WARN",
    "raw": 0.7312,
    "calibrated": 74,
    "band": "Good match",
    "dims": {
        "experience": {"value": 0.7, "weight": 0.15, "effective_weight": 0.18, "abstain_reason": None},
        "preferred_skills": {
            "value": None,
            "weight": 0.1,
            "effective_weight": 0.0,
            "abstain_reason": "NO_PREFERRED_REQUIREMENTS",
        },
        "required_skills": {"value": 0.8, "weight": 0.35, "effective_weight": 0.4, "abstain_reason": None},
        "tools": {"value": 0.5, "weight": 0.1, "effective_weight": 0.12, "abstain_reason": None},
    },
    "requirements": [
        ["skill.python", "REQUIRED", 1.0, "EXACT"],
        ["skill.docker", "REQUIRED", 0.6, "SIBLING"],
        ["skill.terraform", "REQUIRED", 0.0, "NONE"],
        ["skill.git", "IMPLIED", 0.5, "PARENT"],
    ],
    "caps": [{"reason": "CRITICAL_MISS", "cap": 0.7, "requirement_ids": ["skill.terraform"]}],
    "gates": [{"kind": "WORK_AUTHORIZATION", "status": "WARN", "reason": "WORK_AUTH_UNKNOWN"}],
    "confidence": {
        "value": 0.91,
        "components": {
            "dimension_coverage": 1.0,
            "evidence_density": 0.75,
            "parse_quality": 1.0,
            "requirement_count": 0.85,
            "taxonomy_coverage": 0.9,
            "temporal_completeness": None,
        },
        "reasons": ["LOW_EVIDENCE_DENSITY"],
    },
    "calibration": {"method": "percentile", "scope": "panel", "n": 500, "version": "panel-2026-10"},
    "flags": ["OVERQUALIFIED"],
    "reasons": [],
    "engine_version": "1.0.0",
    "config_hash": "0123456789abcdef",
    "input_digest": "fedcba9876543210",
    "as_of": "2026-10-08",
}


def strings_in(value) -> list[str]:
    """Every string in a nested trace, dictionary keys included."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.append(key)
            found.extend(strings_in(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(strings_in(item))
    elif isinstance(value, str):
        found.append(value)
    return found


# --- build_trace: contents ---------------------------------------------------------------------


def test_trace_holds_exactly_the_audited_fields() -> None:
    assert build_trace(full_result()) == EXPECTED_FULL_TRACE


def test_trace_is_json_native_and_round_trips() -> None:
    trace = build_trace(full_result())
    text = json.dumps(trace, allow_nan=False)  # a NaN or a non-JSON type would raise here
    assert json.loads(text) == trace
    assert json.loads(json.dumps(json.loads(text))) == trace


def test_trace_carries_no_free_text() -> None:
    trace = build_trace(full_result())
    leaked = [s for s in strings_in(trace) for probe in FREE_TEXT if probe.casefold() in s.casefold()]
    assert leaked == []
    dumped = json.dumps(trace)
    for probe in FREE_TEXT:
        assert probe not in dumped


def test_every_string_in_a_trace_is_a_code_an_id_a_band_or_a_date() -> None:
    allowed_words = {band.value for band in Band}
    for text in strings_in(build_trace(full_result())):
        assert text in allowed_words or all(ch.isascii() and not ch.isspace() for ch in text), text


def test_explanation_improvements_and_interval_are_not_in_the_trace() -> None:
    trace = build_trace(full_result())
    assert not {"explanation", "improvements", "interval", "facts", "detail"} & set(trace)


def test_only_requirement_facts_become_requirement_rows() -> None:
    # The EXPERIENCE_MONTHS fact (free-text subject and detail) and the CRITICAL_MISS fact (already
    # recorded under `caps`) never appear as rows.
    rows = build_trace(full_result())["requirements"]
    assert [row[0] for row in rows] == ["skill.python", "skill.docker", "skill.terraform", "skill.git"]
    assert all(len(row) == 4 for row in rows)


def test_a_requirement_row_is_skill_necessity_m_relation() -> None:
    result = result_of(
        Outcome.NEEDS_REVIEW,
        reasons=(Flag.NOTHING_SCORABLE,),
        dimensions={
            "preferred_skills": DimensionScore(
                name=DimensionName.PREFERRED_SKILLS,
                value=0.25,
                weight=0.1,
                effective_weight=0.1,
                contribution=0.025,
                abstained=False,
                facts=(req_fact("skill.sql", 0.25, "PARENT", necessity="PREFERRED"),),
            )
        },
    )
    assert build_trace(result)["requirements"] == [["skill.sql", "PREFERRED", 0.25, "PARENT"]]


# --- build_trace: redaction --------------------------------------------------------------------


def test_free_text_in_an_id_position_is_redacted_not_copied() -> None:
    bad_subject = Fact(
        code="REQ_MATCH",
        subject="Jane Doe, 12 Main Street",
        value=1.0,
        detail={"relation": "EXACT", "necessity": "Acme Corp prefers it"},
    )
    result = full_result().model_copy(
        update={
            "dimensions": {
                "required_skills": DimensionScore(
                    name=DimensionName.REQUIRED_SKILLS,
                    value=1.0,
                    weight=0.35,
                    effective_weight=0.35,
                    contribution=0.35,
                    abstained=False,
                    facts=(bad_subject,),
                )
            },
            "caps": (CapApplied(reason=Flag.CRITICAL_MISS, cap=0.7, requirement_ids=("led the migration",)),),
            "calibration": CalibrationInfo(
                method="isotonic", scope="global", n=10, version="Jane Doe's curve"
            ),
        }
    )
    trace = build_trace(result)
    assert trace["requirements"] == [[REDACTED, REDACTED, 1.0, "EXACT"]]
    assert trace["caps"][0]["requirement_ids"] == [REDACTED]
    assert trace["calibration"]["version"] == REDACTED
    for probe in FREE_TEXT:
        assert probe not in json.dumps(trace)


def test_two_unsafe_component_names_do_not_merge_into_one_key() -> None:
    confidence = Confidence(
        value=0.5, components={"Jane Doe": 0.1, "Acme Corp": 0.2, "parse_quality": 0.9}, reasons=()
    )
    trace = build_trace(full_result().model_copy(update={"confidence": confidence}))
    # Placeholders are numbered in the order of the (sorted) original names, so they are stable and
    # distinct: neither value is lost, and no name is copied.
    assert trace["confidence"]["components"] == {
        "<redacted:1>": 0.2,  # "Acme Corp"
        "<redacted:2>": 0.1,  # "Jane Doe"
        "parse_quality": 0.9,
    }
    assert "Jane" not in json.dumps(trace) and "Acme" not in json.dumps(trace)


def test_a_redacted_component_placeholder_cannot_be_a_valid_name() -> None:
    confidence = Confidence(value=0.5, components={"has space": 0.3}, reasons=())
    (key,) = build_trace(full_result().model_copy(update={"confidence": confidence}))["confidence"][
        "components"
    ]
    assert key.startswith(REDACTED[:-1]) and any(ch in key for ch in "<>")
    assert key == "<redacted:1>"  # every unsafe name is numbered, so a lone one is number 1


def test_the_redaction_marker_cannot_be_a_valid_id() -> None:
    assert not REDACTED.isalnum() and any(ch in REDACTED for ch in "<>")


def test_ids_with_the_characters_real_ids_use_are_kept() -> None:
    for skill in ("skill.c++", "skill.c#", "cred.aws_saa", "skill.node-js", "esco:abc/123", "skill.x" * 20):
        fact = req_fact(skill, 1.0, "EXACT")
        result = result_of(
            Outcome.NEEDS_REVIEW,
            reasons=(Flag.NOTHING_SCORABLE,),
            dimensions={
                "tools": DimensionScore(
                    name=DimensionName.TOOLS,
                    value=1.0,
                    weight=0.1,
                    effective_weight=0.1,
                    contribution=0.1,
                    abstained=False,
                    facts=(fact,),
                )
            },
        )
        assert build_trace(result)["requirements"][0][0] == skill


@given(text=st.text(min_size=6), where=st.sampled_from(["gate_detail", "headline", "fact", "item"]))
def test_text_in_any_free_field_never_reaches_the_trace(text: str, where: str) -> None:
    marker = (chr(0x2603) + "ZQX" + text)[:2000]  # a prefix no id, code or date contains
    result = full_result()
    if where == "gate_detail":
        gate = GateResult(
            kind=GateKind.LOCATION, status=GateStatus.WARN, reason=Flag.LOCATION_MISMATCH, detail=marker
        )
        result = result.model_copy(update={"gates": (gate,)})
    elif where == "headline":
        result = result.model_copy(update={"explanation": Explanation(headline=marker)})
    elif where == "fact":
        experience = DimensionScore(
            name=DimensionName.EXPERIENCE,
            value=0.7,
            weight=0.15,
            effective_weight=0.18,
            contribution=0.126,
            abstained=False,
            facts=(Fact(code="EXPERIENCE_MONTHS", subject=marker, detail={"k": marker}),),
        )
        result = result.model_copy(update={"dimensions": {**result.dimensions, "experience": experience}})
    else:
        item = ExplanationItem(label=marker, note=marker)
        result = result.model_copy(update={"explanation": Explanation(strengths=(item,))})
    assert [s for s in strings_in(build_trace(result)) if "ZQX" in s] == []


# --- build_trace: other outcomes ---------------------------------------------------------------


def test_needs_review_trace_has_reasons_and_no_scores() -> None:
    trace = build_trace(result_of(Outcome.NEEDS_REVIEW, reasons=(Flag.UNREADABLE, Flag.OCR_USED)))
    assert trace == {
        "trace_version": 1,
        "outcome": "NEEDS_REVIEW",
        "gate_status": "PASS",
        "raw": None,
        "calibrated": None,
        "band": None,
        "dims": {},
        "requirements": [],
        "caps": [],
        "gates": [],
        "confidence": None,
        "calibration": None,
        "flags": [],
        "reasons": ["UNREADABLE", "OCR_USED"],
        "engine_version": "1.0.0",
        "config_hash": "0123456789abcdef",
        "input_digest": "",
        "as_of": "2026-10-08",
    }


def test_gate_fail_trace_records_the_failed_gate_and_no_score() -> None:
    gate = GateResult(
        kind=GateKind.SECURITY_CLEARANCE,
        status=GateStatus.FAIL,
        reason=Flag.CLEARANCE_MISSING,
        detail="needs TS/SCI at the Acme Corp site",
    )
    trace = build_trace(
        result_of(
            Outcome.GATE_FAIL,
            gate_status=GateStatus.FAIL,
            gates=(gate,),
            reasons=(Flag.CLEARANCE_MISSING,),
        )
    )
    assert trace["outcome"] == "GATE_FAIL" and trace["gate_status"] == "FAIL"
    assert trace["raw"] is None and trace["calibrated"] is None and trace["band"] is None
    assert trace["gates"] == [{"kind": "SECURITY_CLEARANCE", "status": "FAIL", "reason": "CLEARANCE_MISSING"}]
    assert trace["reasons"] == ["CLEARANCE_MISSING"]
    assert "TS/SCI" not in json.dumps(trace)


def test_gate_without_a_reason_records_none() -> None:
    gate = GateResult(kind=GateKind.LOCATION, status=GateStatus.PASS)
    trace = build_trace(result_of(Outcome.NEEDS_REVIEW, gates=(gate,), reasons=(Flag.NOTHING_SCORABLE,)))
    assert trace["gates"] == [{"kind": "LOCATION", "status": "PASS", "reason": None}]


def test_low_confidence_trace_keeps_the_band_and_the_internal_score() -> None:
    trace = build_trace(
        result_of(
            Outcome.SCORED_LOW_CONFIDENCE,
            raw_score=0.5,
            calibrated_score=62,
            band=Band.PARTIAL,
            reasons=(Flag.LOW_PARSE_QUALITY,),
        )
    )
    assert (trace["outcome"], trace["raw"], trace["calibrated"], trace["band"]) == (
        "SCORED_LOW_CONFIDENCE",
        0.5,
        62,
        "Partial match",
    )
    assert trace["reasons"] == ["LOW_PARSE_QUALITY"]


# --- build_trace: determinism ------------------------------------------------------------------


def test_the_order_dimensions_were_added_in_does_not_change_the_trace() -> None:
    result = full_result()
    reordered = result.model_copy(update={"dimensions": dict(reversed(list(result.dimensions.items())))})
    assert list(reordered.dimensions) != list(result.dimensions)
    assert build_trace(reordered) == build_trace(result)
    assert trace_digest(build_trace(reordered)) == trace_digest(build_trace(result))


def test_building_a_trace_twice_gives_the_same_bytes() -> None:
    first = json.dumps(build_trace(full_result()), sort_keys=True)
    second = json.dumps(build_trace(full_result()), sort_keys=True)
    assert first == second


def test_a_trace_serialises_the_same_without_sorting_whatever_order_the_inputs_came_in() -> None:
    result = full_result()
    confidence = result.confidence
    reordered = result.model_copy(
        update={
            "dimensions": dict(reversed(list(result.dimensions.items()))),
            "confidence": confidence.model_copy(
                update={"components": dict(reversed(list(confidence.components.items())))}
            ),
        }
    )
    assert list(reordered.confidence.components) != list(confidence.components)
    assert json.dumps(build_trace(reordered)) == json.dumps(build_trace(result))  # no sort_keys here


def test_trace_follows_the_real_coverage_scorer(config, small_graph) -> None:
    """The requirement rows are what `match_requirements` computed, not what this test assumes."""
    from matcher.score.coverage import match_requirements
    from matcher.score.dimensions import score_required_skills
    from matcher.score.prepare import prepare_resume
    from tests.builders import AS_OF, listed, posting, req, resume

    # react and git are listed; javascript is react's ancestor (CHILD), vue is react's neighbour
    # (SIBLING), docker has no link to either (NONE).
    cv = resume(blob=listed("skill.react", "skill.git"))
    job = posting(
        req("skill.react"), req("skill.javascript"), req("skill.vue"), req("skill.docker"), req("skill.git")
    )
    prepared = prepare_resume(cv, small_graph, config, AS_OF)
    dimension = score_required_skills(prepared, job, small_graph, config)
    matches = match_requirements(prepared, job.requirements, small_graph, config)
    assert len(matches) == 5

    result = result_of(
        Outcome.NEEDS_REVIEW,
        reasons=(Flag.NOTHING_SCORABLE,),
        dimensions={"required_skills": dimension},
    )
    rows = build_trace(result)["requirements"]
    assert rows == [[m.best_member, m.necessity.value, m.m, m.relation.value] for m in matches]
    assert {row[3] for row in rows} == {"EXACT", "CHILD", "SIBLING", "NONE"}  # every relation but PARENT
    assert all(row[1] == "REQUIRED" for row in rows)
    assert dimension.facts  # the scorer did write facts for the trace to read


def test_requirement_fact_codes_match_the_scorer_constants() -> None:
    from matcher.audit import trace
    from matcher.score.coverage import REQ_MATCH, REQ_MISS, REQ_PARTIAL

    assert trace.REQUIREMENT_CODES == frozenset({REQ_MATCH, REQ_PARTIAL, REQ_MISS})


# --- trace_digest ------------------------------------------------------------------------------


def test_trace_digest_is_sha256_of_the_canonical_json_truncated_to_16_hex() -> None:
    trace = {"c": "x", "b": [True, None], "a": 1}
    assert trace_digest(trace) == "b4d9bb599eed5aa8"  # sha256('{"a":1,"b":[true,null],"c":"x"}')[:16]
    canonical = json.dumps(trace, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert trace_digest(trace) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def test_trace_digest_ignores_key_order_and_sees_every_value() -> None:
    assert trace_digest({"a": 1, "b": 2}) == trace_digest({"b": 2, "a": 1})
    assert trace_digest({"a": 1, "b": 2}) != trace_digest({"a": 1, "b": 3})
    assert trace_digest({"a": [1, 2]}) != trace_digest({"a": [2, 1]})
    assert trace_digest({"a": 1}) != trace_digest({"a": 1.0000000001})


def test_trace_digest_is_16_lowercase_hex_and_stable_across_calls() -> None:
    trace = build_trace(full_result())
    digest = trace_digest(trace)
    assert len(digest) == 16 and set(digest) <= set("0123456789abcdef")
    assert digest == trace_digest(json.loads(json.dumps(trace)))
    assert digest == trace_digest(build_trace(full_result()))


def test_trace_digest_hashes_utf8_not_escapes() -> None:
    canonical = json.dumps(
        {"k": "caf" + chr(0xE9)}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert (
        trace_digest({"k": "caf" + chr(0xE9)}) == hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_trace_digest_refuses_non_finite_numbers(bad: float) -> None:
    with pytest.raises(ValueError):
        trace_digest({"x": bad})


def test_a_changed_score_changes_the_digest() -> None:
    first = build_trace(full_result())
    second = build_trace(full_result().model_copy(update={"calibrated_score": 75}))
    assert trace_digest(first) != trace_digest(second)


# --- opaque_id ---------------------------------------------------------------------------------


def test_opaque_id_is_hmac_sha256_truncated_to_24_hex() -> None:
    assert opaque_id("user-1", SALT) == "afdb4d9e798be51c4ff9f511"
    assert opaque_id("user-2", SALT) == "634410f5453266a458a4a435"
    expected = hmac.new(SALT, "44".encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    assert opaque_id("44", SALT) == expected == "c641aa87a74d91f91c5482d8"


def test_opaque_id_is_stable_for_one_salt_and_different_across_salts() -> None:
    assert opaque_id("user-1", SALT) == opaque_id("user-1", SALT)
    assert opaque_id("user-1", SALT) != opaque_id("user-1", OTHER_SALT)
    assert opaque_id("user-1", OTHER_SALT) == "63d40f121054a1cdc769bd9d"


def test_opaque_id_differs_across_users_and_is_24_lowercase_hex() -> None:
    first, second = opaque_id("user-1", SALT), opaque_id("user-2", SALT)
    assert first != second
    assert len(first) == len(second) == 24
    assert set(first + second) <= set("0123456789abcdef")


def test_opaque_id_accepts_bytes_like_salts_and_unicode_keys() -> None:
    assert opaque_id("user-1", bytearray(SALT)) == opaque_id("user-1", SALT)
    key = "usuari-" + chr(0xE7)
    assert opaque_id(key, SALT) == hmac.new(SALT, key.encode("utf-8"), hashlib.sha256).hexdigest()[:24]


@pytest.mark.parametrize("salt", [b"", b"short", b"x" * 15])
def test_opaque_id_refuses_a_salt_too_short_to_protect_small_user_ids(salt: bytes) -> None:
    with pytest.raises(ValueError):
        opaque_id("user-1", salt)


def test_opaque_id_accepts_a_16_byte_salt() -> None:
    assert len(opaque_id("user-1", b"x" * 16)) == 24


@pytest.mark.parametrize("key", ["", None, 44, b"44"])
def test_opaque_id_needs_a_non_empty_text_key(key) -> None:
    with pytest.raises(ValueError):
        opaque_id(key, SALT)


@pytest.mark.parametrize("salt", ["text-salt-0123456789abcdef", None, 12345678901234567890])
def test_opaque_id_needs_bytes_for_the_salt(salt) -> None:
    with pytest.raises(ValueError):
        opaque_id("user-1", salt)
