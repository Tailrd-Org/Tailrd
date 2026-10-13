# Golden set (EVAL.md layer 3)

Human-labelled (résumé, posting) pairs. Tuning against them is allowed. Everything in this folder
except this README is git-ignored: the repository is public (DECISIONS D-04), so real résumés and
their labels never enter it. Anonymise before labelling: no names, e-mails, phone numbers,
addresses, photos or links; employers and schools may stay (the engine never reads schools).

`make eval` runs layer 3 when `labels.jsonl` exists here and prints `GOLDEN: skipped (no labels)`
otherwise.

## `labels.jsonl`

One JSON object per line:

```json
{"pair_id": "g0001",
 "posting_id": "post-0042",
 "as_of": "2026-10-01",
 "resume": {"roles": [...], "skills_blob": [...]},
 "posting": {"title_raw": "...", "requirements": [...]},
 "labels": [{"rater": "r1", "band": 2, "note": "solid Python, no Kafka"},
            {"rater": "r2", "band": 3}],
 "advanced": null}
```

* `pair_id` unique in the file; `posting_id` groups the pairs of one posting (Kendall's tau is
  computed within a posting).
* `resume` is a `matcher.models.Resume` as JSON. Instead, `resume_file` may name a file next to
  `labels.jsonl` (`"resume_file": "files/g0001.pdf"`); the harness parses it with the real parser.
* `posting` is a `matcher.models.JobPosting` as JSON (from `matcher.parse.posting.parse_posting`).
* `as_of` is the date the pair is judged at (defaults to the corpus date).
* `labels`: one entry per rater. `band` is 0 Limited, 1 Partial, 2 Good, 3 Strong, the four bands
  the product shows (DECISIONS D-52). `note` is optional free text.
* `advanced`: the real outcome when known (true / false), else null.
* `group` (optional): an opaque fairness group id (for example "g1"), never a demographic label in
  clear text and never read by the engine. Pairs with a group feed the four-fifths guardrail
  (selected = raw >= 0.70, ratio >= 0.80 over groups with at least 100 pairs); with no eligible group
  it reads "not assessed", never a pass (DECISIONS D-93, D-110).

## Labelling protocol (dual labelling)

1. Every pair is labelled by two raters working independently. Neither sees the engine's score,
   band or explanation, nor the other rater's label, until both are recorded.
2. Raters judge fit for the posting from the résumé alone, with the band definitions above and the
   posting's stated requirements, and never on school prestige, graduation year, gaps, names or
   anything SPEC section 9 forbids the engine to read.
3. The harness reports inter-rater agreement (exact share and quadratic-weighted Cohen's kappa over
   the first two raters). Below kappa 0.6 the band definitions are not shared: review them with
   both raters and relabel before anything is tuned against the set.
4. Disagreements of two or more bands go to adjudication by a third rater, recorded as a third
   label; the consensus band is the median of the labels (an even split rounds down).
5. Rotate 20 % of the set each quarter and retire pairs that have been tuned against (EVAL 5.3).
