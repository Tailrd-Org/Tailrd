# Build Manifest

Everything Claude Code needs to produce, ordered by milestone. Tick as you go — this file is
the shared checklist between you and the agent.

---

## 0. Before anything: drop these four in the repo root

```
CLAUDE.md             standing orders — auto-loaded by Claude Code every session
BUILD.md              this file — the milestone checklist, the agent's entry point
SPEC.md               the engine
EVAL.md               the test harness
docs/future/ADVANCED.md   V2/V3 roadmap — deliberately NOT in the root
```

**Put `ADVANCED.md` under `docs/future/`, not the repo root.** An agent that finds the
V2 roadmap while building Milestone 3 will start adding graph embeddings to a scorer whose
arithmetic isn't finished. Move it up only when Milestone 7 is ticked.

### `CLAUDE.md` — paste this verbatim

```markdown
# Project: resume ↔ job match engine

## Read in this order, every session
1. BUILD.md — the milestone checklist. Find the first unticked milestone.
   That milestone is the entire scope of this session.
2. SPEC.md  — read the sections that milestone cites. Authoritative on
   scoring math, data model, and compliance.
3. EVAL.md  — from Milestone 5 onward only. Authoritative on measurement.

## Do NOT read docs/future/ADVANCED.md
It is the V2/V3 roadmap and nothing in it is in scope. If a task seems to
need it, you are building ahead of the checklist — stop and ask.

## Hard rules
- `src/matcher/score/` is pure: no I/O, no clock, no config loading, no network.
- Never commit to `config/weights.yaml` directly. Open a proposal under `proposals/`.
- Never read or decrypt `eval/corpora/holdout/`.
- A fairness guardrail failure is terminal. Do not tune around it, adjust the
  threshold, or exclude the failing group. Stop and surface it.
- Prohibited features (SPEC §9) are enforced by `tests/test_forbidden_features.py`.
  Never modify that test to make a build pass.

## Definition of done
- `make eval-fast` green
- New mutation test added for every bug fixed
- Metric table from `make eval` pasted into the PR, with confidence intervals
- Regressions reported even when headline metrics improve

## Before you start
Confirm your understanding of the current milestone, flag anything ambiguous or
that you disagree with, and propose an order. Do not write code until I confirm.
```

---

## Milestone 1 — Foundations

> **Status: complete (2026-10-08).** Gate met: config loads, weights validate, every model
> round-trips through JSON byte-identically (`tests/test_models.py`, `tests/test_config_integrity.py`).

No scoring yet. Get the contracts right.

```
pyproject.toml                      deps, pinned
Makefile                            eval, eval-fast, tune, lint
.pre-commit-config.yaml             blocks commits touching eval/corpora/holdout/
config/weights.yaml                 SPEC §5, sums to 1.0
config/gates.yaml                   SPEC §4
config/bands.yaml                   SPEC §7
config/engine_version.txt
src/matcher/models.py               SPEC §2 — all dataclasses, frozen, validated
src/matcher/errors.py               reason codes for NEEDS_REVIEW / GATE_FAIL
tests/test_models.py
tests/test_config_integrity.py      asserts weights sum to 1.0
```

**Gate:** config loads, weights validate, every dataclass round-trips through JSON.

---

## Milestone 2 — Skill graph

```
scripts/build_taxonomy.py           ESCO/O*NET ingest → canonical nodes
scripts/compute_idf.py              SPEC §3.1
scripts/compute_pmi.py              SPEC §3.2
src/matcher/graph/loader.py
src/matcher/graph/aliases.py
src/matcher/graph/traversal.py      parent/child/sibling, depth-limited
src/matcher/graph/idf.py
src/matcher/graph/pmi.py
src/matcher/normalize/resolver.py   surface form → canonical ID
src/matcher/normalize/fuzzy.py      bounded, logged (SPEC §3.3)
data/taxonomy/                      generated artifacts, hash-pinned
tests/test_graph_traversal.py
tests/test_alias_resolution.py
```

**Gate:** 1000 hand-picked surface forms resolve correctly; traversal terminates on cycles.

---

## Milestone 3 — Scoring core (hand-built fixtures, no parsing)

The most important milestone. Do not move on until the math is right.

