"""Corpus statistics from Tailrd's postings (Task 2.3; SPEC §3.1-§3.2; DECISIONS D-21, D-22).

The first part runs scripts/corpus_stats.py, compute_idf.py and compute_pmi.py over
tests/fixtures/corpus_mini.jsonl, 30 invented postings whose skills are laid out so every count
can be checked by hand:

    postings  1-6   PostgreSQL and SQL            (PostgreSQL sits under SQL: an ancestor pair)
    postings  7-10  SQL and Python                (4 together: under the co-occurrence floor of 5)
    postings 11-18  Python (11-13 escaped HTML, 13 escaped twice; 14 names Python three times)
    postings 19-24  Kubernetes and Docker         (Kubernetes sits under Docker: an ancestor pair)
    postings 20-26  Terraform; 21-27 AWS; 19-25 Docker
    postings 28-30  React                         (its curated edge to Vue needs no Vue)

The second part checks the committed artifacts computed from the real corpus.
"""

from __future__ import annotations

import importlib
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from matcher.graph.idf import normalize_idf
from matcher.graph.loader import (
    load_graph,
    load_json_artifact,
    load_resolver,
    load_resolver_settings,
    write_artifacts,
)
from matcher.graph.pmi import npmi
from matcher.graph.traversal import ancestors
from matcher.normalize.resolver import Resolver
from tests.fixtures.build_graph_small import EDGES, NODES, SOURCES

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TAXONOMY = ROOT / "data" / "taxonomy"
CURATED_EDGES = ROOT / "config" / "curated_edges.yaml"
MINI = Path(__file__).resolve().parent / "fixtures" / "corpus_mini.jsonl"


def _script(name: str):
    if str(SCRIPTS) not in sys.path:  # the compute scripts import corpus_stats as a sibling
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def corpus_stats():
    return _script("corpus_stats")


@pytest.fixture(scope="module")
def compute_pmi():
    return _script("compute_pmi")


@pytest.fixture(scope="module")
def compute_idf():
    return _script("compute_idf")


@pytest.fixture(scope="module")
def resolver(config):
    return load_resolver(TAXONOMY, config)


@pytest.fixture(scope="module")
def graph():
    return load_graph(TAXONOMY)


@pytest.fixture(scope="module")
def postings(corpus_stats):
    return corpus_stats.read_postings(MINI)


@pytest.fixture(scope="module")
def counts(corpus_stats, resolver, postings):
    return corpus_stats.count_corpus(resolver, postings)


# ─── counting (corpus_stats.py) ────────────────────────────────────────────────────────────


def test_document_and_co_document_frequency_match_hand_counts(postings, counts) -> None:
    assert len(postings) == counts.n == 30
    assert counts.df["skill.python"] == 12  # postings 7-18, once each (posting 14 names it three times)
    assert counts.df["skill.sql"] == 10  # postings 1-10 ("PostgreSQL" is its own skill)
    assert counts.df["skill.kubernetes"] == 6  # postings 19-24
    assert counts.co_df[("skill.python", "skill.sql")] == 4  # postings 7-10
    assert counts.co_df[("skill.postgresql", "skill.sql")] == 6
    assert all(a < b for a, b in counts.co_df)  # unordered pairs, each written once
    assert list(counts.df) == sorted(counts.df) and list(counts.co_df) == sorted(counts.co_df)


def test_an_html_description_is_read_as_text(corpus_stats, resolver) -> None:
    # 62 % of Tailrd's stored descriptions are escaped HTML: entities are decoded (twice-escaped
    # ones too), block tags end a line and inline tags become a space, so "Python" is a word again.
    assert corpus_stats.html_to_text("&lt;p&gt;We build in &lt;strong&gt;Python&lt;/strong&gt;.&lt;/p&gt;") == (
        "\nWe build in  Python .\n")
    assert corpus_stats.html_to_text("&amp;lt;li&amp;gt;SQL&amp;lt;/li&amp;gt;") == "\nSQL\n"
    assert corpus_stats.html_to_text("R&D, AT&T and 5 < 6") == "R&D, AT&T and 5 < 6"  # plain text stays
    posting = {"title": "Engineer", "description": "&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;"}
    assert [m.canonical_id for m in resolver.extract(corpus_stats.posting_text(posting))] == ["skill.python"]


