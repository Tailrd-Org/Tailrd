"""Skill-graph artifacts on disk: written once, hash-pinned, loaded on every cold start.

Formats (all UTF-8 JSON carrying `"format": 1`):

- `nodes.json.gz` (gzip) or `nodes.json`: `{"format":1,"nodes":[<SkillNode JSON>, ...]}` sorted by id
- `idf.json`: `{"format":1,"n_postings":<int>,"idf":{"<id>":<float in [0,1]>}}`
- `pmi_edges.json`: `{"format":1,"edges":{"<id>":{"<id>":<float in [0,1]>}}}`
- `MANIFEST.json`: `{"format":1,"artifacts":{"<file name>":"<sha256 of the file bytes>"},"sources":{...}}`
- extra JSON documents (`resolver.json`, `titles.json`, ...): any `{"format":1, ...}` object, written
  next to the graph by `write_artifacts(extra=...)` and pinned in the same MANIFEST. The resolver
  settings are `resolver.json`: `{"format":1,"ambiguous":{"<key>":"<required casing>"},
  "cased":{"<key>":"<required casing>"},"blocked_phrases":["<phrase>", ...],
  "in_progress":{"<credential id>":"<id of its formal in-progress designations>"},
  "currency_skill_nouns":["<noun>", ...]}`.

Every reader refuses an artifact whose bytes do not match the manifest: `load_graph` checks the
three graph files it reads, `load_json_artifact` the document it is asked for. The graph version
is derived from the hashes of every listed artifact (sources excluded), so rebuilding identical
artifacts yields the identical version that every score is persisted with (SPEC §0.4).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Optional

from matcher.graph.model import SkillGraph, SkillNode
from matcher.models import SkillKind

if TYPE_CHECKING:  # imported lazily at call time: graph/ must not need normalize/ to load
    from matcher.config import EngineConfig
    from matcher.normalize.fuzzy import FuzzyLog
    from matcher.normalize.resolver import Resolver

FORMAT = 1
NODES_GZ = "nodes.json.gz"
NODES_JSON = "nodes.json"
IDF_JSON = "idf.json"
EDGES_JSON = "pmi_edges.json"
MANIFEST_JSON = "MANIFEST.json"
RESOLVER_JSON = "resolver.json"
_RESERVED = frozenset({NODES_GZ, NODES_JSON, IDF_JSON, EDGES_JSON, MANIFEST_JSON})
_EXTRA_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json")


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
    extra: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> None:
    """Validate the graph, then write its artifacts and MANIFEST (last) into `taxonomy_dir`.

    Output bytes depend only on the content (sorted keys, gzip without a timestamp), so identical
    inputs give identical files and the identical graph version. `compress=False` writes
    `nodes.json` instead of `nodes.json.gz` (small, diff-friendly fixtures); the other form is
    removed if a previous build left it behind. `extra` maps further `*.json` file names to
    documents carrying `"format": 1`; they are pinned in the MANIFEST like the graph files, and
    an extra file the previous MANIFEST listed but this call does not write is removed.
    """
    extra_payloads = _encode_extra(extra or {})
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
        **extra_payloads,
    }
    manifest = {
        "format": FORMAT,
        "artifacts": {name: _sha256(data) for name, data in sorted(payloads.items())},
        "sources": dict(sorted(sources.items())),
    }

    root = Path(taxonomy_dir)
    root.mkdir(parents=True, exist_ok=True)
    previous = _previously_listed(root)
    for name, data in payloads.items():
        _write_atomic(root / name, data)
    _write_atomic(root / MANIFEST_JSON, _encode(manifest))
    for name in sorted(previous | {NODES_JSON if compress else NODES_GZ}):
        stale = root / name
        if name not in payloads and name != MANIFEST_JSON and stale.exists():
            stale.unlink()


def _encode_extra(extra: Mapping[str, Mapping[str, Any]]) -> dict[str, bytes]:
    payloads = {}
    for name in sorted(extra):
        if name in _RESERVED:
            raise ArtifactError(f"{name} is a reserved artifact name")
        if not isinstance(name, str) or not _EXTRA_NAME.fullmatch(name):
            raise ArtifactError(f"invalid extra artifact name {name!r} (a plain *.json file name)")
        document = extra[name]
        if not isinstance(document, Mapping) or document.get("format") != FORMAT:
            raise ArtifactError(f"{name}: an extra artifact must be an object with \"format\": {FORMAT}")
        payloads[name] = _encode(document)
    return payloads


def _previously_listed(root: Path) -> set[str]:
    """File names the MANIFEST already in `root` lists (plain names only), or none."""
    try:
        artifacts = json.loads((root / MANIFEST_JSON).read_text(encoding="utf-8")).get("artifacts", {})
    except (OSError, ValueError, AttributeError):
        return set()
    if not isinstance(artifacts, dict):
        return set()
    return {name for name in artifacts if isinstance(name, str) and _is_plain_name(name)}


def _is_plain_name(name: str) -> bool:
    return name not in ("", ".", "..") and Path(name).name == name and "\\" not in name and "/" not in name


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
    """Verify the artifacts the graph is built from (nodes, idf, edges), then build it.

    Other listed documents (`titles.json`, ...) are verified by `load_json_artifact` when read.
    Raises ArtifactError on a missing file, a hash mismatch, an unknown format, a malformed
    document or an invalid graph.
    """
    root = Path(taxonomy_dir)
    return _load_graph(root, _manifest_artifacts(root))


def _load_graph(root: Path, artifacts: Mapping[str, str]) -> SkillGraph:
    if NODES_GZ in artifacts and NODES_JSON in artifacts:
        raise ArtifactError(f"{MANIFEST_JSON} lists both {NODES_GZ} and {NODES_JSON}")
    nodes_name = NODES_GZ if NODES_GZ in artifacts else NODES_JSON
    for required in (nodes_name, IDF_JSON, EDGES_JSON):
        if required not in artifacts:
            raise ArtifactError(f"{MANIFEST_JSON} does not list {required}")
    blobs = {name: _verified(root, name, artifacts) for name in (nodes_name, IDF_JSON, EDGES_JSON)}

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


def load_json_artifact(taxonomy_dir: str | Path, name: str) -> dict[str, Any]:
    """One JSON artifact the MANIFEST lists, hash-verified and decoded (`"format": 1` checked).

    Raises ArtifactError when the MANIFEST does not list it, or it is missing, tampered with or
    malformed.
    """
    root = Path(taxonomy_dir)
    return _json_artifact(root, name, _manifest_artifacts(root))


def _json_artifact(root: Path, name: str, artifacts: Mapping[str, str]) -> dict[str, Any]:
    if name not in artifacts:
        raise ArtifactError(f"{MANIFEST_JSON} in {root} does not list {name}")
    return _decode(_verified(root, name, artifacts), name)


def load_resolver_settings(taxonomy_dir: str | Path) -> dict[str, Any]:
    """The hash-verified `resolver.json`: `{"format": 1, "ambiguous": {key: required casing},
    "cased": {key: required casing}, "blocked_phrases": [phrase, ...], "in_progress": {credential
    id: in-progress id}, "currency_skill_nouns": [noun, ...]}`. `ambiguous` holds the curated keys,
    `cased` the automatic ones (D-26). `in_progress` links each credential that has formal
    in-progress designations to their node (D-28: the professional licences and the CFA charter,
    which is no gated licence); which licences the gate requires is `config/gates.yaml`
    (`license.professional_credential_ids`), not this map. `currency_skill_nouns` keep a currency
    code that is also a skill after an amount ("1,200 CAD drawings", D-27). All but `ambiguous`
    may be absent (empty)."""
    root = Path(taxonomy_dir)
    return _resolver_settings(root, _manifest_artifacts(root))


def _resolver_settings(root: Path, artifacts: Mapping[str, str]) -> dict[str, Any]:
    settings = _json_artifact(root, RESOLVER_JSON, artifacts)
    if "ambiguous" not in settings:
        raise ArtifactError(f"{RESOLVER_JSON}: 'ambiguous' is missing")
    for name in ("ambiguous", "cased", "in_progress"):
        mapping = settings.get(name, {})
        if not isinstance(mapping, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
        ):
            raise ArtifactError(f"{RESOLVER_JSON}: {name!r} must map strings to strings")
    for name in ("blocked_phrases", "currency_skill_nouns"):
        values = settings.get(name, [])
        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
            raise ArtifactError(f"{RESOLVER_JSON}: {name!r} must be a list of non-empty strings")
    return settings


def load_resolver(
    taxonomy_dir: str | Path, config: "EngineConfig", *, fuzzy_log: Optional["FuzzyLog"] = None
) -> "Resolver":
    """The graph and its resolver settings in one call (one MANIFEST read): a ready `Resolver`."""
    from matcher.normalize.resolver import Resolver

    root = Path(taxonomy_dir)
    artifacts = _manifest_artifacts(root)
    settings = _resolver_settings(root, artifacts)
    return Resolver(
        _load_graph(root, artifacts),
        config,
        ambiguous=settings["ambiguous"],
        cased=settings.get("cased", {}),
        blocked_phrases=settings.get("blocked_phrases", ()),
        in_progress=settings.get("in_progress", {}),
        currency_skill_nouns=settings.get("currency_skill_nouns", ()),
        fuzzy_log=fuzzy_log,
    )


def _manifest_artifacts(root: Path) -> dict[str, str]:
    manifest = _decode(_read(root, MANIFEST_JSON), MANIFEST_JSON)
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not all(
        isinstance(name, str) and isinstance(digest, str) for name, digest in artifacts.items()
    ):
        raise ArtifactError(f"{MANIFEST_JSON}: 'artifacts' must map file names to sha256 hex digests")
    for name in artifacts:
        if name == MANIFEST_JSON or not _is_plain_name(name):
            raise ArtifactError(f"{MANIFEST_JSON}: invalid artifact name {name!r}")
    return artifacts


def _verified(root: Path, name: str, artifacts: Mapping[str, str]) -> bytes:
    data = _read(root, name)
    if _sha256(data) != artifacts[name]:
        raise ArtifactError(f"{name} does not match its {MANIFEST_JSON} hash (tampered or stale)")
    return data


_NODE_FIELDS = ("id", "label", "kind", "parents", "aliases", "release_year", "provisional", "source")
_set_attribute = object.__setattr__


def _node_from_record(record: Mapping[str, Any]) -> SkillNode:
    # The records were validated by write_artifacts and are pinned by their hash, so skip
    # re-validation (cold-start time) but restore the types validation would have produced.
    values = {
        "id": record["id"],
        "label": record["label"],
        "kind": SkillKind(record["kind"]),
        "parents": tuple(record.get("parents", ())),
        "aliases": tuple(record.get("aliases", ())),
        "release_year": record.get("release_year"),
        "provisional": bool(record.get("provisional", False)),
        "source": record.get("source", ""),
    }
    if not _FAST_NODES:
        return SkillNode.model_construct(**values)
    return _fast_node(values)


def _fast_node(values: dict[str, Any]) -> SkillNode:
    """What `SkillNode.model_construct` stores for values that supply every field, without its
    generic per-field loop (a taxonomy holds tens of thousands of nodes; cold start counts)."""
    node = SkillNode.__new__(SkillNode)
    _set_attribute(node, "__dict__", values)
    _set_attribute(node, "__pydantic_fields_set__", set(_NODE_FIELDS))
    _set_attribute(node, "__pydantic_extra__", None)
    _set_attribute(node, "__pydantic_private__", None)
    return node


def _fast_nodes_hold() -> bool:
    """Whether `_fast_node` is safe on the installed pydantic: SkillNode still has the plain shape
    it relies on (read through getattr: the private names vary across pydantic 2.x releases), and a
    sample node built both ways is the same. Otherwise every node goes through model_construct."""
    fields = getattr(SkillNode, "__pydantic_fields__", None) or getattr(SkillNode, "model_fields", None)
    if (
        not isinstance(fields, Mapping)
        or tuple(fields) != _NODE_FIELDS
        or getattr(SkillNode, "__pydantic_post_init__", None) is not None
        or getattr(SkillNode, "__private_attributes__", None)
        or getattr(SkillNode, "__pydantic_root_model__", False)
    ):
        return False
    sample = {
        "id": "skill.sample",
        "label": "Sample",
        "kind": SkillKind.TECHNOLOGY,
        "parents": ("skill.parent",),
        "aliases": ("sample alias",),
        "release_year": 2020,
        "provisional": True,
        "source": "curated",
    }
    try:
        fast, built = _fast_node(dict(sample)), SkillNode.model_construct(**sample)
        return (
            fast == built
            and fast.model_dump() == built.model_dump()
            and fast.model_fields_set == built.model_fields_set
            and hash(fast) == hash(built)
        )
    except Exception:  # any internals mismatch: take the public constructor
        return False


_FAST_NODES = _fast_nodes_hold()


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
