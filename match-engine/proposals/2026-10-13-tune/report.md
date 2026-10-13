# Tuning report, 2026-10-13

> **synthetic-only: do not adopt.** Tuned on the synthetic split only (no golden or dev labels exist). The synthetic labels are constructed fit levels, not human judgements or outcomes.

> **fairness not assessed: proposal cannot be adopted until a grouped labelled set exists.** The four-fifths guardrail needs groups of n >= 100; these pairs carry no group (DECISIONS D-119).

## Search

- Split: synthetic (500 pairs, 40 postings)
- Objective: NDCG@10 (maximise)
- Sampler: Optuna TPE, seed 0, budget 50 trials
- Trials: 50; rejected before scoring (a weight above 0.45): 0; infeasible on abstention > 15%: 0; infeasible on fairness FAIL: 0; feasible: 50
- Search time 40.0 s; wall time including sensitivity 55.4 s

## Current vs proposed

| Metric | Current | Proposed | Δ | 95% CI | Verdict |
|---|---:|---:|---:|---|---|
| NDCG@10 | 0.991 | 0.994 | +.002 | [-.002, +.009] | NOISE |
| Precision@10 | 0.632 | 0.632 | +.000 | [+.000, +.000] | NOISE |
| Abstention rate | 9.0% | 9.0% | +0.0% | [+0.0, +0.0] | NOISE |
| AUC (advanced = fit >= 2) | 0.984 | 0.983 | -.002 | [-.004, +.001] | NOISE |
| Band exact | 47.3% | 44.6% | -2.6% | [-5.7, +0.2] | NOISE |
| Band off-by-one | 51.4% | 53.6% | +2.2% | [-0.7, +5.3] | NOISE |
| Band off-by-two | 1.3% | 1.8% | +0.4% | [-0.4, +1.3] | NOISE |
| ECE (10 bins) | 0.244 | 0.231 | -.013 | [-.027, +.000] | NOISE |
| Abstention precision | 0.0% | 0.0% | +0.0% | [+0.0, +0.0] | NOISE |

Bootstrap: 1000 resamples over postings. A CI that includes zero is noise.

## Constraints on the proposed vector (EVAL 5.1)

- Weights in [0.03, 0.45], sum 1: yes
- Abstention rate 0.090 (max 0.15)
- Four-fifths: not assessed (no group with n >= 100 (pairs carry no group))
- Current weights: feasible

## Accept rules (EVAL 5.3)

- Rule 2 (CI excludes 0 and point > +0.01): FAIL: NDCG@10 gain +0.0021, 95% CI [-0.0020, +0.0086]; the improvement is not real (noise or too small)
- Rule 4 (dev gain > 2x holdout gain): not assessed: the holdout is evaluated only in CI on a release branch (`make eval-holdout PROPOSAL=<folder>`)
- synthetic-only: do not adopt
- fairness not assessed: proposal cannot be adopted until a grouped labelled set exists
- Verdict: REJECT

## Best feasible trials

| Trial | NDCG@10 | Abstention | required_skills | preferred_skills | experience | seniority | domain | education | tools |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 46 | 0.9936 | 0.090 | 0.2538 | 0.1910 | 0.2116 | 0.0373 | 0.1898 | 0.0362 | 0.0803 |
| 29 | 0.9934 | 0.090 | 0.2456 | 0.1504 | 0.3478 | 0.0648 | 0.0943 | 0.0367 | 0.0604 |
| 43 | 0.9933 | 0.090 | 0.2665 | 0.1733 | 0.3011 | 0.0723 | 0.0902 | 0.0323 | 0.0643 |
| 38 | 0.9931 | 0.090 | 0.2611 | 0.1912 | 0.2731 | 0.0537 | 0.1116 | 0.0310 | 0.0783 |
| 47 | 0.9928 | 0.090 | 0.1797 | 0.1977 | 0.3397 | 0.0493 | 0.1222 | 0.0304 | 0.0810 |
| 13 | 0.9928 | 0.090 | 0.2171 | 0.2123 | 0.3390 | 0.0576 | 0.1001 | 0.0305 | 0.0434 |
| 18 | 0.9924 | 0.090 | 0.2026 | 0.1709 | 0.2621 | 0.0448 | 0.2165 | 0.0328 | 0.0703 |
| 30 | 0.9924 | 0.090 | 0.1709 | 0.2365 | 0.3472 | 0.0696 | 0.1019 | 0.0433 | 0.0306 |
| 28 | 0.9923 | 0.090 | 0.2386 | 0.1947 | 0.3073 | 0.0749 | 0.0937 | 0.0554 | 0.0354 |
| 45 | 0.9919 | 0.090 | 0.2983 | 0.1389 | 0.2769 | 0.0852 | 0.0941 | 0.0375 | 0.0691 |
