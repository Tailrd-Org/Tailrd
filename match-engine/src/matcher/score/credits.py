"""Bare relation credits: what a set of held ids earns for each wanted id (DECISIONS D-23, D-24).

`coverage.match_skill` scores one requirement against a prepared résumé: the best relation credit
× the evidence and recency value of the mention that earns it. Three uses need the bare credit,
with no evidence and no recency: a role's skills against the posting's requirement ids (experience
relevance, D-35), the candidate's fields of study against the required fields, and a credential
against the posting's skills and certifications (education, D-41).

`RelationTargets(wanted, graph, config).credits(held)` returns, for every wanted id that `held`
relates to, exactly what `match_skill` returns for it on a résumé holding every held id at value
1.0 (tests/test_credits.py holds the two to that): EXACT `relations.exact`; CHILD
`relations.child` for a held id below the wanted one; SIBLING `clamp(edge, sibling_min,
sibling_max)` for an edge stored either way, read as `graph.related_weight(held, wanted)`; PARENT
`relations.parent` for a held id above the wanted one (CHILD and PARENT within
`relations.max_depth`); × `relations.provisional_multiplier` when either id is provisional (D-24).
Equal credits keep the order EXACT, CHILD, SIBLING, PARENT, then the smaller held id, compared at
`numeric.TIE_DECIMALS` places; a credit below `coverage.NO_CREDIT` is no relation. A wanted id
absent from the graph relates by EXACT only.

The walk starts from the held side: each held id's ancestors, its related neighbours (or the
wanted ids, whichever list is shorter, as match_skill walks the smaller side) and the wanted ids
it sits above, indexed once per set of targets. So scoring every role of a résumé against one
posting costs the roles' skills and their neighbourhoods, not roles × requirements × skills.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Optional

from matcher.config import EngineConfig
from matcher.graph.model import SkillGraph
from matcher.graph.traversal import ancestors, related_neighbors
from matcher.models import RelationKind

# One definition of the relation order and of "provisional", shared with requirement matching.
from matcher.score.coverage import NO_CREDIT, _KIND_ORDER, _provisional
from matcher.score.numeric import clamp01, finite, tie_key

__all__ = ["Credit", "RelationTargets", "best_credit"]

Credit = tuple[float, RelationKind, str]  # (bare credit, relation, the held id that earns it)


def _order(credit: Credit) -> tuple[float, int, str]:
    """Smaller is better: the higher credit at tie precision, then the relation order, then the
    smaller held id."""
    return (-tie_key(credit[0]), _KIND_ORDER[credit[1]], credit[2])


class RelationTargets:
    """The wanted ids, indexed once so any number of held sets can be scored against them."""

    __slots__ = ("_graph", "_relations", "_wanted", "_above")

    def __init__(self, wanted: Iterable[str], graph: SkillGraph, config: EngineConfig) -> None:
        self._graph = graph
        self._relations = config.scoring.relations
        self._wanted = frozenset(wanted)
        above: dict[str, list[str]] = {}  # an ancestor of wanted ids -> those wanted ids (PARENT)
        for want in sorted(self._wanted):
            for ancestor in ancestors(graph, want, self._relations.max_depth):
                above.setdefault(ancestor, []).append(want)
        self._above = above

    def credits(self, held: Iterable[str]) -> dict[str, Credit]:
        """`{wanted id: (credit, relation, via)}` for every wanted id `held` relates to, keys sorted."""
        graph, relations, wanted = self._graph, self._relations, self._wanted
        found: dict[str, Credit] = {}

        def offer(want: str, credit: float, kind: RelationKind, via: str) -> None:
            if _provisional(graph, want) or _provisional(graph, via):
                credit *= relations.provisional_multiplier
            candidate = (clamp01(credit), kind, via)
            current = found.get(want)
            if current is None or _order(candidate) < _order(current):
                found[want] = candidate

        for have in sorted(set(held)):
            if have in wanted:
                offer(have, relations.exact, RelationKind.EXACT, have)
            for ancestor in ancestors(graph, have, relations.max_depth):  # `have` sits below it
                if ancestor in wanted:
                    offer(ancestor, relations.child, RelationKind.CHILD, have)
            neighbors = related_neighbors(graph, have)  # weights as related_weight(have, other) reads them
            if len(neighbors) <= len(wanted):
                linked = [want for want in neighbors if want in wanted]
            else:
                linked = [want for want in wanted if want in neighbors]
            for want in linked:
                weight = min(relations.sibling_max, max(relations.sibling_min, finite(neighbors[want])))
                offer(want, weight, RelationKind.SIBLING, have)
            for want in self._above.get(have, ()):
                offer(want, relations.parent, RelationKind.PARENT, have)
        return {want: found[want] for want in sorted(found) if found[want][0] >= NO_CREDIT}


def best_credit(credits: Mapping[str, Credit]) -> Optional[tuple[str, Credit]]:
    """The strongest `(wanted id, credit)` (relation order as above, then the smaller wanted id);
    None when nothing relates."""
    if not credits:
        return None
    want = min(credits, key=lambda key: (_order(credits[key]), key))
    return want, credits[want]
