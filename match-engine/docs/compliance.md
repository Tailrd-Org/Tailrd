# Compliance: human review, audit trail and bias monitoring

This is the documented human-review path and audit procedure that SPEC section 9 asks for. It
says what the engine does, what is recorded about every score, where demographic data may live,
and how the annual bias audit is run. It is a working checklist written by engineers. **It is not
legal advice.** SPEC section 9 says: "Get an employment attorney to review before launch." That
review should cover this document and the audit procedure before launch, and nothing here claims
that any statute does or does not apply to Tailrd.

## Status and owner decisions

This document mixes three kinds of statement. This block names them. The sections below say which
kind each statement is, and every proposal is marked "(proposed, owner to confirm)" where it is made.

**(a) Built and tested in this package.** The result contract and the reason codes
(`matcher.models`, `matcher.errors`); the audit trace (`build_trace`), its digest and the opaque id
(`opaque_id`); the four-fifths check (`four_fifths`); the PSI and the drift alarms (`psi`,
`drift_alarms`, `evaluate_drift`); and `scripts/bias_audit.py`. The scoring entry point, the gates,
the confidence thresholds and the calibration are specified in SPEC and built by other tasks; this
document relies on them and does not claim more than they provide.

**(b) Still to be built by the Tailrd integration (none of it exists yet).** Milestone 9 of the
current build plan covers part of this: storing traces keyed by opaque id (Task 9.1), the web
display of the engine's result (Task 9.4) and the drift report in the cron sweep (Tasks 9.2 and
9.6). It has no task for the rest, so none of it is scheduled in the current plan: the review log
and the weekly reading of it (section 2); the voluntary demographics form, the separate store and
the salt on that side (section 4); the export that feeds the audit (section 6); and the scheduled
four-fifths job (section 5). Building these needs the owner's decision first, in particular
collecting demographic data. Where this document says "must", it is a requirement on that work.

**(c) Policy calls and a product premise. Nothing below has been decided by the owner.** Each is a
proposal, and each is repeated, marked, where it is used.

1. Premise: Tailrd shows a candidate only their own score for a job, and no employer, recruiter or
   agency sees a score, a ranking or a shortlist (proposed, owner to confirm). If that changes, the
   attorney review in section 8 must be repeated first.
2. A job with no score is never hidden or filtered because it has none, and the integration must say
   that it was not scored rather than show a zero or a low band (proposed, owner to confirm).
   Section 1.
3. A named person must read the log at least weekly (proposed, owner to confirm). Section 2.
4. Answers that a user stores to fill employers' application forms, including voluntary EEO
   answers, are not an audit source (proposed, owner to confirm). Section 4.
5. Demographic answers are collected only voluntarily and separately, and only the person who owns
   the audit joins them, at audit time, with the join logged and the exports deleted afterwards
   (proposed, owner to confirm). Section 4.
6. How long traces, replay material, the review log and audit artifacts are kept is set with counsel;
   this document proposes no period (proposed, owner to confirm). Section 3.
7. Cadence: the monitoring runs at least weekly, and the full audit runs once a year and after any
   release that changes weights, the skill graph or the parser (proposed, owner to confirm).
   Sections 5 and 6.
8. A candidate can ask for a person to look at any score or any non-score, and the reviewer answers
   in plain language (proposed, owner to confirm). Section 2.
9. A score is never overwritten by hand; a wrong score is fixed at its cause and recomputed
   (proposed, owner to confirm). SPEC, EVAL and DECISIONS are silent on it. Section 2.

Not proposals: "The score ranks; it never auto-rejects." (section 1) and "Get an employment attorney
to review before launch." (section 8) are SPEC section 9's own sentences.

## 1. What the engine does, and what it never does

- It computes a deterministic fit score between one résumé and one job posting. There is no model
  inference, no network call and no randomness: the same inputs under the same config give the
  same bytes (SPEC section 0).
- **The score ranks; it never auto-rejects.** That is SPEC section 9's sentence, word for word. The
  engine's result has no field that hides, removes or blocks anything, and the integration must not
  use a score to hide a job, take it out of a feed or block an application.
