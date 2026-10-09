"""The skill graph's in-memory form (SPEC §3).

A `SkillGraph` is an input to scoring like a résumé is: built once by `graph.loader`, then
passed to `score()`. It never changes after construction. Traversal lives in
`graph.traversal`; this module only holds and validates the data.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping, Optional

from pydantic import Field

from matcher.models import Frozen, SkillKind


class SkillNode(Frozen):
    id: str = Field(min_length=1, max_length=200)
    label: str = Field(max_length=500)
    kind: SkillKind
    parents: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    release_year: Optional[int] = Field(default=None, ge=1900, le=2100)  # SPEC §14.4
    provisional: bool = False  # SPEC §16: 0.8 multiplier until a human confirms placement
    source: str = Field(default="", max_length=50)  # esco | onet | curated | provisional


class SkillGraph:
    """Nodes, related edges (symmetric weights in [0, 1]) and normalised IDF (in [0, 1]).

    `version` identifies the artifacts the graph was built from; it is folded into the config
    hash persisted with every score.
    """

    __slots__ = ("nodes", "related", "idf", "version", "cache")

    def __init__(
        self,
        nodes: Mapping[str, SkillNode],
        related: Mapping[str, Mapping[str, float]],
        idf: Mapping[str, float],
        version: str,
    ) -> None:
        for node_id, node in nodes.items():
            if node_id != node.id:
                raise ValueError(f"node key {node_id!r} does not match its id {node.id!r}")
            for parent in node.parents:
                if parent not in nodes:
                    raise ValueError(f"{node_id} has unknown parent {parent}")
        frozen_related: dict[str, Mapping[str, float]] = {}
        for a, edges in related.items():
            if a not in nodes:
                raise ValueError(f"related edge from unknown node {a}")
            clean: dict[str, float] = {}
            for b, w in edges.items():
                if b not in nodes:
                    raise ValueError(f"related edge {a}->{b} to unknown node")
                if not (0.0 <= float(w) <= 1.0):
                    raise ValueError(f"related edge {a}->{b} weight {w} outside [0, 1]")
                clean[b] = float(w)
            frozen_related[a] = MappingProxyType(clean)
        for node_id, value in idf.items():
            if not (0.0 <= float(value) <= 1.0):
                raise ValueError(f"idf of {node_id} = {value} outside [0, 1]")
        self.nodes: Mapping[str, SkillNode] = MappingProxyType(dict(nodes))
        self.related: Mapping[str, Mapping[str, float]] = MappingProxyType(frozen_related)
        self.idf: Mapping[str, float] = MappingProxyType({k: float(v) for k, v in idf.items()})
        self.version = version
        # Memo space for graph.traversal (ancestor sets). Derived data only; never part of
        # any output and never mutated by score/.
        self.cache: dict = {}

    def __contains__(self, node_id: object) -> bool:
        return node_id in self.nodes

    def __len__(self) -> int:
        return len(self.nodes)

    def node(self, node_id: str) -> Optional[SkillNode]:
        return self.nodes.get(node_id)

    def idf_of(self, node_id: str, unseen_default: float) -> float:
        """Normalised IDF, or the configured default for skills absent from the corpus
        (DECISIONS D-21)."""
        return self.idf.get(node_id, unseen_default)

    def related_weight(self, a: str, b: str) -> Optional[float]:
        """Edge weight between a and b in either direction, or None."""
        w = self.related.get(a, {}).get(b)
        if w is None:
            w = self.related.get(b, {}).get(a)
        return w
