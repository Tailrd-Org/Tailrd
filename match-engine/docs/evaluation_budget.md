# Evaluation budget (EVAL.md section 5.3)

Every look at the holdout burns signal: once a decision depends on a holdout number, the holdout
has been tuned against a little. The budget keeps that cost visible and bounded.

## The rules

| Rule | Value | Enforced by |
|---|---|---|
| Holdout looks per calendar quarter (UTC) | 10 | `eval/holdout.py` claims each look on the `holdout-ledger` branch before decrypting and refuses the 11th (exit 3) |
| Looks per release candidate | 1 per commit SHA | the same claim refuses a SHA already in the ledger (exit 3) |
| Where a look may happen | CI, `release/**` push, `make eval-holdout` | the `holdout` environment (release/** deployments, required reviewer) and `evaluate`'s ref check (exit 2) |
| What is logged | one row per look: `date,ref,sha,purpose` | pushed to `holdout-ledger` before decryption; a failed push refuses the run |
| Tuning split | dev (or synthetic while no dev labels exist), never the holdout | `eval/tune/search.py` has no holdout code path |
| Accepting a proposal | NDCG@10 gain CI excludes 0 and point > +0.01 | `eval/tune/proposal.py`, printed in `report.md` and at the top of `weights.diff` |
| Overfitting | dev gain > 2x holdout gain: `OVERFIT_SUSPECTED`, reject | `make eval-holdout PROPOSAL=...` |

A look counts once its row is pushed, whether or not its result is used or decryption succeeds.

## The ledger

The durable ledger is `evaluation_budget.ledger.csv` on the branch `holdout-ledger`, shared by every
release branch, so the budget cannot be split across `release/1.2` and `release/1.3`. The workflow
writes it; nobody edits it by hand. Inspect it with
`git show origin/holdout-ledger:evaluation_budget.ledger.csv`.

```
date,ref,sha,purpose
2027-01-14,release/1.2,3f1c...e9,release candidate evaluation
```

`docs/evaluation_budget.ledger.csv` in the source tree holds only the header: the format reference.
The branch must be protected against deletion and force pushes (eval/HOLDOUT.md, "Repository
settings").

## Tracking the dev-holdout gap (EVAL 5.3 rule 6)

Each holdout run with `PROPOSAL=...` prints the dev gain and the holdout gain. Record both in the
release notes. A widening gap over successive releases means the dev pool is exhausted and needs
refreshing, regardless of individual runs.

## Today

No golden, dev or holdout labels exist yet. Tuning runs on the synthetic split and every proposal
says "synthetic-only: do not adopt" and "fairness not assessed" (DECISIONS D-119). The ledger branch
does not exist yet; the first look creates it.
