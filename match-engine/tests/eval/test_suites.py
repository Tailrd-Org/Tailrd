"""Evaluation suites (Task 5.3): closed form, mutations, properties, synthetic, golden, holdout."""

from __future__ import annotations

import json
import math
from datetime import date

import pytest

from eval.metrics import PairScore
from eval.suites import Engine, IdMap, build_id_map, percentile
from eval.suites import closed_form, golden, mutations, properties, synthetic
from eval.suites.closed_form import CF_AS_OF, OutOfClass, day
from matcher.models import (
    DatePrecision,
    DateValue,
    Evidence,
    JobPosting,
    JobRequirement,
    Necessity,
    Resume,
    Role,
    SeniorityLevel,
    SkillRef,
    SkillSource,
    Track,
    VerbClass,
)

IC2 = SeniorityLevel(track=Track.IC, level=2)


@pytest.fixture(scope="module")
def cf_engine(config):
    return Engine(config, closed_form.closed_form_graph())


@pytest.fixture(scope="module")
def cf_pairs():
    return [(p.case, p.resume, p.posting) for p in closed_form.constructed_pairs(60, seed=3)]


def _ref(skill_id: str, evidence: Evidence, verb: VerbClass = VerbClass.NO_VERB) -> SkillRef:
    return SkillRef(canonical_id=skill_id, surface_form=skill_id, evidence=evidence, verb_class=verb,
                    source=SkillSource.ROLE)


# ─── closed form ───────────────────────────────────────────────────────────────────────────


def test_closed_form_matches_a_hand_computed_pair(cf_engine) -> None:
    posting = JobPosting(
        seniority=IC2, min_years_total=2.0, industry_codes=("5112",),
        requirements=(JobRequirement(skill_id="cf.req01", necessity=Necessity.REQUIRED),
                      JobRequirement(skill_id="cf.req05", necessity=Necessity.REQUIRED),
                      JobRequirement(skill_id="cf.req11", necessity=Necessity.PREFERRED)),
        requirement_token_count=3,
    )
    role = Role(title_raw="Software Engineer", seniority=IC2, industry_codes=("5111",),
                start=day(date(2023, 10, 1)), is_current=True,
                skills=(_ref("cf.req01", Evidence.ROLE_BULLET, VerbClass.OWNERSHIP),
                        _ref("cf.req05", Evidence.ROLE_BULLET_QUANTIFIED, VerbClass.EXECUTION)))
    resume = Resume(roles=(role,), skills_blob=(_ref("cf.req11", Evidence.SKILLS_BLOB),))
    # By hand: required = (0.8 x 1.00 + 0.95 x min(1, 0.85 x 1.15)) / (0.8 + 0.95); preferred 0.65 (listed,
    # R = 1 for a current role); tools abstain (no IMPLIED); experience 1.00 (36 months x relevance 0.95 >= 24);
    # seniority 1.00; domain 0.70 (5111 vs 5112, same 3-digit); education 1.00 (no requirement).
    # The scored weights are 0.90, so each is divided by 0.90.
    required = (0.8 * 1.0 + 0.95 * 0.85 * 1.15) / 1.75
    by_hand = (0.35 * required + 0.10 * 0.65 + 0.15 * 1.0 + 0.10 * 1.0 + 0.10 * 0.70 + 0.10 * 1.0) / 0.90
    want = closed_form.expected_raw(resume, posting, cf_engine.graph, cf_engine.config, CF_AS_OF)
    assert want == pytest.approx(by_hand, abs=1e-12)
    got = cf_engine.score(resume, posting, as_of=CF_AS_OF).raw_score
    assert got == pytest.approx(by_hand, abs=closed_form.TOLERANCE)


def test_engine_agrees_with_the_closed_form_on_constructed_pairs(cf_engine) -> None:
    pairs = closed_form.constructed_pairs(300, seed=11)
    mismatches, worst = closed_form.check(pairs, cf_engine)
    assert not mismatches, [(m.case, m.engine_raw, m.expected_raw) for m in mismatches[:5]]
    assert worst <= closed_form.TOLERANCE
    # the class is exercised, not degenerate: abstentions, caps and both tracks all occur
    values = [closed_form.expected(p.resume, p.posting, cf_engine.graph, cf_engine.config, p.as_of) for p in pairs]
    assert any(v.critical_misses >= 2 for v in values) and any(v.critical_misses == 1 for v in values)
    assert any(v.values["seniority"] is None for v in values) and any(v.values["domain"] is None for v in values)
    assert len({round(v.raw, 3) for v in values if v.raw is not None}) > 100