def test_counts_ignore_the_order_of_the_postings(corpus_stats, compute_pmi, resolver, graph, postings, counts,
                                                 config) -> None:
    shuffled = list(postings)
    random.Random(2026).shuffle(shuffled)
    again = corpus_stats.count_corpus(resolver, shuffled)
    assert again == counts
    assert normalize_idf(again.df, again.n) == normalize_idf(counts.df, counts.n)
    taxonomy = config.scoring.taxonomy
    curated = compute_pmi.curated_edges(CURATED_EDGES)
    assert compute_pmi.related_edges(graph, again, taxonomy, curated) == compute_pmi.related_edges(
        graph, counts, taxonomy, curated)


def test_ancestor_pairs_are_never_mined(compute_pmi, graph, counts, config) -> None:
    taxonomy = config.scoring.taxonomy
    for a, b in (("skill.postgresql", "skill.sql"), ("skill.docker", "skill.kubernetes")):
        together = counts.co_df[(a, b)]
        assert together >= taxonomy.pmi_min_cooccurrence  # frequent and associated enough to be an edge ...
        assert npmi(counts.n, counts.df[a], counts.df[b], together) >= taxonomy.pmi_threshold
        assert b in ancestors(graph, a, 64) or a in ancestors(graph, b, 64)  # ... but the hierarchy relates them
    edges, stats = compute_pmi.related_edges(graph, counts, taxonomy, {})
    for a, b in (("skill.postgresql", "skill.sql"), ("skill.docker", "skill.kubernetes")):
        assert b not in edges.get(a, {}) and a not in edges.get(b, {})
    assert stats.ancestor_pairs == 2
    # The pairs that are no family: kept in both directions with their NPMI.
    assert edges["skill.aws"]["skill.terraform"] == edges["skill.terraform"]["skill.aws"] == pytest.approx(
        npmi(30, 7, 7, 6))
    assert edges["skill.kubernetes"]["skill.terraform"] == pytest.approx(npmi(30, 6, 7, 5))
    assert "skill.sql" not in edges.get("skill.python", {})  # 4 postings: under the floor
    assert sorted((a, b) for a in edges for b in edges[a] if a < b) == [
        ("skill.aws", "skill.docker"), ("skill.aws", "skill.terraform"), ("skill.docker", "skill.terraform"),
        ("skill.kubernetes", "skill.terraform")]


def test_curated_edges_are_laid_over_the_mined_ones(compute_pmi, graph, counts, config) -> None:
    curated = compute_pmi.curated_edges(CURATED_EDGES)
    edges, stats = compute_pmi.related_edges(graph, counts, config.scoring.taxonomy, curated)
    assert counts.df.get("skill.vue", 0) == 0
    assert edges["skill.react"]["skill.vue"] == edges["skill.vue"]["skill.react"] == 0.65  # Vue never appears
    for a, targets in curated.items():
        for b, weight in targets.items():
            assert edges[a][b] == edges[b][a] == weight, (a, b)
    assert edges["skill.aws"]["skill.terraform"] == pytest.approx(npmi(30, 7, 7, 6))  # mined pairs stay
    assert stats.curated_pairs == len({frozenset((a, b)) for a in curated for b in curated[a]})


def test_common_words_are_frequent_words_that_resolve_to_nothing(corpus_stats, resolver, counts) -> None:
    words = corpus_stats.common_words(resolver, counts.word_df, min_df=5)
    assert words and all(counts.word_df[word] >= 5 for word in words)
    assert len(words) == len(set(words))
    assert {"python", "sql", "aws", "terraform"}.isdisjoint(words)  # skills resolve exactly
    assert {"and", "write"} <= set(words)
    ranks = [(-counts.word_df[word], word) for word in words]  # most frequent first, then alphabetical
    assert ranks == sorted(ranks)
    assert all(word == word.lower() and " " not in word for word in words)


def test_a_posting_word_is_a_word_of_letters(corpus_stats) -> None:
    # Numbers, e-mail and URL fragments are no dictionary words (an e-mail fragment can name a person).
    words = corpus_stats.posting_words("Write SQL; e-mail jane.doe@example.com or visit www.example.com by 2026, "
                                       "l'équipe d'Excel.")
    assert words == frozenset({"write", "sql", "e-mail", "or", "visit", "by", "équipe", "excel"})


