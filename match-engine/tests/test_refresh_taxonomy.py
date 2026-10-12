"""Taxonomy freshness (Task 7.2 part B; SPEC §16): scripts/refresh_taxonomy.py.

The counting functions are checked on hand-built `ParsedPosting` records, so every number can be
worked out by hand; one test runs the real parser over invented postings, and one runs the script
end to end on an invented corpus file. Nothing here is scraped text (DECISIONS D-04).
"""

from __future__ import annotations

import csv
import datetime as dt
import importlib
import json
import sys
from pathlib import Path

import pytest

from matcher.config import load_config
from matcher.graph.loader import load_graph, load_resolver, load_resolver_settings, write_artifacts
from matcher.models import SkillKind
from tests.fixtures.build_graph_small import EDGES, NODES, SOURCES

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TAXONOMY = ROOT / "data" / "taxonomy"
AS_OF = dt.date(2026, 5, 31)


@pytest.fixture(scope="module")
def rt():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module("refresh_taxonomy")


def _p(rt, days_old, ids=(), unresolved=(), tokens=None):
    """A parsed posting first seen `days_old` days before AS_OF (None: undated)."""
    seen = None if days_old is None else AS_OF - dt.timedelta(days=days_old)
    found = {rt.token_key(s): s for s in unresolved}
    count = len(set(ids)) + len(found) if tokens is None else tokens
    return rt.ParsedPosting(seen=seen, skill_ids=frozenset(ids), unresolved=found, token_count=count)


# ─── coverage ──────────────────────────────────────────────────────────────────────────────


def test_coverage_is_resolved_requirement_tokens_over_all_of_them(rt) -> None:
    parsed = [
        _p(rt, 1, ids=("skill.python", "skill.sql"), unresolved=("Zorblax",)),  # 2 of 3
        _p(rt, 2, ids=("skill.python",)),  # 1 of 1
        _p(rt, 3, unresolved=("Quuxdb", "Flimflam")),  # 0 of 2
        _p(rt, 400, ids=("skill.java",)),  # outside the window: not counted
    ]
    result = rt.coverage(parsed, as_of=AS_OF, window_days=30)
    assert (result.resolved, result.total) == (3, 6)
    assert result.ratio == pytest.approx(0.5)


def test_coverage_of_no_tokens_is_undefined(rt) -> None:
    result = rt.coverage([_p(rt, 1)], as_of=AS_OF, window_days=30)
    assert (result.resolved, result.total, result.ratio) == (0, 0, None)


# ─── ranking ───────────────────────────────────────────────────────────────────────────────


def test_ranking_is_frequency_times_growth_over_the_previous_window(rt) -> None:
    # "Steady": 6 now, 6 in the previous window (growth 1); "Rising": 4 now, 0 before (growth 5).
    parsed = ([_p(rt, 1, unresolved=("Steady",)) for _ in range(6)]
              + [_p(rt, 40, unresolved=("Steady",)) for _ in range(6)]
              + [_p(rt, 2, unresolved=("Rising",)) for _ in range(4)])
    ranked = rt.rank_tokens(parsed, as_of=AS_OF, window_days=30)
    assert [(r.surface, r.recent, r.earlier) for r in ranked] == [("Rising", 4, 0), ("Steady", 6, 6)]
    assert ranked[0].score == pytest.approx(4 * 5 / 1)
    assert ranked[1].score == pytest.approx(6 * 7 / 7)


def test_window_edges_and_undated_postings(rt) -> None:
    parsed = [
        _p(rt, 0, unresolved=("Edge",)),  # as_of itself: in the window
        _p(rt, 29, unresolved=("Edge",)),  # last day of the window
        _p(rt, 30, unresolved=("Edge",)),  # first day of the previous window
        _p(rt, 59, unresolved=("Edge",)),  # last day of the previous window
        _p(rt, 60, unresolved=("Edge",)),  # older: neither
        _p(rt, -1, unresolved=("Edge",)),  # after as_of: neither
        _p(rt, None, unresolved=("Edge",)),  # undated: neither
    ]
    (edge,) = rt.rank_tokens(parsed, as_of=AS_OF, window_days=30)
    assert (edge.recent, edge.earlier) == (2, 2)


