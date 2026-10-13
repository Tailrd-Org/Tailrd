"""Graph artifacts: write, hash-pin, load (SPEC §0.4 auditability, §3; DECISIONS D-21, D-22)."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
import time
from pathlib import Path

import pytest

from matcher.graph import loader as loader_module
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


@pytest.mark.parametrize(
    "payload",
    [
        b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\x07" + b"\x00" * 8,  # gzip header, invalid deflate block
        gzip.compress(b'{"format":1,"nodes":[]}', mtime=0)[:-12],  # stream cut short
        b"not gzip at all",  # no gzip header
    ],
)
def test_a_corrupt_gzip_stream_is_rejected(tmp_path: Path, payload: bytes) -> None:
    _write(tmp_path)
    (tmp_path / "nodes.json.gz").write_bytes(payload)
    manifest = _manifest(tmp_path)
    manifest["artifacts"]["nodes.json.gz"] = hashlib.sha256(payload).hexdigest()  # passes the hash check
    (tmp_path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactError, match="nodes.json.gz"):
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
    load_graph(tmp_path)  # the graph does not read the extra artifacts
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


def test_load_graph_verifies_only_the_files_it_reads(tmp_path: Path) -> None:
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": RESOLVER_DOC, "titles.json": TITLES_DOC})
    version = load_graph(tmp_path).version
    path = tmp_path / "titles.json"
    path.write_bytes(path.read_bytes().replace(b"15-1252.00", b"15-1252.01"))
    assert load_graph(tmp_path).version == version  # cold start never hashes what it does not use
    with pytest.raises(ArtifactError, match="titles.json"):
        load_json_artifact(tmp_path, "titles.json")  # its own reader still refuses it


def test_load_resolver_reads_the_manifest_once(tmp_path: Path, config, monkeypatch: pytest.MonkeyPatch) -> None:
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": RESOLVER_DOC, "titles.json": TITLES_DOC})
    reads: list[str] = []
    original = loader_module._read

    def counting(root: Path, name: str) -> bytes:
        reads.append(name)
        return original(root, name)

    monkeypatch.setattr(loader_module, "_read", counting)
    load_resolver(tmp_path, config)
    assert sorted(reads) == ["MANIFEST.json", "idf.json", "nodes.json.gz", "pmi_edges.json", "resolver.json"]


@pytest.mark.parametrize(
    "settings, message",
    [
        ({"format": 1}, "ambiguous"),
        ({"format": 1, "ambiguous": {}, "cased": ["Go"]}, "cased"),
        ({"format": 1, "ambiguous": {}, "cased": {"go": 1}}, "cased"),
        ({"format": 1, "ambiguous": {}, "blocked_phrases": "R&D"}, "blocked_phrases"),
        ({"format": 1, "ambiguous": {}, "blocked_phrases": ["R&D", ""]}, "blocked_phrases"),
        ({"format": 1, "ambiguous": {}, "in_progress": ["cred.cpa"]}, "in_progress"),
        ({"format": 1, "ambiguous": {}, "in_progress": {"cred.cpa": 1}}, "in_progress"),
        ({"format": 1, "ambiguous": {}, "currency_skill_nouns": "drawings"}, "currency_skill_nouns"),
        ({"format": 1, "ambiguous": {}, "currency_skill_nouns": ["drawings", ""]}, "currency_skill_nouns"),
        ({"format": 1, "ambiguous": {}, "fuzzy_exclusions": "patients"}, "fuzzy_exclusions"),
        ({"format": 1, "ambiguous": {}, "fuzzy_exclusions": ["patients", 3]}, "fuzzy_exclusions"),
        ({"format": 1, "ambiguous": {}, "sentence_initial": ["go"]}, "sentence_initial"),
        ({"format": 1, "ambiguous": {}, "sentence_initial": {"keys": "go"}}, "sentence_initial"),
        ({"format": 1, "ambiguous": {}, "sentence_initial": {"keys": ["go"], "cues": [""]}}, "sentence_initial"),
        ({"format": 1, "ambiguous": {}, "sentence_initial": {"keys": ["go"], "other": []}}, "sentence_initial"),
        ({"format": 1, "ambiguous": {}, "sentence_initial": {"keys": ["go"], "languages": ["english"]}},
         "sentence_initial"),  # a language must be one of the keys
    ],
)
def test_bad_resolver_settings_are_refused(tmp_path: Path, settings: dict, message: str) -> None:
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": settings})
    with pytest.raises(ArtifactError, match=message):
        load_resolver_settings(tmp_path)


@pytest.mark.parametrize("line", ["Shipped React - Native apps.", "Stack: React: Native apps."])
def test_load_resolver_applies_the_in_progress_map(tmp_path: Path, config, line: str) -> None:
    # D-28: the map's in-progress nodes are formal designations, read only as written, never across
    # ":" or a dash; any other node may span them.
    found = {}
    for name, in_progress in (("plain", {}), ("mapped", {"skill.react": "skill.react_native"})):
        settings = {"format": 1, "ambiguous": {}, "in_progress": in_progress}
        write_artifacts(tmp_path / name, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                        extra={"resolver.json": settings})
        found[name] = [m.canonical_id for m in load_resolver(tmp_path / name, config).extract(line)]
    assert found == {"plain": ["skill.react_native"], "mapped": ["skill.react"]}


def test_load_resolver_applies_the_currency_skill_nouns(tmp_path: Path, config) -> None:
    # D-27: after an amount a currency code is money unless one of these nouns follows it.
    nodes = (*NODES, SkillNode(id="skill.cad", label="CAD", kind=SkillKind.TOOL, source="curated"))
    found = {}
    for name, nouns in (("plain", None), ("nouns", ["drawings"])):
        settings = {"format": 1, "ambiguous": {"cad": "CAD"}}
        if nouns is not None:
            settings["currency_skill_nouns"] = nouns
        write_artifacts(tmp_path / name, nodes, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                        extra={"resolver.json": settings})
        found[name] = [m.canonical_id for m in load_resolver(tmp_path / name, config).extract(
            "Produced 1,200 CAD drawings for the plant.")]
    assert found == {"plain": [], "nouns": ["skill.cad"]}


def test_load_resolver_applies_the_common_words(tmp_path: Path, config, monkeypatch: pytest.MonkeyPatch) -> None:
    # Task 2.3: common_words.json (hash-pinned like every extra) lists corpus words the fuzzy
    # fallback never reads as typos; without the file the resolver works as before.
    settings = {"format": 1, "ambiguous": {}}
    write_artifacts(tmp_path / "plain", NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": settings})
    write_artifacts(tmp_path / "words", NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": settings, "common_words.json": {"format": 1, "words": ["kubernets"]}})
    assert load_resolver(tmp_path / "plain", config).resolve("kubernets").canonical_id == "skill.kubernetes"
    assert "common_words.json" in _manifest(tmp_path / "words")["artifacts"]
    reads: list[str] = []
    original = loader_module._read

    def counting(root: Path, name: str) -> bytes:
        reads.append(name)
        return original(root, name)

    monkeypatch.setattr(loader_module, "_read", counting)
    guarded = load_resolver(tmp_path / "words", config)
    assert guarded.resolve("kubernets").method == "NONE"
    assert guarded.resolve("kubernetess").canonical_id == "skill.kubernetes"  # another typo still fuzzes
    assert sorted(reads) == ["MANIFEST.json", "common_words.json", "idf.json", "nodes.json.gz", "pmi_edges.json",
                             "resolver.json"]


@pytest.mark.parametrize("document", [{"format": 1}, {"format": 1, "words": "kubernets"},
                                      {"format": 1, "words": ["kubernets", ""]}, {"format": 1, "words": [3]}])
def test_bad_common_words_are_refused(tmp_path: Path, config, document: dict) -> None:
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": {"format": 1, "ambiguous": {}}, "common_words.json": document})
    with pytest.raises(ArtifactError, match="common_words.json"):
        load_resolver(tmp_path, config)


def test_a_tampered_common_words_file_is_refused(tmp_path: Path, config) -> None:
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": {"format": 1, "ambiguous": {}},
                           "common_words.json": {"format": 1, "words": ["kubernets"]}})
    path = tmp_path / "common_words.json"
    path.write_bytes(path.read_bytes().replace(b"kubernets", b"kubernetz"))
    with pytest.raises(ArtifactError, match="common_words.json"):
        load_resolver(tmp_path, config)


def test_load_resolver_applies_cased_keys_and_blocked_phrases(tmp_path: Path, config) -> None:
    settings = {"format": 1, "ambiguous": {"go": "Go"}, "cased": {"react": "React"}, "blocked_phrases": ["Go-Live"]}
    write_artifacts(tmp_path, NODES, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": settings})
    resolver = load_resolver(tmp_path, config)
    assert [m.canonical_id for m in resolver.extract("go, react, React", context="list")] == ["skill.go", "skill.react"]
    assert resolver.resolve("Go-Live").method == "NONE"
    mentions = resolver.extract("Led the Go Live of the Go service.")  # "Go Live": a variant of the phrase
    assert [(m.canonical_id, m.surface) for m in mentions] == [("skill.go", "Go")]


# ─── the fast node path (cold start) and its fallback ──────────────────────────────────────


def test_the_fast_node_path_holds_on_this_pydantic() -> None:
    assert loader_module._fast_nodes_hold() is True


def test_the_fast_node_path_steps_aside_when_pydantic_internals_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    class Wider(SkillNode):
        note: str = ""

    monkeypatch.setattr(loader_module, "SkillNode", Wider)
    assert loader_module._fast_nodes_hold() is False  # another field set: never guess the layout

    def broken(values):
        raise AttributeError("__pydantic_fields_set__")

    monkeypatch.setattr(loader_module, "SkillNode", SkillNode)
    monkeypatch.setattr(loader_module, "_fast_node", broken)
    assert loader_module._fast_nodes_hold() is False  # internals that refuse the shortcut


def test_nodes_built_by_model_construct_load_the_same_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path)
    fast = load_graph(tmp_path)
    monkeypatch.setattr(loader_module, "_FAST_NODES", False)
    slow = load_graph(tmp_path)
    assert dict(fast.nodes) == dict(slow.nodes)
    assert all(node.model_dump() == slow.nodes[node.id].model_dump() for node in fast.nodes.values())


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


def test_load_resolver_passes_the_fuzzy_exclusions_and_the_sentence_initial_rule(tmp_path: Path, config) -> None:
    # Task 7.3 (D-140): both settings are optional; when present they reach the resolver.
    nodes = (*NODES, SkillNode(id="esco.patents", label="patents", kind=SkillKind.DOMAIN),
             SkillNode(id="esco.english", label="English", kind=SkillKind.DOMAIN))
    settings = {"format": 1, "ambiguous": {"go": "Go"}, "cased": {"english": "English"},
                "fuzzy_exclusions": ["patients"],
                "sentence_initial": {"keys": ["english", "go"], "languages": ["english"], "cues": ["fluent"]}}
    write_artifacts(tmp_path, nodes, IDF, EDGES, n_postings=N_POSTINGS, sources=SOURCES,
                    extra={"resolver.json": settings})
    assert load_resolver_settings(tmp_path) == settings
    resolver = load_resolver(tmp_path, config)
    assert resolver.resolve("patients").method == "NONE"
    assert resolver.resolve("patentss").canonical_id == "esco.patents"
    assert [m.canonical_id for m in resolver.extract("English, fluent.")] == ["esco.english"]