- When the input is too poor to score, SPEC sections 13 and 17 require the engine to say so, with a
  reason, instead of guessing. The outcome and reason-code contract is built (section 2); the
  scoring entry point that applies it is built by other tasks (Status (a)).
- A missing score is not a low score. The integration must not hide or filter a job because it has
  no score, and must say that it was not scored rather than show a zero or a low band (proposed,
  owner to confirm).

## 2. The human-review path

Every pair ends in exactly one of four outcomes (SPEC section 13; the result model enforces the
shapes). The integration must show and record the following for each; neither the display nor the
review log exists yet (Status (b)).

| Outcome | The integration must show the candidate | The integration must record for a person |
|---|---|---|
| `SCORED` | "Fit score 76 - Good match", with the strengths, the gaps and how to improve | the trace (section 3) |
| `SCORED_LOW_CONFIDENCE` | the band only, no percentage, and a sentence naming what limited it | the trace, with the reasons; it counts in the abstention statistics |
| `NEEDS_REVIEW` | "We couldn't score this", followed by the plain sentence for each reason code (below) | the trace **and an entry in a review log, so that a person checks it** |
| `GATE_FAIL` | no percentage; the sentence for the gate that failed (work authorization, licence, clearance) | the trace, with the gate and its reason |

A gate result is a statement about the facts the candidate gave Tailrd (for example, a stated need
for sponsorship against a posting that offers none), not a verdict on the candidate; the integration
must let the candidate correct those facts and recompute the result. An unknown fact warns and never
fails a gate (DECISIONS D-60).

### Reason codes that route to a person

`NEEDS_REVIEW` always carries at least one of these codes (`matcher.errors.NEEDS_REVIEW_REASONS`),
and the integration must show the candidate the sentence in `matcher.errors.FLAG_TEXT`:

| Code | What the candidate is told |
|---|---|
| `ENCRYPTED` | This file is password-protected, so we couldn't read it. |
| `UNREADABLE` | We couldn't read the text in this file (it may be a scanned image). |
| `CORRUPT` | This file appears to be damaged or incomplete. |
| `NOT_A_RESUME` | This document doesn't look like a résumé. |
| `UNSUPPORTED_LANGUAGE` | This document is in a language we can't score yet. |
| `UNSUPPORTED_CALENDAR` | Some dates use a calendar we couldn't convert. |
| `POSTING_UNSCORABLE` | We couldn't find any requirements in this job posting. |
| `INVALID_INPUT` | The information was incomplete or malformed, so it couldn't be scored. |
| `INVALID_AS_OF` | The scoring date is earlier than every date on the résumé. |
| `NUMERIC_FAULT` | A calculation failed, so no score is shown. |
| `NO_REQUIREMENTS_RESOLVED` | None of this posting's requirements are skills we recognise yet. |
| `NOTHING_SCORABLE` | There wasn't enough information on either side to score this match. |
| `INTERNAL_ERROR` | Something went wrong while scoring, so no score is shown. |
| `LOW_CONFIDENCE` | There isn't enough reliable information to show a score. |

### What the person does

1. **Engine-initiated.** The integration must append every `NEEDS_REVIEW` result to a review log
   with its reason codes, its trace digest and a timestamp. A named person must read the log at
   least weekly (proposed, owner to confirm) and sort each entry into one cause: the candidate's
   file (ask for a better one), a parser defect (open a bug and add a regression test), a taxonomy
   gap (it joins the weekly review queue, SPEC section 16), or an engine fault (`NUMERIC_FAULT`,
   `INTERNAL_ERROR`: always a bug). A reason code that suddenly grows, or that is concentrated on one
   kind of candidate, is raised as a drift or fairness question (sections 5 and 6), not just closed
   one entry at a time.
2. **Candidate-initiated.** Any candidate must be able to ask for a person to look at any score or
   any non-score (proposed, owner to confirm). The reviewer opens the trace, checks `input_digest`
   against the stored résumé and posting, re-runs the engine under the recorded config hash and
   engine version (the result is byte-identical, which needs the replay material in section 3) and
   answers in plain language.