def test_ties_rank_by_key_and_surface_is_the_most_common_spelling(rt) -> None:
    parsed = [_p(rt, 1, unresolved=("beta",)), _p(rt, 1, unresolved=("Alpha",)),
              _p(rt, 1, unresolved=("BETA",)), _p(rt, 1, unresolved=("ALPHA",)), _p(rt, 2, unresolved=("Alpha",))]
    ranked = rt.rank_tokens(parsed, as_of=AS_OF, window_days=30)
    assert [(r.key, r.surface, r.recent) for r in ranked] == [("alpha", "Alpha", 3), ("beta", "BETA", 2)]


# ─── parent inference ──────────────────────────────────────────────────────────────────────


def test_parent_is_the_co_mention_with_the_highest_npmi(rt) -> None:
    # 100 postings. Zorblax in 10: 9 with python (in 30), 6 with kubernetes (in 6), 8 with sql (in 80).
    parsed = []
    for i in range(100):
        ids = set()
        if i < 30:
            ids.add("skill.python")
        if i < 80:
            ids.add("skill.sql")
        unresolved = ("Zorblax",) if i < 10 else ()
        parsed.append(_p(rt, 1, ids=ids, unresolved=unresolved))
    for i in range(6):  # kubernetes in postings 4-9 only: every one has Zorblax
        old = parsed[4 + i]
        parsed[4 + i] = rt.ParsedPosting(seen=old.seen, skill_ids=old.skill_ids | {"skill.kubernetes"},
                                         unresolved=old.unresolved, token_count=old.token_count + 1)
    found = rt.infer_parent("zorblax", parsed, as_of=AS_OF, window_days=30, min_cooccurrence=5, min_df=1)
    assert found is not None and found[0] == "skill.kubernetes"
    # with a minimum df of 20 the rare kubernetes (df 6) cannot be the parent
    guarded = rt.infer_parent("zorblax", parsed, as_of=AS_OF, window_days=30, min_cooccurrence=5, min_df=20)
    assert guarded is not None and guarded[0] == "skill.python"


def test_a_soft_skill_one_under_it_or_an_unknown_id_is_never_the_parent(rt) -> None:
    graph = load_graph(TAXONOMY)
    flexible = "esco.perform_services_in_a_flexible_manner"  # METHODOLOGY under adaptability (SOFT)
    assert graph.nodes["skill.communication"].kind is SkillKind.SOFT
    assert graph.nodes[flexible].kind is not SkillKind.SOFT
    assert graph.nodes["skill.adaptability"].kind is SkillKind.SOFT
    # Zorblax in 10 of 100: every one also requires communication and the flexible-services node
    # (each in 12) and the gone id (in 10); python (in 40) is with it in 6.
    parsed = []
    for i in range(100):
        ids = set()
        if i < 12:
            ids.update(("skill.communication", flexible))
        if i < 10:
            ids.add("skill.no_longer_there")
        if 4 <= i < 44:
            ids.add("skill.python")
        parsed.append(_p(rt, 1, ids=ids, unresolved=("Zorblax",) if i < 10 else ()))
    found = rt.infer_parent("zorblax", parsed, as_of=AS_OF, window_days=30, min_cooccurrence=5, min_df=1,
                            graph=graph)
    assert found is not None and found[0] == "skill.python"


def test_no_parent_below_the_co_occurrence_floor(rt) -> None:
    parsed = [_p(rt, 1, ids=("skill.python",), unresolved=("Zorblax",)) for _ in range(4)]
    parsed += [_p(rt, 1, ids=("skill.python",)) for _ in range(30)]
    assert rt.infer_parent("zorblax", parsed, as_of=AS_OF, window_days=30, min_cooccurrence=5, min_df=1) is None


# ─── promotion and the review queue ────────────────────────────────────────────────────────


def _settings(**over):
    taxonomy = load_config().scoring.taxonomy
    return taxonomy.model_copy(update=over)