def test_closed_form_refuses_pairs_outside_its_class(cf_engine) -> None:
    pair = closed_form.constructed_pairs(1, seed=0)[0]
    grouped = pair.posting.model_copy(update={"requirements": tuple(
        r.model_copy(update={"or_group": "or:1"}) for r in pair.posting.requirements)})
    with pytest.raises(OutOfClass):
        closed_form.expected(pair.resume, grouped, cf_engine.graph, cf_engine.config, CF_AS_OF)


# ─── mutations ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("case", mutations.MUTATIONS, ids=lambda c: c.name)
def test_mutation(case, cf_engine) -> None:
    outcome = case.run(cf_engine)
    assert outcome.passed, outcome.problems


def test_mutation_suite_covers_every_eval_row_and_grows() -> None:
    assert len(mutations.MUTATIONS) >= 7
    assert len({c.name for c in mutations.MUTATIONS}) == len(mutations.MUTATIONS)
    assert "add a case for every bug fixed" in mutations.__doc__.lower()


def test_remove_one_required_is_exactly_the_coverage_arithmetic(cf_engine) -> None:
    outcome = mutations.MUTATIONS[0].run(cf_engine)
    assert outcome.delta == pytest.approx(-0.35 * 0.25, abs=1e-9)


# ─── properties ────────────────────────────────────────────────────────────────────────────


class _Flaky:
    """Scores against a different posting every other call: a nondeterministic engine."""

    def __init__(self, engine, other) -> None:
        self.engine, self.other, self.calls = engine, other, 0

    def score(self, resume, posting, *, as_of, options=None):
        self.calls += 1
        return self.engine.score(resume, self.other if self.calls % 2 else posting, as_of=as_of,
                                 **({"options": options} if options is not None else {}))


class _Raising:
    def __init__(self, engine) -> None:
        self.engine, self.calls = engine, 0

    def score(self, resume, posting, *, as_of, options=None):
        self.calls += 1
        if self.calls % 3 == 0:
            raise RuntimeError("boom")
        return self.engine.score(resume, posting, as_of=as_of)


def test_determinism_passes_for_the_engine_and_catches_a_flaky_one(cf_engine, cf_pairs) -> None:
    guard, failures = properties.check_determinism(cf_engine, cf_pairs[:2], CF_AS_OF, runs=5)
    assert guard.passed and not failures
    flaky = _Flaky(cf_engine, cf_pairs[5][2])
    guard, failures = properties.check_determinism(flaky, cf_pairs[:1], CF_AS_OF, runs=4)
    assert guard.passed is False and failures[0].kind == "determinism"


def test_totality_counts_every_escaping_exception(cf_engine, cf_pairs) -> None:
    guard, failures = properties.check_totality(cf_engine, cf_pairs, CF_AS_OF, n=400, seed=1)
    assert guard.passed and guard.value == 0.0
    guard, failures = properties.check_totality(_Raising(cf_engine), cf_pairs, CF_AS_OF, n=30, seed=1)
    assert guard.passed is False and guard.value == 10.0
    assert "boom" in failures[0].detail["exception"]


def test_bounded_monotone_gates_latency_and_stuffing_hold_on_constructed_pairs(cf_engine, cf_pairs) -> None:
    assert properties.check_bounded(cf_engine, cf_pairs, CF_AS_OF)[0].passed
    monotone, failures = properties.check_monotone(cf_engine, cf_pairs, CF_AS_OF)
    assert all(g.passed for g in monotone), failures[:2]
    assert properties.check_gate_precedence(cf_engine, cf_pairs, CF_AS_OF)[0].passed
    latency, stats = properties.check_latency(cf_engine, cf_pairs[:10], CF_AS_OF)
    assert latency.passed and stats["latency_p95_ms"] < 150
    postings = [(case, posting) for case, _, posting in cf_pairs]
    assert properties.check_stuffing(cf_engine, postings, CF_AS_OF)[0].passed


