"""The taxonomy builder (Task 2.2; SPEC §3, §14.3, §14.4; DECISIONS D-13, D-20).

Every test builds from tests/fixtures/raw_mini (a few rows in the real file layouts) with the
curated config in tests/fixtures/curated_mini, so no network and no real download is needed.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from matcher.graph.loader import ArtifactError, load_graph, load_json_artifact, load_resolver
from matcher.models import SkillKind
from tests.fixtures import build_raw_mini

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_taxonomy.py"
_spec = importlib.util.spec_from_file_location("build_taxonomy", _SCRIPT)
builder = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("build_taxonomy", builder)  # dataclasses look their module up while loading
_spec.loader.exec_module(builder)

RAW_MINI = build_raw_mini.RAW_MINI
CURATED = Path(__file__).resolve().parent / "fixtures" / "curated_mini" / "curated_taxonomy.yaml"
EDGES = Path(__file__).resolve().parent / "fixtures" / "curated_mini" / "curated_edges.yaml"
REQUIRED = ("cred.registered_nurse",)
EXTRA_JSON = ("mos_crosswalk.json", "naics.json", "resolver.json", "titles.json")


def _build(out: Path, **overrides):
    options = {"curated_path": CURATED, "edges_path": EDGES, "required_ids": REQUIRED, **overrides}
    return builder.build(RAW_MINI, out, **options)


@pytest.fixture(scope="module")
def built(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("taxonomy_mini")
    _build(out)
    return out


@pytest.fixture(scope="module")
def graph(built):
    return load_graph(built)


def _aliases(graph, node_id: str) -> set[str]:
    return set(graph.nodes[node_id].aliases)


def _json(built: Path, name: str) -> dict:
    return json.loads((built / name).read_text(encoding="utf-8"))


# ─── fixture and determinism ───────────────────────────────────────────────────────────────


def test_raw_mini_fixture_is_current() -> None:
    for relative, text in build_raw_mini.files().items():
        assert (RAW_MINI / relative).read_bytes() == text.encode("utf-8"), relative


def test_build_is_deterministic(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    _build(first)
    _build(second)
    names = sorted(p.name for p in first.iterdir())
    assert names == sorted(p.name for p in second.iterdir())
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name
    assert load_graph(first).version == load_graph(second).version


def test_every_json_artifact_is_hash_pinned(built: Path) -> None:
    manifest = _json(built, "MANIFEST.json")
    assert sorted(manifest["artifacts"]) == sorted(("idf.json", "nodes.json.gz", "pmi_edges.json", *EXTRA_JSON))
    assert (built / "SOURCES.md").exists()
    assert (built / ".gitattributes").read_text(encoding="utf-8").strip().endswith("* -text")
    for name in EXTRA_JSON:
        assert load_json_artifact(built, name)["format"] == 1


def test_a_tampered_extra_artifact_is_rejected_by_its_own_reader(built: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(built, copy)
    titles = copy / "titles.json"
    titles.write_bytes(titles.read_bytes().replace(b"software engineer", b"software engineeR"))
    assert load_graph(copy).version == load_graph(built).version  # the graph never reads titles.json
    with pytest.raises(ArtifactError, match="titles.json"):
        load_json_artifact(copy, "titles.json")


def test_the_cli_build_is_byte_identical_under_different_hash_seeds(tmp_path: Path) -> None:
    # Set and dict order must never reach the bytes: two processes with different string hashing.
    outputs = []
    for seed in ("1", "4242"):
        out = tmp_path / f"seed_{seed}"
        env = {**os.environ, "PYTHONHASHSEED": seed}
        subprocess.run([sys.executable, str(_SCRIPT), "--raw", str(RAW_MINI), "--out", str(out),
                        "--curated", str(CURATED), "--edges", str(EDGES), "--required", *REQUIRED],
                       check=True, env=env, capture_output=True, timeout=300)
        outputs.append(out)
    first, second = outputs
    names = sorted(p.name for p in first.iterdir())
    assert names == sorted(p.name for p in second.iterdir()) and "MANIFEST.json" in names
    for name in names:
        assert (first / name).read_bytes() == (second / name).read_bytes(), name


def test_the_cli_requires_the_gate_credentials_unless_told_otherwise(tmp_path: Path) -> None:
    # Without --required the CLI checks config/gates.yaml's professional licences, which the mini
    # curated config lacks; "--required" alone checks none.
    result = subprocess.run([sys.executable, str(_SCRIPT), "--raw", str(RAW_MINI), "--out", str(tmp_path / "a"),
                             "--curated", str(CURATED), "--edges", str(EDGES)], capture_output=True, text=True,
                            timeout=300)
    assert result.returncode != 0 and "cred.cpa" in result.stderr
    subprocess.run([sys.executable, str(_SCRIPT), "--raw", str(RAW_MINI), "--out", str(tmp_path / "b"),
                    "--curated", str(CURATED), "--edges", str(EDGES), "--required"], check=True,
                   capture_output=True, timeout=300)


def test_idf_is_empty_and_edges_are_the_curated_ones(built: Path) -> None:
    assert _json(built, "idf.json")["idf"] == {}
    edges = _json(built, "pmi_edges.json")["edges"]
    assert edges["skill.react"]["skill.javascript"] == edges["skill.javascript"]["skill.react"] == 0.6
    assert edges["cred.registered_nurse"]["field.science"] == 0.5
    assert sorted(edges) == ["cred.registered_nurse", "field.science", "skill.javascript", "skill.react"]


# ─── merging into curated nodes ────────────────────────────────────────────────────────────


def test_esco_and_onet_merge_into_the_curated_node(graph) -> None:
    python = graph.nodes["skill.python"]
    assert (python.label, python.kind, python.release_year, python.source) == (
        "Python", SkillKind.TECHNOLOGY, 1991, "curated")
    assert set(python.aliases) == {"Python 3", "Python (computer programming)", "Python (programmation informatique)",
                                   "Python programming", "Python 2.7", "langage Python"}
    assert not any(node_id.startswith("esco.python") for node_id in graph.nodes)
    # Curated parents absent: the merged sources' parents are inherited (ESCO broader, O*NET class),
    # the ESCO "computer programming" concept being merged into skill.programming itself.
    assert python.parents == ("cat.43232405", "esco.group.0613", "skill.programming")


def test_a_merged_concept_keeps_only_alternatives_that_share_a_content_word(graph) -> None:
    # D-26: an alternative or hidden label of a merged concept becomes an alias only when it has
    # several words and one of them is a content word of the curated node's own names; "web
    # design" under computer programming, a single word ("coding", "Py") never.
    assert {"Python programming", "Python 2.7", "langage Python"} <= _aliases(graph, "skill.python")
    assert "Py" not in _aliases(graph, "skill.python")
    assert _aliases(graph, "skill.programming") == {"programmation informatique", "software programming"}
    assert {"statistical learning", "apprentissage automatique"} <= _aliases(graph, "skill.machine_learning")
    # "apprentissage machine" shares only "machine" with "Machine learning": a generic word.
    assert "apprentissage machine" not in _aliases(graph, "skill.machine_learning")
    # Not merged: the concept keeps every label that passes the filters.
    assert {"Ruby", "Ruby language"} <= _aliases(graph, "esco.ruby_computer_programming")


@pytest.mark.parametrize(
    "label, shares",
    [
        ("web design", False),  # under "Computer programming": no word in common
        ("software programming", True),
        ("C and Smalltalk", False),  # a one-letter word says nothing (under "Objective-C")
        ("gestion de programmes", False),  # "gestion", "programme": generic words
        ("gestion de projets agiles", True),  # French function words dropped, a plural "s" too
        ("financial planning", False),  # under forecasting ("financial forecasting"): generic words only
        ("regulatory systems", False),  # under "Control systems"
        ("plasma mechanics", False),  # under "Fluid mechanics"
        ("customer support", True),  # under "Customer service"
        ("supervised learning", True),  # under "Machine learning"
        ("regression analysis", False),  # under "Data analysis": "analysis" is generic as a token too
        ("farming machines", False),  # under "Milling machines"
        ("show courtesy", False),  # under "show empathy": a light verb
        ("systèmes électroniques et informatiques", False),  # under "systèmes embarqués"
        ("entrepôt de données", False),  # under "bases de données"
        ("manier des outils à main", True),  # under "outils à main"
    ],
)
def test_a_synonym_needs_a_specific_shared_word(label: str, shares: bool) -> None:
    curated = {"web design": "Computer programming", "software programming": "Computer programming",
               "C and Smalltalk": "Objective-C", "gestion de programmes": "gestion de projets",
               "gestion de projets agiles": "gestion de projets", "financial planning": "financial forecasting",
               "regulatory systems": "Control systems", "plasma mechanics": "Fluid mechanics",
               "customer support": "Customer service", "supervised learning": "Machine learning",
               "regression analysis": "Data analysis", "farming machines": "Milling machines",
               "show courtesy": "show empathy", "systèmes électroniques et informatiques": "systèmes embarqués",
               "entrepôt de données": "bases de données", "manier des outils à main": "outils à main"}[label]
    assert builder.shares_specific_word(label, builder.content_tokens(curated)) is shares


@pytest.mark.parametrize(
    "label, own, named",
    [
        ("control system", "Control systems", True),  # generic words, every one the node's own
        ("system control", "Control systems", True),
        ("business analyses", "Business analysis", True),  # the plural of "analysis"
        ("analysis of businesses", "Business analysis", True),
        ("regulatory systems", "Control systems", False),  # a word of its own
        ("process engineering", "Control systems", False),
        ("commercial analysis", "Business analysis", False),
        ("gestion des programmes", "Program management", False),  # French words are other words
        ("of the", "Control systems", False),  # no content word at all
    ],
)
def test_a_label_in_the_curated_labels_own_words_names_the_node(label: str, own: str, named: bool) -> None:
    # D-26, fix round 4: such a label is the curated label rephrased, generic words or not.
    assert builder.names_the_node(label, builder.content_tokens(own)) is named


def test_a_merged_concept_keeps_the_curated_label_rephrased(graph) -> None:
    # "control system" and "business analyses" share only generic words with their curated node,
    # all of them its label's own: kept. "regulatory systems" and "commercial analysis" have words
    # of their own: out.
    assert _aliases(graph, "skill.control_systems") == {"control system", "system control", "systèmes de contrôle"}
    assert _aliases(graph, "skill.business_analysis") == {"business analyses", "analyse commerciale"}


def test_a_field_of_study_imports_no_alternative_label(graph) -> None:
    # D-26: "maths knowledge" (ESCO mathematics) stays out of field.mathematics; a field's names are
    # its curated ones, so no ESCO paraphrase ever consumes prose as a field.
    assert _aliases(graph, "field.mathematics") == {"Math", "Maths"}
    assert _aliases(graph, "field.computer_programming") == set()


def test_a_one_word_french_preferred_label_needs_to_be_the_english_one(graph) -> None:
    assert "morale" not in _aliases(graph, "esco.morality")  # an ordinary French word
    assert {"moral standards", "principes moraux"} <= _aliases(graph, "esco.morality")
    assert "mathématiques" not in _aliases(graph, "field.mathematics")
    assert graph.nodes["esco.common_lisp"].label == "Common Lisp"  # French = English: nothing to drop


def test_curated_parents_win_even_when_empty(graph) -> None:
    assert graph.nodes["skill.javascript"].parents == ()
    assert graph.nodes["skill.react"].parents == ("skill.js_framework",)
    assert graph.nodes["skill.js_framework"].parents == ("skill.javascript",)


def test_an_onet_example_merges_through_its_vendor_free_or_acronym_form(graph) -> None:
    assert "Microsoft Excel" in _aliases(graph, "skill.excel")
    assert {"Amazon Web Services AWS software", "Amazon Web Services"} <= _aliases(graph, "skill.aws")
    assert not any("microsoft_excel" in node_id or "amazon" in node_id for node_id in graph.nodes)


def test_esco_knowledge_and_its_group_merge_into_a_field(graph) -> None:
    mathematics = graph.nodes["field.mathematics"]
    assert mathematics.kind is SkillKind.DOMAIN and mathematics.parents == ("field.science",)
    assert set(mathematics.aliases) == {"Math", "Maths"}  # a field imports no alternative label (D-26)
    assert "esco.mathematics" not in graph.nodes and "esco.group.0541" not in graph.nodes


def test_an_onet_commodity_merges_into_the_curated_node_and_parents_its_examples(graph) -> None:
    assert "cat.43232604" not in graph.nodes
    assert {"Computer aided design CAD software", "Computer aided design"} <= _aliases(graph, "skill.cad")
    autocad = graph.nodes["skill.autodesk_autocad"]
    assert autocad.parents == ("skill.cad",) and "AutoCAD" in autocad.aliases
    assert autocad.kind is SkillKind.TOOL and autocad.source == "onet"


# ─── O*NET nodes ───────────────────────────────────────────────────────────────────────────


def test_onet_example_aliases(graph) -> None:
    confluence = graph.nodes["skill.atlassian_confluence"]
    assert "Confluence" in confluence.aliases and confluence.parents == ("cat.43232201",)
    sql = graph.nodes["skill.structured_query_language_sql"]
    assert {"SQL", "Structured query language"} <= set(sql.aliases)
    assert "Analytics" not in _aliases(graph, "skill.google_analytics")  # blocked


def test_onet_kinds_follow_the_commodity_table(graph) -> None:
    assert graph.nodes["skill.structured_query_language_sql"].kind is SkillKind.TECHNOLOGY
    assert graph.nodes["cat.43232306"].kind is SkillKind.TECHNOLOGY
    assert graph.nodes["skill.atlassian_confluence"].kind is SkillKind.TOOL
    assert graph.nodes["cat.43232201"].kind is SkillKind.TOOL
    assert graph.nodes["skill.epic_systems"].kind is SkillKind.TOOL


def test_onet_tools_are_tools_under_their_commodity(graph) -> None:
    laptop = graph.nodes["skill.laptop_computers"]
    assert laptop.kind is SkillKind.TOOL and laptop.parents == ("cat.43211503",)
    assert graph.nodes["cat.43211503"].label == "Notebook computers"
    forklifts = graph.nodes["cat.24101622"]  # the example is its own commodity: one node
    assert forklifts.label == "Forklifts" and forklifts.kind is SkillKind.TOOL
    assert not any(node.label == "Forklifts" and node_id != "cat.24101622" for node_id, node in graph.nodes.items())


def test_a_two_character_onet_label_is_dropped(graph) -> None:
    assert not any(node.label == "J" for node in graph.nodes.values())


def test_a_bare_acronym_or_a_software_remainder_never_merges(graph) -> None:
    # "Advanced Weather System AWS": the acronym is a curated alias of Amazon Web Services but no
    # evidence of the same concept. "Forklift software" minus "software" is the curated label.
    weather = graph.nodes["skill.advanced_weather_system_aws"]
    assert "Advanced Weather System" in weather.aliases and "AWS" not in weather.aliases
    assert "Advanced Weather System AWS" not in _aliases(graph, "skill.aws")
    software = graph.nodes["skill.forklift_software"]
    assert "Forklift" not in software.aliases  # the curated node keeps that key
    assert "Forklift software" not in _aliases(graph, "skill.forklift")


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Structured query language SQL", ("SQL", "Structured query language")),
        ("Hypertext markup language HTML", ("HTML", "Hypertext markup language")),
        ("Extensible markup language XML", ("XML", "Extensible markup language")),
        ("Point of sale POS", ("POS", "Point of sale")),
        ("Visual Basic for Applications VBA", ("VBA", "Visual Basic for Applications")),
        ("Amazon Web Services AWS", ("AWS", "Amazon Web Services")),
        ("HarrisData Human Resources Information System HRIS", None),  # a product, not the expansion
        ("Acrendo Medical Software Family Practice EMR", None),
        ("Microsoft Dynamics AX", None),
        ("IBM DB2", None),
    ],
)
def test_split_acronym(name: str, expected) -> None:
    assert builder.split_acronym(name) == expected


def test_no_merge_keeps_a_source_concept_apart(tmp_path: Path) -> None:
    uri = "http://data.europa.eu/esco/skill/00000000-0000-4000-8000-000000000001"  # Python (computer programming)
    text = CURATED.read_text(encoding="utf-8") + f"\nno_merge: [{uri}, 'onet:Python']\n"
    path = tmp_path / "curated.yaml"
    path.write_text(text, encoding="utf-8")
    _build(tmp_path / "out", curated_path=path)
    graph = load_graph(tmp_path / "out")
    assert "Python programming" in graph.nodes["esco.python_computer_programming"].aliases
    assert "Python programming" not in graph.nodes["skill.python"].aliases
    assert "Python" not in graph.nodes["esco.python_computer_programming"].aliases  # the curated label's key


# ─── ESCO nodes ────────────────────────────────────────────────────────────────────────────


def test_esco_kinds(graph) -> None:
    kinds = {node_id: graph.nodes[node_id].kind for node_id in (
        "esco.work_in_teams", "esco.think_critically", "esco.speak_french", "esco.morality",
        "esco.use_spreadsheets_software", "esco.group.t4.3", "esco.group.0613", "esco.group.s5", "esco.group.l1")}
    assert kinds == {
        "esco.work_in_teams": SkillKind.SOFT,  # transversal skills collection
        "esco.think_critically": SkillKind.SOFT,
        "esco.speak_french": SkillKind.METHODOLOGY,  # a language skill, not a soft skill
        "esco.morality": SkillKind.DOMAIN,  # knowledge
        "esco.use_spreadsheets_software": SkillKind.METHODOLOGY,
        "esco.group.t4.3": SkillKind.SOFT,
        "esco.group.0613": SkillKind.DOMAIN,
        "esco.group.s5": SkillKind.METHODOLOGY,
        "esco.group.l1": SkillKind.DOMAIN,
    }


def test_esco_programming_languages_are_technologies(graph) -> None:
    # ESCO files languages as knowledge; labelled "(computer programming)" or filed under computer
    # programming (here the curated skill.programming), they are TECHNOLOGY like the curated ones.
    for node_id in ("esco.ruby_computer_programming", "esco.scratch_computer_programming",
                    "esco.ml_computer_programming", "esco.common_lisp"):
        assert graph.nodes[node_id].kind is SkillKind.TECHNOLOGY, node_id
    assert graph.nodes["esco.common_lisp"].parents == ("skill.programming",)
    assert graph.nodes["skill.programming"].kind is SkillKind.DOMAIN  # curated: its own kind


def test_esco_parents_and_pillar_roots(graph) -> None:
    assert graph.nodes["esco.work_in_teams"].parents == ("esco.group.t4.3",)
    assert graph.nodes["esco.group.t4.3"].parents == ("esco.group.t4",)
    assert graph.nodes["esco.group.t4"].parents == ()  # its parent is the T pillar root, not a node
    assert graph.nodes["esco.think_critically"].parents == ()
    assert not {"esco.group.k", "esco.group.s", "esco.group.t", "esco.group.l"} & set(graph.nodes)


def test_esco_alias_filtering(graph) -> None:
    teams = _aliases(graph, "esco.work_in_teams")
    assert {"team working", "collaborate with colleagues", "team player", "travailler en équipe",
            "travail d'équipe"} <= teams
    assert not {"teamwork", "collaboration"} & teams  # single-word alt labels (en and fr)
    ruby = _aliases(graph, "esco.ruby_computer_programming")
    assert {"Ruby", "Ruby language"} <= ruby and "Matz" not in ruby  # "Ruby" is the bare label
    scratch = _aliases(graph, "esco.scratch_computer_programming")
    assert "Scratch programming" in scratch and "Scratch" not in scratch  # blocked
    assert "ML" not in _aliases(graph, "esco.ml_computer_programming")  # two characters
    assert "synergy" not in _aliases(graph, "esco.think_critically")


def test_a_key_claimed_by_a_curated_node_leaves_the_others(graph) -> None:
    spreadsheets = _aliases(graph, "esco.use_spreadsheets_software")
    assert "Microsoft Office Excel" not in spreadsheets and "Gnumeric" not in spreadsheets
    assert "work with spreadsheets" in spreadsheets
    assert "Microsoft Office Excel" in _aliases(graph, "skill.excel")
    assert "ML" in _aliases(graph, "skill.machine_learning")  # curated: exempt from the 2-character rule


def test_esco_slug_collisions_append_the_uuid_prefix(graph) -> None:
    assert "esco.operate_pumping_equipment" not in graph.nodes
    assert graph.nodes["esco.operate_pumping_equipment_a7c3f2"].label == "operate pumping equipment"
    assert graph.nodes["esco.operate_pumping_equipment_b8d4e5"].label == "operate pumping equipment"


def test_credentials_and_fields(graph) -> None:
    nurse = graph.nodes["cred.registered_nurse"]
    assert nurse.kind is SkillKind.CREDENTIAL and nurse.source == "curated"
    assert graph.nodes["field.science"].kind is SkillKind.DOMAIN


# ─── the resolver over the built taxonomy ──────────────────────────────────────────────────


def test_load_resolver_uses_the_ambiguous_casings(built: Path, config) -> None:
    # "go" is curated (lists waive its casing); the cased keys are automatic: one-word names with a
    # capital on nodes that are not curated (O*NET labels, vendor-free names, an ESCO bare label, an
    # acronym), cased as written everywhere. "[A-Z]" expands to one blocked phrase per letter.
    settings = _json(built, "resolver.json")
    assert settings["ambiguous"] == {"go": "Go"}
    assert settings["cased"] == {
        "analyze": "Analyze", "autocad": "AutoCAD", "confluence": "Confluence", "forklifts": "Forklifts",
        "levels": "Levels", "ruby": "Ruby", "sql": "SQL", "stethoscopes": "Stethoscopes"}
    assert settings["blocked_phrases"] == ["American Express", *(f"Building {chr(c)}" for c in range(65, 91))]
    resolver = load_resolver(built, config)
    assert resolver.resolve("Go").canonical_id == "skill.go"
    assert resolver.resolve("go").method == "NONE"
    assert resolver.resolve("ReactJS").canonical_id == "skill.react"
    assert resolver.resolve("mathematics").canonical_id == "field.mathematics"
    assert resolver.resolve("Mathématiques").method == "NONE"  # the one-word French label was dropped
    assert resolver.resolve("apprentissage automatique").canonical_id == "skill.machine_learning"
    assert resolver.extract("go, sql, SQL", context="list")[0].canonical_id == "skill.go"
    assert [m.surface for m in resolver.extract("go, sql, SQL", context="list")] == ["go", "SQL"]
    assert resolver.extract("Lab work in Building C with Python.")[0].surface == "Python"
    assert resolver.resolve("American Express").method == "NONE"


def test_currency_skill_nouns_are_written_for_the_resolver(built: Path, config) -> None:
    # D-27, fix round 4: after an amount a currency code is money unless one of these curated nouns
    # follows it; written normalised and sorted.
    assert _json(built, "resolver.json")["currency_skill_nouns"] == ["drawings", "licences"]
    resolver = load_resolver(built, config)
    assert [m.canonical_id for m in resolver.extract("Produced 1,200 CAD drawings.")] == ["skill.cad"]
    assert [m.canonical_id for m in resolver.extract("Managed 1,500 CAD licences.")] == ["skill.cad"]
    assert resolver.extract("We offer 85,000 CAD plus benefits.") == ()


@pytest.mark.parametrize("nouns", ["drawings", "[shop drawings]", '[drawings, ""]'])
def test_bad_currency_skill_nouns_fail_the_build(tmp_path: Path, nouns: str) -> None:
    text = CURATED.read_text(encoding="utf-8").replace("currency_skill_nouns: [drawings, Licences]",
                                                       f"currency_skill_nouns: {nouns}")
    path = tmp_path / "curated.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(builder.BuildError, match="currency_skill_nouns"):
        _build(tmp_path / "out", curated_path=path)


def test_fuzzy_exclusions_and_the_sentence_initial_rule_are_written_for_the_resolver(built: Path, config) -> None:
    # Task 7.3 (D-140): curated, written normalised and sorted.
    settings = _json(built, "resolver.json")
    assert settings["fuzzy_exclusions"] == ["patient", "patients"]
    assert settings["sentence_initial"] == {"keys": ["go"], "languages": [], "cues": ["fluent", "spoken"]}
    resolver = load_resolver(built, config)
    assert [m.canonical_id for m in resolver.extract("Go, Python and SQL.")] == [
        "skill.go", "skill.python", "skill.structured_query_language_sql"]


@pytest.mark.parametrize(
    "block, message",
    [
        ("sentence_initial:\n  keys: [Python]\n", "sentence_initial"),  # no casing rule to relax
        ("sentence_initial:\n  keys: [Go]\n  languages: [SQL]\n", "sentence_initial"),  # not one of the keys
        ("sentence_initial:\n  keys: [Go]\n  cues: [two words]\n", "sentence_initial"),
        ("sentence_initial:\n  keys: [Go]\n  other: [x]\n", "sentence_initial"),
        ("sentence_initial: [Go]\n", "sentence_initial"),
        ("fuzzy_exclusions: patients\n", "fuzzy_exclusions"),
    ],
)
def test_bad_fuzzy_exclusions_or_sentence_initial_settings_fail_the_build(tmp_path: Path, block: str,
                                                                           message: str) -> None:
    text = CURATED.read_text(encoding="utf-8")
    start = text.index("fuzzy_exclusions:")
    end = text.index("generic_titles:")
    path = tmp_path / "curated.yaml"
    path.write_text(text[:start] + block + "\n" + text[end:], encoding="utf-8")
    with pytest.raises(builder.BuildError, match=message):
        _build(tmp_path / "out", curated_path=path)


def test_formal_designations_are_read_as_written(graph, built: Path, config) -> None:
    # D-28: a professional licence names the node of its formal in-progress designations; no name
    # is generated, a status written around the licence is the context's business.
    assert _aliases(graph, "cred.nursing_student") == {"student nurse"}
    assert _json(built, "resolver.json")["in_progress"] == {"cred.registered_nurse": "cred.nursing_student"}
    resolver = load_resolver(built, config)
    for text, expected in (("Nursing student at Humber.", ["cred.nursing_student"]),
                           ("RN (in progress)", ["cred.registered_nurse"]),
                           ("Ideal candidate: RN with 3+ years.", ["cred.registered_nurse"])):
        assert [m.canonical_id for m in resolver.extract(text)] == expected, text


def test_a_posting_probe_that_loses_its_licence_fails_the_build(tmp_path: Path) -> None:
    # "candidate RN" as a designation would read a posting's "Ideal candidate RN" as a student.
    text = CURATED.read_text(encoding="utf-8").replace(
        "aliases: [student nurse]", "aliases: [student nurse, candidate RN]").replace(
        '  - ["Ideal candidate: RN with 3+ years.", cred.registered_nurse]\n',
        '  - ["Ideal candidate: RN with 3+ years.", cred.registered_nurse]\n'
        '  - ["Ideal candidate RN with 3+ years.", cred.registered_nurse]\n')
    path = tmp_path / "curated.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(builder.BuildError, match="licence status check failed.*Ideal candidate RN"):
        _build(tmp_path / "out", curated_path=path)


def test_a_professional_licence_without_an_in_progress_node_fails_the_build(tmp_path: Path) -> None:
    text = CURATED.read_text(encoding="utf-8").replace("in_progress:\n  cred.registered_nurse: cred.nursing_student\n",
                                                       "")
    path = tmp_path / "curated.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(builder.BuildError, match="in_progress"):
        _build(tmp_path / "out", curated_path=path)


def test_an_in_progress_name_that_reaches_the_licence_fails_the_build(tmp_path: Path) -> None:
    # "RN, BScN" resolves to the student node as a whole, but extraction stops at the comma and
    # reads "RN": the build check refuses it (D-28).
    text = CURATED.read_text(encoding="utf-8").replace("aliases: [student nurse]",
                                                       'aliases: [student nurse, "RN, BScN"]')
    path = tmp_path / "curated.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(builder.BuildError, match="licence status check failed"):
        _build(tmp_path / "out", curated_path=path)


def test_a_field_and_a_skill_may_share_a_key(graph, built: Path, config) -> None:
    # D-27: "Computer programming" is a skill and a field of study; the ESCO concept merges into
    # the skill, both keep the key, extraction reads the skill and an education line the field.
    assert graph.nodes["skill.programming"].label == graph.nodes["field.computer_programming"].label
    assert "programmation informatique" not in _aliases(graph, "field.computer_programming")
    resolver = load_resolver(built, config)
    text = "Studied computer programming."
    assert [m.canonical_id for m in resolver.extract(text)] == ["skill.programming"]
    assert [m.canonical_id for m in resolver.extract(text, include_fields=True)] == ["field.computer_programming"]
    assert resolver.resolve("Computer programming").canonical_id == "skill.programming"
    assert resolver.resolve("Computer programming", prefer_fields=True).canonical_id == "field.computer_programming"


def test_two_curated_skills_still_may_not_share_a_key(tmp_path: Path) -> None:
    text = CURATED.read_text(encoding="utf-8").replace("label: Forklift, kind: TOOL, aliases: [forklift operation]",
                                                       "label: Forklift, kind: TOOL, aliases: [forklift operation, Maths]")
    path = tmp_path / "curated.yaml"
    path.write_text(text, encoding="utf-8")
    _build(tmp_path / "ok", curated_path=path)  # a skill may share "maths" with the field
    text = text.replace("aliases: [forklift operation, Maths]", "aliases: [forklift operation, Golang]")
    path.write_text(text, encoding="utf-8")
    with pytest.raises(builder.BuildError, match="golang"):
        _build(tmp_path / "out", curated_path=path)


def test_a_blocked_phrase_that_is_also_a_curated_alias_fails_the_build(tmp_path: Path) -> None:
    text = CURATED.read_text(encoding="utf-8").replace("blocked_phrases: [American Express",
                                                       "blocked_phrases: [Golang, American Express")
    path = tmp_path / "curated.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(builder.BuildError, match="Golang"):
        _build(tmp_path / "out", curated_path=path)


def test_common_words_never_become_derived_aliases_and_common_labels_need_their_casing(built: Path, graph,
                                                                                         config) -> None:
    assert "Billing" not in _aliases(graph, "skill.billing_software")  # generic example: no short form
    assert "Paint" not in _aliases(graph, "skill.microsoft_paint")  # vendor-free, but an ordinary word
    resolver = load_resolver(built, config)
    assert resolver.resolve("Analyze").canonical_id == "skill.analyze"
    assert resolver.resolve("analyze").method == "NONE"  # "analyze the data" is not the product
    assert resolver.resolve("Levels").canonical_id == "cat.41111604"
    # Automatic cased keys keep their casing in skills lists too (D-27): "levels" is a word there.
    assert [m.canonical_id for m in resolver.extract("Skills: levels, Analyze, Levels", context="list")] == [
        "skill.analyze", "cat.41111604"]
    assert resolver.extract("Worked at all levels to analyze billing.") == ()


# ─── extra artifacts ───────────────────────────────────────────────────────────────────────


def test_titles(built: Path) -> None:
    titles = _json(built, "titles.json")["titles"]
    developers = {"soc": "15-1252.00", "family": "Computer and Mathematical Occupations", "job_zone": 4}
    for title in ("software developers", "software developer", "software engineer", "application developer",
                  "app developer"):
        assert titles[title] == developers, title
    assert titles["office manager"]["soc"] == "11-1021.00"  # one vote per family: the smaller family code
    assert titles["program manager"]["soc"] == "11-1021.00"  # same family: the smaller code
    assert titles["ceo"] == titles["chief executive officer"] == {
        "soc": "11-1011.00", "family": "Management Occupations", "job_zone": 5}
    assert titles["infantry"] == {"soc": "55-3016.00", "family": "Military Specific Occupations", "job_zone": None}
    # Generic titles (curated generic_titles) name no occupation, in any hyphen variant.
    assert not {"intern", "co-op", "co op", "coop"} & set(titles)


def test_mos_crosswalk(built: Path) -> None:
    mos = _json(built, "mos_crosswalk.json")
    codes = mos["codes"]
    # 15-1299.00's own data: Excel and Confluence (hot / in demand) and its knowledge areas, of
    # which Mathematics is a field of study and Computers and Electronics (mapped to skill.python
    # in the mini config) reaches more than a quarter of all codes: both left out.
    assert codes["25B"] == {
        "branch": "army", "branches": ["army"], "title": "Information Technology Specialist",
        "soc": ["15-1299.00"], "skill_ids": ["skill.atlassian_confluence", "skill.excel"],
    }
    assert mos["common_knowledge_left_out"] == ["skill.python"]
    assert "skill.python" in codes["3D072"]["skill_ids"]  # a hot technology of 15-1252.00 stays
    assert "11X" not in codes and "2210" not in codes and "09L" not in codes  # obsolete, civilian, unmapped
    rifleman = codes["0311"]  # its only knowledge area is below the importance floor
    assert rifleman["skill_ids"] == [] and "military-specific" in rifleman["note"]
    clash = codes["0952"]
    assert (clash["branch"], clash["title"]) == ("marine_corps", "Formal School Officer Instructor")
    assert clash["alternatives"] == [{"branch": "navy", "title": "Nurse Anesthetist", "soc": ["29-2043.00"],
                                      "skill_ids": ["skill.epic_systems"]}]  # field.mathematics left out
    assert codes["3D052"]["branches"] == ["air_force", "space_force"]
    # An "All Other" occupation without data of its own stays empty (D-29): never its siblings' skills.
    technical = codes["12T"]
    assert technical["skill_ids"] == [] and technical["note"].startswith("17-2199.00: O*NET publishes no skill data")
    generic = codes["3D0X2"]  # Air Force specialty code with the skill level as X
    assert generic["title"] == "Cyber Transport Systems" and generic["soc"] == ["15-1252.00", "15-1299.00"]


def _military_row(code: str, *socs: str) -> dict[str, str]:
    row = {"SVC": "A", "MPC": "E", "MOC": code, "MOC_TITLE": f"Code {code}", "STATUS": "A"}
    row.update({f"ONET{i}": soc for i, soc in enumerate(socs, 1)})
    return row


def test_mos_skill_ids_leave_out_fields_and_knowledge_most_codes_share() -> None:
    raw = SimpleNamespace(military=[_military_row(f"{n:02d}A", f"11-{n:04d}.00") for n in range(1, 9)]
                          + [_military_row("99Z", "55-0001.00")])
    technologies = {"11-0001.00": {"skill.excel"}}
    knowledge = {f"11-{n:04d}.00": {"esco.english", "field.mathematics"} for n in range(1, 4)}  # 3 of 9 codes
    knowledge["11-0004.00"] = {"esco.law"}
    knowledge["11-0005.00"] = {"field.law"}
    codes = builder.build_mos(raw, builder.OccupationSkills(technologies, knowledge))["codes"]
    assert codes["01A"]["skill_ids"] == ["skill.excel"]  # esco.english: 3 of 9 codes > 25 %
    assert codes["02A"]["skill_ids"] == [] and "left out" in codes["02A"]["note"]
    assert codes["04A"]["skill_ids"] == ["esco.law"]  # 1 of 9: kept
    assert codes["05A"]["skill_ids"] == [] and "left out" in codes["05A"]["note"]  # a field of study only
    assert "no skill data" in codes["06A"]["note"]
    assert "military-specific" in codes["99Z"]["note"]
    two = builder.build_mos(SimpleNamespace(military=raw.military[:2]),
                            builder.OccupationSkills(technologies, knowledge))
    assert two["common_knowledge_left_out"] == ["esco.english"]  # 2 of 2 codes


def test_naics(built: Path) -> None:
    codes = _json(built, "naics.json")["codes"]
    assert codes["31"] == codes["32"] == codes["33"] == "Manufacturing" and "31-33" not in codes
    assert codes["513210"] == "Software Publishers" and codes["11"].startswith("Agriculture")


# ─── refusing bad input ────────────────────────────────────────────────────────────────────


def test_a_missing_required_credential_fails_the_build(tmp_path: Path) -> None:
    with pytest.raises(builder.BuildError, match="cred.nope"):
        _build(tmp_path, required_ids=("cred.registered_nurse", "cred.nope"))


@pytest.mark.parametrize(
    "edges, message",
    [
        ("format: 1\nedges:\n  - [skill.react, skill.javascript, 0.9]\n", "outside"),
        ("format: 1\nedges:\n  - [skill.react, skill.nope, 0.5]\n", "skill.nope"),
    ],
)
def test_bad_curated_edges_fail_the_build(tmp_path: Path, edges: str, message: str) -> None:
    path = tmp_path / "edges.yaml"
    path.write_text(edges, encoding="utf-8")
    with pytest.raises(builder.BuildError, match=message):
        _build(tmp_path / "out", edges_path=path)


def test_two_curated_nodes_claiming_one_alias_fail_the_build(tmp_path: Path) -> None:
    text = CURATED.read_text(encoding="utf-8").replace("aliases: [Golang]", "aliases: [Golang, ReactJS]")
    path = tmp_path / "curated.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(builder.BuildError, match="reactjs"):
        _build(tmp_path / "out", curated_path=path)


def test_sources_md_names_every_source_and_the_counts(built: Path) -> None:
    text = (built / "SOURCES.md").read_text(encoding="utf-8")
    for needle in ("ESCO v1.2.1", "O*NET 30.0", "NAICS 2022", "military crosswalk", "CC BY 4.0", "French",
                   "| TECHNOLOGY |", "sha256", "Task Statements", "occupation descriptions", "blocked phrases",
                   "3 of 8 codes have none: 1 map only to military-specific occupations"):
        assert needle in text, needle
