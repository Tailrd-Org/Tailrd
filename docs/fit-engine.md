# Job scoring: the fit engine

Every job score Tailrd shows (feed cards, "best match" sort, the Strong-match filter, the
sidebar, the match breakdown, the tailoring before/after and the alert emails) comes from the
deterministic fit engine in `match-engine/` (package `matcher`). No AI is called, waited on or
paid for to score a job. The same résumé, posting, config and date always give the same result.

The backend touches the engine in one place, `backend/services/fit_engine.py`. Request paths
that only read banked scores use `backend/services/fit_reads.py`, which imports nothing from the
engine. Design decisions are in `match-engine/docs/DECISIONS.md` (D-80 onward for the
integration, D-150 for shadow scoring). The dry run against real data is in
[fit-engine-dry-run.md](fit-engine-dry-run.md).

## How a job is scored

1. **Résumé.** The stored résumé document (or its raw text, when the document is empty) is
   parsed into roles, dated skills, education and credentials. The saved application answers
   (work authorisation, sponsorship, location, clearance) fill in what the résumé does not say.
   A bare "United States" settings location is the column default, never read as a residence.
2. **Posting.** Title, description and the job's columns are parsed into required, preferred and
   implied requirements, seniority, location and work type. The parse is cached in
   `scraped_jobs.match_terms` and redone only when the description length, the parse tables, the
   config or the year changes.
3. **Gates.** Work authorisation, licence, security clearance and location. A failed gate is
   `GATE_FAIL`: "Not eligible", with the reason, and never a number.
4. **Score.** Seven weighted dimensions (required skills, preferred skills, experience,
   seniority, domain, education, tools), with caps for critical misses. The raw score is
   calibrated to a percentile against a reference group: Tailrd's own users once a job family has
   30 distinct people, else the shipped synthetic panel (`match-engine/data/calibration/`).
5. **Confidence.** Evidence density, dated roles, taxonomy coverage, requirement count and
   dimension coverage. Too little confidence and the engine withholds the number.

Each result has one outcome:

| Outcome | Shown as | Stored `score` |
| --- | --- | --- |
| `SCORED` | Band and number ("Good match", 76). The card shows the band only | the display score |
| `SCORED_LOW_CONFIDENCE` | Band only, with the reasons | NULL |
| `NEEDS_REVIEW` | "We couldn't score this", with the reasons | NULL |
| `GATE_FAIL` | "Not eligible", with the gate's reason | NULL |

Bands: Strong 85+, Good 70+, Partial 55+, Limited below. Alerts go out only for a `SCORED`
Strong match. A NULL score is never sorted, averaged or thresholded as a number.

Every banked score has an audit trace in `match_traces`, keyed by an opaque id derived from the
user id and `FIT_TRACE_SALT` (the trace never holds the user id).

## Where scoring runs

- **Cron sweep** (`match_notifier.sweep_match_alerts`, run by the cron-poll job): scores the
  newest visible jobs for each eligible user in chunks of 500, banks them, sends Strong-match
  alerts, then refreshes the live calibration references and computes the drift report.
- **Résumé upload:** pre-scores the 300 newest visible jobs in a background thread.
- **Match breakdown** (opening a job): one full result with explanation, interval and
  counterfactual.
- **Feed, sidebar, job list:** read banked rows only. They bank a page inline only when this
  process has already built the engine; they never build it.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `FIT_TRACE_SALT` | none, **required** | Secret, at least 16 bytes. Only the two trace writers read it. Without it nothing is banked (no score without its trace) and every sweep reports `fit_trace_salt_missing: true`. Changing it orphans existing traces (they can no longer be found for deletion), so set it once |
