"""Constrained weight tuning (Task 8.1, EVAL section 5, SPEC section 10 sensitivity).

Toy evaluators keep these fast: they score pairs from fixed per-dimension values, so every expected
number is arithmetic on the test's own inputs. `test_engine_evaluator_*` touches the real engine once.
"""

from __future__ import annotations

import hashlib
import math
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pytest

from eval.metrics import PairScore
from eval.suites import Guardrail
from eval.tune import constraints, proposal, search, sensitivity

DIMS = constraints.DIMENSIONS
CURRENT = {"required_skills": 0.35, "preferred_skills": 0.10, "experience": 0.15, "seniority": 0.10,
           "domain": 0.10, "education": 0.10, "tools": 0.10}
ROOT = Path(__file__).resolve().parents[2]


def _decimal_sum(weights: dict[str, float]) -> Decimal:
    return sum((Decimal(repr(w)) for w in weights.values()), Decimal(0))


# ─── simplex normalisation ─────────────────────────────────────────────────────────────────


def test_dimensions_are_the_seven_weights() -> None:
    assert DIMS == tuple(CURRENT)


@pytest.mark.parametrize("raw", [
    {d: 1.0 for d in DIMS},
    {d: float(i + 1) for i, d in enumerate(DIMS)},
    {d: 1 / 3 for d in DIMS},
    {"required_skills": 7.0, **{d: 0.9 for d in DIMS[1:]}},
])
def test_normalise_sums_to_exactly_one(raw: dict[str, float]) -> None:
    out = constraints.normalise(raw)
    assert set(out) == set(DIMS)
    assert _decimal_sum(out) == Decimal(1)  # exactly, as decimals
    assert math.fsum(out.values()) == pytest.approx(1.0, abs=1e-12)
    for w in out.values():
        assert round(w, constraints.DECIMALS) == w


def test_normalise_puts_the_rounding_residual_on_the_largest_weight() -> None:
    out = constraints.normalise({d: 1 / 3 for d in DIMS})
    # 1/7 = 0.142857... rounds to 0.1429; six of them are 0.8574, the first absorbs the rest.
    assert out["required_skills"] == 0.1426
    assert all(out[d] == 0.1429 for d in DIMS[1:])


def test_normalise_keeps_a_valid_vector_unchanged() -> None:
    assert constraints.normalise(CURRENT) == CURRENT


def test_normalise_rejects_non_positive_and_missing() -> None:
    with pytest.raises(ValueError):
        constraints.normalise({**CURRENT, "tools": 0.0})
    with pytest.raises(ValueError):
        constraints.normalise({d: 0.2 for d in DIMS[:-1]})


@pytest.mark.parametrize("seed", range(20))
def test_from_unit_lands_on_the_simplex_inside_the_lower_bound(seed: int) -> None:
    import random

    rng = random.Random(seed)
    u = [rng.uniform(1e-6, 1.0) for _ in DIMS]
    w = constraints.from_unit(u)
    assert _decimal_sum(w) == Decimal(1)
    assert min(w.values()) >= constraints.WEIGHT_MIN


def test_bounds_are_eval_5_1() -> None:
    assert (constraints.WEIGHT_MIN, constraints.WEIGHT_MAX) == (0.03, 0.45)
    assert constraints.FAIRNESS_MIN == 0.80
    assert constraints.ABSTENTION_MAX == 0.15


def test_bound_violations_name_the_dimension() -> None:
    assert constraints.bound_violations(CURRENT) == []
    bad = {**CURRENT, "required_skills": 0.46, "tools": 0.02}
    found = constraints.bound_violations(bad)
    assert any("required_skills" in v for v in found) and any("tools" in v for v in found)


# ─── feasibility ───────────────────────────────────────────────────────────────────────────


def _fair(passed: Optional[bool], value: Optional[float] = None) -> Guardrail:
    return Guardrail("Four-fifths (min ratio)", passed, value, ">= 0.80", "toy", "fairness")


def test_feasible_when_every_constraint_holds() -> None:
    f = constraints.assess(CURRENT, abstention_rate=0.10, fairness=_fair(True, 0.9))
    assert f.feasible and f.fairness == "pass" and f.reasons == ()


