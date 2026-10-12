"""Related edges mined from Tailrd's postings, and the corpus report (Task 2.3; SPEC §3.2; DECISIONS D-22).

Run from match-engine/ after scripts/compute_idf.py, on the same corpus:

    .venv/Scripts/python.exe scripts/compute_pmi.py [--corpus FILE] [--taxonomy DIR] [--edges FILE]
        [--report FILE]

Counts every unordered pair of a posting's skills once (co-df; corpus_stats.py), leaves out the
pairs where one id is an ancestor of the other (the hierarchy relates them already: PostgreSQL sits
under SQL), and builds the edges with `graph.pmi.build_edges(df, co_df, N,
threshold=taxonomy.pmi_threshold, min_cooccurrence=taxonomy.pmi_min_cooccurrence,
max_out_degree=taxonomy.max_out_degree, curated=<config/curated_edges.yaml>)`. Writes, through
`write_artifacts` (nodes and IDF unchanged, every other artifact kept as it was):

    pmi_edges.json      the mined edges, the curated ones laid over them (a curated weight wins)
    corpus_report.md    aggregates only: the corpus, the 100 most frequent ids with df and idf, the
                        50 strongest mined edges, the noisy alias suspects and a watch list
    SOURCES.md          the "Corpus statistics" section

It refuses to run when idf.json was not computed from the same corpus over the same taxonomy.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import corpus_stats  # noqa: E402
from matcher.config import load_config  # noqa: E402
from matcher.graph.idf import normalize_idf  # noqa: E402
from matcher.graph.loader import load_graph, load_resolver  # noqa: E402
from matcher.graph.model import SkillGraph  # noqa: E402
from matcher.graph.pmi import build_edges, npmi  # noqa: E402
from matcher.graph.traversal import ancestors  # noqa: E402
from matcher.models import SkillKind  # noqa: E402

ANCESTOR_DEPTH = 64  # deeper than any parent chain of the taxonomy: "an ancestor" at any distance
SUSPECT_SHARE = 0.30  # noisy alias suspects: ids in more than this share of postings, not SOFT
WATCH_SHARE = 0.02  # the watch list read by hand for generic words: ids in at least this share
TOP_IDS = 100
TOP_EDGES = 50
SURFACES = 3  # matched surface forms shown per id


@dataclass(frozen=True)
class EdgeStats:
    frequent_pairs: int  # pairs of ids that co-occur in at least min_cooccurrence postings
    ancestor_pairs: int  # of those, left out: one id is an ancestor of the other
    above_threshold: int  # of the rest, pairs whose NPMI reaches the threshold
    mined_pairs: int  # mined pairs kept after the out-degree cap (stored one way or both), not curated
    curated_pairs: int


def curated_edges(path: str | Path = corpus_stats.DEFAULT_EDGES) -> dict[str, dict[str, float]]:
    """config/curated_edges.yaml as `{a: {b: weight}}` (validated by the taxonomy builder's reader)."""
    from build_taxonomy import load_curated_edges  # a sibling script

    found: dict[str, dict[str, float]] = defaultdict(dict)
    for a, b, weight in load_curated_edges(Path(path)):
        found[a][b] = weight
    return {a: dict(sorted(targets.items())) for a, targets in sorted(found.items())}


def _related_by_hierarchy(graph: SkillGraph, a: str, b: str) -> bool:
    return b in ancestors(graph, a, ANCESTOR_DEPTH) or a in ancestors(graph, b, ANCESTOR_DEPTH)


def related_edges(graph: SkillGraph, counts: corpus_stats.CorpusCounts, taxonomy: Any,
                  curated: Mapping[str, Mapping[str, float]]) -> tuple[dict[str, dict[str, float]], EdgeStats]:
    """The related edges (D-22) over the corpus counts, ancestor pairs left out, curated edges laid
    over; and what happened to the pairs on the way."""
    co_df: dict[tuple[str, str], int] = {}
    frequent = family = above = 0
    for (a, b), together in counts.co_df.items():
        related = _related_by_hierarchy(graph, a, b)
        if together >= taxonomy.pmi_min_cooccurrence:
            frequent += 1
            if related:
                family += 1
            else:
                weight = npmi(counts.n, counts.df[a], counts.df[b], together)
                above += weight is not None and weight >= taxonomy.pmi_threshold
        if not related:
            co_df[(a, b)] = together
    edges = build_edges(counts.df, co_df, counts.n, threshold=taxonomy.pmi_threshold,
                        min_cooccurrence=taxonomy.pmi_min_cooccurrence, max_out_degree=taxonomy.max_out_degree,
                        curated=curated)
    curated_pairs = {frozenset((a, b)) for a in curated for b in curated[a] if a != b}
    kept = {frozenset((a, b)) for a in edges for b in edges[a]}
    return edges, EdgeStats(frequent, family, above, len(kept - curated_pairs), len(curated_pairs))


# ─── the report ────────────────────────────────────────────────────────────────────────────


def _cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _surfaces(counts: corpus_stats.CorpusCounts, skill: str) -> str:
    found = sorted(counts.surfaces.get(skill, {}).items(), key=lambda item: (-item[1], item[0]))[:SURFACES]
    return ", ".join(f"{_cell(surface)} ({count:,})" for surface, count in found)


def report_markdown(summary: corpus_stats.CorpusSummary, counts: corpus_stats.CorpusCounts, graph: SkillGraph,
                    stats: EdgeStats, curated: Mapping[str, Mapping[str, float]], taxonomy: Any) -> str:
    """The corpus report: aggregates only (counts, ids, labels, matched surface forms)."""
    n = counts.n
    mentions = sum(counts.df.values())
    lines = [
        "# Corpus statistics report",
        "",
        "Generated by `scripts/compute_pmi.py` (Task 2.3; SPEC §3.1-§3.2; DECISIONS D-21, D-22, D-26). Aggregates only:",
        "counts, ids, labels and the surface forms the resolver matched, never posting text. Do not edit by hand:",
        "recompute (`SOURCES.md`, \"Corpus statistics\").",
        "",
        "## Corpus",
        "",
        f"- Source: {summary.source}.",
        f"- {n:,} postings; first seen {corpus_stats._span(summary.first_seen)}; scraped "
        f"{corpus_stats._span(summary.scraped)}; `{summary.file}` sha256 `{summary.sha256}`.",
        f"- {counts.html_postings:,} descriptions are stored as HTML (decoded before extraction); {counts.truncated:,} "
        "are stored cut short (they end in \"...\"), so skills named late in a posting are undercounted.",
        f"- {counts.skill_postings:,} postings name at least one skill; {mentions:,} (posting, skill) pairs, "
        f"{mentions / n:.2f} per posting; {len(counts.df):,} distinct ids.",
        "",
        f"## The {TOP_IDS} most frequent ids",
        "",
        "| # | id | label | kind | df | share | idf |",
        "|---:|---|---|---|---:|---:|---:|",
    ]
    ranked = sorted(counts.df.items(), key=lambda item: (-item[1], item[0]))
    for rank, (skill, df) in enumerate(ranked[:TOP_IDS], 1):
        node = graph.nodes[skill]
        lines.append(f"| {rank} | `{skill}` | {_cell(node.label)} | {node.kind.value} | {df:,} | {df / n:.1%} | "
                     f"{graph.idf[skill]:.4f} |")

    curated_pairs = {frozenset((a, b)) for a in curated for b in curated[a] if a != b}
    mined: dict[tuple[str, str], float] = {}
    for a, targets in graph.related.items():
        for b, weight in targets.items():
            if frozenset((a, b)) not in curated_pairs:
                mined[(a, b) if a < b else (b, a)] = weight
    strongest = sorted(mined.items(), key=lambda item: (-item[1], item[0]))[:TOP_EDGES]
    lines += [
        "",
        f"## The {TOP_EDGES} strongest mined edges",
        "",
        f"{stats.frequent_pairs:,} pairs of ids co-occur in at least {taxonomy.pmi_min_cooccurrence} postings; "
        f"{stats.ancestor_pairs:,} of them are left out because one id is an ancestor of the other; "
        f"{stats.above_threshold:,} of the rest reach NPMI {taxonomy.pmi_threshold}; {stats.mined_pairs:,} are kept "
        f"after the out-degree cap of {taxonomy.max_out_degree} per node; {stats.curated_pairs:,} curated pairs are "
        "laid over them (a curated weight wins). Weight = NPMI (D-22).",
        "",
        "| weight | a | b | co-df | df a | df b |",
        "|---:|---|---|---:|---:|---:|",
    ]
    for (a, b), weight in strongest:
        lines.append(f"| {weight:.3f} | `{a}` ({_cell(graph.nodes[a].label)}) | `{b}` ({_cell(graph.nodes[b].label)}) | "
                     f"{counts.co_df.get((a, b), 0):,} | {counts.df.get(a, 0):,} | {counts.df.get(b, 0):,} |")

    def not_soft(skill: str) -> bool:
        return graph.nodes[skill].kind is not SkillKind.SOFT

    suspects = [(skill, df) for skill, df in ranked if df / n > SUSPECT_SHARE and not_soft(skill)]
    lines += ["", "## Noisy alias suspects", "",
              f"Ids named in more than {SUSPECT_SHARE:.0%} of the postings that are not SOFT: a generic word mapped "
              "to a specific skill shows up here (D-26).", ""]
    if suspects:
        lines += ["| id | label | kind | df | share | most frequent surfaces |", "|---|---|---|---:|---:|---|"]
        for skill, df in suspects:
            node = graph.nodes[skill]
            lines.append(f"| `{skill}` | {_cell(node.label)} | {node.kind.value} | {df:,} | {df / n:.1%} | "
                         f"{_surfaces(counts, skill)} |")
    else:
        first = next(((skill, df) for skill, df in ranked if not_soft(skill)), None)
        lines.append("None." + (f" The most frequent id that is not SOFT is `{first[0]}`, in {first[1] / n:.1%} of the "
                                "postings." if first else ""))
    watch = [(skill, df) for skill, df in ranked if df / n >= WATCH_SHARE and not_soft(skill)]
    lines += ["", "## Watch list", "",
              f"Ids that are not SOFT named in at least {WATCH_SHARE:.0%} of the postings, with the surface forms that "
              "matched most often: read by hand for generic words, which `config/curated_taxonomy.yaml` then blocks "
              "(D-26).", "",
              "| id | label | kind | df | share | most frequent surfaces |", "|---|---|---|---:|---:|---|"]
    for skill, df in watch:
        node = graph.nodes[skill]
        lines.append(f"| `{skill}` | {_cell(node.label)} | {node.kind.value} | {df:,} | {df / n:.1%} | "
                     f"{_surfaces(counts, skill)} |")
    return "\n".join(lines) + "\n"


# ─── command line ──────────────────────────────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", type=Path, default=corpus_stats.DEFAULT_CORPUS)
    parser.add_argument("--taxonomy", type=Path, default=corpus_stats.DEFAULT_TAXONOMY)
    parser.add_argument("--edges", type=Path, default=corpus_stats.DEFAULT_EDGES, help="the curated edges")
    parser.add_argument("--report", type=Path, default=None, help="default: <taxonomy>/corpus_report.md")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    config = load_config()
    taxonomy = config.scoring.taxonomy
    resolver = load_resolver(args.taxonomy, config)
    graph = load_graph(args.taxonomy)
    postings = corpus_stats.read_postings(args.corpus)
    counts = corpus_stats.count_corpus(resolver, postings)
    if counts.n == 0 or dict(graph.idf) != normalize_idf(counts.df, counts.n):
        raise SystemExit("compute_pmi: idf.json was not computed from this corpus over this taxonomy: "
                         "run scripts/compute_idf.py on it first")
    curated = curated_edges(args.edges)
    edges, stats = related_edges(graph, counts, taxonomy, curated)
    corpus_stats.rewrite_taxonomy(args.taxonomy, edges=edges)
    summary = corpus_stats.summarize(args.corpus, postings)
    report = args.report if args.report is not None else args.taxonomy / "corpus_report.md"
    text = report_markdown(summary, counts, load_graph(args.taxonomy), stats, curated, taxonomy)
    report.write_bytes(text.encode("utf-8"))
    corpus_stats.write_sources_section(args.taxonomy, summary, counts, taxonomy, args.edges)
    print(f"{stats.mined_pairs:,} mined pairs ({stats.ancestor_pairs:,} ancestor pairs left out) and "
          f"{stats.curated_pairs:,} curated over {counts.n:,} postings; report {report}; "
          f"{time.perf_counter() - started:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