| `FIT_SHADOW_CONFIG` | unset | A directory with the layout of `match-engine/config` (the six YAML files and `engine_version.txt`). When set, the sweep scores every chunk again with that config and reports the difference in `summary.shadow`. Nothing of the shadow pass is persisted. See "Shadow scoring" below |
| `MATCH_ALERTS_ENABLED` | on | `false` turns the sweep off |
| `CRON_MATCH_JOBS_PER_USER` | `2000` | Newest visible jobs scored per user per run |
| `CRON_MATCH_MAX_USERS` | `25` | Users per run, least recently scored first |
| `CRON_MATCH_BUDGET_S` | `120` | Wall-clock box for the sweep's scoring (shadow pass included); `0` disables |
| `FIT_ANALYSIS_PER_MINUTE` / `FIT_ANALYSIS_PER_DAY` | `20` / `1000` | Per-user limit on the tailoring analysis routes |

## Owner actions before deploy

1. **Set `FIT_TRACE_SALT`** on Vercel (Production and Preview), a random secret of at least
   16 bytes, for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Never
   commit it. Without it the feed shows no scores.
2. **Smoke-test the Postgres migration on the Neon Development branch** (never main). The
   migration (`backend/migrations/add_fit_engine.py`) adds seven nullable columns to
   `job_match_scores`, drops NOT NULL on `score`, and the app's `create_all` creates
   `match_traces`, `match_calibration` and `match_fingerprints`. It is catalog-only and
   additive, but it has only been verified against SQLite and the recorded SQL. Start the app
   once against Development, then check the columns and tables exist. The 2026-10-13 dry run
   found Development still on the old schema (no `outcome` column, no `match_traces`), so this
   has not been done yet.
3. **Rebuild the Chrome extension** (`chrome-extension`, `npm run build`) so the tailor card
   shows the engine's before/after band and reasons (Task 9.3). The extension is a separate
   local build and Store upload.
