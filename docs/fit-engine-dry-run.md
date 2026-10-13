# Fit engine dry run on real data (2026-10-13)

Task 9.6 of the match-engine integration: score the résumés and active jobs held on the Neon
**Development** branch with the fit engine, in memory, and measure latency, abstention,
distribution and agreement with the old scorers. Aggregates only: no résumé or posting text, no
user ids, no emails, no company tied to a user.

## How it was run

- **Branch.** Neon project `divine-base-11638078`, branch `Development` only. The script refused
  any connection string that did not name the Development endpoint. Production was not touched.
- **Read-only, physically.** Connected with `options=-c default_transaction_read_only=on`, the
  session set read-only, and both `default_transaction_read_only` and `transaction_read_only`
  checked to be `on` before the first query. No statement wrote anything.
- **No app lifespan.** `backend.main` was never imported (its lifespan migrates the database), and
  `DATABASE_URL` pointed at a throwaway SQLite file for the process, so nothing could reach the
  app's own database. No committing function was called (`postings_for`, `bank_scores`,
  `explain_job`): postings were parsed with `parse_posting` and pairs scored with the engine's
  `score`, in memory.
- **Calibration.** The shipped panel only (rebuilt in this task, `panel-bed969e57383`); the
  Development branch has no live references.
- **Engine.** Version 1.0.0, combined config hash `48335e3c488c5fe9`.
- **Sample.** Every user with a résumé (cap 25) against the newest 400 jobs the feed would show
  (not a duplicate, active or stale, a company, a description over 50 characters). The newest
  résumé per user.
- **Egress.** 405 rows, about 1.3 MB (almost all of it the 400 descriptions). Working files were
  kept outside the repository and deleted afterwards.

## What the Development branch holds

| | Count |
| --- | --- |
| Users with a résumé | **1** (cap was 25) |
| Active jobs | 5,920 (5,349 with a description over 50 characters) |
| Rows in `job_match_scores` | **0** |
| Jobs with a stored AI `match_score` above 0 | 0 |
| Schema | The pre-engine one: `job_match_scores` has no `outcome`, `band` or `raw_score`; no `match_traces` |

So the sample is 1 résumé x 400 jobs = **400 pairs**. That is enough for latency and for the
posting side, but every résumé-side number below is one person's résumé, not a population.

## Latency

| Measure | Value |
| --- | --- |
| Cold engine build (after import) | 1.95 s |
| Import of the adapter | 0.87 s |
| Per pair, p50 | 2.44 ms |
| Per pair, p95 | 3.49 ms |
| Per pair, max | 6.53 ms |
| Posting parse, p50 / p95 (first time, then cached) | 47 ms / 79 ms |
| Résumé parse (once per résumé and month) | 0.7 s |

Local machine, warm disk. Earlier in the same session a first build took 5.9 s; Task 9.1
measured about 10 s on a cold process, which is what `docs/fit-engine.md` budgets for. Pair
latency is far under the EVAL section 6 alarm (p95 300 ms). Posting parses are slower than the
20 ms Task 9.1 measured on the corpus; at 79 ms p95, 500 new postings in one sweep chunk cost
about 25 to 40 s, which matters for the 300 s cron after a re-parse (1 January, or a parse-table
change).

## Outcomes

**Update, same day: the résumé-side cause was a legacy document, now handled (D-152).** The one
résumé was extracted before 2026-07-09, when upload extraction dropped every date from the stored
document although the raw text keeps them. The adapter now scores such a document from its raw
text when the engine's text parse finds a dated role (DECISIONS D-152, amending D-80). The same
sample, re-run read-only (403 rows, about 1.3 MB):

| Outcome | Before D-152 | After D-152 |
| --- | --- | --- |
| SCORED | 0 | **213** (53.25 %) |
| SCORED_LOW_CONFIDENCE | 0 | 147 (36.75 %) |
| NEEDS_REVIEW | 399 (99.75 %) | **39** (9.75 %) |
| GATE_FAIL | 1 | 1 |

After the fix the drift monitor's abstention rate (NEEDS_REVIEW plus SCORED_LOW_CONFIDENCE) is
46.5 %. That is still above the 20 % alarm, and it is now posting-driven: across the NEEDS_REVIEW
and low-confidence pairs the reasons are POSTING_TRUNCATED 136, LOW_DIMENSION_COVERAGE 107,
FEW_REQUIREMENTS 88, LOW_EVIDENCE_DENSITY 35, POSTING_UNSCORABLE 32, LOW_TAXONOMY_COVERAGE 16 and
LOW_CONFIDENCE 7. LOW_TEMPORAL_COMPLETENESS is gone. Bands: Limited 358, Partial 2. The display
score on the 213 SCORED pairs runs p10 0, p50 10, p90 34: one early-career résumé against the 400
newest jobs of every family, so low percentiles are expected. The tables below are the original,
pre-fix run.

