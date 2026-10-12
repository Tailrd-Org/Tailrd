"""Taxonomy freshness: coverage metric, provisional nodes and the review queue (SPEC §16; Task 7.2).

Reads a posting corpus (`data/raw/corpus/postings.jsonl.gz`, one JSON object per posting with a
`first_seen_at`), counts each employer blurb and each exact duplicate once (`corpus_stats.
corpus_texts`, DECISIONS D-21), and parses every posting with `matcher.parse.posting.parse_posting`.
The parser reports, per posting, the requirement tokens it found (`requirement_token_count`: the
distinct ids in REQUIRED and PREFERRED clauses plus the tokens there that resolved to nothing) and
the unresolved ones (`unresolved_requirement_tokens`). From those:

* Coverage: resolved requirement tokens over all requirement tokens, summed over the postings first
  seen in the window (the `auto_promote_window_days` days ending at `as_of`). Printed, written to
  `reports/taxonomy/<as_of>.json`, and the exit code is 1 below `coverage_alert_threshold` (0.92),
  or when no posting in the window has a requirement token; 0 otherwise.
* Ranking: an unresolved token (keyed by `normalize_surface`) scores `recent x growth`, where
  `recent` is the distinct postings naming it in the window and `growth = (recent + 1) /
  (earlier + 1)` against the window before it: frequency times growth, a token that is new this
  window scoring above one that has been there all along. Ties: more recent postings, then the key.
* Provisional nodes: a token in more than `auto_promote_min_postings` (50) distinct postings of the
  window becomes a provisional node (`provisional=True`, `source="provisional"`, id
  `skill.<slug>`, label its most common spelling, kind its parent's) whose parent is the existing
  node with the highest NPMI (DECISIONS D-22) against the token over the window's postings, among
  the ids its postings require in at least `pmi_min_cooccurrence` (5) of them and named by at least
  `--min-parent-df` (20) postings: PMI rewards rarity, and a node in a handful of postings would
  otherwise win on chance (the low-df tail of Task 2.3). A SOFT node, or one under a SOFT node,
  never qualifies (a soft skill co-occurs with everything and places nothing). A token with no such neighbour is queued with the
  note "no parent", never promoted parentless.
* The committed taxonomy changes only with `--apply`: the promoted nodes are written through
  `write_artifacts` (a one-word name with a capital becomes a `cased` resolver key, as the builder
  does for nodes that are not curated, D-26); IDF and edges are kept, so `compute_idf.py` and
  `compute_pmi.py` must run afterwards (compute_pmi refuses a stale IDF). Without it the promotions
  are reported only.
* Review queue: the top `review_queue_size` (50) tokens not promoted, written to
  `data/taxonomy/review_queue.csv`: the token (at most MAX_SURFACE characters), its counts, score
  and suggested parent. Short phrases and counts only, never posting text (D-04).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from corpus_stats import DEFAULT_CORPUS, DEFAULT_TAXONOMY, corpus_texts, read_postings  # noqa: E402

from matcher.config import load_config  # noqa: E402
from matcher.graph.aliases import normalize_surface  # noqa: E402
from matcher.graph.loader import load_graph, load_json_artifact, load_resolver, write_artifacts  # noqa: E402
from matcher.graph.model import SkillGraph, SkillNode  # noqa: E402
from matcher.graph.pmi import npmi  # noqa: E402
from matcher.graph.traversal import ancestors  # noqa: E402
from matcher.models import SkillKind  # noqa: E402
from matcher.parse.config import load_parse_config  # noqa: E402
from matcher.parse.posting import PostingMeta, parse_posting  # noqa: E402
from matcher.parse.taxonomy_extras import load_title_index  # noqa: E402

DEFAULT_QUEUE = DEFAULT_TAXONOMY / "review_queue.csv"
DEFAULT_REPORTS = ROOT / "reports" / "taxonomy"
MIN_PARENT_DF = 20  # a parent must be required by this many postings of the window (see the docstring)
SOFT_DEPTH = 10  # parent links searched upwards for a SOFT ancestor
MAX_SURFACE = 80  # characters of a token written to the queue
PARSE_AS_OF = dt.date(2026, 10, 1)  # the parser's clock: dates in a posting never decide its tokens
QUEUE_COLUMNS = ("rank", "token", "key", "postings_30d", "postings_prior_30d", "score", "suggested_parent",
                 "parent_npmi", "note")
_SLUG = re.compile(r"[^a-z0-9]+")


# ─── parsing ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ParsedPosting:
    """What freshness needs from one parsed posting: aggregates and short token surfaces."""

    seen: Optional[dt.date]  # first_seen_at, None when absent or unreadable
    skill_ids: frozenset[str]  # the ids its requirements name (the co-mentions of its tokens)
    unresolved: Mapping[str, str]  # token key -> surface as written
    token_count: int  # requirement tokens, resolved and not


def token_key(surface: str) -> str:
    return normalize_surface(surface)


def _date(value: Any) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def load_env(taxonomy_dir: str | Path) -> dict[str, Any]:
    """Config, graph, resolver and title index over one taxonomy directory."""
    config = load_config()
    parse_config = load_parse_config()
    return {
        "config": config,
        "graph": load_graph(taxonomy_dir),
        "resolver": load_resolver(taxonomy_dir, config),
        "parse_config": parse_config,
        "titles": load_title_index(taxonomy_dir, parse_config=parse_config),
    }


def parse_corpus(postings: Iterable[Mapping[str, Any]], env: Mapping[str, Any]) -> list[ParsedPosting]:
    """Each distinct posting (boilerplate counted once, `corpus_texts`) through the posting parser."""
    prepared = corpus_texts(postings)
    parsed = []
    for posting, text in zip(prepared.kept, prepared.texts):
        title, _, body = text.partition("\n")
        job = parse_posting(title, body, meta=PostingMeta(), as_of=PARSE_AS_OF, resolver=env["resolver"],
                            graph=env["graph"], config=env["config"], parse_config=env["parse_config"],
                            titles=env["titles"])
        unresolved: dict[str, str] = {}
        for surface in job.unresolved_requirement_tokens:
            key = token_key(surface)
            if key and key not in unresolved:
                unresolved[key] = surface
        parsed.append(ParsedPosting(
            seen=_date(posting.get("first_seen_at")),
            skill_ids=frozenset(r.skill_id for r in job.requirements),
            unresolved=dict(sorted(unresolved.items())),
            token_count=job.requirement_token_count,
        ))
    return parsed


# ─── windows and coverage ──────────────────────────────────────────────────────────────────


def _age(posting: ParsedPosting, as_of: dt.date) -> Optional[int]:
    return None if posting.seen is None else (as_of - posting.seen).days


def in_window(posting: ParsedPosting, as_of: dt.date, window_days: int) -> bool:
    age = _age(posting, as_of)
    return age is not None and 0 <= age < window_days


def _in_prior(posting: ParsedPosting, as_of: dt.date, window_days: int) -> bool:
    age = _age(posting, as_of)
    return age is not None and window_days <= age < 2 * window_days


@dataclass(frozen=True)
class Coverage:
    resolved: int
    total: int
    ratio: Optional[float]  # None when there is no requirement token at all


def coverage(parsed: Sequence[ParsedPosting], *, as_of: dt.date, window_days: int) -> Coverage:
    """SPEC §16's metric: % of posting requirement tokens resolved, over the window's postings."""
    window = [p for p in parsed if in_window(p, as_of, window_days)]
    total = sum(p.token_count for p in window)
    resolved = total - sum(len(p.unresolved) for p in window)
    return Coverage(resolved, total, resolved / total if total else None)


def exit_code(result: Coverage, threshold: float) -> int:
    return 0 if result.ratio is not None and result.ratio >= threshold else 1


# ─── ranking and parents ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TokenRank:
    key: str
    surface: str  # the most common spelling (ties: the first in code-point order)
    recent: int  # distinct postings in the window
    earlier: int  # distinct postings in the window before it
    score: float  # recent x (recent + 1) / (earlier + 1)


def rank_tokens(parsed: Sequence[ParsedPosting], *, as_of: dt.date, window_days: int) -> list[TokenRank]:
    recent: Counter[str] = Counter()
    earlier: Counter[str] = Counter()
    spellings: dict[str, Counter[str]] = defaultdict(Counter)
    for posting in parsed:
        if in_window(posting, as_of, window_days):
            recent.update(posting.unresolved.keys())
            for key, surface in posting.unresolved.items():
                spellings[key][surface] += 1
        elif _in_prior(posting, as_of, window_days):
            earlier.update(posting.unresolved.keys())
    ranked = []
    for key in recent:
        exact = Fraction(recent[key] * (recent[key] + 1), earlier[key] + 1)
        surface = min(spellings[key].items(), key=lambda item: (-item[1], item[0]))[0]
        ranked.append((exact, TokenRank(key, surface, recent[key], earlier[key], float(exact))))
    ranked.sort(key=lambda pair: (-pair[0], -pair[1].recent, pair[1].key))
    return [rank for _, rank in ranked]


def infer_parent(key: str, parsed: Sequence[ParsedPosting], *, as_of: dt.date, window_days: int,
                 min_cooccurrence: int, min_df: int, graph: Optional[SkillGraph] = None
                 ) -> Optional[tuple[str, float]]:
    """The id with the highest NPMI against the token over the window's postings, among the ids
    required by at least `min_cooccurrence` of the token's postings and by at least `min_df`
    postings in all; ties by id. With a `graph`, only its nodes outside the SOFT branches qualify
    (`_can_parent`: a soft skill co-occurs with everything and places nothing). None when no id
    qualifies."""
    window = [p for p in parsed if in_window(p, as_of, window_days)]
    df: Counter[str] = Counter()
    co: Counter[str] = Counter()
    token_df = 0
    for posting in window:
        df.update(posting.skill_ids)
        if key in posting.unresolved:
            token_df += 1
            co.update(posting.skill_ids)
    best: Optional[tuple[str, float]] = None
    for skill in sorted(co):
        if co[skill] < min_cooccurrence or df[skill] < min_df:
            continue
        if graph is not None and not _can_parent(graph, skill):
            continue
        value = npmi(len(window), token_df, df[skill], co[skill])
        value = 1.0 if value is None else value  # co-occurs in every posting of the window
        if best is None or value > best[1]:
            best = (skill, value)
    return best


def _can_parent(graph: SkillGraph, skill: str) -> bool:
    """A node of the graph that is not SOFT and sits under no SOFT node (ESCO files transversal
    skills such as "perform services in a flexible manner" under a soft skill)."""
    if skill not in graph.nodes or graph.nodes[skill].kind is SkillKind.SOFT:
        return False
    return all(graph.nodes[up].kind is not SkillKind.SOFT for up in ancestors(graph, skill, SOFT_DEPTH))


@dataclass(frozen=True)
class Candidate:
    rank: TokenRank
    parent: Optional[str]
    npmi: Optional[float]
    parent_without_df_floor: Optional[str]  # what the parent would be with min_df 1 (reported)
    note: str = ""


@dataclass(frozen=True)
class Plan:
    promoted: tuple[Candidate, ...]
    queue: tuple[Candidate, ...]
    ranked: int  # distinct unresolved tokens in the window


def plan(parsed: Sequence[ParsedPosting], graph: SkillGraph, *, as_of: dt.date, settings: Any,
         min_parent_df: int = MIN_PARENT_DF) -> Plan:
    """Promotions and the review queue under the `taxonomy` settings of config/scoring.yaml."""
    window_days = settings.auto_promote_window_days
    ranked = rank_tokens(parsed, as_of=as_of, window_days=window_days)
    promoted, queue = [], []
    for rank in ranked:
        parent = loose = None
        value = None
        if rank.recent > settings.auto_promote_min_postings or len(queue) < settings.review_queue_size:
            found = infer_parent(rank.key, parsed, as_of=as_of, window_days=window_days,
                                 min_cooccurrence=settings.pmi_min_cooccurrence, min_df=min_parent_df,
                                 graph=graph)
            unguarded = infer_parent(rank.key, parsed, as_of=as_of, window_days=window_days,
                                     min_cooccurrence=settings.pmi_min_cooccurrence, min_df=1, graph=graph)
            parent, value = found if found is not None else (None, None)
            loose = unguarded[0] if unguarded is not None else None
        if rank.recent > settings.auto_promote_min_postings:
            if parent is not None:
                promoted.append(Candidate(rank, parent, value, loose))
                continue
            note = "no parent"
        else:
            note = ""
        if len(queue) < settings.review_queue_size:
            queue.append(Candidate(rank, parent, value, loose, note))
    return Plan(tuple(promoted), tuple(queue), len(ranked))


# ─── provisional nodes ─────────────────────────────────────────────────────────────────────


def provisional_id(key: str) -> str:
    return "skill." + (_SLUG.sub("_", key.lower()).strip("_") or "token")


def provisional_node(candidate: Candidate, graph: SkillGraph) -> SkillNode:
    assert candidate.parent is not None
    return SkillNode(
        id=provisional_id(candidate.rank.key),
        label=candidate.rank.surface,
        kind=graph.nodes[candidate.parent].kind,
        parents=(candidate.parent,),
        provisional=True,
        source="provisional",
    )


def cased_key(label: str) -> Optional[tuple[str, str]]:
    """A one-word name with a capital needs its casing (D-26): (key, casing), else None."""
    if len(label.split()) != 1 or label == label.lower():
        return None
    return normalize_surface(label), label


def apply_provisional(taxonomy_dir: str | Path, promoted: Sequence[Candidate]) -> list[SkillNode]:
    """Write the promoted tokens as provisional nodes into the taxonomy (IDF and edges kept; every
    other artifact written back as it was). A token that resolves already, or whose id exists, is
    skipped. Returns the nodes written."""
    root = Path(taxonomy_dir)
    config = load_config()
    graph = load_graph(root)
    resolver = load_resolver(root, config)
    added = []
    for candidate in promoted:
        node = provisional_node(candidate, graph)
        if node.id in graph.nodes or resolver.resolve(node.label).canonical_id is not None:
            continue
        added.append(node)
    if not added:
        return []
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    graph_files = {"nodes.json.gz", "nodes.json", "idf.json", "pmi_edges.json"}
    documents = {name: load_json_artifact(root, name) for name in sorted(manifest["artifacts"])
                 if name not in graph_files}
    settings = dict(documents["resolver.json"])
    cased = dict(settings.get("cased", {}))
    for node in added:
        pair = cased_key(node.label)
        if pair is not None and pair[0] not in settings.get("ambiguous", {}):
            cased[pair[0]] = pair[1]
    settings["cased"] = dict(sorted(cased.items()))
    documents["resolver.json"] = settings
    write_artifacts(
        root,
        [*graph.nodes.values(), *added],
        dict(graph.idf),
        {a: dict(targets) for a, targets in graph.related.items()},
        n_postings=load_json_artifact(root, "idf.json")["n_postings"],
        sources=manifest["sources"],
        compress="nodes.json.gz" in manifest["artifacts"],
        extra=documents,
    )
    return added


# ─── outputs ───────────────────────────────────────────────────────────────────────────────


def _short(surface: str) -> str:
    surface = " ".join(surface.split())
    return surface if len(surface) <= MAX_SURFACE else surface[:MAX_SURFACE].rstrip()


def write_queue(path: str | Path, queue: Sequence[Candidate]) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(QUEUE_COLUMNS)
    for i, c in enumerate(queue, 1):
        writer.writerow([i, _short(c.rank.surface), _short(c.rank.key), c.rank.recent, c.rank.earlier,
                         f"{c.rank.score:.2f}", c.parent or "", "" if c.npmi is None else f"{c.npmi:.3f}", c.note])
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buffer.getvalue().encode("utf-8"))


def _candidate_doc(c: Candidate, graph: SkillGraph) -> dict[str, Any]:
    return {"token": _short(c.rank.surface), "key": _short(c.rank.key), "postings": c.rank.recent,
            "postings_prior": c.rank.earlier, "score": round(c.rank.score, 4), "id": provisional_id(c.rank.key),
            "parent": c.parent, "parent_label": graph.nodes[c.parent].label if c.parent in graph.nodes else None,
            "parent_npmi": None if c.npmi is None else round(c.npmi, 4),
            "parent_without_df_floor": c.parent_without_df_floor}


def report_document(*, as_of: dt.date, settings: Any, result: Coverage, result_plan: Plan, graph: SkillGraph,
                    read: int, distinct: int, window: int, min_parent_df: int, applied: Sequence[SkillNode],
                    queue_path: str) -> dict[str, Any]:
    threshold = settings.coverage_alert_threshold
    return {
        "format": 1,
        "as_of": as_of.isoformat(),
        "window_days": settings.auto_promote_window_days,
        "corpus": {"postings_read": read, "postings_distinct": distinct, "postings_in_window": window},
        "coverage": {"resolved": result.resolved, "total": result.total,
                     "ratio": None if result.ratio is None else round(result.ratio, 6),
                     "threshold": threshold, "alert": exit_code(result, threshold) != 0},
        "unresolved_tokens": result_plan.ranked,
        "promotion_rule": {"min_postings_exclusive": settings.auto_promote_min_postings,
                           "min_cooccurrence": settings.pmi_min_cooccurrence, "min_parent_df": min_parent_df},
        "promoted": [_candidate_doc(c, graph) for c in result_plan.promoted],
        "applied": [node.id for node in applied],
        "review_queue": {"path": queue_path, "size": len(result_plan.queue)},
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--queue", type=Path, default=DEFAULT_QUEUE)
    parser.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)
    parser.add_argument("--as-of", type=dt.date.fromisoformat, default=None,
                        help="the window's last day (default: the corpus's latest first_seen_at)")
    parser.add_argument("--min-parent-df", type=int, default=MIN_PARENT_DF)
    parser.add_argument("--apply", action="store_true",
                        help="write the promoted tokens into the taxonomy as provisional nodes")
    args = parser.parse_args(argv)
    if not args.corpus.exists():
        print(f"refresh_taxonomy: no corpus at {args.corpus}", file=sys.stderr)
        return 2

    postings = read_postings(args.corpus)
    env = load_env(args.taxonomy)
    settings = env["config"].scoring.taxonomy
    parsed = parse_corpus(postings, env)
    dates = [p.seen for p in parsed if p.seen is not None]
    as_of = args.as_of or (max(dates) if dates else dt.date.today())
    window_days = settings.auto_promote_window_days
    result = coverage(parsed, as_of=as_of, window_days=window_days)
    result_plan = plan(parsed, env["graph"], as_of=as_of, settings=settings, min_parent_df=args.min_parent_df)
    applied = apply_provisional(args.taxonomy, result_plan.promoted) if args.apply else []
    write_queue(args.queue, result_plan.queue)
    window = sum(1 for p in parsed if in_window(p, as_of, window_days))
    document = report_document(as_of=as_of, settings=settings, result=result, result_plan=result_plan,
                               graph=env["graph"], read=len(postings), distinct=len(parsed), window=window,
                               min_parent_df=args.min_parent_df, applied=applied, queue_path=args.queue.name)
    args.reports.mkdir(parents=True, exist_ok=True)
    report_path = args.reports / f"{as_of.isoformat()}.json"
    report_path.write_bytes((json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"))

    shown = "n/a" if result.ratio is None else f"{result.ratio:.2%}"
    print(f"coverage {shown} ({result.resolved:,} of {result.total:,} requirement tokens resolved; "
          f"{window:,} postings first seen in the {window_days} days to {as_of}); alert below "
          f"{settings.coverage_alert_threshold:.0%}")
    for c in result_plan.promoted:
        verb = "added" if provisional_id(c.rank.key) in {n.id for n in applied} else "proposed (rerun with --apply)"
        print(f"provisional {verb}: {_short(c.rank.surface)!r} in {c.rank.recent} postings, parent {c.parent}")
    if applied:
        print("now run scripts/compute_idf.py and scripts/compute_pmi.py, then the gate (tests/test_surface_forms.py)")
    print(f"review queue: {len(result_plan.queue)} tokens -> {args.queue}; report -> {report_path}")
    return exit_code(result, settings.coverage_alert_threshold)


if __name__ == "__main__":
    sys.exit(main())
