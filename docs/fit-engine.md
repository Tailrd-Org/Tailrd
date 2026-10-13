# Job scoring: the fit engine

Every job score Tailrd shows (feed cards, "best match" sort, the Strong-match filter, the
sidebar, the match breakdown, the tailoring before/after and the alert emails) comes from the
deterministic fit engine in `match-engine/` (package `matcher`). No AI is called, waited on or
paid for to score a job. The same résumé, posting, config and date always give the same result.

The backend touches the engine in one place, `backend/services/fit_engine.py`. Request paths
that only read banked scores use `backend/services/fit_reads.py`, which imports nothing from the
engine. Design decisions are in `match-engine/docs/DECISIONS.md` (D-80 onward for the
integration, D-150 for shadow scoring, D-152 for the legacy-date fallback). The dry run against real data is in
[fit-engine-dry-run.md](fit-engine-dry-run.md).

## How a job is scored

1. **Résumé.** The stored résumé document (or its raw text, when the document is empty) is
   parsed into roles, dated skills, education and credentials. The saved application answers
   (work authorisation, sponsorship, location, clearance) fill in what the résumé does not say.
   A bare "United States" settings location is the column default, never read as a residence.
   A stored document whose roles all lost their dates (uploads extracted before 2026-07-09) is
   scored from its raw text when that text has a dated role, the profile was never edited in the
   app, and the text covers at least 80 % of the document's skills (D-152). The tailoring flow
   never does this: it scores each document as written.
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

## Privacy

The trace ids are linkable by anyone with production DB read access. Each trace row is written in
the same transaction as its score row in `job_match_scores` (which holds the user id), with the
same job and the same timestamp, and a user's set of scored jobs is close to unique on its own, so
`subject_id` can be mapped back to `user_id` without the salt. The salt-separation described in
`match-engine/docs/compliance.md` section 4 is therefore procedural, not technical, while traces sit
next to the score rows. No demographic data is collected today, so nothing sensitive can be joined.
This must be revisited before any demographic data is collected (a separate schema or role for
`match_traces` that serving code cannot read back, or a coarser `scored_at` on traces).

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
| `FIT_TRACE_SALT` | none, **required** | Secret, at least 16 bytes. Only the two trace writers read it. Without it nothing is banked (no score without its trace) and every sweep reports `fit_trace_salt_missing: true`. Changing it orphans existing traces (they can no longer be found for deletion), so set it once. Every environment that writes to the same database (Production and any Preview that shares the prod DB) must use the same value, or its traces land under another subject id |
| `FIT_SHADOW_CONFIG` | unset | A directory with the layout of `match-engine/config` (the six YAML files and `engine_version.txt`). When set, the sweep scores every chunk again with that config and reports the difference in `summary.shadow`. Nothing of the shadow pass is persisted. See "Shadow scoring" below |
| `MATCH_ALERTS_ENABLED` | on | `false` turns the sweep off |
| `CRON_MATCH_JOBS_PER_USER` | `2000` | Newest visible jobs scored per user per run |
| `CRON_MATCH_MAX_USERS` | `25` | Users per run, least recently scored first |
| `CRON_MATCH_BUDGET_S` | `120` | Wall-clock box for the sweep's scoring (shadow pass included); `0` disables |
| `FIT_ANALYSIS_PER_MINUTE` / `FIT_ANALYSIS_PER_DAY` | `20` / `1000` | Per-user limit on the tailoring analysis routes |
| `MATCH_BREAKDOWN_PER_MINUTE` / `MATCH_BREAKDOWN_PER_DAY` | `60` / `2000` | Per-user limit on `/ai/match-breakdown` (opening a job), a bucket of its own |

## Owner actions before deploy