3. **No hand edits.** A score is never overwritten by hand (proposed, owner to confirm; SPEC, EVAL
   and DECISIONS are silent on it). If it is wrong, the cause is fixed (the input, the parser, the
   taxonomy, or a config proposal under `proposals/`) and the score is recomputed, so the trace
   always explains the number that is shown.

## 3. What is persisted for every score

SPEC section 0.4: every score persists its config hash, engine version and full feature trace. This
package builds the record, `matcher.audit.build_trace(result)`; `trace_digest(trace)` fingerprints
it (sha256 of the canonical JSON, 16 hex characters) so a stored trace can be checked later. The
integration must write one line for every score (the format is in section 6).

| Field | Meaning |
|---|---|
| `trace_version` | the layout of this record |
| `outcome`, `gate_status` | one of the four outcomes; the combined gate result |
| `raw`, `calibrated`, `band` | the weighted raw score, the integer percentile used for ordering, the display band |
| `dims` | per dimension: `value`, `weight`, `effective_weight` after redistribution, and `abstain_reason` |
| `requirements` | one row per requirement group: `[skill_id, necessity, m, relation]` |
| `caps` | any cap applied (the critical-miss cap), with the requirement ids behind it |
| `gates` | each gate's kind, status and reason code |
| `confidence` | the confidence value, its six components and its reason codes |
| `calibration` | method, scope, reference size and version behind the percentile |
| `flags`, `reasons` | informational codes; the reasons for a non-score or a low-confidence band |
| `engine_version`, `config_hash` | which engine and which config (and skill-graph artifacts) produced it |
| `input_digest`, `as_of` | which inputs, and the date the score is as of |

A trace holds **no free text** (DECISIONS D-14). It reads only fields that hold canonical ids,
enums, numbers and dates, and it leaves out every explanation sentence, job title, employer, surface
form and gate detail. As a backstop, a string in an id position that is not shaped like an id is
replaced by `<redacted>` instead of being copied; that check makes a stray string visible, and it is
not a proof that none can get through. The persisted line must also carry the person's `opaque_id`
(section 4) and the trace's `digest`, and must carry no user id, name, email or résumé text.

**Retention and replay.** Replaying a score (section 2, step 2) needs more than the trace: the
engine version (the code at that version), the config files and skill-graph artifacts that produced
`config_hash` (kept by hash), and the inputs (the résumé and the posting that produced
`input_digest`). Retention must cover all of them for the same period, or the byte-identical re-run
is not possible, and the annual audit needs the whole audit period of traces. How long any of it is
kept is set with counsel (proposed, owner to confirm); this document proposes no period.

## 4. Demographic data: where it lives and who can join it

SPEC section 9: monitor selection rates by demographic group **where that data is voluntarily and
separately collected**, in a separate system, keyed by an opaque id, never accessible to the scorer.
None of the store, the form or the salt exists yet (Status (b)); this is what they must do.

- **Voluntary and separate.** Demographic answers must be collected only if the person chooses, in a
  place of their own and not as part of applying or scoring, with the purpose stated as fairness
  monitoring and nothing else (proposed, owner to confirm). "Prefer not to say" and a blank answer
  are both valid, and withdrawing must delete the row. In the export for the audit a declined answer
  must be blank; `scripts/bias_audit.py` also reads the literal "Prefer not to say" (any
  capitalization) as blank, so it is never audited as a group of its own.
- **Not an audit source.** Answers that a user stores for another purpose (for example, to fill
  employers' application forms, including voluntary EEO answers) are not an audit source (proposed,
  owner to confirm): they were collected for that purpose and are keyed by user id.
- **Keyed by opaque id.** `matcher.audit.opaque_id(user_key, salt)` is an HMAC-SHA256 of the user
  key under a secret salt of at least 16 bytes, first 24 hex characters. The demographics store must
  have an `opaque_id` column and **no user id column**. The same user and salt always give the same
  id; a different salt gives an unrelated one, so rotating the salt severs every earlier link (do
  this if the salt may have been exposed).
- **Never reachable from the scorer.** The scoring function receives no salt and no demographic
  value, and the engine's model has nowhere to put one (SPEC section 9,
  `tests/test_models.py::test_no_prohibited_fields_in_the_model`). The salt must be read only by
  the two writers (trace persistence and the demographics form), and the demographic store must be
  a separate database or schema with its own credentials that the scoring and serving roles cannot
  read.