def test_the_computation_is_the_same_under_two_hash_seeds() -> None:
    code = (
        "import sys, json, hashlib; sys.path[:0] = [r'{src}', r'{scripts}'];"
        "import corpus_stats, compute_pmi;"
        "from matcher.config import load_config; from matcher.graph.loader import load_graph, load_resolver;"
        "from matcher.graph.idf import normalize_idf;"
        "c = load_config(); r = load_resolver(r'{taxonomy}', c); g = load_graph(r'{taxonomy}');"
        "n = corpus_stats.count_corpus(r, corpus_stats.read_postings(r'{mini}'));"
        "e, s = compute_pmi.related_edges(g, n, c.scoring.taxonomy, compute_pmi.curated_edges(r'{edges}'));"
        "doc = [normalize_idf(n.df, n.n), e, corpus_stats.common_words(r, n.word_df, min_df=3)];"
        "print(hashlib.sha256(json.dumps(doc, sort_keys=False).encode()).hexdigest())"
    ).format(src=ROOT / "src", scripts=SCRIPTS, taxonomy=TAXONOMY, mini=MINI, edges=CURATED_EDGES)
    digests = set()
    for seed in ("1", "4242"):
        environment = {**os.environ, "PYTHONHASHSEED": seed}
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=environment,
                                check=True)
        digests.add(result.stdout.strip())
    assert len(digests) == 1, digests


# ─── the scripts end to end, on a copy of the taxonomy ─────────────────────────────────────


@pytest.fixture(scope="module")
def slim_taxonomy(tmp_path_factory) -> Path:
    """The committed graph and resolver settings, nodes uncompressed and without the big extras
    (titles, military codes, NAICS): everything the compute scripts read, in a fraction of the time."""
    target = tmp_path_factory.mktemp("slim") / "taxonomy"
    graph = load_graph(TAXONOMY)
    manifest = json.loads((TAXONOMY / "MANIFEST.json").read_text(encoding="utf-8"))
    write_artifacts(target, graph.nodes.values(), dict(graph.idf), {a: dict(t) for a, t in graph.related.items()},
                    n_postings=load_json_artifact(TAXONOMY, "idf.json")["n_postings"], sources=manifest["sources"],
                    compress=False, extra={"resolver.json": load_json_artifact(TAXONOMY, "resolver.json")})
    shutil.copy(TAXONOMY / "SOURCES.md", target / "SOURCES.md")
    return target


def test_a_rewrite_writes_every_other_artifact_back_byte_for_byte(tmp_path: Path, corpus_stats) -> None:
    extra = {"resolver.json": {"format": 1, "ambiguous": {"go": "Go"}},
             "titles.json": {"format": 1, "titles": {"software developers": {"soc": "15-1252.00"}}}}
    write_artifacts(tmp_path, NODES, {}, EDGES, n_postings=0, sources=SOURCES, extra=extra)
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    skill = sorted(node.id for node in NODES)[0]
    corpus_stats.rewrite_taxonomy(tmp_path, idf={skill: 0.5}, n_postings=10, sources={"corpus": "a corpus"})
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert set(after) == set(before)
    assert {name for name in before if before[name] != after[name]} == {"idf.json", "MANIFEST.json"}
    assert load_json_artifact(tmp_path, "idf.json") == {"format": 1, "n_postings": 10, "idf": {skill: 0.5}}
    sources = json.loads((tmp_path / "MANIFEST.json").read_text(encoding="utf-8"))["sources"]
    assert sources == {**SOURCES, "corpus": "a corpus"}


