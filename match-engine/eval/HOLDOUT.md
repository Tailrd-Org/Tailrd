# The holdout (EVAL.md layer 4)

The holdout is the only measurement that tells whether the engine is useful, and it stays useful
only while nobody tunes against it (EVAL section 0). This file is its format and procedure. The
tooling is `eval/holdout.py`; the look budget is `docs/evaluation_budget.md`.

## Rules

1. Nothing is ever committed under `eval/corpora/holdout/`. The folder is git-ignored, and
   `scripts/guard_holdout.py` rejects any commit or pull request that touches it (the CI job
   `holdout-guard` runs it on every pull request). The guard is never bypassed (`--no-verify` is
   not an option for this path).
2. The labels are decrypted only in CI, only on `release/**` branches, only by
   `make eval-holdout`, and at most 10 times per calendar quarter.
3. Claude Code and every developer machine never hold the key. Locally, `make eval-holdout`
   prints `HOLDOUT: skipped (no key)` and exits 0.
4. Only aggregate metrics leave the CI runner: NDCG@10 with its CI, AUC, the abstention rate and
   the four-fifths ratio. No per-pair score, label or regression list is printed.

## Bundle format

The bundle lives outside this public repository (the pairs are real, anonymised résumés,
DECISIONS D-04). CI downloads it into the runner from the secret URL `MATCH_HOLDOUT_BUNDLE_URL`
(a private, authenticated location, for example a short-lived signed URL) as a `.tar.gz`:

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

The key is 32 random bytes, base64 (standard or URL-safe), stored only as the GitHub secret
`MATCH_HOLDOUT_KEY` (environment `holdout`). A wrong key or a modified file fails decryption; a
key that is set but malformed is refused, never treated as absent.

## Procedure

Creating or refreshing the holdout (the label owner, on a machine outside this repository):

1. `python -m eval.holdout keygen` once; store the output as the `MATCH_HOLDOUT_KEY` secret.
2. Write the plaintext labels JSONL, then
   `MATCH_HOLDOUT_KEY=... python -m eval.holdout encrypt --in labels.jsonl --out labels.enc`.
   Delete the plaintext.
3. Pack `pairs/pairs.jsonl` and `labels.enc` into a `.tar.gz`, upload it to the private store and
   set `MATCH_HOLDOUT_BUNDLE_URL`.

Evaluating a release candidate (CI, `release/**` push):

1. The workflow runs `make test` and `make eval-fast` first; the holdout job runs only when both
   pass, so a candidate with a failing guardrail never spends a look.
2. `make eval-holdout` checks the ref (`release/...`), checks the quarter's budget, appends
   `date,ref,purpose` to `docs/evaluation_budget.ledger.csv`, decrypts in memory and prints the
   aggregates. `make eval-holdout PROPOSAL=proposals/<date>-tune` also scores the proposal's
   weights and applies EVAL 5.3 rule 4 (dev gain > 2x holdout gain: `OVERFIT_SUSPECTED`, reject).
3. The job uploads the ledger as an artifact. Commit its new row to the release branch the same
   day (`docs/evaluation_budget.md`), so the next run counts it.

Exit codes of `python -m eval.holdout evaluate`: 0 done or skipped, 1 fairness FAIL, 2 refused
(not a release ref, bad key, missing or corrupt file), 3 the quarter's budget is spent.
