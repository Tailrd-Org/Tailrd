"""Walking the skill graph (SPEC §3, §5.1; DECISIONS D-23).

Pure functions over a `SkillGraph`. Derived indexes are memoised in `graph.cache`; they never
reach any output. Every walk is bounded by `max_depth` and tracks visited nodes, so bad source
data (a parent cycle) cannot make it loop. Credit values for a relation are not computed here:
that is score/'s job, with the config.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Iterable, Mapping, Optional

from pydantic import Field

from matcher.graph.model import SkillGraph
from matcher.models import Frozen, RelationKind

_KIND_ORDER = {RelationKind.EXACT: 0, RelationKind.CHILD: 1, RelationKind.SIBLING: 2, RelationKind.PARENT: 3}
_ADJACENCY_KEY = ("adjacency",)
_RELATED_KEY = ("related_neighbors",)
_EMPTY: Mapping[str, int] = MappingProxyType({})
_NO_NEIGHBORS: Mapping[str, float] = MappingProxyType({})


class Relation(Frozen):
    """How a résumé skill (`have`) relates to a requirement (`want`)."""

    kind: RelationKind
    depth: int = Field(ge=0)  # 0 for EXACT, hops for CHILD / PARENT, 1 for SIBLING
    weight: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # the edge weight, SIBLING only


def ancestors(graph: SkillGraph, node_id: str, max_depth: int) -> Mapping[str, int]:
    """`{ancestor_id: shortest hop count}` for ancestors 1..max_depth parent links up.

    Never contains `node_id` itself, even when a cycle leads back to it. Read-only and memoised
    in `graph.cache` under `("anc", node_id, max_depth)`. Unknown ids have no ancestors.
    """
    key = ("anc", node_id, max_depth)
    cached = graph.cache.get(key)
    if cached is not None:
        return cached
    if node_id not in graph.nodes:
        return _EMPTY  # not memoised: arbitrary ids must not grow the cache
    found: dict[str, int] = {}
    frontier = [node_id]
    for depth in range(1, max_depth + 1):
        next_frontier = []
        for current in frontier:
            for parent in graph.nodes[current].parents:
                if parent != node_id and parent not in found:
                    found[parent] = depth
                    next_frontier.append(parent)
        if not next_frontier:
            break
        frontier = next_frontier
    result = MappingProxyType(found)
    graph.cache[key] = result
    return result


def relations(graph: SkillGraph, have: str, want: str, max_depth: int) -> tuple[Relation, ...]:
    """Every relation from résumé skill `have` to requirement `want`, ordered EXACT, CHILD,
    SIBLING, PARENT; `()` when there is none.

    CHILD: `want` is an ancestor of `have` (depth = hops). PARENT: `have` is an ancestor of
    `want`. SIBLING: a related edge in either direction (depth 1, weight = edge weight). A shared
    parent without an edge is not a relation (DECISIONS D-23).
    """
    if have == want:
        return (Relation(kind=RelationKind.EXACT, depth=0),)
    found: list[Relation] = []
    up = ancestors(graph, have, max_depth).get(want)
    if up is not None:
        found.append(Relation(kind=RelationKind.CHILD, depth=up))
    weight = graph.related_weight(have, want)
    if weight is not None:
        found.append(Relation(kind=RelationKind.SIBLING, depth=1, weight=weight))
    down = ancestors(graph, want, max_depth).get(have)
    if down is not None:
        found.append(Relation(kind=RelationKind.PARENT, depth=down))
    return tuple(sorted(found, key=lambda relation: _KIND_ORDER[relation.kind]))


def related_neighbors(graph: SkillGraph, node_id: str) -> Mapping[str, float]:
    """Every node joined to `node_id` by a related edge stored in either direction (the
    out-degree cap can leave an edge stored one way only, DECISIONS D-22), with the weight
    `graph.related_weight(node_id, other)` gives: the edge stored out of `node_id` if there is
    one, else the edge into it. Never `node_id` itself; empty for unknown ids and for nodes
    without edges. Read-only, keys sorted; the index of every node is built once per graph and
    memoised in `graph.cache`."""
    return _related_index(graph).get(node_id, _NO_NEIGHBORS)


def _related_index(graph: SkillGraph) -> Mapping[str, Mapping[str, float]]:
    cached = graph.cache.get(_RELATED_KEY)
    if cached is not None:
        return cached
    merged: dict[str, dict[str, float]] = {}
    for a, targets in graph.related.items():  # every edge seen from its far end first ...
        for b, weight in targets.items():
            if a != b:
                merged.setdefault(b, {})[a] = weight
    for a, targets in graph.related.items():  # ... then the edges stored out of a node win
        for b, weight in targets.items():
            if a != b:
                merged.setdefault(a, {})[b] = weight
    result = MappingProxyType(
        {node_id: MappingProxyType(dict(sorted(merged[node_id].items()))) for node_id in sorted(merged)}
    )
    graph.cache[_RELATED_KEY] = result
    return result


def distance(graph: SkillGraph, a: str, b: str, max_depth: int) -> Optional[int]:
    """Undirected hop count between two skills over parent links (both ways) and related edges.

    0 when `a == b`; None when the skills are unknown or more than `max_depth` hops apart.
    """
    if a == b:
        return 0
    if a not in graph.nodes or b not in graph.nodes:
        return None
    adjacency = _adjacency(graph)
    seen = {a}
    frontier = [a]
    for depth in range(1, max_depth + 1):
        next_frontier = []
        for current in frontier:
            for neighbour in adjacency[current]:
                if neighbour == b:
                    return depth
                if neighbour not in seen:
                    seen.add(neighbour)
                    next_frontier.append(neighbour)
        if not next_frontier:
            return None
        frontier = next_frontier
    return None


def nearest_distance(graph: SkillGraph, source: str, targets: Iterable[str], max_depth: int) -> Optional[int]:
    """`min(distance(graph, source, t, max_depth) for t in targets)` in one breadth-first walk
    from `source` (None when no target is within `max_depth` hops, or `source` is unknown and not
    itself a target). Used by the counterfactual learning cost (Task 4.1)."""
    wanted = set(targets)
    if source in wanted:
        return 0
    wanted &= set(graph.nodes)
    if source not in graph.nodes or not wanted:
        return None
    adjacency = _adjacency(graph)
    seen = {source}
    frontier = [source]
    for depth in range(1, max_depth + 1):
        next_frontier = []
        for current in frontier:
            for neighbour in adjacency[current]:
                if neighbour in wanted:
                    return depth
                if neighbour not in seen:
                    seen.add(neighbour)
                    next_frontier.append(neighbour)
        if not next_frontier:
            return None
        frontier = next_frontier
    return None


def _adjacency(graph: SkillGraph) -> Mapping[str, tuple[str, ...]]:
    """Undirected neighbours of every node: parents, children and related edges in both
    directions (stored edges can be one-way after the out-degree cap). Built once per graph."""
    cached = graph.cache.get(_ADJACENCY_KEY)
    if cached is not None:
        return cached
    links: dict[str, set[str]] = {node_id: set() for node_id in graph.nodes}
    for node_id, node in graph.nodes.items():
        for parent in node.parents:
            links[node_id].add(parent)
            links[parent].add(node_id)
    for a, targets in graph.related.items():
        for b in targets:
            links[a].add(b)
            links[b].add(a)
    for node_id, neighbours in links.items():
        neighbours.discard(node_id)
    result = MappingProxyType({node_id: tuple(sorted(links[node_id])) for node_id in sorted(links)})
    graph.cache[_ADJACENCY_KEY] = result
    return result