def test_the_compute_scripts_write_hash_pinned_artifacts(tmp_path: Path, slim_taxonomy: Path, compute_idf,
                                                         compute_pmi, config) -> None:
    taxonomy = tmp_path / "taxonomy"
    shutil.copytree(slim_taxonomy, taxonomy)
    report = taxonomy / "corpus_report.md"
    arguments = ["--corpus", str(MINI), "--taxonomy", str(taxonomy)]
    assert compute_idf.main([*arguments, "--min-word-df", "5"]) == 0
    assert compute_pmi.main([*arguments, "--report", str(report)]) == 0
    first = {path.name: path.read_bytes() for path in taxonomy.iterdir()}

    graph = load_graph(taxonomy)
    idf = load_json_artifact(taxonomy, "idf.json")
    assert idf["n_postings"] == 30 and graph.idf == normalize_idf(
        compute_idf.corpus_stats.count_corpus(load_resolver(taxonomy, config),
                                              compute_idf.corpus_stats.read_postings(MINI)).df, 30)
    words = load_json_artifact(taxonomy, "common_words.json")["words"]
    assert "and" in words and "python" not in words
    assert graph.related["skill.aws"]["skill.terraform"] == pytest.approx(npmi(30, 7, 7, 6))
    manifest = json.loads((taxonomy / "MANIFEST.json").read_text(encoding="utf-8"))
    assert set(manifest["artifacts"]) == {"common_words.json", "idf.json", "nodes.json", "pmi_edges.json",
                                          "resolver.json"}
    assert "corpus" in manifest["sources"]
    load_resolver(taxonomy, config)  # every pinned artifact still verifies

    sources = (taxonomy / "SOURCES.md").read_text(encoding="utf-8")
    assert sources.count("## Corpus statistics") == 1 and "--env-file" in sources
    text = report.read_text(encoding="utf-8")
    for heading in ("## Corpus", "## The 100 most frequent ids", "## The 50 strongest mined edges",
                    "## Noisy alias suspects"):
        assert heading in text, heading
    # No posting text: matched skill surfaces may appear ("billing software"), the prose around them never.
    for leaked in ("Maple Grid", "Northwind", "document every query", "payments group", "explain pricing"):
        assert leaked not in text and leaked not in sources

    # compute_pmi again over the same input: the same bytes everywhere (one Corpus section).
    assert compute_pmi.main([*arguments, "--report", str(report)]) == 0
    assert {path.name: path.read_bytes() for path in taxonomy.iterdir()} == first


def test_the_sources_section_is_replaced_never_repeated(tmp_path: Path, corpus_stats, config) -> None:
    write_artifacts(tmp_path, NODES, {}, EDGES, n_postings=0, sources=SOURCES,
                    extra={"common_words.json": {"format": 1, "words": ["patients", "salary"]}})
    head = "# Skill taxonomy sources\n\nBuilt by the builder.\n"
    (tmp_path / "SOURCES.md").write_text(head, encoding="utf-8")
    summary = corpus_stats.CorpusSummary(source="Test postings", file="postings.jsonl", sha256="0" * 64, postings=3,
                                         first_seen=("2026-05-12", "2026-05-14"), scraped=None, pulled="")
    counts = corpus_stats.CorpusCounts(n=3, df={}, co_df={}, word_df={"patients": 20, "salary": 25}, surfaces={},
                                       skill_postings=0, html_postings=0, truncated=1)
    corpus_stats.write_sources_section(tmp_path, summary, counts, config.scoring.taxonomy)
    once = (tmp_path / "SOURCES.md").read_text(encoding="utf-8")
    corpus_stats.write_sources_section(tmp_path, summary, counts, config.scoring.taxonomy)
    assert (tmp_path / "SOURCES.md").read_text(encoding="utf-8") == once
    assert once.startswith(head + "\n## Corpus statistics\n")
    assert once.count("## Corpus statistics") == 1
    assert "3 postings; first seen 2026-05-12 to 2026-05-14; scraped unknown" in once
    assert "2 words, each used in at least 20 postings" in once and "1 of the 3 descriptions" in once


def test_compute_pmi_refuses_an_idf_from_another_corpus(tmp_path: Path, slim_taxonomy: Path, compute_pmi) -> None:
    taxonomy = tmp_path / "taxonomy"
    shutil.copytree(slim_taxonomy, taxonomy)  # its idf.json counts the real corpus, not the 30 postings
    with pytest.raises(SystemExit, match="compute_idf"):
        compute_pmi.main(["--corpus", str(MINI), "--taxonomy", str(taxonomy), "--report",
                          str(tmp_path / "report.md")])


