"""The audit trace, its digest and the opaque id (SPEC section 0.4 and 9; DECISIONS D-14).

SPEC section 0.4: "every score persists its config hash, engine version, and full feature trace".
`build_trace` is that record. It holds the outcome, the raw and calibrated scores and the band; each
dimension's value and weights (and why it abstained); the caps, gates and confidence components;
one `[skill_id, necessity, m, relation]` row per requirement; the flags and reasons; and the
engine version, config hash, input digest and `as_of` date that let anyone replay the score.

It never holds free text (D-14). Explanations, improvement sentences, gate details, fact details and
every `surface_form` stay out. Strings that remain are enum values, flag codes, dates, hashes and
canonical ids, and an id-position string that is not shaped like an id (a letter or digit, then
letters, digits and . _ : / + # -, at most 200 characters in all) is replaced by `REDACTED` rather
than copied. A leak is therefore visible in the record and never silently persisted.

`trace_digest` fingerprints a trace for integrity checks. `opaque_id` is the only key demographics
are joined on: an HMAC of the user key under a secret salt, so a user id alone can never reach the
fairness store (SPEC section 9).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from enum import Enum
from typing import Any, Optional

from matcher.models import Confidence, DimensionScore, Fact, MatchResult, Necessity, RelationKind

__all__ = [
    "REDACTED",
    "REQUIREMENT_CODES",
    "TRACE_VERSION",
    "build_trace",
    "opaque_id",
    "trace_digest",
]

TRACE_VERSION = 1

# Stands where a string that is not shaped like an id would have been copied. It contains
# characters no id can contain, so it can never be mistaken for one.
REDACTED = "<redacted>"

# Fact codes that describe one requirement group (score/coverage.py: REQ_MATCH, REQ_PARTIAL,
# REQ_MISS). Duplicated here so audit/ imports nothing from score/; a test pins the two together.
REQUIREMENT_CODES = frozenset({"REQ_MATCH", "REQ_PARTIAL", "REQ_MISS"})

# What a canonical id, code, version label or digest looks like. Narrower than the model's `_ID`
# on purpose: when in doubt, redact.
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+#-]{0,199}")

_MIN_SALT_BYTES = 16  # user ids are small integers: a short salt would let anyone enumerate them
_OPAQUE_ID_HEX = 24  # 96 bits
_DIGEST_HEX = 16  # 64 bits, the same width as the config hash


def _token(value: Optional[str]) -> Optional[str]:
    """`value` when it is shaped like an id, `REDACTED` when it is not. None and "" pass through."""
    if value is None or value == "":
        return value
    return value if _TOKEN.fullmatch(value) else REDACTED


def _enum_value(kind: type[Enum], raw: object) -> Optional[str]:
    """The canonical value of `raw` as a member of `kind`; None when absent, REDACTED when it is
    not a member (a free-text string in an enum position)."""
    if raw is None:
        return None
    try:
        return kind(raw).value
    except ValueError:
        return REDACTED


def _code(flag: Optional[Enum]) -> Optional[str]:
    return None if flag is None else flag.value


def _requirement_row(fact: Fact) -> list[Any]:
    """`[skill_id, necessity, m, relation]` from the fact a coverage scorer wrote for one group."""
    return [
        _token(fact.subject),
        _enum_value(Necessity, fact.detail.get("necessity")),
        fact.value,
        _enum_value(RelationKind, fact.detail.get("relation")),
    ]


def _dimension(dimension: DimensionScore) -> dict[str, Any]:
    return {
        "value": dimension.value,
        "weight": dimension.weight,
        "effective_weight": dimension.effective_weight,
        "abstain_reason": _code(dimension.abstain_reason),
    }


def _confidence(confidence: Optional[Confidence]) -> Optional[dict[str, Any]]:
    if confidence is None:
        return None
    return {
        "value": confidence.value,
        "components": {_token(name): value for name, value in sorted(confidence.components.items())},
        "reasons": [flag.value for flag in confidence.reasons],
    }


def build_trace(result: MatchResult) -> dict[str, Any]:
    """The compact, JSON-native audit record of one score (SPEC section 0.4; D-14).

    Deterministic: dimensions are listed by name, requirement rows in dimension order and then in
    the order the scorer wrote them, so equal results give equal traces whatever order a caller
    built its dictionaries in. The input is never changed.
    """
    dims: dict[str, Any] = {}
    requirements: list[list[Any]] = []
    for _, dimension in sorted(result.dimensions.items(), key=lambda item: (item[1].name.value, item[0])):
        dims[dimension.name.value] = _dimension(dimension)
        requirements.extend(
            _requirement_row(fact) for fact in dimension.facts if fact.code in REQUIREMENT_CODES
        )
    calibration = result.calibration
    return {
        "trace_version": TRACE_VERSION,
        "outcome": result.outcome.value,
        "gate_status": result.gate_status.value,
        "raw": result.raw_score,
        "calibrated": result.calibrated_score,
        "band": None if result.band is None else result.band.value,
        "dims": dims,
        "requirements": requirements,
        "caps": [
            {
                "reason": cap.reason.value,
                "cap": cap.cap,
                "requirement_ids": [_token(skill_id) for skill_id in cap.requirement_ids],
            }
            for cap in result.caps
        ],
        "gates": [
            {"kind": gate.kind.value, "status": gate.status.value, "reason": _code(gate.reason)}
            for gate in result.gates
        ],
        "confidence": _confidence(result.confidence),
        "calibration": None
        if calibration is None
        else {
            "method": calibration.method,
            "scope": calibration.scope,
            "n": calibration.n,
            "version": _token(calibration.version),
        },
        "flags": [flag.value for flag in result.flags],
        "reasons": [flag.value for flag in result.reasons],
        "engine_version": _token(result.engine_version),
        "config_hash": _token(result.config_hash),
        "input_digest": _token(result.input_digest),
        "as_of": result.as_of.isoformat(),
    }


def _canonical(trace: dict[str, Any]) -> str:
    """The one serialisation hashed and persisted (the same shape as models.canonical_json)."""
    return json.dumps(trace, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def trace_digest(trace: dict[str, Any]) -> str:
    """sha256 of the trace's canonical JSON, first 16 hex characters. Raises ValueError for a NaN
    or infinity and TypeError for a value JSON cannot hold."""
    return hashlib.sha256(_canonical(trace).encode("utf-8")).hexdigest()[:_DIGEST_HEX]


def opaque_id(user_key: str, salt: bytes) -> str:
    """HMAC-SHA256 of `user_key` under `salt`, first 24 hex characters.

    Demographic records are joined to traces on this value and never on a user id (SPEC section 9).
    The same user and salt always give the same id; another salt gives an unrelated one, so rotating
    the salt severs every earlier link. `salt` must be at least 16 secret bytes kept outside the
    repository and outside the scorer's environment; `user_key` is the stable internal user key,
    never an email address.
    """
    if not isinstance(user_key, str) or not user_key:
        raise ValueError("user_key must be a non-empty string")
    if not isinstance(salt, (bytes, bytearray)) or len(salt) < _MIN_SALT_BYTES:
        raise ValueError(f"salt must be at least {_MIN_SALT_BYTES} bytes")
    digest = hmac.new(bytes(salt), user_key.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:_OPAQUE_ID_HEX]
