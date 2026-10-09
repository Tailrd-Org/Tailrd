"""Graph artifacts: write, hash-pin, load (SPEC §0.4 auditability, §3; DECISIONS D-21, D-22)."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path

import pytest

from matcher.graph.idf import normalize_idf
from matcher.graph.loader import (
    ArtifactError,
    load_graph,
    load_json_artifact,
    load_resolver,
    load_resolver_settings,
    write_artifacts,
)
from matcher.graph.model import SkillNode
from matcher.models import SkillKind
from tests.fixtures.build_graph_small import DF, EDGES, FIXTURE_DIR, N_POSTINGS, NODES, SOURCES, build

ARTIFACTS = ("idf.json", "pmi_edges.json")
IDF = normalize_idf(DF, N_POSTINGS)


def _write(target: Path, *, nodes=NODES, idf=IDF, edges=EDGES, n_postings=N_POSTINGS, sources=SOURCES,
           compress: bool = True, extra=None) -> None:
    write_artifacts(target, nodes, idf, edges, n_postings=n_postings, sources=sources, compress=compress,
                    extra=extra)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True)


def _manifest(target: Path) -> dict:
    return json.loads((target / "MANIFEST.json").read_text(encoding="utf-8"))


# ─── round trip ────────────────────────────────────────────────────────────────────────────


def test_round_trip_compressed(tmp_path: Path) -> None:
    _write(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["MANIFEST.json", "idf.json", "nodes.json.gz",
                                                         "pmi_edges.json"]
    graph = load_graph(tmp_path)
    assert dict(graph.nodes) == {node.id: node for node in NODES}
    assert _canonical({a: dict(t) for a, t in graph.related.items()}) == _canonical(EDGES)
    assert _canonical(dict(graph.idf)) == _canonical(IDF)
    react = graph.nodes["skill.react"]
    assert react.kind is SkillKind.TECHNOLOGY
    assert isinstance(react.parents, tuple) and isinstance(react.aliases, tuple)
    assert graph.nodes["skill.svelte"].provisional is True


def test_loaded_nodes_are_indistinguishable_from_validated_ones(tmp_path: Path) -> None:
    _write(tmp_path)
    graph = load_graph(tmp_path)
    for node in NODES:
        loaded = graph.nodes[node.id]
        assert loaded == node and hash(loaded) == hash(node)
        assert loaded.model_dump(mode="json") == node.model_dump(mode="json")
        assert type(loaded.kind) is SkillKind and isinstance(loaded.parents, tuple)
        with pytest.raises(Exception):  # still frozen
            loaded.label = "changed"


def test_round_trip_uncompressed(tmp_path: Path) -> None:
    _write(tmp_path, compress=False)
    assert (tmp_path / "nodes.json").exists() and not (tmp_path / "nodes.json.gz").exists()
    assert dict(load_graph(tmp_path).nodes) == {node.id: node for node in NODES}


def test_switching_format_removes_the_stale_nodes_file(tmp_path: Path) -> None:
    _write(tmp_path)
    _write(tmp_path, compress=False)
    assert not (tmp_path / "nodes.json.gz").exists()
    assert sorted(_manifest(tmp_path)["artifacts"]) == ["idf.json", "nodes.json", "pmi_edges.json"]
    load_graph(tmp_path)


def test_manifest_pins_every_artifact_and_records_sources(tmp_path: Path) -> None:
    _write(tmp_path)
    manifest = _manifest(tmp_path)
    assert manifest["format"] == 1
    assert manifest["sources"] == SOURCES
    for name, digest in manifest["artifacts"].items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest, name
    idf_doc = json.loads((tmp_path / "idf.json").read_text(encoding="utf-8"))
    assert idf_doc["format"] == 1 and idf_doc["n_postings"] == N_POSTINGS


def test_nodes_are_written_sorted_by_id(tmp_path: Path) -> None:
    _write(tmp_path, nodes=tuple(reversed(NODES)), compress=False)
    doc = json.loads((tmp_path / "nodes.json").read_text(encoding="utf-8"))
    ids = [record["id"] for record in doc["nodes"]]
    assert ids == sorted(ids) and doc["format"] == 1


# ─── integrity ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["nodes.json.gz", "idf.json", "pmi_edges.json"])
def test_a_tampered_byte_is_rejected(tmp_path: Path, name: str) -> None:
    _write(tmp_path)
    data = bytearray((tmp_path / name).read_bytes())
    data[len(data) // 2] ^= 0x01
    (tmp_path / name).write_bytes(bytes(data))
    with pytest.raises(ArtifactError, match=re.escape(name)):
        load_graph(tmp_path)


@pytest.mark.parametrize("name", ["nodes.json.gz", "idf.json", "pmi_edges.json", "MANIFEST.json"])
def test_a_missing_file_is_rejected(tmp_path: Path, name: str) -> None:
    _write(tmp_path)
    (tmp_path / name).unlink()
    with pytest.raises(ArtifactError, match=re.escape(name)):
        load_graph(tmp_path)


def test_an_unlisted_required_artifact_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path)
    manifest = _manifest(tmp_path)
    del manifest["artifacts"]["idf.json"]
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="idf.json"):
        load_graph(tmp_path)


def test_an_artifact_name_outside_the_directory_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["artifacts"]["../outside.json"] = "0" * 64
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="outside.json"):
        load_graph(tmp_path)


def test_an_unknown_format_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path)
    manifest = _manifest(tmp_path)
    manifest["format"] = 2
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="format"):
        load_graph(tmp_path)


def _node(node_id: str, *parents: str) -> SkillNode:
    return SkillNode(id=node_id, label=node_id, kind=SkillKind.TECHNOLOGY, parents=parents)


@pytest.mark.parametrize(
    "overrides",
    [
        {"nodes": (_node("skill.a", "skill.missing"),), "idf": {}, "edges": {}},  # unknown parent
        {"nodes": (_node("skill.a"), _node("skill.a")), "idf": {}, "edges": {}},  # duplicate id
        {"nodes": (_node("skill.a"),), "idf": {"skill.zz": 0.5}, "edges": {}},  # idf for an unknown id
        {"nodes": (_node("skill.a"),), "idf": {"skill.a": 1.5}, "edges": {}},  # idf outside [0, 1]
        {"nodes": (_node("skill.a"), _node("skill.b")), "idf": {}, "edges": {"skill.a": {"skill.b": 2.0}}},
        {"n_postings": -1},
    ],
)
def test_invalid_graphs_are_never_written(tmp_path: Path, overrides: dict) -> None:
    with pytest.raises(ArtifactError):
        _write(tmp_path, **overrides)
    assert not (tmp_path / "MANIFEST.json").exists()


# ─── version ───────────────────────────────────────────────────────────────────────────────


def test_version_is_derived_from_the_artifact_hashes(tmp_path: Path) -> None:
    _write(tmp_path)
    artifacts = _manifest(tmp_path)["artifacts"]
    lines = "".join(f"{line}\n" for line in sorted(f"{name}:{digest}" for name, digest in artifacts.items()))
    expected = hashlib.sha256(lines.encode("utf-8")).hexdigest()[:16]
    assert load_graph(tmp_path).version == expected
    # A hand-edited MANIFEST with the same hashes in another order and layout: same version.
    manifest = _manifest(tmp_path)
    manifest["artifacts"] = dict(reversed(list(manifest["artifacts"].items())))
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest, indent=4), encoding="utf-8")
    assert load_graph(tmp_path).version == expected


def test_version_is_stable_across_rebuilds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "first")
    monkeypatch.setattr(time, "time", lambda: 2_000_000_000.0)  # a later clock must not leak into gzip
    _write(tmp_path / "second")
    _write(tmp_path / "other_sources", sources={"fixture": "a different provenance note"})
    for name in ("nodes.json.gz", *ARTIFACTS):
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes(), name
    version = load_graph(tmp_path / "first").version
    assert re.fullmatch(r"[0-9a-f]{16}", version)
    assert load_graph(tmp_path / "second").version == version
    assert load_graph(tmp_path / "other_sources").version == version  # sources are not hashed


def test_version_changes_with_content(tmp_path: Path) -> None:
    _write(tmp_path / "base")
    changed = dict(IDF)
    changed["skill.react"] = 0.5
    _write(tmp_path / "changed", idf=changed)
    assert load_graph(tmp_path / "base").version != load_graph(tmp_path / "changed").version


# ─── extra JSON artifacts and the resolver settings (Task 2.2) ─────────────────────────────

RESOLVER_DOC = {"format": 1, "ambiguous": {"go": "Go", "r": "R"}}
TITLES_DOC = {"format": 1, "titles": {"software engineer": {"soc": "15-1252.00", "family": "x", "job_zone": 4}}}


def test_extra_json_artifacts_are_written_and_hash_pinned(tmp_path: Path) -> None:
    _write(tmp_path)
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": RESOLVER_DOC, "titles.json": TITLES_DOC})
    manifest = _manifest(tmp_path)
    assert sorted(manifest["artifacts"]) == ["idf.json", "nodes.json.gz", "pmi_edges.json", "resolver.json",
                                             "titles.json"]
    assert load_json_artifact(tmp_path, "titles.json") == TITLES_DOC
    assert load_resolver_settings(tmp_path) == RESOLVER_DOC
    load_graph(tmp_path)  # extra artifacts are verified, not interpreted
    # Writing again without extras removes the ones the previous build left behind.
    _write(tmp_path)
    assert sorted(_manifest(tmp_path)["artifacts"]) == ["idf.json", "nodes.json.gz", "pmi_edges.json"]
    assert not (tmp_path / "titles.json").exists()


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"nodes.json.gz": {"format": 1}}, "reserved"),
        ({"MANIFEST.json": {"format": 1}}, "reserved"),
        ({"../titles.json": {"format": 1}}, "invalid"),
        ({"titles.txt": {"format": 1}}, "invalid"),
        ({"titles.json": {"titles": {}}}, "format"),
    ],
)
def test_bad_extra_artifacts_are_refused(tmp_path: Path, extra: dict, message: str) -> None:
    with pytest.raises(ArtifactError, match=message):
        _write(tmp_path, extra=extra)
    assert not (tmp_path / "MANIFEST.json").exists()


def test_a_tampered_or_unlisted_json_artifact_is_refused(tmp_path: Path) -> None:
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": RESOLVER_DOC})
    with pytest.raises(ArtifactError, match="titles.json"):
        load_json_artifact(tmp_path, "titles.json")
    path = tmp_path / "resolver.json"
    path.write_bytes(path.read_bytes().replace(b'"Go"', b'"GO"'))
    with pytest.raises(ArtifactError, match="resolver.json"):
        load_resolver_settings(tmp_path)


def test_resolver_settings_are_required_and_validated(tmp_path: Path, config) -> None:
    _write(tmp_path)
    with pytest.raises(ArtifactError, match="resolver.json"):
        load_resolver(tmp_path, config)
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": {"format": 1, "ambiguous": ["go"]}})
    with pytest.raises(ArtifactError, match="ambiguous"):
        load_resolver_settings(tmp_path)


def test_load_resolver_builds_the_graph_and_the_ambiguous_map_in_one_call(tmp_path: Path, config) -> None:
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": RESOLVER_DOC})
    resolver = load_resolver(tmp_path, config)
    assert resolver.resolve("Go").canonical_id == "skill.go"
    assert resolver.resolve("go").method == "NONE"
    assert resolver.resolve("ReactJS").canonical_id == "skill.react"


# ─── the committed fixture ─────────────────────────────────────────────────────────────────


def test_committed_fixture_is_current(tmp_path: Path) -> None:
    build(tmp_path)
    for name in ("MANIFEST.json", "nodes.json", *ARTIFACTS):
        assert (FIXTURE_DIR / name).read_bytes() == (tmp_path / name).read_bytes(), (
            f"{name} drifted from tests/fixtures/build_graph_small.py; rerun the builder"
        )


def test_committed_fixture_loads(small_graph) -> None:
    assert len(small_graph) == len(NODES)
    assert math.isclose(small_graph.related_weight("skill.vue", "skill.react"), 0.65)
    assert math.isclose(small_graph.related_weight("skill.aws", "skill.terraform"), 0.55)
    assert sorted(_manifest(FIXTURE_DIR)["artifacts"]) == ["idf.json", "nodes.json", "pmi_edges.json"]
