"""The taxonomy builder (Task 2.2; SPEC §3, §14.3, §14.4; DECISIONS D-13, D-20).

Every test builds from tests/fixtures/raw_mini (a few rows in the real file layouts) with the
curated config in tests/fixtures/curated_mini, so no network and no real download is needed.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

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


def test_a_tampered_extra_artifact_is_rejected(built: Path, tmp_path: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(built, copy)
    titles = copy / "titles.json"
    titles.write_bytes(titles.read_bytes().replace(b"software engineer", b"software engineeR"))
    with pytest.raises(ArtifactError, match="titles.json"):
        load_graph(copy)
    with pytest.raises(ArtifactError, match="titles.json"):
        load_json_artifact(copy, "titles.json")


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
    assert {"Python 3", "Python (computer programming)", "Python programming", "Python 2.7",
            "Python (programmation informatique)", "langage Python"} <= set(python.aliases)
    assert not any(node_id.startswith("esco.python") for node_id in graph.nodes)
    # Curated parents absent: the merged sources' parents are inherited (ESCO broader, O*NET class).
    assert python.parents == ("cat.43232405", "esco.computer_programming", "esco.group.0613")


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
    assert "mathématiques" in mathematics.aliases
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
        "esco.work_in_teams", "esco.think_critically", "esco.speak_french", "esco.computer_programming",
        "esco.use_spreadsheets_software", "esco.group.t4.3", "esco.group.0613", "esco.group.s5", "esco.group.l1")}
    assert kinds == {
        "esco.work_in_teams": SkillKind.SOFT,  # transversal skills collection
        "esco.think_critically": SkillKind.SOFT,
        "esco.speak_french": SkillKind.METHODOLOGY,  # a language skill, not a soft skill
        "esco.computer_programming": SkillKind.DOMAIN,
        "esco.use_spreadsheets_software": SkillKind.METHODOLOGY,
        "esco.group.t4.3": SkillKind.SOFT,
        "esco.group.0613": SkillKind.DOMAIN,
        "esco.group.s5": SkillKind.METHODOLOGY,
        "esco.group.l1": SkillKind.DOMAIN,
    }


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
    # "go" is curated; the rest are automatic: one-word names with a capital on nodes that are not
    # curated (O*NET labels, vendor-free names, an ESCO bare label, an acronym), cased as written.
    assert _json(built, "resolver.json") == {"format": 1, "ambiguous": {
        "analyze": "Analyze", "autocad": "AutoCAD", "confluence": "Confluence", "forklifts": "Forklifts",
        "go": "Go", "levels": "Levels", "ruby": "Ruby", "sql": "SQL", "stethoscopes": "Stethoscopes"}}
    resolver = load_resolver(built, config)
    assert resolver.resolve("Go").canonical_id == "skill.go"
    assert resolver.resolve("go").method == "NONE"
    assert resolver.resolve("ReactJS").canonical_id == "skill.react"
    assert resolver.resolve("Mathématiques").canonical_id == "field.mathematics"
    assert resolver.resolve("apprentissage automatique").canonical_id == "skill.machine_learning"


def test_common_words_never_become_derived_aliases_and_common_labels_need_their_casing(built: Path, graph,
                                                                                         config) -> None:
    assert "Billing" not in _aliases(graph, "skill.billing_software")  # generic example: no short form
    assert "Paint" not in _aliases(graph, "skill.microsoft_paint")  # vendor-free, but an ordinary word
    resolver = load_resolver(built, config)
    assert resolver.resolve("Analyze").canonical_id == "skill.analyze"
    assert resolver.resolve("analyze").method == "NONE"  # "analyze the data" is not the product
    assert resolver.resolve("Levels").canonical_id == "cat.41111604"
    assert resolver.extract("Skills: levels, Analyze", context="list")[0].canonical_id == "cat.41111604"
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


def test_mos_crosswalk(built: Path) -> None:
    codes = _json(built, "mos_crosswalk.json")["codes"]
    assert codes["25B"] == {
        "branch": "army", "branches": ["army"], "title": "Information Technology Specialist",
        "soc": ["15-1299.00"],
        "skill_ids": ["field.mathematics", "skill.atlassian_confluence", "skill.excel", "skill.python"],
    }
    assert "11X" not in codes and "2210" not in codes and "09L" not in codes  # obsolete, civilian, unmapped
    assert codes["0311"]["skill_ids"] == []  # its only knowledge area is below the importance floor
    clash = codes["0952"]
    assert (clash["branch"], clash["title"]) == ("marine_corps", "Formal School Officer Instructor")
    assert clash["alternatives"] == [{"branch": "navy", "title": "Nurse Anesthetist", "soc": ["29-2043.00"],
                                      "skill_ids": ["field.mathematics", "skill.epic_systems"]}]
    assert codes["3D052"]["branches"] == ["air_force", "space_force"]
    # An "All Other" occupation without data of its own takes what two or more detailed
    # occupations of its family share (AutoCAD; Python is listed by only one).
    assert codes["12T"]["skill_ids"] == ["skill.autodesk_autocad"]
    generic = codes["3D0X2"]  # Air Force specialty code with the skill level as X
    assert generic["title"] == "Cyber Transport Systems" and generic["soc"] == ["15-1252.00", "15-1299.00"]


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
                   "| TECHNOLOGY |", "sha256"):
        assert needle in text, needle
