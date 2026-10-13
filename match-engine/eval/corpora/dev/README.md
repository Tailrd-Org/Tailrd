# Dev pool (EVAL.md sections 5 and 6)

Pairs with downstream outcome labels (advanced, rejected, hired, 90-day retention) joined from the
ATS. Tuning (`make tune --split=dev`) runs on this pool only; the holdout stays encrypted.

Everything here except this README is git-ignored (public repository, DECISIONS D-04). Outcome
ingestion appends to the pool with a 30-day embargo, so recent decisions do not leak into tuning
before their outcomes settle (EVAL section 6).

Expected layout, written by the outcome-ingestion job (Task 8.x):

```
pairs.jsonl      {"pair_id", "posting_id", "as_of", "resume", "posting", "advanced", "outcome",
                  "decided_at"}  one per line; resume and posting as engine model JSON
manifest.json    source, extraction date, embargo cut-off, counts
```

The same anonymisation rules as the golden set apply: no names, contact details, photos or links.
