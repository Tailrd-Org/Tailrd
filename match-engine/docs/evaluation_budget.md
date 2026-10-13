# Evaluation budget (EVAL.md section 5.3)

Every look at the holdout burns signal: once a decision depends on a holdout number, the holdout
has been tuned against a little. The budget keeps that cost visible and bounded.

## The rules

| Rule | Value | Enforced by |
|---|---|---|
| Holdout looks per calendar quarter | 10 | `eval/holdout.py` refuses the 11th (exit 3) before decrypting |
| Where a look may happen | CI, `release/**` push, `make eval-holdout` | the workflow's `holdout` job; `evaluate` refuses other refs (exit 2) |
| What is logged | one row per decrypting run: `date,ref,purpose` | `docs/evaluation_budget.ledger.csv`, appended before decryption |
| Tuning split | dev (or synthetic while no dev labels exist), never the holdout | `eval/tune/search.py` has no holdout code path |
| Accepting a proposal | NDCG@10 gain CI excludes 0 and point > +0.01 | `eval/tune/proposal.py`, printed in `report.md` |
| Overfitting | dev gain > 2x holdout gain: `OVERFIT_SUSPECTED`, reject | `make eval-holdout PROPOSAL=...` |

A look counts whether or not its result is used, and whether or not decryption succeeds once the
row is written. Quarters are calendar quarters (January to March, and so on), by the UTC date of
the run.

## The ledger

`docs/evaluation_budget.ledger.csv`, committed, CSV with a header:

```
date,ref,purpose
2027-01-14,release/1.2,release candidate evaluation
```

The CI runner appends the row to its own checkout and uploads the file as the
`evaluation-budget-ledger` artifact. The release manager commits that row to the release branch the
same day; the next run counts only committed rows. A missing row is a process failure: add it with
the run's date and ref as soon as it is noticed, never leave it out.

## Tracking the dev-holdout gap (EVAL 5.3 rule 6)

Each holdout run with `PROPOSAL=...` prints the dev gain and the holdout gain. Record both in the
release notes. A widening gap over successive releases means the dev pool is exhausted and needs
refreshing, regardless of individual runs.

## Today

No golden, dev or holdout labels exist yet. Tuning runs on the synthetic split and every proposal
says "synthetic-only: do not adopt" and "fairness not assessed" (DECISIONS D-119). The ledger is
empty.