def test_abstention_above_15_percent_is_infeasible() -> None:
    assert constraints.assess(CURRENT, abstention_rate=0.15, fairness=_fair(True, 0.9)).feasible
    f = constraints.assess(CURRENT, abstention_rate=0.151, fairness=_fair(True, 0.9))
    assert not f.feasible and any("abstention" in r for r in f.reasons)


def test_fairness_fail_is_infeasible() -> None:
    f = constraints.assess(CURRENT, abstention_rate=0.0, fairness=_fair(False, 0.79))
    assert not f.feasible and f.fairness == "fail"


def test_fairness_not_assessed_is_feasible_but_flagged() -> None:
    """D-119: not assessable -> feasible, and the proposal says it cannot be adopted."""
    f = constraints.assess(CURRENT, abstention_rate=0.0, fairness=_fair(None))
    assert f.feasible and f.fairness == "not assessed"


def test_out_of_bounds_is_infeasible() -> None:
    f = constraints.assess({**CURRENT, "required_skills": 0.5}, abstention_rate=0.0, fairness=_fair(True, 1.0))
    assert not f.feasible and any("required_skills" in r for r in f.reasons)


# ─── a toy evaluator ───────────────────────────────────────────────────────────────────────

# Four postings, four pairs each; each pair has fixed per-dimension values in [0, 1].
_TOY = []
for _p in range(4):
    for _i in range(4):
        _vals = {d: ((_p * 7 + _i * 3 + k * 5) % 11) / 10 for k, d in enumerate(DIMS)}
        # the relevance follows required skills and experience, so weights matter
        _rel = min(3, int(4 * (0.6 * _vals["required_skills"] + 0.4 * _vals["experience"])))
        _TOY.append((f"post-{_p}", f"pair-{_p}-{_i}", _vals, _rel))


def _band(score: float) -> int:
    return 0 if score < 0.4 else 1 if score < 0.55 else 2 if score < 0.7 else 3


class ToyEvaluator:
    split = "synthetic"
    synthetic_only = True

    def __init__(self, *, abstain_when=None, fairness=None) -> None:
        self.calls = 0
        self.abstain_when = abstain_when or (lambda w: False)
        self.fairness = fairness or (lambda w: _fair(None))

    def pairs(self, weights: dict[str, float]) -> list[PairScore]:
        self.calls += 1
        abstain = self.abstain_when(weights)
        out = []
        for posting, pair, vals, rel in _TOY:
            s = math.fsum(weights[d] * vals[d] for d in DIMS)
            out.append(PairScore(posting_id=posting, pair_id=pair, score=None if abstain else s, relevance=rel,
                                 advanced=rel >= 2, band_pred=None if abstain else _band(s), band_true=rel))
        return out

    def evaluate(self, weights: dict[str, float]) -> search.Evaluation:
        evaluation = search.evaluation_of(self.pairs(weights))
        return search.Evaluation(evaluation.pairs, evaluation.ndcg, evaluation.abstention_rate,
                                 self.fairness(weights))


# ─── search ────────────────────────────────────────────────────────────────────────────────


def test_search_is_seeded_and_deterministic() -> None:
    a = search.run_search(ToyEvaluator(), CURRENT, budget=12, seed=3)
    b = search.run_search(ToyEvaluator(), CURRENT, budget=12, seed=3)
    assert [t.weights for t in a.trials] == [t.weights for t in b.trials]
    assert a.best is not None and a.best.weights == b.best.weights
    assert len(a.trials) == 12


def test_every_trial_vector_is_on_the_simplex() -> None:
    result = search.run_search(ToyEvaluator(), CURRENT, budget=15, seed=0)
    for trial in result.trials:
        assert _decimal_sum(trial.weights) == Decimal(1)


def test_best_is_feasible_and_never_worse_than_any_feasible_trial() -> None:
    result = search.run_search(ToyEvaluator(), CURRENT, budget=15, seed=0)
    feasible = [t for t in result.trials if t.feasibility.feasible]
    assert result.best in feasible
    assert result.best.objective == max(t.objective for t in feasible)