def test_monotonicity_reports_a_decrease(cf_engine, cf_pairs) -> None:
    class Contrary:
        def score(self, resume, posting, *, as_of, options=None):
            result = cf_engine.score(resume, posting, as_of=as_of)
            if result.raw_score is None or not resume.skills_blob:
                return result
            return result.model_copy(update={"raw_score": max(0.0, result.raw_score - 0.01 * len(resume.skills_blob))})

    monotone, failures = properties.check_monotone(Contrary(), cf_pairs, CF_AS_OF)
    assert monotone[0].passed is False and failures and failures[0].kind == "monotonicity"


def test_percentile_is_numpys_linear_interpolation() -> None:
    import numpy as np

    values = [3.0, 1.0, 4.0, 1.5, 9.0, 2.6]
    for q in (0, 50, 95, 99, 100):
        assert percentile(values, q) == pytest.approx(float(np.percentile(values, q)))
    assert percentile([], 99) is None


# ─── ids ───────────────────────────────────────────────────────────────────────────────────


class _Resolution:
    def __init__(self, canonical_id, method) -> None:
        self.canonical_id, self.method = canonical_id, method


class _Resolver:
    table = {"golang": ("skill.go", "EXACT"), "python": ("skill.python", "EXACT"), "fuzzy": ("skill.x", "FUZZY")}

    def resolve(self, surface, *, prefer_fields=False):
        cid, method = self.table.get(surface.casefold(), (None, "NONE"))
        return _Resolution(cid, method)


def test_id_map_translates_by_resolvable_spellings_and_merges_requirements() -> None:
    from eval.generate.vocabulary import VocabSkill, Vocabulary
    from matcher.models import SkillKind

    vocab = Vocabulary(skills=(
        VocabSkill(id="skill.golang", label="Golang", kind=SkillKind.TECHNOLOGY, families=("sw",)),
        VocabSkill(id="skill.py", label="Snake", kind=SkillKind.TECHNOLOGY, families=("sw",), aliases=("Python",)),
        VocabSkill(id="skill.fz", label="Fuzzy", kind=SkillKind.TECHNOLOGY, families=("sw",)),
    ), families=("sw",))
    id_map = build_id_map(vocab, _Resolver())
    assert id_map("skill.golang") == "skill.go" and id_map("skill.py") == "skill.python"
    assert id_map.unknown == frozenset({"skill.fz"}) and id_map("skill.fz") == "skill.fz"
    posting = JobPosting(requirements=(
        JobRequirement(skill_id="skill.golang", necessity=Necessity.PREFERRED),
        JobRequirement(skill_id="skill.go", necessity=Necessity.REQUIRED),
        JobRequirement(skill_id="skill.fz", necessity=Necessity.REQUIRED, surface_form="Fuzzy"),
    ))
    translated = id_map.posting(posting)
    (merged,) = translated.requirements
    assert merged.skill_id == "skill.go" and merged.necessity == Necessity.REQUIRED
    assert translated.unresolved_requirement_tokens == ("Fuzzy",)
    resume = Resume(skills_blob=(SkillRef(canonical_id="skill.fz", evidence=Evidence.SKILLS_BLOB),
                                 SkillRef(canonical_id="skill.golang", evidence=Evidence.SKILLS_BLOB)))
    assert [r.canonical_id for r in id_map.resume(resume).skills_blob] == ["skill.go"]


# ─── synthetic ─────────────────────────────────────────────────────────────────────────────


def _pair(posting: str, pair: str, score, relevance: int) -> PairScore:
    return PairScore(posting_id=posting, pair_id=pair, score=score, relevance=relevance,
                     advanced=relevance >= 2, band_pred=relevance, band_true=relevance, hard=relevance in (1, 2))


def test_ranking_metrics_on_a_perfect_ordering() -> None:
    pairs = [_pair(f"p{k}", f"p{k}-{r}", 0.2 + 0.2 * r, r) for k in range(4) for r in range(4)]
    metrics = synthetic.ranking_metrics(pairs, n_resamples=50)
    assert metrics["ndcg@10"]["point"] == pytest.approx(1.0)
    assert metrics["auc"]["point"] == pytest.approx(1.0)
    assert metrics["band_exact"]["point"] == pytest.approx(1.0)
    assert metrics["abstention_rate"]["point"] == 0.0 and metrics["abstention_precision"]["point"] is None
    assert metrics["precision@10"]["point"] == pytest.approx(0.5)
    assert {s.key: s.min_effect for s in synthetic.RANKING_METRICS}["band_off_by_two"] == 0.005
    assert {s.key: s.min_effect for s in synthetic.RANKING_METRICS}["ndcg@10"] == 0.01


