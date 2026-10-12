"""Corpus statistics shared by compute_idf.py and compute_pmi.py (Task 2.3; SPEC §3.1-§3.2).

The corpus is `data/raw/corpus/postings.jsonl.gz` (scripts/pull_corpus.py; git-ignored, DECISIONS
D-04): one JSON object per posting. Each posting is read as its title, a line break and its
description, the description decoded from HTML first (most of Tailrd's stored descriptions are
escaped HTML: `&lt;li&gt;Python&lt;/li&gt;` would otherwise glue the skill to the markup), and
run through `Resolver.extract` in prose context over the taxonomy being built. A skill counts once
per posting (document frequency, df) and every unordered pair of a posting's skills once
(co-document frequency, co-df); a posting's single words count once too (for common_words.json).
Everything is integer counting into sorted mappings, so nothing depends on the order of the
postings or on the hash seed.

Only aggregates leave this module (counts, ids, dates, short matched surface forms), never posting
text. `rewrite_taxonomy` rewrites the hash-pinned artifacts with some of them replaced and every
other one written back unchanged; `write_sources_section` keeps the "Corpus statistics" section of
SOURCES.md, which scripts/build_taxonomy.py regenerates without it, in step with the artifacts.
"""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import re
import sys
import textwrap
from collections import Counter, defaultdict
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from matcher.graph.loader import load_graph, load_json_artifact, write_artifacts  # noqa: E402
from matcher.normalize.resolver import Resolver, _tokenize  # noqa: E402

DEFAULT_CORPUS = ROOT / "data" / "raw" / "corpus" / "postings.jsonl.gz"
DEFAULT_TAXONOMY = ROOT / "data" / "taxonomy"
DEFAULT_EDGES = ROOT / "config" / "curated_edges.yaml"
PULL_RECORD = "pull_record.json"
COMMON_WORDS_JSON = "common_words.json"
COMMON_WORD_MIN_DF = 20  # a word in this many postings or more is a word, not a typo
SECTION = "## Corpus statistics"
WRAP = 104  # SOURCES.md line width
RECOMPUTE = (
    ".venv/Scripts/python.exe scripts/pull_corpus.py --env-file <path to .env> && "
    ".venv/Scripts/python.exe scripts/compute_idf.py && .venv/Scripts/python.exe scripts/compute_pmi.py"
)
_GRAPH_FILES = frozenset({"nodes.json.gz", "nodes.json", "idf.json", "pmi_edges.json"})

# Block tags end a line (a list item is a line of its own, as in plain-text postings); other tags
# become a space, so "<strong>Python</strong>" stays one word.
_BLOCK_TAG = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|hr|table|thead|tbody|tfoot|tr|td|th|section|article|header|footer"
    r"|blockquote|pre|dl|dt|dd)\b[^<>]*>",
    re.IGNORECASE,
)
_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9]*\b[^<>]*>")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MARKUP = re.compile(r"&lt;|</?[A-Za-z][A-Za-z0-9]*\b[^<>]*>")
# A word: letters, perhaps hyphen-joined ("e-mail"). Numbers, URL and e-mail fragments ("jane.doe")
# are no dictionary words, and an e-mail fragment can be a person's name.
_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")


# ─── reading ───────────────────────────────────────────────────────────────────────────────