def test_abstention_constraint_excludes_trials() -> None:
    # any vector with required_skills above 0.2 abstains on everything: infeasible
    evaluator = ToyEvaluator(abstain_when=lambda w: w["required_skills"] > 0.2)
    result = search.run_search(evaluator, CURRENT, budget=20, seed=1)
    assert not result.baseline_feasibility.feasible  # an abstaining incumbent is reported, not terminal
    for trial in result.trials:
        if trial.weights["required_skills"] > 0.2:
            assert not trial.feasibility.feasible
    assert result.best is None or result.best.weights["required_skills"] <= 0.2


def test_fairness_fail_trials_are_infeasible() -> None:
    evaluator = ToyEvaluator(fairness=lambda w: _fair(False, 0.7) if w["tools"] > 0.12 else _fair(True, 0.95))
    result = search.run_search(evaluator, CURRENT, budget=20, seed=2)
    scored = [t for t in result.trials if t.objective is not None]
    assert scored
    for trial in scored:
        assert trial.feasibility.feasible == (trial.weights["tools"] <= 0.12)
    assert result.fairness_failures == sum(1 for t in result.trials if t.weights["tools"] > 0.12)


def test_fairness_fail_on_current_weights_is_terminal() -> None:
    evaluator = ToyEvaluator(fairness=lambda w: _fair(False, 0.7))
    with pytest.raises(constraints.FairnessFailure):
        search.run_search(evaluator, CURRENT, budget=5, seed=0)
    assert evaluator.calls == 1  # stopped before any trial ran


def test_out_of_bounds_trials_are_never_scored(monkeypatch: pytest.MonkeyPatch) -> None:
    evaluator = ToyEvaluator()
    calls = {"n": 0}
    real = constraints.from_unit

    def sometimes_out(u):
        calls["n"] += 1
        w = real(u)
        if calls["n"] % 2 == 0:  # every second sample breaks the upper bound
            return constraints.normalise({**{d: 0.04 for d in DIMS}, "required_skills": 0.9})
        return w

    monkeypatch.setattr(constraints, "from_unit", sometimes_out)
    result = search.run_search(evaluator, CURRENT, budget=10, seed=0)
    rejected = [t for t in result.trials if constraints.bound_violations(t.weights)]
    assert rejected and all(not t.feasibility.feasible and t.objective is None for t in rejected)
    assert evaluator.calls == 1 + 10 - len(rejected)  # baseline + scored trials only


def test_unknown_objective_is_refused() -> None:
    with pytest.raises(SystemExit):
        search.parse_args(["--objective", "auc"])


def test_dev_split_without_labels_falls_back_to_synthetic(tmp_path: Path) -> None:
    split, note = search.choose_split("dev", dev_labels=tmp_path / "pairs.jsonl")
    assert split == "synthetic" and "no dev labels" in note


# ─── accept rules (EVAL 5.3) ───────────────────────────────────────────────────────────────


def _delta(point: float, low: float, high: float) -> dict:
    return {"point": point, "low": low, "high": high}


def test_accept_needs_ci_excluding_zero_and_more_than_one_point() -> None:
    real = proposal.accept_rules(_delta(0.02, 0.005, 0.035), holdout_gain=None, synthetic_only=False,
                                 fairness_assessed=True)
    assert real.improvement_real
    assert not proposal.accept_rules(_delta(0.008, 0.002, 0.014), holdout_gain=None, synthetic_only=False,
                                     fairness_assessed=True).improvement_real
    assert not proposal.accept_rules(_delta(0.03, -0.001, 0.06), holdout_gain=None, synthetic_only=False,
                                     fairness_assessed=True).improvement_real
    assert not proposal.accept_rules(_delta(0.01, 0.001, 0.02), holdout_gain=None, synthetic_only=False,
                                     fairness_assessed=True).improvement_real  # must EXCEED +0.01


