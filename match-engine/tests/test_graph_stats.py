"""Corpus statistics math: IDF (SPEC §3.1, DECISIONS D-21) and NPMI edges (SPEC §3.2, D-22)."""

from __future__ import annotations

import json
import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from matcher.graph.idf import idf, normalize_idf, raw_idf
from matcher.graph.pmi import build_edges, npmi


def _same(a: dict, b: dict) -> bool:
    """Byte-identical as canonical JSON (the determinism standard, DECISIONS D-10)."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ─── IDF ───────────────────────────────────────────────────────────────────────────────────


def test_raw_idf_matches_spec_formula() -> None:
    assert math.isclose(raw_idf(1000, 9), math.log(100), rel_tol=0.0, abs_tol=1e-12)
    assert math.isclose(raw_idf(50, 0), math.log(50), rel_tol=0.0, abs_tol=1e-12)


@pytest.mark.parametrize("n,df", [(0, 0), (10, -1), (10, 11)])
def test_raw_idf_rejects_impossible_counts(n, df) -> None:
    with pytest.raises(ValueError):
        raw_idf(n, df)


def test_normalize_idf_hand_computed_example() -> None:
    # 4 skills, 100 mentions in total. Lower raw idf = more common. Each value is
    # (mentions of strictly more common skills + half its own mentions) / 100:
    #   a: (0           + 20) / 100 = 0.20
    #   b: (40          + 15) / 100 = 0.55
    #   c: (40 + 30     + 10) / 100 = 0.80
    #   d: (40 + 30 + 20 + 5) / 100 = 0.95
    out = normalize_idf({"a": 40, "b": 30, "c": 20, "d": 10}, 100)
    assert sorted(out) == ["a", "b", "c", "d"]
    for skill, expected in {"a": 0.20, "b": 0.55, "c": 0.80, "d": 0.95}.items():
        assert math.isclose(out[skill], expected, rel_tol=0.0, abs_tol=1e-12), skill


def test_normalize_idf_ties_share_half_their_mass() -> None:
    # b, c and d have equal raw idf; together they hold 60 mentions, half of which counts:
    #   a: (0 + 20) / 100 = 0.20;  b, c, d: (40 + 30) / 100 = 0.70
    out = normalize_idf({"a": 40, "b": 20, "c": 20, "d": 20}, 100)
    for skill, expected in {"a": 0.20, "b": 0.70, "c": 0.70, "d": 0.70}.items():
        assert math.isclose(out[skill], expected, rel_tol=0.0, abs_tol=1e-12), skill


def test_normalize_idf_omits_unseen_skills() -> None:
    out = normalize_idf({"a": 5, "z": 0}, 10)
    assert sorted(out) == ["a"]
    assert math.isclose(out["a"], 0.5, rel_tol=0.0, abs_tol=1e-12)
    assert normalize_idf({}, 10) == {}
    assert normalize_idf({"z": 0}, 10) == {}


def test_normalize_idf_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        normalize_idf({"a": 11}, 10)
    with pytest.raises(ValueError):
        normalize_idf({"a": -1}, 10)
    with pytest.raises(ValueError):
        normalize_idf({"a": 1}, 0)


def test_normalize_idf_ignores_input_order() -> None:
    df = {"a": 40, "b": 30, "c": 20, "d": 10, "e": 30}
    reordered = {k: df[k] for k in ("e", "c", "a", "d", "b")}
    assert _same(normalize_idf(df, 100), normalize_idf(reordered, 100))


@given(st.dictionaries(st.text(alphabet="abcdefgh", min_size=1, max_size=3), st.integers(0, 50), max_size=12))
def test_normalize_idf_properties(df: dict[str, int]) -> None:
    out = normalize_idf(df, 50)
    assert sorted(out) == sorted(k for k, v in df.items() if v > 0)
    assert all(0.0 <= v <= 1.0 for v in out.values())
    assert _same(out, normalize_idf(dict(reversed(list(df.items()))), 50))
    for a in out:  # monotone in raw idf: a rarer skill never weighs less
        for b in out:
            if df[a] > df[b]:
                assert out[a] < out[b]


def test_idf_lookup_uses_unseen_default(small_graph, config) -> None:
    assert math.isclose(idf(small_graph, "skill.kubernetes", config), small_graph.idf["skill.kubernetes"])
    assert idf(small_graph, "skill.communication", config) < idf(small_graph, "skill.kubernetes", config)
    assert math.isclose(idf(small_graph, "skill.svelte", config), config.scoring.idf.unseen_default)
    assert math.isclose(idf(small_graph, "skill.does_not_exist", config), 0.75)


# ─── NPMI ──────────────────────────────────────────────────────────────────────────────────


def test_npmi_closed_form() -> None:
    # p_a = 0.1, p_b = 0.2, p_ab = 0.05: PMI = ln(0.05 / 0.02) = ln 2.5; NPMI = ln 2.5 / -ln 0.05
    assert math.isclose(npmi(100, 10, 20, 5), math.log(2.5) / math.log(20), rel_tol=1e-12)


def test_npmi_independent_pair_is_zero() -> None:
    assert math.isclose(npmi(100, 10, 20, 2), 0.0, rel_tol=0.0, abs_tol=1e-12)  # p_ab == p_a * p_b


def test_npmi_always_together_is_one() -> None:
    assert math.isclose(npmi(100, 10, 10, 10), 1.0, rel_tol=0.0, abs_tol=1e-12)


def test_npmi_is_none_when_never_together_or_always_present() -> None:
    assert npmi(100, 10, 20, 0) is None
    assert npmi(50, 50, 50, 50) is None  # p_ab == 1: -ln p_ab == 0


@pytest.mark.parametrize("n,df_a,df_b,df_ab", [(0, 0, 0, 0), (10, 3, 5, 4), (10, 11, 5, 1), (10, 8, 8, 5)])
def test_npmi_rejects_impossible_counts(n, df_a, df_b, df_ab) -> None:
    # n < 1; co-occurrence above a marginal; a marginal above n; |A ∪ B| = 8 + 8 - 5 > 10
    with pytest.raises(ValueError):
        npmi(n, df_a, df_b, df_ab)


@given(st.integers(1, 200), st.data())
def test_npmi_stays_in_range(n: int, data) -> None:
    df_a = data.draw(st.integers(0, n))
    df_b = data.draw(st.integers(0, n))
    df_ab = data.draw(st.integers(max(0, df_a + df_b - n), min(df_a, df_b)))
    value = npmi(n, df_a, df_b, df_ab)
    if df_ab == 0 or df_ab == n:
        assert value is None
    else:
        assert -1.0 <= value <= 1.0


# ─── build_edges ───────────────────────────────────────────────────────────────────────────

N = 1000
DF = {"a": 50, "b": 50, "c": 50, "x": 4, "y": 4, "h": 100, "s1": 20, "s2": 20, "s3": 20, "s4": 20}
W_AB = math.log(16) / math.log(25)  # npmi(1000, 50, 50, 40) ≈ 0.861
W_AC = math.log(2) / math.log(200)  # npmi(1000, 50, 50, 5) ≈ 0.131
W_H10 = math.log(5) / math.log(100)  # npmi(1000, 100, 20, 10) ≈ 0.349
W_H20 = math.log(10) / math.log(50)  # npmi(1000, 100, 20, 20) ≈ 0.589
HUB = {("h", "s1"): 10, ("h", "s2"): 10, ("h", "s3"): 10, ("h", "s4"): 20}


def _edges(co, **kwargs):
    params = {"threshold": 0.4, "min_cooccurrence": 5, "max_out_degree": 20}
    params.update(kwargs)
    return build_edges(DF, co, N, **params)


def _close(actual: dict[str, dict[str, float]], expected: dict[str, dict[str, float]]) -> None:
    assert sorted(actual) == sorted(expected)
    for node, targets in expected.items():
        assert sorted(actual[node]) == sorted(targets), node
        for target, weight in targets.items():
            assert math.isclose(actual[node][target], weight, rel_tol=1e-12), (node, target)


def test_edges_keep_pairs_above_threshold_in_both_directions() -> None:
    co = {("a", "b"): 40, ("a", "c"): 5}
    _close(_edges(co), {"a": {"b": W_AB}, "b": {"a": W_AB}})  # a-c (0.13) is below 0.4
    _close(_edges(co, threshold=0.1), {"a": {"b": W_AB, "c": W_AC}, "b": {"a": W_AB}, "c": {"a": W_AC}})


def test_edges_need_min_cooccurrence() -> None:
    co = {("x", "y"): 4}  # NPMI 1.0, but only 4 shared postings
    assert _edges(co) == {}
    _close(_edges(co, min_cooccurrence=4), {"x": {"y": 1.0}, "y": {"x": 1.0}})


def test_out_degree_cap_keeps_heaviest_and_breaks_ties_by_id() -> None:
    # h -> s4 is strongest; s1, s2, s3 tie, so the cap of 2 keeps the smallest id (s1).
    edges = _edges(HUB, threshold=0.0, min_cooccurrence=1, max_out_degree=2)
    _close(
        edges,
        {
            "h": {"s1": W_H10, "s4": W_H20},
            "s1": {"h": W_H10},
            "s2": {"h": W_H10},  # the cap is per node: the spokes keep their edge to the hub
            "s3": {"h": W_H10},
            "s4": {"h": W_H20},
        },
    )


def test_curated_edges_win_both_ways_and_survive_the_cap() -> None:
    curated = {"h": {"s2": 0.9}, "x": {"y": 0.5}}
    edges = _edges(HUB, threshold=0.0, min_cooccurrence=1, max_out_degree=2, curated=curated)
    _close(
        edges,
        {
            "h": {"s1": W_H10, "s2": 0.9, "s4": W_H20},  # three edges despite the cap of 2
            "s1": {"h": W_H10},
            "s2": {"h": 0.9},  # the curated weight replaces the mined one in this direction too
            "s3": {"h": W_H10},
            "s4": {"h": W_H20},
            "x": {"y": 0.5},  # never co-occurred: added from the curated table alone
            "y": {"x": 0.5},
        },
    )
    # A curated weight wins even when it is lower than the mined one.
    _close(_edges({("a", "b"): 40}, curated={"b": {"a": 0.3}}), {"a": {"b": 0.3}, "b": {"a": 0.3}})


def test_self_pairs_are_omitted() -> None:
    edges = _edges({("a", "a"): 50, ("a", "b"): 40}, curated={"c": {"c": 1.0}})
    _close(edges, {"a": {"b": W_AB}, "b": {"a": W_AB}})


def test_edges_ignore_input_order_and_pair_orientation() -> None:
    forward = {("a", "b"): 40, ("a", "c"): 5, ("h", "s1"): 10, ("h", "s4"): 20}
    backward = {("s4", "h"): 20, ("s1", "h"): 10, ("c", "a"): 5, ("b", "a"): 40}
    params = {"threshold": 0.0, "min_cooccurrence": 1, "max_out_degree": 1}
    assert _same(_edges(forward, **params), _edges(backward, **params))


@pytest.mark.parametrize(
    "co,curated",
    [
        ({("a", "b"): 40, ("b", "a"): 39}, {}),  # one pair, two different counts
        ({("a", "zz"): 40}, {}),  # no document frequency for zz
        ({}, {"a": {"b": 1.5}}),  # curated weight outside [0, 1]
        ({}, {"a": {"b": 0.5}, "b": {"a": 0.6}}),  # curated weights disagree
    ],
)
def test_edges_reject_inconsistent_input(co, curated) -> None:
    with pytest.raises(ValueError):
        _edges(co, curated=curated)