- **Who may join.** The join is made by only the person who owns the audit, at audit time, offline,
  on exports (proposed, owner to confirm). It is by opaque id inside `scripts/bias_audit.py`, which
  refuses any key that is not a 24-character hex opaque id, so a user id cannot be joined by
  mistake, and refuses a column that looks like an identifier (every person has a different value,
  or the values are email addresses). No one may hold standing access to both the demographics store
  and the salt. While Tailrd is one person's project that separation is procedural, so the join must
  be logged (who, when, which files) and the exports deleted when the audit is archived.
- **Only aggregates leave the audit.** Reports contain group-level statistics and no person-level
  row or identifier. A group with fewer than 100 people is named with its size and nothing else.

## 5. Continuous monitoring

`matcher.audit.four_fifths` and `matcher.audit.evaluate_drift` (or `drift_alarms`, which returns the
alarms alone) work over the recent traces. The integration must run these on a schedule, at least
weekly (proposed, owner to confirm). Each alarm is a page (EVAL section 6):

| Alarm code | Fires when | First response |
|---|---|---|
| `COVERAGE_LOW` | taxonomy coverage is below 92% | the weekly taxonomy job (SPEC section 16); new skills are not being recognised |
| `ABSTENTION_HIGH` | scores withheld on more than 20% of pairs | read the review log by reason code |
| `ABSTENTION_RISING` | more than twice the 30-day baseline | the same, and check what changed (a parser release, a new posting source) |
| `SCORE_DRIFT` | score-distribution PSI above 0.25 against the 30-day baseline | compare config hashes and engine versions in the window |
| `FOUR_FIFTHS_VIOLATION` | any group's selection ratio below 0.80 | **terminal: stop and surface it** |
| `LATENCY_HIGH` | p95 scoring latency above 300 ms | performance, not fairness |

Abstention here means that no percentage was shown: `NEEDS_REVIEW` or `SCORED_LOW_CONFIDENCE`. A
`GATE_FAIL` is an ineligibility finding, not an abstention.

Every limit is strict: a value exactly on a limit is healthy. A metric that was not measured raises
nothing; a metric that is NaN, negative or out of range is an error, not a silent pass.

**PSI needs enough scores.** PSI is noise on small samples, so it can alarm only when both the
baseline and the current window hold at least 200 scores (`PSI_MIN_SAMPLE`). The caller passes the
two sizes. Below 200 on either side `evaluate_drift` raises no `SCORE_DRIFT` alarm and reports the
check as skipped, with the reason "insufficient sample". The scheduled job must log skipped checks,
because `drift_alarms` returns alarms only.

**Not assessed is not a pass.** The four-fifths check (`matcher.audit.fairness`) works on records of
`(opaque_id, group, selected)`. It uses exact fractions, so 5/100 against 4/100 is exactly 4/5 and
passes while 79/100 against 100/100 fails. A group is assessed only with at least 100 **people**
(distinct opaque ids), not 100 scored pairs, because one person scored against thousands of
postings is still one person. The report's `assessed` field is True only when at least two groups
are large enough to be compared. When it is False there is no verdict: `passed`, `min_ratio` and
`min_ratio_exact` are None, and the monitor must report "not assessed" and never a pass (nor a
failure). `drift_alarms` given that `min_ratio` raises nothing, which is not a pass either.

**A fairness failure is terminal.** Do not tune around it, move the threshold, or exclude the
failing group. Stop, tell the owner, and treat it as an incident (section 6, step 6).

## 6. The annual bias audit

Run once a year, and after any release that changes weights, the skill graph or the parser
(proposed, owner to confirm).

1. **Fix the period** (normally the previous twelve months) and the attributes to audit.
2. **Export the persisted traces** for the period as JSON lines, one per score:
   `{"opaque_id": "<24 hex>", "digest": "<16 hex>", "trace": {...}}`.
3. **Export the demographics store** as a CSV with an `opaque_id` column and one column per
   attribute. A blank cell means "not disclosed", and a declined answer ("Prefer not to say") must be
   exported as blank (the script also reads that literal as blank). Include no user id.
