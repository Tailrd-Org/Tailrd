"""Skill-graph artifacts on disk: written once, hash-pinned, loaded on every cold start.

Formats (all UTF-8 JSON carrying `"format": 1`):

- `nodes.json.gz` (gzip) or `nodes.json`: `{"format":1,"nodes":[<SkillNode JSON>, ...]}` sorted by id
- `idf.json`: `{"format":1,"n_postings":<int>,"idf":{"<id>":<float in [0,1]>}}`
- `pmi_edges.json`: `{"format":1,"edges":{"<id>":{"<id>":<float in [0,1]>}}}`
- `MANIFEST.json`: `{"format":1,"artifacts":{"<file name>":"<sha256 of the file bytes>"},"sources":{...}}`

`load_graph` refuses any listed artifact whose bytes do not match the manifest. The graph version
is derived from the artifact hashes only (sources excluded), so rebuilding identical artifacts
yields the identical version that every score is persisted with (SPEC §0.4).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import zlib
from pathlib import Path
from typing import Any, Iterable, Mapping

from matcher.graph.model import SkillGraph, SkillNode
from matcher.models import SkillKind

FORMAT = 1
NODES_GZ = "nodes.json.gz"
NODES_JSON = "nodes.json"
IDF_JSON = "idf.json"
EDGES_JSON = "pmi_edges.json"
MANIFEST_JSON = "MANIFEST.json"


class ArtifactError(ValueError):
    """A graph artifact is missing, tampered with, malformed or describes an invalid graph."""


# ─── writing ───────────────────────────────────────────────────────────────────────────────


def write_artifacts(
    taxonomy_dir: str | Path,
    nodes: Iterable[SkillNode],
    idf: Mapping[str, float],
    edges: Mapping[str, Mapping[str, float]],
    *,
    n_postings: int,
    sources: Mapping[str, str],
    compress: bool = True,
) -> None:
    """Validate the graph, then write its artifacts and MANIFEST (last) into `taxonomy_dir`.

    Output bytes depend only on the content (sorted keys, gzip without a timestamp), so identical
    inputs give identical files and the identical graph version. `compress=False` writes
    `nodes.json` instead of `nodes.json.gz` (small, diff-friendly fixtures); the other form is
    removed if a previous build left it behind.
    """
    ordered = sorted(nodes, key=lambda node: node.id)
    by_id: dict[str, SkillNode] = {}
    for node in ordered:
        if node.id in by_id:
            raise ArtifactError(f"duplicate node id {node.id}")
        by_id[node.id] = node
    unknown = sorted(set(idf) - set(by_id))
    if unknown:
        raise ArtifactError(f"idf for unknown node ids: {unknown[:5]}")
    if isinstance(n_postings, bool) or not isinstance(n_postings, int) or n_postings < 0:
        raise ArtifactError(f"n_postings must be a non-negative integer, got {n_postings!r}")
    try:
        SkillGraph(by_id, edges, idf, version="")  # the checks load_graph relies on
    except ValueError as exc:
        raise ArtifactError(f"refusing to write an invalid graph: {exc}") from exc

    nodes_bytes = _encode({"format": FORMAT, "nodes": [node.model_dump(mode="json") for node in ordered]})
    payloads = {
        NODES_GZ if compress else NODES_JSON: _gzip(nodes_bytes) if compress else nodes_bytes,
        IDF_JSON: _encode(
            {"format": FORMAT, "n_postings": n_postings, "idf": {k: float(idf[k]) for k in sorted(idf)}}
        ),
        EDGES_JSON: _encode(
            {
                "format": FORMAT,
                "edges": {
                    a: {b: float(w) for b, w in sorted(targets.items())}
                    for a, targets in sorted(edges.items())
                    if targets
                },
            }
        ),
    }
    manifest = {
        "format": FORMAT,
        "artifacts": {name: _sha256(data) for name, data in sorted(payloads.items())},
        "sources": dict(sorted(sources.items())),
    }

    root = Path(taxonomy_dir)
    root.mkdir(parents=True, exist_ok=True)
    for name, data in payloads.items():
        _write_atomic(root / name, data)
    _write_atomic(root / MANIFEST_JSON, _encode(manifest))
    stale = root / (NODES_JSON if compress else NODES_GZ)
    if stale.exists():
        stale.unlink()


def _encode(document: Mapping[str, Any]) -> bytes:
    text = json.dumps(document, sort_keys=True, ensure_ascii=False, indent=1, separators=(",", ":"),
                      allow_nan=False)
    return (text + "\n").encode("utf-8")


def _gzip(data: bytes) -> bytes:
    # GzipFile (not gzip.compress) with mtime=0 and no file name: the header is then fixed bytes
    # (including OS = 255), so the output depends only on the content and zlib.
    buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as fh:
        fh.write(data)
    return buffer.getvalue()


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ─── loading ───────────────────────────────────────────────────────────────────────────────


def load_graph(taxonomy_dir: str | Path) -> SkillGraph:
    """Verify every artifact the MANIFEST lists, then build the graph from them.

    Raises ArtifactError on a missing file, a hash mismatch, an unknown format, a malformed
    document or an invalid graph.
    """
    root = Path(taxonomy_dir)
    manifest = _decode(_read(root, MANIFEST_JSON), MANIFEST_JSON)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) for name, digest in artifacts.items()
    ):
        raise ArtifactError(f"{MANIFEST_JSON}: 'artifacts' must map file names to sha256 hex digests")

    blobs: dict[str, bytes] = {}
    for name in sorted(artifacts):
        if name in ("", ".", "..", MANIFEST_JSON) or Path(name).name != name or "\\" in name:
            raise ArtifactError(f"{MANIFEST_JSON}: invalid artifact name {name!r}")
        data = _read(root, name)
        if _sha256(data) != artifacts[name]:
            raise ArtifactError(f"{name} does not match its {MANIFEST_JSON} hash (tampered or stale)")
        blobs[name] = data

    if NODES_GZ in blobs and NODES_JSON in blobs:
        raise ArtifactError(f"{MANIFEST_JSON} lists both {NODES_GZ} and {NODES_JSON}")
    nodes_name = NODES_GZ if NODES_GZ in blobs else NODES_JSON
    for required in (nodes_name, IDF_JSON, EDGES_JSON):
        if required not in blobs:
            raise ArtifactError(f"{MANIFEST_JSON} does not list {required}")

    nodes_bytes = blobs[nodes_name]
    if nodes_name == NODES_GZ:
        try:
            nodes_bytes = gzip.decompress(nodes_bytes)
        except (OSError, EOFError, zlib.error) as exc:  # bad header / cut short / corrupt deflate data
            raise ArtifactError(f"{NODES_GZ} is not valid gzip: {exc}") from exc
    nodes_doc = _decode(nodes_bytes, nodes_name)
    idf_doc = _decode(blobs[IDF_JSON], IDF_JSON)
    edges_doc = _decode(blobs[EDGES_JSON], EDGES_JSON)

    try:
        nodes: dict[str, SkillNode] = {}
        for record in nodes_doc["nodes"]:
            node = _node_from_record(record)
            if node.id in nodes:
                raise ArtifactError(f"{nodes_name}: duplicate node id {node.id}")
            nodes[node.id] = node
        return SkillGraph(nodes, edges_doc["edges"], idf_doc["idf"], _version(artifacts))
    except ArtifactError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ArtifactError(f"malformed graph artifacts in {root}: {exc!r}") from exc


def _node_from_record(record: Mapping[str, Any]) -> SkillNode:
    # The records were validated by write_artifacts and are pinned by their hash, so skip
    # re-validation (cold-start time) but restore the types validation would have produced.
    return SkillNode.model_construct(
        id=record["id"],
        label=record["label"],
        kind=SkillKind(record["kind"]),
        parents=tuple(record.get("parents", ())),
        aliases=tuple(record.get("aliases", ())),
        release_year=record.get("release_year"),
        provisional=bool(record.get("provisional", False)),
        source=record.get("source", ""),
    )


def _version(artifacts: Mapping[str, str]) -> str:
    """First 16 hex of sha256 over the sorted `name:hash` lines (each newline-terminated)."""
    lines = sorted(f"{name}:{digest}" for name, digest in artifacts.items())
    return hashlib.sha256("".join(f"{line}\n" for line in lines).encode("utf-8")).hexdigest()[:16]


def _read(root: Path, name: str) -> bytes:
    try:
        return (root / name).read_bytes()
    except FileNotFoundError as exc:
        raise ArtifactError(f"missing artifact {name} in {root}") from exc
    except OSError as exc:
        raise ArtifactError(f"cannot read artifact {name} in {root}: {exc}") from exc


def _decode(data: bytes, name: str) -> dict[str, Any]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"{name} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        found = document.get("format") if isinstance(document, dict) else type(document).__name__
        raise ArtifactError(f"{name}: unsupported format {found!r} (expected {FORMAT})")
    return document