```
src/matcher/score/evidence.py              SPEC §15 verb + negation
config/verbs.yaml                          SPEC §15.1
config/negation.yaml                       SPEC §15.2
src/matcher/score/dimensions/required_skills.py    §5.1
src/matcher/score/dimensions/preferred_skills.py   §5.2
src/matcher/score/dimensions/experience.py         §5.3
src/matcher/score/dimensions/seniority.py          §5.4
src/matcher/score/dimensions/domain.py             §5.5
src/matcher/score/dimensions/education.py          §5.6
src/matcher/score/dimensions/tools.py              §5.7
src/matcher/score/gates.py                         §4
src/matcher/score/aggregate.py                     §6 + weight redistribution §14.5
src/matcher/score/confidence.py                    §17
src/matcher/score/intervals.py                     §17.3
src/matcher/score/engine.py                        the one public entry point
tests/fixtures/profiles/*.json                     30 hand-written pairs
tests/test_dimensions_*.py                         one per dimension
tests/test_aggregate.py
tests/test_weight_redistribution.py                ← the silent-bug test
tests/test_totality.py                             §13, Hypothesis
tests/test_determinism.py
tests/test_forbidden_features.py                   §9, never modify
```

**Gate:** all seven dimensions match closed-form expected values; totality test green over
50k fuzzed inputs.

---

## Milestone 4 — Explanation

```
src/matcher/explain/breakdown.py        SPEC §8
src/matcher/explain/gaps.py
src/matcher/explain/counterfactual.py   ADVANCED §7 — cheap, build it now
tests/test_explanation_completeness.py  every score produces a full explanation
```

---

## Milestone 5 — Eval harness (before parsing, before tuning)

```
eval/generate/profiles.py            EVAL §2.1
eval/generate/render.py              8 templates × 6 formats
eval/generate/templates/*.j2
eval/suites/properties.py            EVAL §1 layer 1
eval/suites/synthetic.py             layer 2
eval/suites/mutations.py             EVAL §2.3
eval/metrics/ranking.py              NDCG, precision@k, AUC
eval/metrics/calibration.py          ECE
eval/metrics/fairness.py             four-fifths
eval/metrics/bootstrap.py            CIs over postings
eval/report.py                       summary.md + metrics.json + regressions.md
eval/run.py                          `make eval`
eval/baseline.py                     diffing
```

**Gate:** `make eval-fast` under 2 min; synthetic corpus of 500 profiles generated.

---

## Milestone 6 — Parsing (budget 50–60% of total effort)

```
src/matcher/ingest/detect.py            file type, encryption, corruption
src/matcher/ingest/pdf.py               layout-aware, column detection
src/matcher/ingest/docx.py
src/matcher/ingest/ocr.py               scanned fallback
src/matcher/ingest/normalize_text.py    NFKC, zero-width, ligatures
src/matcher/ingest/quality.py           parse tier A/B/C, stuffing detection
src/matcher/parse/sections.py           header classification
src/matcher/parse/dates.py              ← own module, own exhaustive test file
src/matcher/parse/roles.py
src/matcher/parse/education.py
src/matcher/parse/scope.py              ADVANCED §2.3 quantity extraction
src/matcher/parse/posting.py            required vs preferred, OR-groups
tests/fixtures/edge/                    ~60 fixtures, SPEC §14
tests/test_dates.py                     every row of SPEC §14.2
tests/test_edge_ingest.py
tests/test_posting_pathologies.py       SPEC §14.4
```

**Gate:** format invariance ≤ 0.02 spread across all 48 renderings.

---

## Milestone 7 — Calibration & audit

```
src/matcher/calibrate/percentile.py     SPEC §7 phase 1
src/matcher/calibrate/isotonic.py       phase 2
src/matcher/audit/trace.py              persist features + config hash
src/matcher/audit/fairness.py           SPEC §9
src/matcher/audit/drift.py              EVAL §6 alarms
scripts/refresh_taxonomy.py             SPEC §16 weekly job
```

---

## Milestone 8 — Tuning

```
eval/tune/search.py                  Optuna, constrained
eval/tune/constraints.py             fairness hard constraint
eval/tune/proposal.py                writes proposals/, never commits
eval/corpora/holdout/labels.enc      encrypted, CI key only
.github/workflows/eval.yml           holdout on release branches only
docs/evaluation_budget.md            the 10-looks-per-quarter ledger
```

---

## Not yet

Everything in `ADVANCED.md`. Layers A, C and F become available once Milestone 7 is done
and need no labels. Layer B waits on ~50k labeled outcomes — there is no shortcut.

---

## Suggested session prompts

```
Session 1:  Read SPEC.md, EVAL.md and CLAUDE.md. Summarize the architecture
            back to me, flag anything ambiguous or that you disagree with, then
            build Milestone 1 only. Stop at the gate.

Session 2:  Milestone 2. Start by proposing the taxonomy node schema and waiting
            for my confirmation.

Session 3:  Milestone 3, one dimension at a time. Write the test before the
            implementation for each. Do not start the aggregator until all seven
            dimensions pass independently.
```

One milestone per session. The context window is the real constraint — an agent that starts
Milestone 6 with Milestones 1–5 still in context will lose the early decisions and
quietly contradict them.
