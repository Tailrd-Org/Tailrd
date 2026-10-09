# Evaluation & Tuning Harness

> Companion to `SPEC.md`. Read both. This file governs how the engine is measured and tuned.
>
> Claude Code entry point: `make eval`

---

## 0. The governing constraint

**Claude Code must never see the holdout labels.**

If the agent tuning the weights can read the labels it is scored against, it will fit those
labels — not through dishonesty, but because that is what optimization does. Metrics improve,
real performance degrades, and you find out in production. Every design decision below
follows from this.

The mechanism is physical, not procedural:

```
eval/
  corpora/
    synthetic/         # generated, ground truth derivable, fully visible
    golden/            # human-labeled, visible — tuning allowed
    dev/               # outcome labels, visible — tuning allowed
    holdout/
      pairs/           # inputs visible
      labels.enc       # AES-encrypted, key in CI secrets only
```

`labels.enc` decrypts only inside CI, only on release branches, at most once per release
candidate. Local `make eval` runs every layer except holdout and prints
`HOLDOUT: skipped (no key)`. A pre-commit hook rejects any commit touching
`eval/corpora/holdout/`.

---

## 1. Four layers

| Layer | Corpus | Labels from | Runtime | Runs on |
|---|---|---|---|---|
| 1 — Properties | Generated | None needed | < 30 s | Every commit |
| 2 — Synthetic | Constructed | Derivable exactly | < 2 min | Every commit |
| 3 — Golden | Real, anonymized | Human band labels | < 5 min | Every PR |
| 4 — Holdout | Real applications | Downstream outcomes | < 20 min | Release only |

Layers 1 and 2 catch the overwhelming majority of real bugs and need no human labeling.
Build them first. Layer 3 tells you whether the scoring *agrees with people*. Layer 4 is the
only layer that tells you whether the engine is actually useful.

---

## 2. Layer 2 — the synthetic corpus (your main workhorse)

This is the unlock for testing "a bunch of resumes" without touching PII or waiting on human
labelers. You generate resumes **from** a known profile, so ground truth is exact by
construction.

### 2.1 Generator design

```python
CandidateProfile:          # the ground truth
    skills: [(skill_id, years, evidence_tier, last_used)]
    roles:  [(title, seniority, employer, industry, start, end)]
    education, certifications, locations

render(profile, template, format) -> bytes
```

**Templates** (at minimum): `chronological`, `functional`, `two_column`, `academic_cv`,
`ats_plain`, `designer_heavy`, `federal_long`, `military_transition`.

**Formats**: `pdf_latex`, `pdf_word`, `pdf_canva_like`, `docx`, `txt`, `pdf_scanned_300dpi`.

8 templates × 6 formats = 48 renderings of every profile. Generate 500 profiles and you have
24,000 test documents, all with perfect labels, all free, none of them PII.

### 2.2 What it proves

```python
# Parser fidelity
assert recovered_skills(parse(render(p))) >= 0.95 * p.skills
assert date_error_months(parse(render(p)), p) <= 1

# Format invariance — SPEC §10
scores = [score(parse(render(p, t, f)), job) for t in TEMPLATES for f in FORMATS]
assert max(scores) - min(scores) <= 0.02

# Analytic correctness: compute the expected score by hand from the profile,
# independently of the implementation, and compare
assert isclose(score(p, job), expected_score_closed_form(p, job), abs_tol=0.001)
```

That last one is the strongest test in the suite. It verifies the implementation against the
*spec's math* rather than against its own previous output, which is what snapshot tests
degrade into.

### 2.3 Mutation testing

Take a profile, change exactly one thing, assert the score moves in the predicted direction
and magnitude. No labels required, enormous coverage.