@pytest.fixture(scope="module")
def small_corpus():
    from eval.suites import synthetic_corpus

    return synthetic_corpus(n_profiles=12)


def _identity_map(corpus) -> IdMap:
    ids = [s.id for s in corpus.vocab.skills] + [f.id for f in corpus.vocab.fields]
    return IdMap(skills={i: i for i in ids}, unknown=frozenset())


def _oracle_parser(corpus, id_map):
    """A perfect parser: the filename names the profile and the template."""
    from eval.generate.profiles import expected_resume

    by_id = {p.profile_id: p for p in corpus.profiles}

    def parse(data: bytes, filename: str):
        profile_id, rest = filename.split("_", 1)
        template = rest.rsplit(".", 1)[0]
        resume = expected_resume(by_id[profile_id], corpus.vocab, corpus.as_of,
                                 template="functional" if template == "functional" else None)
        return id_map.resume(resume)

    return parse


def _fidelity(corpus, engine, parse, formats, tmp_cache=None):
    id_map = _identity_map(corpus)
    templates = ("chronological", "functional", "ats_plain")
    profiles = corpus.profiles[:4]
    rendered, stats = synthetic.parse_renderings(profiles, corpus.seed, templates, formats, parse=parse,
                                                 cache=tmp_cache)
    postings = {pid: id_map.posting(p) for pid, p in corpus.by_id.items()}
    return synthetic.fidelity(corpus, id_map, engine, profiles, rendered, templates, formats, postings), stats


def test_fidelity_of_a_perfect_parser_is_perfect(small_corpus, config) -> None:
    from matcher.graph.model import SkillGraph

    engine = Engine(config, SkillGraph({}, {}, {}, "empty"))
    report, _ = _fidelity(small_corpus, engine, _oracle_parser(small_corpus, _identity_map(small_corpus)), ("txt",))
    assert report.metrics["recovered_skills"] == pytest.approx(1.0)
    assert report.metrics["date_error_max_months"] == 0
    assert report.metrics["spread_within_template_p99"] == 0.0
    assert report.metrics["spread_across_templates_p99"] == 0.0
    assert report.metrics["functional_deviation_p99"] == 0.0
    assert all(g.passed for g in report.guardrails), report.guardrails


def test_fidelity_fails_a_parser_that_loses_one_format(small_corpus, config, tmp_path) -> None:
    from matcher.graph.model import SkillGraph

    engine = Engine(config, SkillGraph({}, {}, {}, "empty"))
    perfect = _oracle_parser(small_corpus, _identity_map(small_corpus))

    def lossy(data: bytes, filename: str):
        return Resume() if filename.endswith(".docx") else perfect(data, filename)

    cache = synthetic.ParseCache(tmp_path, "fake-parser-v1", small_corpus.seed)
    report, stats = _fidelity(small_corpus, engine, lossy, ("txt", "docx"), cache)
    assert stats["parsed"] == stats["renderings"] == 4 * 3 * 2
    within = next(g for g in report.guardrails if g.name.endswith("within a template"))
    assert within.passed is False and within.value > synthetic.INVARIANCE_MAX
    assert next(g for g in report.guardrails if "recovered" in g.name).passed is False
    assert report.failures and report.failures[0].kind == "invariance within" == within.kind
    _, again = _fidelity(small_corpus, engine, lossy, ("txt", "docx"), cache)
    assert again["cached"] == again["renderings"] and again["parsed"] == 0


def test_parse_cache_round_trip_and_keys(tmp_path, small_corpus) -> None:
    cache = synthetic.ParseCache(tmp_path, "p1", 7)
    profile = small_corpus.profiles[0]
    key = cache.key(profile, "chronological", "txt")
    assert key != cache.key(profile, "chronological", "docx") != synthetic.ParseCache(tmp_path, "p2", 7).key(
        profile, "chronological", "txt")
    assert cache.get(key) == (False, None)
    cache.put(key, Resume(total_experience_months=12.0))
    assert cache.get(key) == (True, Resume(total_experience_months=12.0))
    cache.put(key, None)
    assert cache.get(key) == (True, None)
    assert synthetic.ParseCache(tmp_path, None, 7).get(key) == (False, None)  # no fingerprint, no cache


