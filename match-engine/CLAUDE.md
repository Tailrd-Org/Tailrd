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