1. **Set `FIT_TRACE_SALT`** on Vercel (Production and Preview), a random secret of at least
   16 bytes, for example `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Never
   commit it. Without it the feed shows no scores. Use the same value wherever the prod database
   is shared (a Preview that writes to it included).
2. **Smoke-test the Postgres migration on the Neon Development branch** (never main). The
   migration (`backend/migrations/add_fit_engine.py`) adds seven nullable columns to
   `job_match_scores`, drops NOT NULL on `score`, and the app's `create_all` creates
   `match_traces`, `match_calibration` and `match_fingerprints`. It is catalog-only and
   additive, but it has only been verified against SQLite and the recorded SQL. Start the app
   once against Development, then check the columns and tables exist. The 2026-10-13 dry run
   found Development still on the old schema (no `outcome` column, no `match_traces`), so this
   has not been done yet.
3. **Ship extension 0.5.1 together with the backend.** Build `chrome-extension` (`npm run
   build`) at version 0.5.1 and upload it to the Store; do not upload the 0.5.0 zip built
   earlier (31a4371), which predates the fix. The backend deploy and the 0.5.1 Store upload must
   go out together: the backend now returns a NULL score whenever a result is not `SCORED`, and
   the Store extension (0.4.0) formats that NULL as a number, so its tailor card would read
   "Match 0.0" and "ATS null · null% coverage", showing a withheld score as a zero and
   suggesting tailoring made the résumé worse. 0.5.1 shows the band or the reasons instead.
   Preferably let 0.5.1 go live before the backend push; otherwise accept that window.
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

## Decisions

Owner decisions taken on 2026-10-13. Each is one rule and its reason; the engine-side ones are
recorded in full in `match-engine/docs/DECISIONS.md`.

- **Trace retention (D-153).** The sweep deletes the traces, and their score rows, of closed jobs
  (any listing status but active or stale) scored more than 90 days ago: at most 5,000 per run, in
  its own transaction, inside the sweep deadline, reported as `traces_pruned`. Jobs that can still
  be applied to keep their traces. Reason: storage grows with every job ever shown, but a trace's
  audit value ends when the job closes.
- **Shipping with the M6 eval gate red: accepted (D-154).** `make eval-fast` still fails 4
  guardrails (format invariance p99 within and across templates and against the functional
  oracle, and an unbounded date error on 2 scanned-PDF renderings). The gate stays red and honest
  and is never loosened; in CI only its step continues on error, so a pull request is not blocked
  (`make test` still is, and on `release/**` a red gate still stops the holdout look). Reason: most
  users are scored from the stored document (D-80), not from file parsing; the remaining drift is
  OCR on scanned PDFs.
- **Taxonomy approvals audit (D-155).** HPO is withdrawn from the approved tokens and the curated
  aliases (the spelled-out forms stay); ASNT stays the testing domain; SparkTM stays out. The
  taxonomy and the calibration panel were rebuilt with the scripts (panel `panel-66e8ae79ee29`), so
  every banked row is rescored once after deploy. Reason: HPO is ambiguous (Human Phenotype
  Ontology or hyperparameter optimisation) and rare.
- **Over-qualification stays unpenalised (D-39 confirmed).** Being above a posting's level costs
  nothing (it raises an `OVERQUALIFIED` flag only). Reason: penalising surplus experience proxies
  for age, a protected attribute.
- **No default location.** A new `user_settings` row starts with an empty `location` instead of
  "United States"; existing rows are untouched (no data migration), and the engine still ignores
  that old default. Every reader (autofill, the extension profile sync, the profile and settings
  pages, the engine) falls through to the résumé or shows a blank. Reason: a default value posing
  as a user answer misstates location to employers and to the engine.

## Costs

- **Money:** none per score. No LLM call of any kind.
- **Cold start:** about 10 s per process on Vercel (import plus building the engine: config,
  taxonomy, resolver, title indexes, the verified panel). Measured locally at 2 to 6 s. Only the
  sweep, the upload pre-score and a breakdown pay it.
- **CPU:** about 2.5 ms per (résumé, job) pair (p95 about 3.5 ms) and 20 to 80 ms to parse a
  posting the first time.
- **Storage:** about 2 KB of trace per (user, job) pair plus a score row. At 2,000 jobs per user
  that is about 4 MB per user. Traces are overwritten in place when a job is rescored, and the
  sweep prunes closed jobs' traces (D-153): see "Decisions" below.
- **Memory:** the built engine adds about 175 MB of Python heap per instance (peak about 220 MB).
  Fine on 1 to 2 GB functions; recheck before lowering the function memory.
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
| `traces_pruned` | Traces deleted this run, with their score rows (D-153: closed jobs, scored more than 90 days ago, at most 5,000 per run) |
| `traces_prune_skipped` / `traces_prune_error` | Only when pruning did not run: `deadline` (the scoring budget was spent) or `fit_trace_salt_missing`; or the error class of a failed prune (rolled back, retried next run) |
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
once per sweep (about 3 s). The shadow sample is the jobs the sweep newly banks (current rows are
skipped), so in steady state a week of shadow summaries covers that week's new jobs plus the
monthly rescore, not the whole served distribution. Tuned configs still go through a reviewed proposal under
`match-engine/proposals/`; shadow scoring is evidence for that review, not a way around it.

## Calibration panel

`match-engine/data/calibration/panel.json` is pinned by `MANIFEST.json` (sha256 and version)
and verified at every engine build. The MANIFEST records the config hash, graph version and
taxonomy MANIFEST hash it was built with. After a change to the config, the taxonomy or the
parsers, rebuild it from `match-engine/` with `python scripts/build_reference_panel.py --corpus
<postings.jsonl.gz>` (about 2.5 minutes; the corpus is not committed) and commit both files.

The panel version is part of every résumé fingerprint, so a rebuilt panel makes every banked row
stale and the sweeps rescore them. The live references are not: once a family has 30 or more
people, its live reference (refreshed every sweep) calibrates new scores, while rows banked earlier
keep the display score they were banked with until they are rescored (at the latest, the monthly
rescore). In that window a freshly opened breakdown can show a slightly different number than the
card for the same job.

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