| Outcome | Pairs | Share |
| --- | --- | --- |
| SCORED | 0 | 0 % |
| SCORED_LOW_CONFIDENCE | 0 | 0 % |
| NEEDS_REVIEW | 399 | **99.75 %** |
| GATE_FAIL | 1 | 0.25 % |

**Abstention rate: 99.75 %** (NEEDS_REVIEW; the drift monitor's rate, NEEDS_REVIEW plus
SCORED_LOW_CONFIDENCE, is the same). Far above the EVAL section 6 alarm of 20 %. Reasons on the
NEEDS_REVIEW pairs (a pair can carry several):

| Reason | Pairs | Side |
| --- | --- | --- |
| LOW_CONFIDENCE | 367 | summary of the ones below |
| LOW_TEMPORAL_COMPLETENESS | 367 | résumé: roles without dates |
| LOW_DIMENSION_COVERAGE | 339 | both |
| POSTING_TRUNCATED | 307 | posting: description cut at a stored-length cap |
| FEW_REQUIREMENTS | 101 | posting |
| LOW_EVIDENCE_DENSITY | 63 | résumé |
| LOW_TAXONOMY_COVERAGE | 41 | posting |
| POSTING_UNSCORABLE | 32 | posting: no requirement found |

Two causes dominate, one on each side:
1. **The one résumé has undated roles** (LOW_TEMPORAL_COMPLETENESS on every pair that reached the
   confidence step). This is the person's résumé, not the catalogue: a résumé with dated roles
   would not carry it. It says nothing about other users, but it does show how a single missing
   field withholds every score for a person; the detail view's reasons tell them so.
2. **330 of the 400 descriptions (82 %) are truncated** at the old 2,000 or 3,000 character
   storage caps (posting flag POSTING_TRUNCATED; D-104 caps confidence for those). The
   Development catalogue is an older copy and production's ingest has changed since (the July 2026
   description work), so its share may well differ, but this dry run could not check production
   (forbidden). **Measure it on production before deploy** with a read-only count of
   descriptions whose length is within 12 characters of 2,000 or 3,000 and that end in "..." or
   "…".

Posting flags over the 400 postings: POSTING_TRUNCATED 330, POSTING_UNSCORABLE 32,
CONTRADICTORY_REQUIREMENTS 28, REQUIREMENTS_TRUNCATED 1.

## Bands and scores

No pair was SCORED or SCORED_LOW_CONFIDENCE, so there is **no band or display-score
distribution** to report. The raw score (computed for the 367 pairs that reached scoring before
the engine withheld it): p10 0.21, p50 0.38, p90 0.71.

## Gates

GATE_FAIL by gate kind: LICENSE 1. No work authorisation, clearance or location failures.

## Agreement with the old scorers

**Not measurable on this branch.** `job_match_scores` holds no rows at all on Development (no
`local1:` rows from the old local scorer and no AI rows), and no job has a stored AI
`match_score`. Spearman correlation and the band-agreement table (old cut-offs 80 / 60 mapped to
Strong / Good / Partial) were computed by the script but have zero shared pairs. Production holds
the old rows; comparing there needs the owner's go-ahead for a read-only run.

## Top 10 unresolved posting tokens

Requirement words the taxonomy did not resolve, by the number of postings naming them:

| Token | Postings |
| --- | --- |
| STEM | 8 |
| training | 6 |
| networking | 6 |
| GTM | 6 |
| SpaceX | 5 |
| DoD | 3 |
| monitoring | 3 |
| LOB | 3 |
| scripting | 3 |
| ZoomInfo | 3 |

"SpaceX" is an employer name inside a requirement clause; "ZoomInfo" is a sales tool and a
taxonomy candidate; "STEM", "GTM", "DoD" and "LOB" are acronyms for the review
queue.

## What this means for the deploy

- Latency is fine: about 2.5 ms a pair, and a cold build well inside the budget.
- The abstention rate on this sample (99.75 % before D-152, 46.5 % with low confidence included after it) would page the drift alarm on day one. Before deploy, check
  on production (read-only) how many descriptions are truncated, and expect "We couldn't score
  this" on most cards for any user whose roles have no dates and for every truncated posting.
- The Development schema has not been migrated: the Postgres migration smoke test (an owner
  action in `docs/fit-engine.md`) is still to do.
- Agreement with the old scorers needs production data and remains unmeasured.