def read_postings(path: str | Path) -> list[dict[str, Any]]:
    """The postings of a `.jsonl` or `.jsonl.gz` corpus file, one JSON object per line."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def html_to_text(markup: str) -> str:
    """A description as text: entities decoded (up to three times: some are escaped twice), then
    block tags as line breaks and other tags as spaces. Plain text comes back unchanged."""
    text = markup
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = _COMMENT.sub(" ", text)
    return _TAG.sub(" ", _BLOCK_TAG.sub("\n", text))


def posting_text(posting: Mapping[str, Any]) -> str:
    """What extraction reads: the title, a line break, the description as text."""
    return f"{posting.get('title') or ''}\n{html_to_text(posting.get('description') or '')}"


def posting_words(text: str) -> frozenset[str]:
    """The normalised single words of `text`, as `resolve` would look each one up: the resolver's
    own tokens, read past a French elision ("l'équipe" -> "équipe")."""
    words = set()
    for token in _tokenize(text):
        form = token.lead_forms[0][1]
        if _WORD.fullmatch(form):
            words.add(form)
    return frozenset(words)


# ─── counting ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CorpusCounts:
    """Integer counts over a corpus; every mapping is sorted by key."""

    n: int  # postings read
    df: dict[str, int]  # skill id -> postings that name it
    co_df: dict[tuple[str, str], int]  # (a, b) with a < b -> postings that name both
    word_df: dict[str, int]  # normalised word -> postings that use it
    surfaces: dict[str, dict[str, int]]  # skill id -> {surface as matched: mentions}, for the report
    skill_postings: int  # postings that name at least one skill
    html_postings: int  # postings whose description is stored as HTML
    truncated: int  # descriptions stored cut short (they end in "...")


def count_corpus(resolver: Resolver, postings: Iterable[Mapping[str, Any]]) -> CorpusCounts:
    """df, co-df and word df over `postings` (each skill, pair and word once per posting)."""
    n = skill_postings = html_postings = truncated = 0
    df: Counter[str] = Counter()
    co_df: Counter[tuple[str, str]] = Counter()
    word_df: Counter[str] = Counter()
    surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    for posting in postings:
        n += 1
        description = posting.get("description") or ""
        html_postings += bool(_MARKUP.search(description))
        truncated += description.rstrip().endswith(("...", "\u2026"))
        text = posting_text(posting)
        mentions = resolver.extract(text)
        ids = sorted({mention.canonical_id for mention in mentions})
        for mention in mentions:
            surfaces[mention.canonical_id][mention.surface] += 1
        skill_postings += bool(ids)
        df.update(ids)
        co_df.update(combinations(ids, 2))  # ids are sorted: every pair is (smaller, larger)
        word_df.update(posting_words(text))
    return CorpusCounts(
        n=n,
        df=dict(sorted(df.items())),
        co_df=dict(sorted(co_df.items())),
        word_df=dict(sorted(word_df.items())),
        surfaces={skill: dict(sorted(found.items())) for skill, found in sorted(surfaces.items())},
        skill_postings=skill_postings,
        html_postings=html_postings,
        truncated=truncated,
    )


def common_words(resolver: Resolver, word_df: Mapping[str, int], *, min_df: int = COMMON_WORD_MIN_DF) -> list[str]:
    """Every word used in at least `min_df` postings that does not itself resolve exactly (to a
    label or an alias), most frequent first, then alphabetically: the words `resolve` must never
    read as a typo (Task 2.3)."""
    frequent = [word for word, count in word_df.items() if count >= min_df]
    words = [word for word in frequent if resolver.resolve(word).method not in ("EXACT", "ALIAS")]
    return sorted(words, key=lambda word: (-word_df[word], word))


# ─── the corpus as a source ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CorpusSummary:
    source: str  # what the corpus is (the pull record's label, else the file name)
    file: str
    sha256: str
    postings: int
    first_seen: Optional[tuple[str, str]]  # first and last date
    scraped: Optional[tuple[str, str]]
    pulled: str  # the date of the pull, "" when unknown


def _dates(postings: Sequence[Mapping[str, Any]], field: str) -> Optional[tuple[str, str]]:
    values = sorted(str(posting[field])[:10] for posting in postings if posting.get(field))
    return (values[0], values[-1]) if values else None


def summarize(path: str | Path, postings: Sequence[Mapping[str, Any]]) -> CorpusSummary:
    """Who and when, never what: the corpus file's hash, size and dates, and its pull record."""
    path = Path(path)
    record: dict[str, Any] = {}
    if (path.parent / PULL_RECORD).exists():
        record = json.loads((path.parent / PULL_RECORD).read_text(encoding="utf-8"))
    return CorpusSummary(
        source=str(record.get("source") or path.name),
        file=path.name,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        postings=len(postings),
        first_seen=_dates(postings, "first_seen_at"),
        scraped=_dates(postings, "scraped_at"),
        pulled=str(record.get("pulled_at") or "")[:10],
    )


def _span(span: Optional[tuple[str, str]]) -> str:
    return "unknown" if span is None else f"{span[0]} to {span[1]}"


def source_line(summary: CorpusSummary) -> str:
    """The MANIFEST `sources` entry of the corpus."""
    return (f"{summary.source}: {summary.postings:,} postings, first seen {_span(summary.first_seen)}; "
            f"{summary.file} sha256 {summary.sha256}")


# ─── writing ───────────────────────────────────────────────────────────────────────────────


def rewrite_taxonomy(
    taxonomy_dir: str | Path,
    *,
    idf: Optional[Mapping[str, float]] = None,
    n_postings: Optional[int] = None,
    edges: Optional[Mapping[str, Mapping[str, float]]] = None,
    extra: Optional[Mapping[str, Mapping[str, Any]]] = None,
    sources: Optional[Mapping[str, str]] = None,
) -> None:
    """Write the taxonomy's artifacts again through `write_artifacts` with the given ones replaced
    (IDF and its posting count, edges, extra documents, source lines); the nodes and every other
    listed artifact are verified on reading and written back as they were."""
    root = Path(taxonomy_dir)
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    graph = load_graph(root)
    documents = {name: load_json_artifact(root, name) for name in sorted(manifest["artifacts"])
                 if name not in _GRAPH_FILES}
    documents.update(extra or {})
    write_artifacts(
        root,
        graph.nodes.values(),
        dict(graph.idf) if idf is None else idf,
        {a: dict(targets) for a, targets in graph.related.items()} if edges is None else edges,
        n_postings=load_json_artifact(root, "idf.json")["n_postings"] if n_postings is None else n_postings,
        sources={**manifest["sources"], **(sources or {})},
        compress="nodes.json.gz" in manifest["artifacts"],
        extra=documents,
    )