```
remove one required skill (idf 0.8)  → score drops 0.06–0.12
remove two high-idf required skills  → critical-miss cap engages, score ≤ 0.55
shift all dates back 4 years         → score drops via recency decay, 0.02–0.06
change "architected" → "assisted"    → §15 multiplier drops, 0.45/1.00 ratio
add 40 irrelevant skills             → |Δscore| ≤ 0.01  (stuffing defense)
swap IC3 title for M2 title          → §5.4 cross-track penalty visible
blank all dates                      → §5.3 abstains, weight redistributes, no silent zero
```

Each row is a parameterized test. Add one every time you fix a bug — this file becomes the
regression suite.

---

## 3. Metrics — rank, don't grade

The product ranks candidates for a posting. Absolute score accuracy is nearly irrelevant;
**ordering is the thing**. Optimize accordingly.

### Primary

```
NDCG@10, per posting, averaged        # ranking quality where it matters
Precision@10                           # of the top 10 shown, how many were good
AUC-ROC on advanced/not                # threshold-free separation
```

### Secondary

```
Kendall's τ vs. human ordering (Layer 3)
Band accuracy: exact / off-by-one / off-by-two   # off-by-two is the one to alarm on
Calibration error (ECE, 10 bins)                 # does "80%" mean 80%?
Abstention rate + abstention precision           # SPEC §17 — are the abstentions the hard ones?
```

### Guardrails (hard fail, not report-and-continue)

```
Determinism:        1000 runs → identical hashes
Totality:           0 exceptions across 50k fuzzed inputs
Format invariance:  p99 spread ≤ 0.02
Latency:            p95 < 150 ms per pair
Four-fifths ratio:  ≥ 0.80 across every group with n ≥ 100
Stuffing defense:   posting-pasted-into-resume scores < 0.85
```

**Always report confidence intervals.** Bootstrap 1,000 resamples over postings (not pairs —
pairs within a posting are correlated). An NDCG improvement of 0.004 with a CI of ±0.02 is
noise, and the harness should say so rather than letting you celebrate it.

---

## 4. The harness

```bash
make eval                       # layers 1-3, report to reports/
make eval-fast                  # layers 1-2, pre-commit
make eval-holdout               # CI + release branch only
make eval --baseline=main       # diff against baseline
make tune --budget=200          # propose weights (§5)
```

Every run writes `reports/<timestamp>/`:

```
summary.md        # human + agent readable, this is what Claude Code reads
metrics.json      # machine readable, for trend tracking
regressions.md    # pairs that got worse vs. baseline, worst-first
failures/         # every failed case, full feature trace, reproducible
config_hash.txt   # exactly which weights produced this
```

### `summary.md` format

```markdown
# Eval — 2026-10-08T14:22 — config a3f9c21 — vs baseline main@8e2d1

| Metric            | Baseline | Current |    Δ | 95% CI         | Verdict |
|-------------------|---------:|--------:|-----:|----------------|---------|
| NDCG@10           |    0.712 |   0.748 | +.036| [+.018, +.054] | PASS    |
| Precision@10      |    0.640 |   0.655 | +.015| [-.009, +.039] | NOISE   |
| Band off-by-two   |    2.1%  |   1.4%  | -0.7%| [-1.3, -0.1]   | PASS    |
| Four-fifths (min) |    0.87  |   0.79  | -0.08|                | **FAIL**|

## FAIL: fairness guardrail
Group B selection ratio 0.79 < 0.80. Change rejected regardless of NDCG gain.
Driver: seniority weight 0.10 → 0.16 amplified title-ladder effects.

## Top regressions (12)
1. pair_8821  0.81 → 0.62  career-changer, finance → tech
   ...
```

That `regressions.md` file is the highest-value output. A metric moving tells you *that*
something changed; the regression list tells you *what broke*, which is what you can act on.

---

## 5. Tuning — propose, don't apply

### 5.1 The loop

```bash
make tune --budget=200 --objective=ndcg@10 --split=dev
```

Optuna TPE over the weight vector. Constraints:

```python
sum(weights) == 1.0
each weight ∈ [0.03, 0.45]      # no dimension dominates or vanishes
fairness_ratio >= 0.80          # hard constraint, infeasible if violated
abstention_rate <= 0.15         # can't buy accuracy by refusing to score
```