def test_overfit_suspected_when_dev_gain_exceeds_twice_holdout_gain() -> None:
    d = proposal.accept_rules(_delta(0.04, 0.02, 0.06), holdout_gain=0.01, synthetic_only=False,
                              fairness_assessed=True)
    assert d.overfit_suspected and d.verdict == "REJECT" and "OVERFIT_SUSPECTED" in d.text()
    ok = proposal.accept_rules(_delta(0.04, 0.02, 0.06), holdout_gain=0.02, synthetic_only=False,
                               fairness_assessed=True)
    assert not ok.overfit_suspected and ok.verdict == "READY FOR HUMAN REVIEW"


def test_overfit_not_assessed_without_holdout() -> None:
    d = proposal.accept_rules(_delta(0.04, 0.02, 0.06), holdout_gain=None, synthetic_only=False,
                              fairness_assessed=True)
    assert d.overfit_suspected is None and "not assessed" in d.text()


def test_synthetic_or_unassessed_fairness_is_never_adoptable() -> None:
    d = proposal.accept_rules(_delta(0.04, 0.02, 0.06), holdout_gain=0.03, synthetic_only=True,
                              fairness_assessed=False)
    assert d.verdict == "NOT ADOPTABLE"
    assert proposal.SYNTHETIC_BANNER in d.text() and proposal.FAIRNESS_BANNER in d.text()


# ─── proposal output ───────────────────────────────────────────────────────────────────────


def _config_copy(tmp_path: Path) -> Path:
    folder = tmp_path / "config"
    folder.mkdir()
    (folder / "weights.yaml").write_bytes((ROOT / "config" / "weights.yaml").read_bytes())
    return folder


def _write(tmp_path: Path, evaluator=None, *, budget: int = 8):
    evaluator = evaluator or ToyEvaluator()
    result = search.run_search(evaluator, CURRENT, budget=budget, seed=0)
    config_dir = _config_copy(tmp_path)
    rows = sensitivity.analyse(evaluator.pairs, result.proposed_weights)
    folder = proposal.write_proposal(result, evaluator, out_root=tmp_path / "proposals", day=date(2026, 10, 13),
                                     config_dir=config_dir, sensitivity_rows=rows, n_resamples=50,
                                     wall_seconds=1.5)
    return folder, config_dir, result


def test_proposal_writes_the_four_files_and_never_touches_config(tmp_path: Path) -> None:
    real_weights = (ROOT / "config" / "weights.yaml").read_bytes()
    folder, config_dir, _ = _write(tmp_path)
    copy_before = real_weights
    assert folder == tmp_path / "proposals" / "2026-10-13-tune"
    names = {p.name for p in folder.iterdir()}
    assert {"weights.diff", "rationale.md", "report.md", "sensitivity.md"} <= names
    assert (config_dir / "weights.yaml").read_bytes() == copy_before
    assert (ROOT / "config" / "weights.yaml").read_bytes() == real_weights
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert all(folder in p.parents or p == config_dir / "weights.yaml" for p in written)
    assert hashlib.sha256((config_dir / "weights.yaml").read_bytes()).hexdigest() == \
        hashlib.sha256(real_weights).hexdigest()


def test_proposal_refuses_an_output_folder_inside_config(tmp_path: Path) -> None:
    evaluator = ToyEvaluator()
    result = search.run_search(evaluator, CURRENT, budget=3, seed=0)
    config_dir = _config_copy(tmp_path)
    with pytest.raises(ValueError):
        proposal.write_proposal(result, evaluator, out_root=config_dir / "proposals", day=date(2026, 10, 13),
                                config_dir=config_dir, sensitivity_rows=[], n_resamples=10, wall_seconds=0.0)
    with pytest.raises(ValueError):
        proposal.write_proposal(result, evaluator, out_root=config_dir, day=date(2026, 10, 13),
                                config_dir=config_dir, sensitivity_rows=[], n_resamples=10, wall_seconds=0.0)


def test_weights_diff_shows_current_against_proposed(tmp_path: Path) -> None:
    folder, _, result = _write(tmp_path)
    diff = (folder / "weights.diff").read_text(encoding="utf-8")
    assert "--- a/match-engine/config/weights.yaml" in diff and "+++ b/match-engine/config/weights.yaml" in diff
    for dim, value in result.proposed_weights.items():
        if value != CURRENT[dim]:
            assert f"-  {dim}: " in diff
            assert f"+  {dim}: {value:.4f}" in diff