def test_more_than_the_minimum_postings_promotes_exactly_at_it_queues(rt) -> None:
    graph = load_graph(TAXONOMY)
    parsed = [_p(rt, 1, ids=("skill.python",), unresolved=("Promoteme",)) for _ in range(51)]
    parsed += [_p(rt, 1, ids=("skill.python",), unresolved=("Stayqueued",)) for _ in range(50)]
    parsed += [_p(rt, 1, ids=("skill.python", "skill.sql")) for _ in range(100)]
    plan = rt.plan(parsed, graph, as_of=AS_OF, settings=_settings(), min_parent_df=1)
    assert [c.rank.surface for c in plan.promoted] == ["Promoteme"]
    assert plan.promoted[0].parent == "skill.python"
    assert [c.rank.surface for c in plan.queue] == ["Stayqueued"]


def test_a_candidate_with_no_parent_is_queued_not_promoted(rt) -> None:
    graph = load_graph(TAXONOMY)
    parsed = [_p(rt, 1, unresolved=("Orphanword",)) for _ in range(60)] + [_p(rt, 1, ids=("skill.sql",))]
    plan = rt.plan(parsed, graph, as_of=AS_OF, settings=_settings(), min_parent_df=1)
    assert plan.promoted == ()
    assert [(c.rank.surface, c.note) for c in plan.queue] == [("Orphanword", "no parent")]


def test_the_queue_holds_the_top_review_queue_size(rt) -> None:
    graph = load_graph(TAXONOMY)
    parsed = [_p(rt, 1, unresolved=(f"Tok{chr(97 + i)}{chr(97 + j)}",)) for i in range(8) for j in range(8)
              for _ in range(1 + (i * 8 + j) % 3)]
    plan = rt.plan(parsed, graph, as_of=AS_OF, settings=_settings(review_queue_size=5), min_parent_df=1)
    assert len(plan.queue) == 5
    assert all(c.rank.recent == 3 for c in plan.queue)


def test_provisional_node_shape(rt) -> None:
    graph = load_graph(TAXONOMY)
    parsed = [_p(rt, 1, ids=("skill.python",), unresolved=("Zorblax",)) for _ in range(51)]
    parsed += [_p(rt, 1, ids=("skill.sql",)) for _ in range(10)]
    (candidate,) = rt.plan(parsed, graph, as_of=AS_OF, settings=_settings(), min_parent_df=1).promoted
    node = rt.provisional_node(candidate, graph)
    assert node.id == "skill.zorblax"
    assert (node.label, node.parents, node.provisional, node.source) == ("Zorblax", ("skill.python",), True,
                                                                          "provisional")
    assert node.kind is graph.nodes["skill.python"].kind
    assert rt.cased_key(node.label) == ("zorblax", "Zorblax")
    assert rt.cased_key("distributed ledgers") is None
    assert rt.cased_key("ledger") is None


def test_queue_csv_has_short_phrases_and_counts_only(rt, tmp_path: Path) -> None:
    graph = load_graph(TAXONOMY)
    long_surface = "very long phrase " * 10
    parsed = [_p(rt, 1, ids=("skill.python",), unresolved=("Zorblax", long_surface)) for _ in range(3)]
    plan = rt.plan(parsed, graph, as_of=AS_OF, settings=_settings(), min_parent_df=1)
    path = tmp_path / "review_queue.csv"
    rt.write_queue(path, plan.queue)
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == list(rt.QUEUE_COLUMNS)
    assert all(len(row["token"]) <= rt.MAX_SURFACE for row in rows)
    assert {row["postings_30d"] for row in rows} == {"3"}
    first = path.read_bytes()
    rt.write_queue(path, plan.queue)
    assert path.read_bytes() == first


# ─── the real parser and the script ────────────────────────────────────────────────────────


_POSTING = ("Requirements:\n- Experience with Zorblax and Python.\n- Proficiency in SQL.\n"
            "- Familiarity with Quuxdb.\n")