Output is **a pull request**, never a commit to main:

```
proposals/2026-10-08-tune/
  weights.diff
  rationale.md        # which dimensions moved, which cases improved, which broke
  report.md
  sensitivity.md      # ±20% perturbation on the new vector (SPEC §10)
```

### 5.2 Why a human approves

Two reasons, and the second is the binding one.

Technically, 200 trials against a dev set of a few thousand pairs will find weight vectors
that exploit corpus quirks. The CI check below catches most of it, not all.

Legally, under NYC LL144 and the EU AI Act you must be able to describe and justify the
decision logic of an automated employment tool. "An optimizer picked these numbers overnight
and nobody reviewed them" is not a defensible answer in an audit. Each weight change needs a
human-signed rationale, which `rationale.md` exists to draft.

### 5.3 Overfitting guards

```
1. Tuning runs on dev only. Holdout stays encrypted.
2. Accept a proposal only if the improvement CI excludes zero AND the point
   estimate exceeds +0.01 NDCG. Smaller wins are not real.
3. Holdout evaluation budget: 10 per quarter, logged. Each look burns signal.
4. If dev gain > 2× holdout gain, flag OVERFIT_SUSPECTED and reject.
5. Rotate 20% of the golden set quarterly; retire what's been tuned against.
6. Track dev-holdout gap over time. A widening trend means the dev set is exhausted
   and needs refreshing, regardless of individual run results.
```

Guard 4 is the one that will actually catch you. Watch it.

---

## 6. Production feedback — the real "on the fly"

Offline eval tells you about yesterday's data. This closes the loop.

**Shadow scoring.** Every candidate production scores with both the live config and the
candidate config. Only live is shown. Compare distributions over a week before promoting
anything.

**Outcome ingestion.** Nightly job joins scores to ATS outcomes (advanced / rejected /
hired / 90-day retention), appends to the dev pool with a 30-day embargo so recent decisions
don't leak into tuning before outcomes settle.

**Drift alarms** — page on any of these:

```
Taxonomy coverage              < 92%     (SPEC §16)
Abstention rate               > 20% or 2× 30-day baseline
Score distribution PSI        > 0.25 vs. 30-day baseline
Four-fifths ratio, any group  < 0.80
p95 latency                   > 300 ms
```

**Quarterly recalibration.** Refit the isotonic curve (SPEC §7) on the last two quarters,
version it, A/B against the incumbent, keep rollback available.

---

## 7. Working agreement for Claude Code

- **Run `make eval` before claiming anything works.** Paste the summary table into the PR.
  "Tests pass" is not a result; the metric table is.
- **Never read, decrypt, or reconstruct `eval/corpora/holdout/labels.enc`.** If a task seems
  to require it, stop and ask.
- **Never commit a tuned `weights.yaml` directly.** Open a proposal under `proposals/`.
- **Report regressions even when the headline metric improves.** A +0.03 NDCG that breaks
  career-changers is a bad trade, and only `regressions.md` reveals it.
- **Treat a guardrail failure as terminal.** Do not tune around a fairness failure, do not
  adjust the threshold, do not exclude the failing group. Stop and surface it.
- **Add a mutation test for every bug fixed.** The suite should grow monotonically.
- **Never report a delta without its CI.** If the CI includes zero, call it noise in plain
  words rather than reporting it as an improvement.

### Build order

1. Layer 1 property tests + `make eval-fast` (half a day, immediate value)
2. Synthetic generator + 500 profiles (the big unlock — do this before labeling anything)
3. Mutation suite from the §2.3 table
4. Report writer, baseline diffing, bootstrap CIs
5. Golden set of 200 human-labeled pairs, dual-labeled with agreement measured
6. Tuning loop with constraints and proposal output
7. Holdout encryption, CI integration, evaluation budget ledger
8. Production shadow scoring and drift alarms

Steps 1–4 require no human labels at all and will find most of your bugs. Start there.