def test_proposed_yaml_keeps_comments_and_sums_to_one(tmp_path: Path) -> None:
    text = (ROOT / "config" / "weights.yaml").read_text(encoding="utf-8")
    proposed = {**CURRENT, "required_skills": 0.3, "tools": 0.15}
    out = proposal.proposed_yaml(text, proposed)
    import yaml

    loaded = yaml.safe_load(out)["dimensions"]
    assert loaded == proposed
    assert "# §5.1" in out and out.count("\n") == text.count("\n")


def test_synthetic_proposal_is_marked_and_says_fairness_not_assessed(tmp_path: Path) -> None:
    folder, _, _ = _write(tmp_path)
    rationale = (folder / "rationale.md").read_text(encoding="utf-8")
    report = (folder / "report.md").read_text(encoding="utf-8")
    for text in (rationale, report):
        assert proposal.SYNTHETIC_BANNER in text
        assert proposal.FAIRNESS_BANNER in text
    assert "## Dimensions moved" in rationale
    assert "## Cases improved" in rationale and "## Cases broken" in rationale
    assert "Accept rules (EVAL 5.3)" in report
    assert chr(0x2014) not in rationale + report  # no em dashes


# ─── sensitivity (SPEC 10) ─────────────────────────────────────────────────────────────────


def test_perturb_renormalises() -> None:
    """Exact renormalisation (review Minor 5): no grid residual skews the perturbed weight."""
    up = sensitivity.perturb(CURRENT, "required_skills", 1.2)
    assert up["required_skills"] == pytest.approx(0.42 / 1.07, abs=1e-12)  # 0.392523...
    assert up["tools"] == pytest.approx(0.10 / 1.07, abs=1e-12)
    assert math.fsum(up.values()) == pytest.approx(1.0, abs=1e-12)
    down = sensitivity.perturb(CURRENT, "tools", 0.8)
    assert down["tools"] == pytest.approx(0.08 / 0.98, abs=1e-12)
    assert down["required_skills"] == pytest.approx(0.35 / 0.98, abs=1e-12)


def test_band_change_rate_arithmetic() -> None:
    def ps(pid: str, band: Optional[int]) -> PairScore:
        return PairScore(posting_id="p", pair_id=pid, score=None if band is None else 0.5, relevance=0,
                         band_pred=band)

    before = [ps("a", 0), ps("b", 1), ps("c", 2), ps("d", 3), ps("e", None)]
    after = [ps("a", 0), ps("b", 2), ps("c", 2), ps("d", 3), ps("e", 1)]
    assert sensitivity.band_change_rate(before, after) == pytest.approx(2 / 5)
    assert sensitivity.band_change_rate(before, before) == 0.0


def test_sensitivity_flags_over_leveraged_dimensions_on_a_toy() -> None:
    """One pair per posting; each pair's score sits next to a band edge only in `required_skills`."""

    def pairs(weights: dict[str, float]) -> list[PairScore]:
        # 10 pairs: score = required weight * 1.5 + 0.1; at 0.35 that is 0.625 (band 2)
        # +20 % renormalised gives 0.3925 -> 0.689 (band 2), -20 % 0.3011 -> 0.552 (band 2 edge 0.55)
        out = []
        for i in range(10):
            s = weights["required_skills"] * (1.0 + i * 0.1) + 0.1
            out.append(PairScore(posting_id=f"p{i}", pair_id=f"x{i}", score=s, relevance=0, band_pred=_band(s)))
        return out

    rows = sensitivity.analyse(pairs, CURRENT)
    assert len(rows) == 2 * len(DIMS)
    by = {(r.dimension, r.factor): r for r in rows}
    base = [_band(CURRENT["required_skills"] * (1.0 + i * 0.1) + 0.1) for i in range(10)]
    for (dim, factor), row in by.items():
        # closed form, independent of `perturb`: scaling w_d by f and renormalising gives
        # w_d f / (1 + (f - 1) w_d) for the moved weight and w / (1 + (f - 1) w_d) for the others
        w_d = CURRENT[dim]
        req = CURRENT["required_skills"] * (factor if dim == "required_skills" else 1.0) / (1 + (factor - 1) * w_d)
        new = [_band(req * (1.0 + i * 0.1) + 0.1) for i in range(10)]
        expected = sum(1 for a, b in zip(base, new) if a != b) / 10
        assert row.rate == pytest.approx(expected)
        assert row.over_leveraged == (expected > sensitivity.OVER_LEVERAGED)
    assert sensitivity.OVER_LEVERAGED == 0.15
    assert any(r.over_leveraged for r in rows if r.dimension == "required_skills")
    md = sensitivity.markdown(rows, CURRENT)
    assert "over-leveraged" in md and "required_skills" in md


