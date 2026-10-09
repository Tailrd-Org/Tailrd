"""The record every metric consumes, and the one grouping helper they share (EVAL.md section 3)."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Annotated

from pydantic import Field

from matcher.models import Frozen

_ID = Annotated[str, Field(min_length=1)]
_GRADE = Annotated[int, Field(ge=0, le=3)]


class PairScore(Frozen):
    """One (resume, posting) pair: the engine's score next to the labels it is judged against.

    `score` is None when the engine abstained (SPEC section 17). An abstained pair is left out of
    every ranking, but its label still counts toward the ideal ordering, so an abstention can never
    improve NDCG. It is required (no default) so a forgotten score cannot pass as an abstention.

    `relevance` is the graded human label: 0 limited, 1 partial, 2 good, 3 strong. `band_pred` and
    `band_true` use the same 0..3 order. `advanced` is the binary outcome when it is known.
    `group` is an opaque fairness group id; a pair without one is invisible to the fairness
    metrics. `hard` marks pairs that are genuinely ambiguous, which abstentions should concentrate
    on (abstention precision).
    """

    posting_id: _ID
    pair_id: _ID
    score: float | None
    relevance: _GRADE
    advanced: bool | None = None
    band_pred: _GRADE | None = None
    band_true: _GRADE | None = None
    group: _ID | None = None
    hard: bool = False


# A metric over some pairs. None means "undefined on this data" (no qualifying posting, one class
# missing, ...), never zero: a missing value must not read as a measured one.
Metric = Callable[[Sequence[PairScore]], float | None]


def group_by_posting(pairs: Iterable[PairScore]) -> dict[str, list[PairScore]]:
    """Pairs grouped by posting id, keys in ascending order, input order kept inside a group."""
    groups: dict[str, list[PairScore]] = {}
    for pair in pairs:
        groups.setdefault(pair.posting_id, []).append(pair)
    return {posting_id: groups[posting_id] for posting_id in sorted(groups)}
