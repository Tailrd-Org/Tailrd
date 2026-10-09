"""Reference distributions: the raw scores a percentile is read against (SPEC §7; DECISIONS D-50).

A `ReferenceDistribution` is one group's raw scores, sorted, plus how many DISTINCT candidates
(people) stand behind them. The chain in `percentile.py` goes posting -> family -> global -> panel
and takes the first group with enough people. Keys:

- scope "posting": the posting's id; scope "family": the job family's name.
- scope "global": one pooled group over every posting; its key is a label only (`GLOBAL_KEY`).
- scope "panel": the shipped synthetic candidate profiles, scored offline. The key is the job
  family the profiles were written for, or `GLOBAL_KEY` for the panel pooled over all families.
  A panel is never labelled "family" or "global": `bands.basis` would then tell a person they were
  compared with real candidates when they were compared with a reference group (D-50).

Everything here is plain Python and deterministic. The only I/O in the calibration package is
`load_panel`, which reads the explicit path it is given.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from matcher.calibrate.base import check_raw
from matcher.models import Frozen

MAX_SAMPLES = 5_000  # a reference keeps at most this many scores (a deterministic quantile downsample)
GLOBAL_KEY = "*"  # key of the pooled group: the `global` reference and the panel pooled over families
PANEL_FORMAT = 1  # `"format"` of a panel file

Scope = Literal["posting", "family", "global", "panel"]

_UNIT = Annotated[float, Field(ge=0.0, le=1.0)]


class PanelError(ValueError):
    """A panel file is missing, unreadable, malformed or does not describe a valid panel."""


class ReferenceDistribution(Frozen):
    """The raw scores of one group, sorted, and the number of distinct people behind them.

    `n_candidates` counts people, not (candidate, posting) pairs, and is what the minimum-size rule
    reads (D-50). `n_pairs` is how many scores the group had before any downsampling; `samples`
    holds all of them, or the deterministic quantile downsample when there are more than 5,000.
    """

    scope: Scope
    key: Annotated[str, Field(max_length=200)]
    n_candidates: int = Field(ge=0)
    n_pairs: int = Field(ge=0)
    samples: tuple[_UNIT, ...] = Field(max_length=MAX_SAMPLES)
    version: Annotated[str, Field(min_length=1, max_length=200)]

    @field_validator("samples")
    @classmethod
    def _positive_zero(cls, samples: tuple[float, ...]) -> tuple[float, ...]:
        # -0.0 == 0.0 but serialises differently; one spelling keeps the bytes input-order independent.
        return tuple(score + 0.0 for score in samples)

    @model_validator(mode="after")
    def _consistent(self) -> "ReferenceDistribution":
        if any(low > high for low, high in zip(self.samples, self.samples[1:])):
            raise ValueError("samples must be sorted in ascending order")
        if self.n_candidates > self.n_pairs:
            raise ValueError(
                f"n_candidates ({self.n_candidates}) cannot exceed n_pairs ({self.n_pairs}): "
                "every candidate in a reference has at least one pair"
            )
        if len(self.samples) > self.n_pairs:
            raise ValueError(f"{len(self.samples)} samples cannot come from {self.n_pairs} pairs")
        if self.n_pairs > 0 and not self.samples:
            raise ValueError("a reference with pairs needs samples")
        return self


def _downsample(scores: list[float]) -> tuple[float, ...]:
    """At most `MAX_SAMPLES` of the sorted scores, keeping the shape of the distribution.

    The scores are cut into MAX_SAMPLES blocks of equal mass and each block is represented by its
    middle element, index floor((i + 0.5) x m / k) for block i of m scores (integer arithmetic, so
    no rounding choice and no dependence on the platform). The share of scores below any value
    differs from that of all m scores by at most 0.5 / k, so a percentile read from the result moves
    by at most 0.01 points (k = 5,000), far under the integer people see.
    """
    m, k = len(scores), MAX_SAMPLES
    if m <= k:
        return tuple(scores)
    return tuple(scores[(2 * i + 1) * m // (2 * k)] for i in range(k))


def build_reference(
    scope: Scope,
    key: str,
    raw_scores: Sequence[float],
    candidate_ids: Sequence[str],
    version: str,
) -> ReferenceDistribution:
    """A reference from one raw score per (candidate, posting) pair.

    `candidate_ids[i]` names the person who produced `raw_scores[i]`; `n_candidates` is the number
    of DISTINCT names (D-50), `n_pairs` the number of scores. Scores must be finite and in [0, 1].
    The result does not depend on the order of the pairs.
    """
    if len(raw_scores) != len(candidate_ids):
        raise ValueError(
            f"raw_scores and candidate_ids must be parallel: {len(raw_scores)} scores, "
            f"{len(candidate_ids)} candidate ids"
        )
    scores: list[float] = []
    for index, raw in enumerate(raw_scores):
        what = f"raw_scores[{index}]"
        value = check_raw(raw, what)
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{what} = {raw!r} is outside [0, 1]")
        scores.append(value)
    scores.sort()
    return ReferenceDistribution(
        scope=scope,
        key=key,
        n_candidates=len(set(candidate_ids)),
        n_pairs=len(scores),
        samples=_downsample(scores),
        version=version,
    )


def load_panel(path: str | Path) -> tuple[ReferenceDistribution, ...]:
    """Read a panel file: `{"format": 1, "references": [<ReferenceDistribution JSON>, ...]}`.

    Returns the references in file order. Raises PanelError for a file that is missing or not UTF-8
    JSON, a document of another format, no references, any invalid reference, a reference whose
    scope is not "panel", or two references with the same key. Other top-level keys are ignored.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PanelError(f"cannot read the panel file {path}: {exc}") from exc
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PanelError(f"{path.name} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise PanelError(f"{path.name}: expected a JSON object at the top level")
    found = document.get("format")
    if type(found) is not int or found != PANEL_FORMAT:  # not `True`, which equals 1
        raise PanelError(f"{path.name}: unsupported format {found!r} (expected {PANEL_FORMAT})")
    entries = document.get("references")
    if not isinstance(entries, list) or not entries:
        raise PanelError(f"{path.name}: 'references' must be a non-empty list")

    references: list[ReferenceDistribution] = []
    keys: set[str] = set()
    for index, entry in enumerate(entries):
        try:
            reference = ReferenceDistribution.model_validate(entry)
        except ValidationError as exc:
            raise PanelError(f"{path.name}: references[{index}] is invalid: {exc}") from exc
        if reference.scope != "panel":
            raise PanelError(
                f"{path.name}: references[{index}] has scope {reference.scope!r}; a panel file "
                "holds only 'panel' references (D-50)"
            )
        if reference.key in keys:
            raise PanelError(f"{path.name}: references[{index}] repeats the key {reference.key!r}")
        keys.add(reference.key)
        references.append(reference)
    return tuple(references)