4. **Configure the holdout repository settings** in `match-engine/eval/HOLDOUT.md` ("Repository
   settings the owner must apply"): the `holdout` environment with `MATCH_HOLDOUT_KEY` and
   `MATCH_HOLDOUT_BUNDLE_URL` as environment secrets, release branches only and a required
   reviewer; `holdout-guard` as a required status check; protection for the `holdout-ledger`
   branch.
5. **Fix the package-lock drift.** `npm ci` fails because a `package-lock.json` does not match
   its `package.json`: Task 9.4 hit it in `frontend/` (esbuild 0.28.2 missing from the lock) and
   Task 9.3 saw the same kind of drift in `chrome-extension/`. Run `npm install` once in each,
   check the diff and commit the lock files, so CI and fresh checkouts install cleanly. The drift
   predates the match-engine work.

## Costs

- **Money:** none per score. No LLM call of any kind.
- **Cold start:** about 10 s per process on Vercel (import plus building the engine: config,
  taxonomy, resolver, title indexes, the verified panel). Measured locally at 2 to 6 s. Only the
  sweep, the upload pre-score and a breakdown pay it.
- **CPU:** about 2.5 ms per (résumé, job) pair (p95 about 3.5 ms) and 20 to 80 ms to parse a
  posting the first time.
- **Storage:** about 2 KB of trace per (user, job) pair plus a score row. At 2,000 jobs per user
  that is about 4 MB per user, and it grows with the catalogue. **There is no pruning yet**:
  traces are overwritten in place when a job is rescored, but never deleted when a job is
  removed. A retention or pruning policy is an owner decision still to make.
- **Monthly rescore:** scores are "as of" the first of the month, so every user is rescored once
  a month; every cached posting is re-parsed once a year (1 January).

## The sweep summary

`sweep_match_alerts` returns (and logs) one summary per run:

| Key | Meaning |
| --- | --- |
| `status` | `completed`, `disabled`, `fit_trace_salt_missing` or `engine_unavailable` |
| `scoring` | Always `fit-engine` |
| `alert_band` | The band alerts are sent for (`Strong match`) |
| `users_scanned` | Users whose window was scored this run |
| `users_notified` / `jobs_notified` | Alert emails sent, and the jobs in them |
| `jobs_scored` | Pairs newly scored and banked (current rows are skipped) |
| `scoring_errors` | Chunks that failed for another reason (logged by class, retried next run) |
| `chunks_skipped` | Chunks rolled back because a concurrent sweep banked them first |
| `scoring_budget_spent` | True when `CRON_MATCH_BUDGET_S` stopped scoring early |
| `fit_trace_salt_missing` | True when the salt is missing or short: nothing was banked |
| `error` | The engine error class, with `status: engine_unavailable` |
| `calibration` | The live reference refresh: `version`, and per family and globally `n_candidates`, `n_pairs`, `in_charge` (30+ people). `skipped: no scores` when nothing could be banked |
| `drift` | The EVAL section 6 drift report: `alarms` (code, value, threshold, message), `skipped` (checks not evaluated and why, for example a PSI on fewer than 200 scores), `psi_reference_n`, `psi_current_n`, `abstention_rate`, `abstention_baseline`, `not_measured` (coverage, four-fifths and p95 latency are not measured by the sweep) |
| `shadow` | Only with `FIT_SHADOW_CONFIG`; see below |

Alarms are logged at WARNING and skipped checks at INFO, so a skip is never silent.

## Shadow scoring

Set `FIT_SHADOW_CONFIG` to a candidate config directory to compare it with the live config on
real traffic before promoting it (EVAL section 6: compare a week of distributions first). The
shadow pass scores the same jobs the live pass just banked, in memory, and adds:

| `shadow` key | Meaning |
| --- | --- |
| `status`, `error` | `ok`, or `error` with the reason when the directory or config could not be loaded (the live sweep is never affected) |
| `config_hash` | The candidate's combined config hash |
| `postings` | `live parse`: postings are not re-parsed with the candidate (D-150) |
| `jobs` | Jobs compared |
| `outcomes` | `{live: {...}, shadow: {...}}` outcome histograms |
| `bands` | `{live: {...}, shadow: {...}}` band histograms |
| `score_delta` | Shadow minus live display score over jobs SCORED on both sides: `n`, `mean`, `p50` |
| `band_agreement` | `n` jobs with a band on both sides, `agree`, `rate` |
| `crossed_strong` | Jobs that became (`into`) or stopped being (`out_of`) an alertable Strong match, and `total` |
| `errors` | Chunks whose shadow pass failed |
| `skipped_deadline` | Jobs not shadow-scored because the scoring budget was spent |

Limits (D-150): the candidate changes the engine config only (weights, gates, bands, verbs,
negation, scoring); the taxonomy, parse tables and calibration panel are the live ones, and
postings keep their live parse. A change that works through posting parsing is judged offline
with `make eval`. The shadow pass counts against `CRON_MATCH_BUDGET_S` and builds its resolver
once per sweep (about 3 s). Tuned configs still go through a reviewed proposal under
`match-engine/proposals/`; shadow scoring is evidence for that review, not a way around it.

## Calibration panel

`match-engine/data/calibration/panel.json` is pinned by `MANIFEST.json` (sha256 and version)
and verified at every engine build. The MANIFEST records the config hash, graph version and
taxonomy MANIFEST hash it was built with. After a change to the config, the taxonomy or the
parsers, rebuild it from `match-engine/` with `python scripts/build_reference_panel.py --corpus
<postings.jsonl.gz>` (about 2.5 minutes; the corpus is not committed) and commit both files.

## Rollback

Revert the merge commit of the match-engine integration and redeploy, or promote the previous
production deployment in Vercel. The old AI scoring paths and the local scorer were deleted, not
switched off, so there is no flag to flip: a rollback is a redeploy of the previous commit.

The database change is additive (nullable columns, three new tables), so the previous code runs
against the migrated schema. Two things to know:
- engine rows store a NULL `score` for unscored outcomes; their `fit1:` fingerprints are never
  current for the old code, but check its readers on the Development branch before relying on a
  rollback;
- the new tables and columns stay behind and cost only storage; dropping them is a separate,
  deliberate step.