def test_parse_corpus_reads_requirement_tokens_with_the_posting_parser(rt) -> None:
    postings = [{"id": 1, "title": "Software Developer", "description": _POSTING, "first_seen_at": "2026-05-20T10:00:00"}]
    (parsed,) = rt.parse_corpus(postings, rt.load_env(TAXONOMY))
    assert parsed.seen == dt.date(2026, 5, 20)
    assert {"skill.python", "skill.sql"} <= parsed.skill_ids
    assert set(parsed.unresolved) == {"zorblax", "quuxdb"}
    assert parsed.token_count == len(parsed.skill_ids) + 2


def test_parse_corpus_counts_an_exact_duplicate_once(rt) -> None:
    postings = [{"id": i, "title": "Software Developer", "description": _POSTING, "first_seen_at": "2026-05-20"}
                for i in (1, 2)]
    assert len(rt.parse_corpus(postings, rt.load_env(TAXONOMY))) == 1


def _write_corpus(path: Path, n: int) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for i in range(n):
            text = _POSTING + f"- Ticket number {i} for uniqueness.\n"
            fh.write(json.dumps({"id": i, "title": "Software Developer", "description": text,
                                 "first_seen_at": "2026-05-20"}) + "\n")


def test_main_writes_the_queue_and_report_and_fails_below_the_threshold(rt, tmp_path: Path, capsys) -> None:
    corpus = tmp_path / "postings.jsonl"
    _write_corpus(corpus, 3)
    before = {p.name: p.read_bytes() for p in TAXONOMY.iterdir() if p.is_file()}
    code = rt.main(["--corpus", str(corpus), "--taxonomy", str(TAXONOMY), "--queue", str(tmp_path / "q.csv"),
                    "--reports", str(tmp_path / "reports")])
    assert code == 1  # Zorblax and Quuxdb unresolved: coverage 2/4 or so, under 0.92
    report = json.loads((tmp_path / "reports" / "2026-05-20.json").read_text(encoding="utf-8"))
    assert report["coverage"]["ratio"] < 0.92 and report["coverage"]["alert"] is True
    assert report["as_of"] == "2026-05-20" and report["promoted"] == []
    assert "coverage" in capsys.readouterr().out
    rows = list(csv.DictReader((tmp_path / "q.csv").open(encoding="utf-8", newline="")))
    assert {row["token"] for row in rows} == {"Zorblax", "Quuxdb"}
    assert {p.name: p.read_bytes() for p in TAXONOMY.iterdir() if p.is_file()} == before  # no --apply: untouched


def test_exit_code_at_the_threshold_is_zero(rt) -> None:
    assert rt.exit_code(rt.Coverage(92, 100, 0.92), 0.92) == 0
    assert rt.exit_code(rt.Coverage(91, 100, 0.91), 0.92) == 1
    assert rt.exit_code(rt.Coverage(0, 0, None), 0.92) == 1


def test_apply_writes_provisional_nodes_the_resolver_then_reads(rt, tmp_path: Path) -> None:
    resolver_doc = {"format": 1, "ambiguous": {}}
    write_artifacts(tmp_path, NODES, {}, EDGES, n_postings=0, sources=SOURCES, extra={"resolver.json": resolver_doc})
    graph = load_graph(tmp_path)
    parsed = [_p(rt, 1, ids=("skill.python",), unresolved=("Zorblax",)) for _ in range(51)]
    parsed += [_p(rt, 1, ids=("skill.sql",)) for _ in range(10)]
    plan = rt.plan(parsed, graph, as_of=AS_OF, settings=_settings(), min_parent_df=1)
    written = rt.apply_provisional(tmp_path, plan.promoted)
    assert [node.id for node in written] == ["skill.zorblax"]
    resolver = load_resolver(tmp_path, load_config())
    assert resolver.resolve("Zorblax").canonical_id == "skill.zorblax"
    assert load_resolver_settings(tmp_path)["cased"] == {"zorblax": "Zorblax"}  # one word, capitalised (D-26)
    node = load_graph(tmp_path).nodes["skill.zorblax"]
    assert node.provisional and node.kind is SkillKind.TECHNOLOGY
    # a second apply adds nothing (the token now resolves; the id exists)
    assert rt.apply_provisional(tmp_path, plan.promoted) == []