def curated_pairs(path: str | Path = DEFAULT_EDGES) -> frozenset[frozenset[str]]:
    """The unordered pairs of config/curated_edges.yaml."""
    from build_taxonomy import load_curated_edges  # a sibling script: validates ids' shape and weights

    return frozenset(frozenset((a, b)) for a, b, _ in load_curated_edges(Path(path)) if a != b)


def write_sources_section(taxonomy_dir: str | Path, summary: CorpusSummary, counts: CorpusCounts,
                          taxonomy_config: Any, edges_path: str | Path = DEFAULT_EDGES) -> None:
    """Replace (or add) the "Corpus statistics" section at the end of SOURCES.md, describing the
    corpus and the IDF, edges and common words now on disk."""
    root = Path(taxonomy_dir)
    idf = load_json_artifact(root, "idf.json")
    stored = load_json_artifact(root, "pmi_edges.json")["edges"]
    pairs = {frozenset((a, b)) for a, targets in stored.items() for b in targets if a != b}
    curated = curated_pairs(edges_path)
    words: list[str] = []
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    if COMMON_WORDS_JSON in manifest["artifacts"]:
        words = load_json_artifact(root, COMMON_WORDS_JSON)["words"]
    least = min((counts.word_df.get(word, 0) for word in words), default=0)
    intro = ("Written by `scripts/compute_idf.py` and `scripts/compute_pmi.py` (Task 2.3; SPEC §3.1-§3.2; DECISIONS "
             "D-21, D-22) from Tailrd's own postings. Aggregates only: the posting text stays in `data/raw/corpus/` "
             "(git-ignored, D-04). `corpus_report.md` lists the most frequent ids, the strongest mined edges and the "
             "noisy-alias review.")
    bullets = [
        f"Source: {summary.source}: the postings whose description is longer than 200 characters and that are no "
        "duplicate (`duplicate_of IS NULL`), pulled by `scripts/pull_corpus.py`"
        + (f" on {summary.pulled}." if summary.pulled else "."),
        f"{summary.postings:,} postings; first seen {_span(summary.first_seen)}; scraped {_span(summary.scraped)}; "
        f"`{summary.file}` sha256 `{summary.sha256}`.",
        "Extraction: each posting is read as its title, a line break and its description, decoded from HTML "
        "(entities, then tags; a block tag ends a line), through `Resolver.extract` in prose context over this "
        "taxonomy. A skill counts once per posting (df), every unordered pair of a posting's skills once (co-df). "
        f"{counts.truncated:,} of the {counts.n:,} descriptions are stored cut short (they end in \"...\"), so "
        "skills named late in a posting are undercounted.",
        f"`idf.json`: `normalize_idf(df, N)` (D-21) over N = {idf['n_postings']:,} postings, for the "
        f"{len(idf['idf']):,} ids seen at least once; an id never seen takes `idf.unseen_default`.",
        f"`pmi_edges.json`: {len(pairs - curated):,} pairs mined (NPMI >= {taxonomy_config.pmi_threshold} over pairs "
        f"in >= {taxonomy_config.pmi_min_cooccurrence} postings, at most {taxonomy_config.max_out_degree} per node, "
        f"pairs where one id is an ancestor of the other left out; D-22) and {len(curated):,} curated pairs laid "
        "over them.",
        f"`common_words.json`: {len(words):,} words, each used in at least {least:,} postings, that resolve to no "
        "key; `resolve()` never fuzzy-matches them.",
        "Recompute against any database (reads only `DATABASE_URL`), from `match-engine/`; run the two compute "
        "scripts again after every `scripts/build_taxonomy.py`, then the gate (`tests/test_surface_forms.py`):",
    ]
    lines = [SECTION, "", *textwrap.wrap(intro, WRAP), ""]
    for bullet in bullets:
        lines += textwrap.wrap(bullet, WRAP, initial_indent="- ", subsequent_indent="  ", break_on_hyphens=False)
    lines += ["", f"      {RECOMPUTE}", ""]
    path = root / "SOURCES.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    head = text.split("\n" + SECTION + "\n", 1)[0].rstrip("\n")
    path.write_bytes(((head + "\n\n" if head else "") + "\n".join(lines)).encode("utf-8"))
