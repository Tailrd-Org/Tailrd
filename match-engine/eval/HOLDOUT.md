# The holdout (EVAL.md layer 4)

The holdout is the only measurement that tells whether the engine is useful, and it stays useful
only while nobody tunes against it (EVAL section 0). This file is its format and procedure. The
tooling is `eval/holdout.py`; the look budget is `docs/evaluation_budget.md`.

## Rules

1. Nothing is ever committed under `eval/corpora/holdout/`. The folder is git-ignored;
   `scripts/guard_holdout.py` rejects staged changes locally, and the CI job `holdout-guard` runs it
   over every path touched by any commit of a pull request or release push (so adding a file in one
   commit and deleting it in the next is still caught). The guard is never bypassed.
2. The labels are decrypted only in CI, only on `release/**` branches, only by `make eval-holdout`,
   at most 10 times per calendar quarter (UTC) and at most once per commit.
3. Claude Code and every developer machine never hold the key. Locally, `make eval-holdout` prints
   `HOLDOUT: skipped (no key)` and exits 0.
4. Only aggregate metrics leave the CI runner: NDCG@10 with its CI, AUC, the abstention rate and
   the four-fifths ratio. No per-pair score, label or regression list is printed, and a malformed
   input is reported by its index and exception type only, never its content.

## Repository settings the owner must apply

These are GitHub settings, not files, so they are listed here. The physical guarantees of EVAL 0
depend on them.

1. **Environment `holdout`** (Settings, Environments):
   - `MATCH_HOLDOUT_KEY` and `MATCH_HOLDOUT_BUNDLE_URL` are **environment secrets** of `holdout`.
     Never add either as a repository or organisation secret; a same-repository pull request that
     edits the workflow could read a repository secret.
   - Deployment branches: selected branches only, pattern `release/**`.
   - Required reviewers: at least one person who did not author the release.
2. **Required status check** `holdout-guard` on `main` and `release/**` (branch protection or a
   ruleset), so a pull request that touches the holdout path cannot merge.
3. **Branch `holdout-ledger`**: protect it against deletion and force pushes (allow the workflow's
   fast-forward pushes). It holds `evaluation_budget.ledger.csv`, the durable look ledger.
4. Allow GitHub Actions to push (Settings, Actions, Workflow permissions); the `holdout` job asks
   for `contents: write` only to push the ledger row.

## Bundle format

The bundle lives outside this public repository (the pairs are real, anonymised résumés,
DECISIONS D-04). CI downloads it into the runner from `MATCH_HOLDOUT_BUNDLE_URL` (a private,
authenticated location, for example a short-lived signed URL) as a `.tar.gz`:

```
pairs/pairs.jsonl   inputs, one per line:
                    {"pair_id", "posting_id", "as_of", "resume", "posting"}
                    resume = matcher.models.Resume JSON, posting = matcher.models.JobPosting JSON
labels.enc          the labels, encrypted (below)
```

The plaintext of `labels.enc` is JSONL, one line per pair:

```
{"pair_id": "h0001", "outcome": "advanced", "advanced": true, "group": "g1"}
```

* `outcome`: `hired`, `advanced`, `rejected` (or another non-advancing outcome). Relevance for
  NDCG is 3 for hired, 2 for advanced, 0 otherwise; `advanced` is true for hired and advanced.
* `group` (optional): an opaque fairness group id, as in the golden set. With groups of n >= 100
  the four-fifths guardrail is assessed; a FAIL exits 1 and is terminal.

Anonymisation follows the golden set's README: no names, contact details, photos or links.

## Encryption

AES-256-GCM (`cryptography`'s `AESGCM`). File layout:

```
"MHO1" (4 bytes, also the associated data) | nonce (12 random bytes) | ciphertext + 16-byte tag
```

The key is 32 random bytes, base64 (standard or URL-safe; any other character is refused). A wrong
key or a modified file fails decryption; a key that is set but malformed is refused, never treated
as absent. In the workflow the key is in the `env:` of the `make eval-holdout` step only, so package
installs, downloads and other actions never see it.

## The look ledger

The durable ledger is `evaluation_budget.ledger.csv` on the branch `holdout-ledger` of the
repository, shared by every release branch (a row written while evaluating `release/1.2` counts
for `release/1.3`). Columns: `date,ref,sha,purpose`. `docs/evaluation_budget.ledger.csv` in the
source tree is only the format reference.

`make eval-holdout` claims a look before it decrypts anything:

1. It fetches `holdout-ledger` fresh (the branch may not exist yet: the first look creates it).
2. It refuses (exit 3) when the quarter already has 10 rows, or when a row already names this
   commit SHA (a re-run or a re-push of the same release candidate).
3. It commits the new row on the fetched tip and pushes it as a fast-forward. A concurrent claim
   makes the push fail.
4. Only after the push succeeds does it decrypt.

If the branch cannot be read or the push fails, the run is refused (exit 2) and nothing is
decrypted. A release run is never cancelled by a newer push (`cancel-in-progress` is off for
`release/**`), so a recorded look is not lost without a result.

## Procedure

Creating or refreshing the holdout (the label owner, on a machine outside this repository):

1. `python -m eval.holdout keygen` once; store the output as the `holdout` environment secret
   `MATCH_HOLDOUT_KEY`.
2. Write the plaintext labels JSONL, then
   `MATCH_HOLDOUT_KEY=... python -m eval.holdout encrypt --in labels.jsonl --out labels.enc`.
   Delete the plaintext. (`encrypt` refuses an output under `config/` or `eval/corpora/holdout/`.)
3. Pack `pairs/pairs.jsonl` and `labels.enc` into a `.tar.gz`, upload it to the private store and
   set the environment secret `MATCH_HOLDOUT_BUNDLE_URL`.

Evaluating a release candidate (CI, `release/**` push):

1. The workflow runs `make test` and `make eval-fast` first; the holdout job runs only when both
   pass, so a candidate with a failing guardrail never spends a look.
2. A reviewer approves the `holdout` deployment.
3. `make eval-holdout` checks the ref, claims the look on `holdout-ledger`, decrypts in memory and
   prints the aggregates. `make eval-holdout PROPOSAL=proposals/<date>-tune` also scores the
   proposal's weights and applies EVAL 5.3 rule 4 (dev gain > 2x holdout gain:
   `OVERFIT_SUSPECTED`, reject).

Exit codes of `python -m eval.holdout evaluate`: 0 done or skipped, 1 fairness FAIL, 2 refused
(not a release ref, bad key, unreadable ledger, failed push, missing or malformed file), 3 the
quarter's budget is spent or this commit was already evaluated.
