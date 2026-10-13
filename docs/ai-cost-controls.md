# AI cost controls

As of Task 9.2 of the match-engine integration, no job scoring uses AI. Every
match score, band, breakdown and alert comes from the deterministic fit engine
(`match-engine/`, through `backend/services/fit_engine.py`). Nothing in the app
calls OpenAI unless a person clicks something.

## What is free now

| Feature | Before | Now |
| --- | --- | --- |
| Match score for every job, every user (cron sweep) | 1 gpt-4o-mini call per (user, job), then a local scorer plus up to 3 LLM confirmations per user per day | Fit engine, $0, no LLM call of any kind |
| Opening a job (match breakdown panel) | 1 call per open, and it used up the user's daily AI limit | Fit engine explanation, $0, no limit used |
| Opening a job (description sections) | 1 call per job, first open | Client-side parse, $0 (`JOB_STRUCTURE_AI=1` restores) |
| Résumé upload (score the newest jobs) | 10 calls per upload | Fit engine, 300 newest visible jobs, $0 |
| Feed sort "best match", strong-match filter, sidebar | A dead global column (0 everywhere) | Per-user banked fit scores and bands |
| Strong-match alert emails | Sent on LLM-confirmed (or local) scores of 80+ | Sent only for a confident Strong match (outcome SCORED, band Strong, 85+) |

Removed for good: `/ai/batch-score`, `/ai/analyze-fit`, `MatchEngine.compute_breakdown`,
`MatchEngine.analyze_fit`, `MatchEngine.queue_analysis`, `OpenAIService.match_job`,
`OpenAIService.extract_experience_years`, the local scorer (`services/local_match.py`)
and the `MATCH_SCORING`, `MATCH_AI_DAILY_PER_USER`, `MATCH_AI_CONFIRM_MIN` and
`MATCH_NOTIFY_THRESHOLD` settings (match-engine/docs/DECISIONS.md D-83, D-85).

## What the engine costs instead

No money per call, but CPU, database writes and some storage:

- **Cold start.** Building the engine takes about 10 s per process (import plus
  build). Only the cron sweep, the upload pre-score and a match breakdown build
  it. The feed, sidebar and job list never do: they read banked rows through
  `services/fit_reads.py`, which imports nothing from the engine (DECISIONS D-107).
- **Scoring.** About 3 ms per (résumé, job) pair, plus about 20 ms to parse a
  posting the first time (cached in `scraped_jobs.match_terms`, re-parsed only
  when the description length or the parser version changes, D-84).
- **Traces.** Every banked score has an audit trace in `match_traces` (about
  1.8 KB). At the sweep's default scope of 2,000 jobs per user that is about
  3.6 MB per user, plus about 0.3 MB of score rows.

## Settings (Vercel env vars)

| Variable | Default | Effect |
| --- | --- | --- |
| `FIT_TRACE_SALT` | none, **required** | Secret, at least 16 bytes. Without it no score is banked (a score is never stored without its trace) and the sweep reports `fit_trace_salt_missing: true` |
| `MATCH_ALERTS_ENABLED` | on | `false` turns the cron sweep off |
| `CRON_MATCH_JOBS_PER_USER` | `2000` | Newest visible jobs scored per user per run |
| `CRON_MATCH_MAX_USERS` | `25` | Users per run (least recently scored first) |
| `CRON_MATCH_BUDGET_S` | `120` | Wall-clock box for the sweep's scoring; `0` disables |
| `JOB_STRUCTURE_AI` | off | `1` re-enables the LLM description parse on job open |

## Still AI, only on a click

Résumé parsing on upload (once per résumé), résumé analysis/improve, cover
letter ideas, "Suggest Edits", résumé tailoring (its before/after scores move
to the engine in Task 9.3), and the autofill fields that rules and the profile
can't answer (at most 2 batched calls per form).

## Monitoring

Each sweep refreshes the engine's live calibration references once and logs a
drift report (`matcher.audit.evaluate_drift`) in its summary: alarms, the checks
it skipped and why (a PSI over fewer than 200 scores on either side is listed as
"insufficient sample", never silently dropped), and the checks it cannot measure.