def test_sensitivity_known_values_on_the_toy() -> None:
    """Hand-computed: +20 % on required_skills (0.35 -> 0.392523) flips pairs 2, 6 and 7 of the toy."""

    def pairs(weights: dict[str, float]) -> list[PairScore]:
        return [PairScore(posting_id=f"p{i}", pair_id=f"x{i}", relevance=0,
                          score=weights["required_skills"] * (1.0 + i * 0.1) + 0.1,
                          band_pred=_band(weights["required_skills"] * (1.0 + i * 0.1) + 0.1)) for i in range(10)]

    up = next(r for r in sensitivity.analyse(pairs, CURRENT) if (r.dimension, r.factor) == ("required_skills", 1.2))
    # at 0.35: 0.450 0.485 0.520 0.555 0.590 0.625 0.660 0.695 0.730 0.765 -> bands 1 1 1 2 2 2 2 2 3 3
    # at 0.392523: 0.493 0.532 0.571 0.610 0.650 0.689 0.728 0.767 0.807 0.846 -> 1 1 2 2 2 2 3 3 3 3
    assert up.rate == pytest.approx(0.3) and up.over_leveraged


def test_rate_of_exactly_15_percent_is_not_over_leveraged() -> None:
    at = sensitivity.Row("tools", 1.2, 0.1, 0.1, 0.15, False)
    above = sensitivity.Row("tools", 1.2, 0.1, 0.1, 0.1500001, False)
    assert not at.over_leveraged and above.over_leveraged


def test_sensitivity_marks_vectors_outside_the_bounds() -> None:
    near = {**CURRENT, "required_skills": 0.44, "preferred_skills": 0.05, "experience": 0.11}
    rows = sensitivity.analyse(lambda w: [PairScore(posting_id="p", pair_id="a", score=0.5, relevance=0,
                                                    band_pred=1)], near)
    up = next(r for r in rows if r.dimension == "required_skills" and r.factor == 1.2)
    assert up.outside_bounds  # 0.528 / 1.088 = 0.485 > 0.45: evaluated, and said so


# ─── the real engine, once ─────────────────────────────────────────────────────────────────


def test_engine_evaluator_scores_without_parsing() -> None:
    evaluator = search.EngineEvaluator.synthetic(limit=20)
    first = evaluator.evaluate(CURRENT)
    assert len(first.pairs) == 20 and first.ndcg is not None
    shifted = evaluator.evaluate({**CURRENT, "required_skills": 0.25, "tools": 0.20})
    assert [p.pair_id for p in shifted.pairs] == [p.pair_id for p in first.pairs]
    assert any(a.score != b.score for a, b in zip(first.pairs, shifted.pairs) if a.score is not None)
    # an out-of-bounds vector is scored only on request (sensitivity), never by the search
    out = {**CURRENT, "required_skills": 0.5, "preferred_skills": 0.05, "experience": 0.05}
    assert len(evaluator.pairs(out)) == 20


# ─── round 1: proposal hygiene, the CLI, real-engine determinism ───────────────────────────


def test_weights_diff_starts_with_the_do_not_adopt_markers(tmp_path: Path) -> None:
    folder, _, _ = _write(tmp_path)
    diff = (folder / "weights.diff").read_text(encoding="utf-8")
    head = diff.split("--- a/", 1)[0]
    assert head.startswith(f"# {proposal.SYNTHETIC_BANNER}")
    assert f"# {proposal.FAIRNESS_BANNER}" in head and "# verdict: " in head
    assert all(line.startswith("#") for line in head.strip().splitlines())