4. **Run the audit** from `match-engine/`, writing outside the repository (the repository is
   public and the artifacts describe real people in aggregate):

   ```
   python scripts/bias_audit.py --traces traces.jsonl --demographics demographics.csv \
       --out-dir <private-audit-folder>/bias_audit/<year>
   ```

   Pass none of `--min-group-n`, `--thresholds` or `--attributes` for an audit. A smaller group
   floor, other thresholds or fewer attributes could each hide a violation, so a run that uses any
   of them is marked exploratory in both reports and in the summary line, and is a dry run, not an
   audit artifact. A group floor above 100 is refused.
5. **Read the status.** The script writes `bias_audit.json` and `bias_audit.md` and exits 0 when
   every check that could be assessed passed, 2 on a four-fifths violation, 3 when nothing could
   be assessed (no attribute has two groups of 100 people) and 1 on unusable input. The report
   gives, for every attribute: selection rates per group at each band threshold (85, 70, 55 on the
   calibrated score) with the exact four-fifths ratios; score distributions per group; and
   abstention per group with the reason codes. A trace counts as selected only if its outcome is
   `SCORED` or `SCORED_LOW_CONFIDENCE` and its calibrated score reaches the threshold. The report
   also counts malformed lines and traces whose digest does not match.
6. **A violation is terminal.** Stop. Tell the owner. Find the cause in the traces (which
   dimension, which requirements, which outcomes drive the gap). Do not move the threshold, drop
   the group or tune weights until the gap disappears. Any remedy is a proposal under `proposals/`,
   re-audited on the same period under the proposed config before it is adopted, and every bug
   found gets a regression test.
7. **"Not assessed" is not a pass.** Record it as such with the group sizes. While Tailrd has few
   users and little voluntary demographic data it is the expected result; the monitoring in
   section 5 and the review log carry the weight until the groups are large enough.
8. **Archive** the two reports, the exact command line, and the SHA-256 of both input files in the
   private audit folder, and delete the exports. Keep them for the period counsel sets.
9. **Attorney review** (section 8) before any of it is published or used outside Tailrd.

## 7. Prohibited inputs

SPEC section 9 bans these from scoring: name, gender, age, date of birth, graduation year, photo;
address, ZIP or anything derived from them; school prestige or ranking; employment gaps; job
tenure or "stability"; and any free-text field not resolved to a canonical skill, title or sector
id. The engine's models have no field for them, and `tests/test_models.py` asserts that. CLAUDE.md
names `tests/test_forbidden_features.py` as the build-failing check and forbids editing it to make
a build pass; it is planned in Task 3.6. Free text is display-only (DECISIONS D-14).

## 8. Before launch

SPEC section 9: "Get an employment attorney to review before launch." That review should cover this
document and the audit procedure, and the first audit's results once one has run. SPEC section 9
lists NYC Local Law 144, Illinois HB 3773, Colorado SB 24-205 and the EU AI Act as regimes that
treat a rules-based scorer as an automated employment decision tool, and notes that disparate-impact
liability attaches to deterministic rules as it does to models. Whether any of them reaches a
candidate-facing tool is a question for the attorney, and this document does not answer it. It does
rest on the premise in Status (c)1 (proposed, owner to confirm): if a score, a ranking or a
shortlist is ever shown to or used by an employer, a recruiter or an agency, that premise is false
and the review must be repeated before the change ships.

## 9. Open work and known limits

- **Not built, and mostly not scheduled:** everything in Status (b). It belongs to the Tailrd
  integration, and the owner decides first whether to build the parts the plan does not schedule.
- **A pass is not proof.** The audit measures differences in outcomes between groups, not their
  cause. It covers only people who chose to share demographic data, so the audited population can
  differ from the real one. It looks at one attribute at a time, not at intersections.
- **Small data.** With few users most groups are below 100 people and are not assessed.
- **No ground truth.** There is no oracle for a "correct" match (SPEC section 18). Outcome-based
  calibration, when it exists, learns from past decisions that may embed past bias; this audit is
  what keeps that honest, and it does not make the problem disappear.