# ─── the committed artifacts (computed from Tailrd's postings) ─────────────────────────────


def _curated_pairs(compute_pmi) -> set[frozenset[str]]:
    curated = compute_pmi.curated_edges(CURATED_EDGES)
    return {frozenset((a, b)) for a in curated for b in curated[a]}


def test_the_committed_idf_and_edges(graph, compute_pmi, config) -> None:
    assert load_json_artifact(TAXONOMY, "idf.json")["n_postings"] >= 1000
    assert len(graph.idf) >= 500 and set(graph.idf) <= set(graph.nodes)
    assert all(0.0 <= value <= 1.0 for value in graph.idf.values())
    taxonomy = config.scoring.taxonomy
    curated = _curated_pairs(compute_pmi)
    mined = 0
    for a, targets in graph.related.items():
        assert a in graph.nodes and set(targets) <= set(graph.nodes)  # every endpoint is a node
        own = [b for b in targets if frozenset((a, b)) not in curated]
        assert len(own) <= taxonomy.max_out_degree, a  # out-degree <= 20 except curated
        for b in own:
            assert taxonomy.pmi_threshold <= targets[b] <= 1.0, (a, b)
            assert b not in ancestors(graph, a, 64) and a not in ancestors(graph, b, 64), (a, b)
        mined += len(own)
    assert mined > 0
    for pair in curated:  # every curated edge, both ways
        a, b = sorted(pair)
        assert graph.related[a][b] == graph.related[b][a]


def test_a_specialised_technology_outweighs_a_ubiquitous_skill(graph) -> None:
    # SPEC §3.1's point: what many postings ask for weighs less than a specialised technology.
    assert graph.idf["skill.kubernetes"] > graph.idf["skill.communication"]
    assert graph.idf["skill.kubernetes"] > graph.idf["skill.ai"]


@pytest.mark.xfail(strict=True, reason=(
    "SPEC §3.1's motivating example does not hold on Tailrd's Development-branch corpus: Kubernetes is named "
    "in 91 of 5,473 postings and Microsoft Office in 7 (tech-heavy postings whose stored descriptions are "
    "truncated before most requirement lists). strict: this flags the day a fuller corpus makes it hold."))
def test_kubernetes_outweighs_microsoft_office(graph) -> None:
    assert graph.idf["skill.kubernetes"] > graph.idf["skill.ms_office"]


def test_none_of_the_3000_most_frequent_common_words_fuzzy_resolves(graph, config) -> None:
    words = load_json_artifact(TAXONOMY, "common_words.json")["words"]  # most frequent first
    long_words = [word for word in words if len(word) >= config.scoring.taxonomy.fuzzy_min_token_length]
    assert len(words) >= 3000 and len(long_words) >= 1000
    guarded = load_resolver(TAXONOMY, config)
    for word in (*words[:3000], *long_words[:3000]):
        assert guarded.resolve(word).method != "FUZZY", word
    # Without the list the same words do fuzz ("salary" reaches SOLAR-2): the guard is what holds.
    settings = load_resolver_settings(TAXONOMY)
    plain = Resolver(graph, config, ambiguous=settings["ambiguous"], cased=settings["cased"],
                     blocked_phrases=settings["blocked_phrases"], in_progress=settings["in_progress"],
                     currency_skill_nouns=settings["currency_skill_nouns"])
    assert plain.resolve("salary").method == "FUZZY"
    assert sum(plain.resolve(word).method == "FUZZY" for word in long_words[:3000]) >= 100


def test_the_committed_report_and_sources_describe_the_same_corpus() -> None:
    text = (TAXONOMY / "corpus_report.md").read_text(encoding="utf-8")
    sources = (TAXONOMY / "SOURCES.md").read_text(encoding="utf-8")
    n_postings = load_json_artifact(TAXONOMY, "idf.json")["n_postings"]
    assert f"{n_postings:,} postings" in text and f"{n_postings:,} postings" in sources
    assert "## Noisy alias suspects" in text and "## The 50 strongest mined edges" in text
    assert sources.count("## Corpus statistics") == 1 and "--env-file" in sources
    for line in text.splitlines():  # table rows and notes, aggregates only: no line of posting prose
        assert len(line) <= 400, line[:80]