def test_an_existing_proposal_folder_is_never_overwritten(tmp_path: Path) -> None:
    folder, config_dir, result = _write(tmp_path)
    (folder / "rationale.md").write_text("signed by a reviewer", encoding="utf-8")
    with pytest.raises(FileExistsError):
        proposal.write_proposal(result, ToyEvaluator(), out_root=tmp_path / "proposals", day=date(2026, 10, 13),
                                config_dir=config_dir, sensitivity_rows=[], n_resamples=10, wall_seconds=0.0)
    assert (folder / "rationale.md").read_text(encoding="utf-8") == "signed by a reviewer"
    proposal.write_proposal(result, ToyEvaluator(), out_root=tmp_path / "proposals", day=date(2026, 10, 13),
                            config_dir=config_dir, sensitivity_rows=[], n_resamples=10, wall_seconds=0.0, force=True)
    assert (folder / "rationale.md").read_text(encoding="utf-8") != "signed by a reviewer"


def test_a_symlinked_target_file_is_refused(tmp_path: Path) -> None:
    import os

    evaluator = ToyEvaluator()
    result = search.run_search(evaluator, CURRENT, budget=3, seed=0)
    config_dir = _config_copy(tmp_path)
    folder = tmp_path / "proposals" / "2026-10-13-tune"
    folder.mkdir(parents=True)
    try:
        os.symlink(config_dir / "weights.yaml", folder / "weights.diff")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privileges on this platform")
    before = (config_dir / "weights.yaml").read_bytes()
    with pytest.raises(ValueError):
        proposal.write_proposal(result, evaluator, out_root=tmp_path / "proposals", day=date(2026, 10, 13),
                                config_dir=config_dir, sensitivity_rows=[], n_resamples=10, wall_seconds=0.0,
                                force=True)
    assert (config_dir / "weights.yaml").read_bytes() == before


class _CliToy(ToyEvaluator):
    items = list(range(len(_TOY)))


def test_cli_exits_1_on_a_fairness_failure_of_the_current_weights(tmp_path: Path, monkeypatch) -> None:
    toy = _CliToy(fairness=lambda w: _fair(False, 0.7))
    monkeypatch.setattr(search.EngineEvaluator, "synthetic", classmethod(lambda cls, limit=None: toy))
    assert search.main(["--budget", "2", "--split", "synthetic", "--out", str(tmp_path / "p")]) == 1
    assert not (tmp_path / "p").exists()


def test_cli_refuses_out_under_the_default_config(tmp_path: Path, monkeypatch, capsys) -> None:
    """The guard uses `proposal.CONFIG_DIR` (the real config/ by default); pointed at a tmp copy here so a
    broken guard cannot damage the real one."""
    assert proposal.CONFIG_DIR == ROOT / "config"
    config_dir = _config_copy(tmp_path)
    monkeypatch.setattr(proposal, "CONFIG_DIR", config_dir)
    before = (config_dir / "weights.yaml").read_bytes()
    monkeypatch.setattr(search.EngineEvaluator, "synthetic", classmethod(lambda cls, limit=None: _CliToy()))
    assert search.main(["--budget", "2", "--split", "synthetic", "--out", str(config_dir),
                        "--resamples", "10"]) == 2
    assert "refus" in capsys.readouterr().out
    assert [p.name for p in config_dir.iterdir()] == ["weights.yaml"]
    assert (config_dir / "weights.yaml").read_bytes() == before


def test_search_on_the_real_engine_is_deterministic() -> None:
    a = search.run_search(search.EngineEvaluator.synthetic(limit=20), CURRENT, budget=3, seed=0)
    b = search.run_search(search.EngineEvaluator.synthetic(limit=20), CURRENT, budget=3, seed=0)
    assert [(t.weights, t.objective) for t in a.trials] == [(t.weights, t.objective) for t in b.trials]
    assert [p.score for p in a.baseline.pairs] == [p.score for p in b.baseline.pairs]
