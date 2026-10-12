"""BUILD Milestone 2 gate: 1,000 hand-picked surface forms resolve correctly (Task 2.2).

Every line of tests/fixtures/surface_forms.tsv is one case, checked against the committed
data/taxonomy through `load_resolver`:

    surface<TAB>expected_id<TAB>method[<TAB>context]

`method` is EXACT, ALIAS, FUZZY or NONE (expected_id "-"). `context` is `resolve` (default:
`Resolver.resolve`, where an ambiguous word needs its exact casing) or `list` / `prose`
(`Resolver.extract`, which must find exactly one mention, of that id and method). Lines starting
with "#" are comments.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

import pytest
import yaml

from matcher.graph.loader import load_graph, load_json_artifact, load_resolver
from matcher.models import SkillKind

ROOT = Path(__file__).resolve().parents[1]
TAXONOMY = ROOT / "data" / "taxonomy"
FORMS = Path(__file__).resolve().parent / "fixtures" / "surface_forms.tsv"
METHODS = ("EXACT", "ALIAS", "FUZZY", "NONE")
CONTEXTS = ("resolve", "list", "prose")


def _cases() -> list:
    cases = []
    for number, line in enumerate(FORMS.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) not in (3, 4):
            raise ValueError(f"surface_forms.tsv line {number}: expected 3 or 4 tab-separated fields")
        surface, expected, method = fields[:3]
        context = fields[3] if len(fields) == 4 else "resolve"
        if method not in METHODS or context not in CONTEXTS or (method == "NONE") != (expected == "-"):
            raise ValueError(f"surface_forms.tsv line {number}: bad method/context/id: {line!r}")
        cases.append(pytest.param(surface, expected, method, context, id=f"{number}:{context}:{surface}"))
    return cases


CASES = _cases()


@pytest.fixture(scope="module")
def resolver(config):
    return load_resolver(TAXONOMY, config)


def test_the_gate_has_exactly_1000_distinct_lines() -> None:
    assert len(CASES) == 1000
    assert len({(case.values[0], case.values[3]) for case in CASES}) == 1000


@pytest.mark.parametrize("surface, expected, method, context", CASES)
def test_surface_form(resolver, surface: str, expected: str, method: str, context: str) -> None:
    if context == "resolve":
        result = resolver.resolve(surface)
        assert (result.canonical_id or "-", result.method) == (expected, method)
        return
    mentions = resolver.extract(surface, context=context)
    if method == "NONE":
        assert mentions == ()
    else:
        assert [(m.canonical_id, m.method) for m in mentions] == [(expected, method)]


def test_loading_and_building_the_resolver_takes_under_3_seconds(config) -> None:
    timings = []
    for _ in range(2):  # best of two: a busy machine must not fail the build
        started = time.perf_counter()
        load_resolver(TAXONOMY, config)
        timings.append(time.perf_counter() - started)
    assert min(timings) < 3.0, timings


@pytest.fixture(scope="module")
def graph():
    return load_graph(TAXONOMY)


def test_every_professional_credential_id_exists(graph, config) -> None:
    ids = config.gates.license.professional_credential_ids
    assert ids
    for node_id in ids:
        assert graph.nodes[node_id].kind is SkillKind.CREDENTIAL, node_id


def test_every_credential_is_related_to_what_it_certifies(graph) -> None:
    for node_id, node in graph.nodes.items():
        if node.kind is SkillKind.CREDENTIAL:
            assert any(graph.nodes[other].kind is not SkillKind.CREDENTIAL
                       for other in graph.related.get(node_id, {})), node_id


@pytest.mark.parametrize(
    "node_id, year",
    [
        ("skill.kubernetes", 2014), ("skill.docker", 2013), ("skill.react", 2013), ("skill.vue", 2014),
        ("skill.angular", 2016), ("skill.svelte", 2016), ("skill.nextjs", 2016), ("skill.typescript", 2012),
        ("skill.go", 2009), ("skill.rust", 2015), ("skill.swift", 2014), ("skill.kotlin", 2011),
        ("skill.flutter", 2017), ("skill.graphql", 2015), ("skill.terraform", 2014), ("skill.fastapi", 2018),
        ("skill.pytorch", 2016), ("skill.tensorflow", 2015), ("skill.snowflake", 2014), ("skill.dbt", 2016),
        ("skill.airflow", 2015), ("skill.kafka", 2011),
    ],
)
def test_release_years_for_requirements_postings_over_ask(graph, node_id: str, year: int) -> None:
    assert graph.nodes[node_id].release_year == year


def test_fields_of_study_and_their_curated_relations(graph) -> None:
    fields = [node_id for node_id in graph.nodes if node_id.startswith("field.")]
    assert len(fields) >= 40
    assert all(graph.nodes[node_id].kind is SkillKind.DOMAIN for node_id in fields)
    for a, b, weight in [("field.computer_science", "field.software_engineering", 0.80),
                         ("field.computer_science", "field.computer_engineering", 0.70),
                         ("field.statistics", "field.mathematics", 0.70),
                         ("field.data_science", "field.statistics", 0.70),
                         ("field.data_science", "field.computer_science", 0.70),
                         ("field.finance", "field.accounting", 0.60),
                         ("field.economics", "field.finance", 0.60)]:
        assert graph.related_weight(a, b) == weight


def test_curated_edges_stay_in_the_credit_range(graph) -> None:
    # Every curated edge keeps its own weight, in the credit range, stored both ways. Since Task 2.3
    # the graph also holds edges mined from postings (NPMI in [0.4, 1], possibly one way after the
    # out-degree cap, DECISIONS D-22); tests/test_corpus_stats.py checks those.
    curated = yaml.safe_load((ROOT / "config" / "curated_edges.yaml").read_text(encoding="utf-8"))["edges"]
    assert len(curated) >= 100
    for a, b, weight in curated:
        assert 0.4 <= weight <= 0.8, (a, b, weight)
        assert graph.related[a][b] == graph.related[b][a] == weight, (a, b)  # stored both ways


def test_the_committed_taxonomy_was_built_from_the_committed_config() -> None:
    # MANIFEST records the curated config's hash (line endings normalised): editing the config
    # without rebuilding (scripts/build_taxonomy.py) fails here.
    manifest = (TAXONOMY / "MANIFEST.json").read_text(encoding="utf-8")
    for name in ("curated_taxonomy.yaml", "curated_edges.yaml"):
        digest = hashlib.sha256((ROOT / "config" / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        assert f"config/{name} sha256 {digest}" in manifest, name


def test_extra_artifacts_load_and_carry_their_examples() -> None:
    titles = load_json_artifact(TAXONOMY, "titles.json")["titles"]
    assert titles["software developers"]["soc"] == "15-1252.00"
    assert titles["software developers"]["family"] == "Computer and Mathematical Occupations"
    assert not {"intern", "co-op", "co op", "student", "trainee", "apprentice", "volunteer"} & set(titles)
    mos = load_json_artifact(TAXONOMY, "mos_crosswalk.json")
    codes = mos["codes"]
    # D-29: skill ids come only from the occupations a code maps to. 25B maps to "Computer
    # Occupations, All Other" alone, for which O*NET publishes no skill data: no skills, a note.
    assert codes["25B"]["branch"] == "army" and codes["25B"]["skill_ids"] == []
    assert codes["25B"]["note"].startswith("15-1299.00: O*NET publishes no skill data")
    assert {"skill.active_directory", "skill.linux"} <= set(codes["17C"]["skill_ids"])  # Cyber Operations
    assert "esco.english" in mos["common_knowledge_left_out"]
    for code, value in codes.items():
        for entry in (value, *value.get("alternatives", ())):
            assert not any(skill.startswith("field.") for skill in entry["skill_ids"]), code
            assert not set(entry["skill_ids"]) & set(mos["common_knowledge_left_out"]), code
            assert bool(entry["skill_ids"]) != ("note" in entry), code
    naics = load_json_artifact(TAXONOMY, "naics.json")["codes"]
    assert naics["513210"] == "Software Publishers" and naics["31"] == naics["33"] == "Manufacturing"


def test_resolver_settings_hold_the_rounds_rules() -> None:
    settings = load_json_artifact(TAXONOMY, "resolver.json")
    assert "agile" not in settings["ambiguous"] and settings["ambiguous"]["ml"] == "ML"
    assert settings["ambiguous"]["bed"] == "BEd" and settings["cased"]["tables"] == "Tables"
    assert not set(settings["ambiguous"]) & set(settings["cased"])
    for phrase in ("American Express", "Express Entry", "Express Scripts", "Sous Chef", "Chef de partie",
                   "Executive Chef", "Pastry Chef", "Vitamin C", "Vitamin D", "Building A", "Building C",
                   "Building Z", "Swift Current", "Spring Break", "Spring Term", "Spring Semester", "Go-Live",
                   "Go-to-market", "Go/No-Go", "R&D", "Swift Transportation", "Express Employment Professionals",
                   "Technical Skills", "NCLEX-RN", "Statistics Canada", "Department of Finance"):
        assert phrase in settings["blocked_phrases"], phrase
    # Round 2: bare "Teams" and "Chef" are no keys at all; French "cerner" (to grasp) needs "Cerner".
    assert not {"teams", "chef"} & (set(settings["ambiguous"]) | set(settings["cased"]))
    assert settings["ambiguous"]["cerner"] == "Cerner"
    # Round 4 (D-27): the curated nouns that keep CAD the skill after an amount ("1,200 CAD drawings").
    nouns = settings["currency_skill_nouns"]
    assert nouns == sorted(set(nouns)) and all(noun == noun.lower() and " " not in noun for noun in nouns)
    assert {"drawings", "models", "files", "parts", "sheets", "assemblies", "licences", "licenses",
            "standards", "components", "documents"} <= set(nouns)  # the last two: Task 2.3
    # Task 2.3 (D-27): "control system" inside unrelated compounds names no control system.
    for phrase in ("document control system", "document control systems", "change control system",
                   "change control systems", "pest control system", "pest control systems", "cash control system",
                   "fire alarm control system", "climate control system", "climate control systems"):
        assert phrase in settings["blocked_phrases"], phrase


def test_task_2_3_names(graph, resolver) -> None:
    # The corpus review (D-26): generic words that reached a specific node in Tailrd's postings are gone.
    assert not {"skill.act", "skill.google", "esco.ecosystems"} & set(graph.nodes)  # "act on", the company
    assert "translating" not in graph.nodes["skill.translation"].aliases  # "translating X into Y"
    assert "shape the future" not in graph.nodes["esco.think_innovatively"].aliases
    assert "work-life balance" not in graph.nodes["esco.maintain_psychological_well_being"].aliases
    assert graph.nodes["skill.microsoft_windows_sdk"].aliases == ()  # bare "SDK" was its only alias
    assert resolver.resolve("SDK").method == "NONE"
    # PID (D-27): bare "PID" is nothing; PID control is control systems; the detector keeps its names.
    assert resolver.resolve("PID").method == "NONE"
    for name in ("PID control", "PID controller", "PID controllers", "PID loop", "PID loops", "PID tuning"):
        assert resolver.resolve(name).canonical_id == "skill.control_systems", name
    detector = graph.nodes["skill.photoionization_detectors"]
    assert detector.source == "curated" and "PID" not in detector.aliases
    for name in ("photoionization detector", "Photoionization detectors PID", "HNu photoionization detectors PID"):
        assert resolver.resolve(name).canonical_id == "skill.photoionization_detectors", name
    # "access control system" is the access-control node, as its plural always was.
    for name in ("access control system", "access control systems"):
        assert resolver.resolve(name).canonical_id == "skill.access_control_systems", name


def test_round_4_names(graph, resolver) -> None:
    # The British and Canadian spelling reaches the node the American one does: a curated anchor
    # the ESCO concept merges into, keeping its parent.
    assert resolver.resolve("programme management").canonical_id == "skill.program_management"
    assert resolver.resolve("program management").canonical_id == "skill.program_management"
    assert graph.nodes["skill.program_management"].parents == ("esco.group.0413",)
    # Design (D-27): the disciplines are field names too; the skill takes no ESCO parents from the
    # merged "design thinking" concept (arts, marketing and advertising).
    assert {"interior design", "product design", "industrial design"} <= set(graph.nodes["field.design"].aliases)
    assert graph.nodes["skill.design"].parents == ()


IN_PROGRESS = {
    "cred.cpa_in_progress": "cred.cpa", "cred.cfa_candidate": "cred.cfa",
    "cred.eit": "cred.professional_engineer",
    "cred.nursing_student": "cred.registered_nurse", "cred.articling_student": "cred.bar_admission",
    "cred.electrician_apprentice": "cred.electrician_license", "cred.teacher_candidate": "cred.teaching_license",
    "cred.medical_student": "cred.medical_license", "cred.pharmacy_student": "cred.pharmacist_license",
    "cred.paramedic_student": "cred.paramedic_license", "cred.cdl_trainee": "cred.commercial_drivers_license",
}


def test_licences_in_progress_are_related_credentials_not_licences(graph, config) -> None:
    licences = set(config.gates.license.professional_credential_ids)
    for node_id, licence in IN_PROGRESS.items():
        assert graph.nodes[node_id].kind is SkillKind.CREDENTIAL, node_id
        assert node_id not in licences, node_id
        assert licence in licences or licence == "cred.cfa", node_id  # the CFA charter is not a gated licence
        assert 0.4 <= graph.related_weight(node_id, licence) <= 0.8, node_id
    # resolver.json maps every professional licence, and the CFA charter, to its formal in-progress
    # node (D-28).
    in_progress = load_json_artifact(TAXONOMY, "resolver.json")["in_progress"]
    assert set(in_progress) == licences | {"cred.cfa"}
    assert in_progress == {
        "cred.cfa": "cred.cfa_candidate",
        "cred.registered_nurse": "cred.nursing_student", "cred.licensed_practical_nurse": "cred.nursing_student",
        "cred.cpa": "cred.cpa_in_progress", "cred.commercial_drivers_license": "cred.cdl_trainee",
        "cred.professional_engineer": "cred.eit", "cred.bar_admission": "cred.articling_student",
        "cred.medical_license": "cred.medical_student", "cred.pharmacist_license": "cred.pharmacy_student",
        "cred.teaching_license": "cred.teacher_candidate", "cred.electrician_license": "cred.electrician_apprentice",
        "cred.paramedic_license": "cred.paramedic_student"}


@pytest.mark.parametrize(
    "skill, field", [("skill.statistics", "field.statistics"), ("skill.accounting", "field.accounting"),
                     ("skill.marketing", "field.marketing"), ("skill.finance", "field.finance"),
                     ("skill.economics", "field.economics"), ("skill.data_science", "field.data_science"),
                     ("skill.nursing", "field.nursing")])
def test_domains_named_like_a_field_of_study(graph, resolver, skill: str, field: str) -> None:
    # D-27: the skill owns the prose names, the field keeps its label for education lines.
    assert graph.nodes[skill].kind is SkillKind.DOMAIN
    assert graph.related_weight(skill, field) == 0.8
    label = graph.nodes[field].label
    assert graph.nodes[skill].label == label
    assert [m.canonical_id for m in resolver.extract(f"Experience in {label.lower()}.")] == [skill]
    assert [m.canonical_id for m in resolver.extract(f"BSc in {label}", include_fields=True)][-1] == field
    assert resolver.resolve(label, prefer_fields=True).canonical_id == field


def test_artifacts_stay_small() -> None:
    total = sum(path.stat().st_size for path in TAXONOMY.iterdir() if path.is_file())
    assert total < 20_000_000, total


def test_design_is_multi_word_only(graph, resolver) -> None:
    # D-27: "design" alone is everywhere in postings as a verb; the skill has multi-word names only,
    # the field keeps "Design" for education lines.
    names = (graph.nodes["skill.design"].label, *graph.nodes["skill.design"].aliases)
    assert all(" " in name for name in names), names
    assert graph.related_weight("skill.design", "field.design") == 0.8
    assert resolver.extract("Design, build and maintain scalable services; join our design reviews.") == ()
    assert [m.canonical_id for m in resolver.extract("Applied design thinking daily.")] == ["skill.design"]
    assert [m.canonical_id for m in resolver.extract("BDes in Design", include_fields=True)] == [
        "field.design", "field.design"]