def test_date_error_counts_months_and_missing_roles() -> None:
    def month(y: int, m: int) -> DateValue:
        return DateValue.exact(date(y, m, 1), DatePrecision.MONTH)

    oracle = Resume(roles=(Role(title_raw="Analyst", employer="Acme", start=month(2020, 1), end=month(2022, 6)),
                           Role(title_raw="Intern", employer="Beta", start=month(2019, 5), end=month(2019, 8))))
    close = Resume(roles=(Role(title_raw="Analyst", employer="ACME", start=month(2020, 2), end=month(2022, 6)),
                          Role(title_raw="Intern", employer="Beta", start=month(2019, 5), end=month(2019, 8))))
    assert synthetic.date_error(oracle, close, date(2026, 10, 1)) == (1, [])
    error, notes = synthetic.date_error(oracle, close.model_copy(update={"roles": close.roles[:1]}), date(2026, 10, 1))
    assert error == math.inf and "not found" in notes[0]
    assert synthetic.date_error(oracle, None, date(2026, 10, 1))[0] == math.inf


def test_synthetic_suite_reports_the_parser_skipped_when_none_is_injected(small_corpus, config, cf_engine) -> None:
    from eval.suites import corpus_pairs
    from matcher.graph.model import SkillGraph

    engine = Engine(config, SkillGraph({}, {}, {}, "empty"))
    id_map = _identity_map(small_corpus)
    result = synthetic.run_synthetic(engine, cf_engine, small_corpus, id_map, corpus_pairs(small_corpus, id_map),
                                     closed_form_n=20, n_resamples=20)
    assert result.metrics["parser"] == "skipped: parser not available"
    assert any(n.startswith("PARSER: skipped: parser not available") for n in result.notes)
    assert len(result.pairs) == 12 and result.metrics["ranking"]["ndcg@10"]["point"] is not None
    assert all(g.passed for g in result.guardrails), result.guardrails


# ─── golden and holdout ────────────────────────────────────────────────────────────────────


def test_golden_is_skipped_without_labels(tmp_path, cf_engine) -> None:
    result = golden.run_golden(cf_engine, CF_AS_OF, labels=tmp_path / "labels.jsonl")
    assert result.status == "skipped" and golden.GOLDEN_SKIPPED in result.notes
    assert golden.GOLDEN_SKIPPED == "GOLDEN: skipped (no labels)"


def test_golden_scores_labelled_pairs(tmp_path, cf_engine) -> None:
    pairs = closed_form.constructed_pairs(8, seed=5)
    lines = []
    for i, pair in enumerate(pairs):
        lines.append(json.dumps({
            "pair_id": f"g{i}", "posting_id": f"post{i % 2}", "as_of": CF_AS_OF.isoformat(),
            "resume": pair.resume.model_dump(mode="json"), "posting": pair.posting.model_dump(mode="json"),
            "labels": [{"rater": "r1", "band": i % 4}, {"rater": "r2", "band": (i + (i % 3 == 0)) % 4}],
        }))
    labels = tmp_path / "labels.jsonl"
    labels.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = golden.run_golden(cf_engine, CF_AS_OF, labels=labels)
    assert result.status == "ran" and result.metrics["golden_pairs"] == 8
    assert result.metrics["kendall_postings"] == 2 and -1.0 <= result.metrics["kendall_tau"] <= 1.0
    assert 0.0 <= result.metrics["rater_exact_agreement"] <= 1.0
    assert result.metrics["rater_weighted_kappa"] is not None


def test_weighted_kappa_on_textbook_cases() -> None:
    assert golden.weighted_kappa([0, 1, 2, 3], [0, 1, 2, 3]) == pytest.approx(1.0)
    assert golden.weighted_kappa([0, 3, 0, 3], [3, 0, 3, 0]) < 0
    assert golden.weighted_kappa([1], [1]) is None


def test_holdout_is_skipped_without_a_key() -> None:
    result = golden.run_holdout()
    assert result.status == "skipped" and golden.HOLDOUT_SKIPPED in result.notes
