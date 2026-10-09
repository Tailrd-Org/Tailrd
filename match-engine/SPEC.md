# Resume ↔ Job Match Scoring Engine — Build Spec

> **How to use this file:** drop it in the repo root as `SPEC.md` and open Claude Code in that
> directory. Start with: *"Read SPEC.md. Confirm your understanding, flag anything ambiguous or
> that you disagree with, then propose a build order. Do not write code yet."*
> Re-point Claude Code at this file at the start of every session.

---

## 0. Mission

Build a **deterministic, explainable, non-LLM** scoring engine that compares a parsed resume
against a parsed job posting and returns a calibrated match score plus a human-readable
breakdown of why.

**Non-negotiables:**

1. **No model inference at scoring time.** No LLM calls, no network I/O in the scoring path.
   Statistical methods computed offline from our own corpus (IDF, co-occurrence, calibration
   curves) are allowed and encouraged — they are counting, not inference.
2. **Deterministic.** Identical inputs + identical config version → byte-identical output.
   No randomness, no dict-ordering dependence, no wall-clock reads inside scoring (the
   "as-of" date is an explicit input parameter).
3. **Explainable.** Every score decomposes into named contributions. If we can't explain a
   number to a candidate or a regulator, we don't ship it.
4. **Auditable.** Every score persists its config hash, engine version, and full feature trace.

---

## 1. Architecture

Six independent stages. Each is a separate module with its own tests. Never let parsing
concerns leak into scoring, or scoring concerns leak into presentation.

```
ingest → parse → normalize → extract → score → calibrate → explain
```

```
src/
  ingest/        file type detection, text + layout extraction
  parse/         section segmentation, date parsing, role/edu blocks
  normalize/     canonicalization against the skill graph
  graph/         skill taxonomy: nodes, aliases, edges, IDF
  score/         the dimension scorers + aggregator  ← pure functions, zero I/O
  calibrate/     raw score → displayed percentage
  explain/       breakdown + actionable gap report
  audit/         score logging, bias monitoring, drift detection
  config/        weights.yaml, gates.yaml, versioned
```

**Hard rule:** `score/` is pure. Functions take dataclasses in, return dataclasses out.
No database, no filesystem, no clock, no config loading inside. This makes it trivially
testable and is the single most important design decision in this spec.

---

## 2. Data model

Define these as frozen, validated dataclasses (pydantic v2). These are the contract between
stages — get them right before writing any scoring logic.

```python
SkillRef:      canonical_id, surface_form, evidence: Evidence,
               first_used: date|None, last_used: date|None

Evidence:      SKILLS_BLOB | ROLE_BULLET | ROLE_BULLET_QUANTIFIED | ROLE_TITLE | CERTIFICATION

Role:          title_raw, title_canonical, seniority: SeniorityLevel,
               employer, industry_codes: list[str],
               start: date, end: date|None, is_current: bool,
               skills: list[SkillRef]

Resume:        roles, education, certifications, skills_blob,
               total_experience_months, locations, work_auth: WorkAuth|None

JobRequirement: skill_id, necessity: REQUIRED|PREFERRED|IMPLIED, min_years: int|None

JobPosting:    title_canonical, seniority, requirements: list[JobRequirement],
               min_years_total, education_requirement, certifications_required,
               industry_codes, location, remote_policy, gates: list[Gate]

MatchResult:   gate_status, raw_score, calibrated_score, band,
               dimensions: dict[str, DimensionScore], explanation: Explanation,
               engine_version, config_hash
```

---

## 3. The skill graph

This is the core asset. Everything downstream is only as good as this.

**Seed it from ESCO** (free, ~13,900 skills, multilingual) **and/or O\*NET** (free, US-centric,
excellent occupation↔skill mappings). Do not hand-write a taxonomy from scratch.

**Node structure:**

```yaml
- id: skill.react
  label: React
  aliases: [ReactJS, React.js, React 18]
  parents: [skill.js_framework]
  kind: TECHNOLOGY        # TECHNOLOGY | METHODOLOGY | DOMAIN | TOOL | SOFT
  related:
    - {id: skill.vue,     weight: 0.65}
    - {id: skill.nextjs,  weight: 0.80}
```

**Three derived properties, all computed offline from our own job corpus:**

### 3.1 IDF weighting

```
idf(s) = ln(N_postings / (1 + df(s)))
```

normalized to `[0,1]` across the corpus. This makes "Kubernetes" worth several times
"Microsoft Office" automatically, with no hand-maintained stopword list. Recompute weekly.

### 3.2 Relatedness via co-occurrence (PMI)

Rather than hand-curating thousands of edges, mine them:

```
PMI(a,b) = log( P(a,b) / (P(a)·P(b)) )
edge_weight(a,b) = clamp(PMI(a,b) / PMI_max, 0, 1)
```

Keep edges above a threshold (start at 0.4), cap out-degree at ~20 per node, and merge with
hand-curated edges where they exist (hand-curated wins on conflict). This is pure counting —
it satisfies the no-AI constraint.

### 3.3 Alias resolution

Case-folding, punctuation stripping, and a hand-maintained alias table. Add a bounded
fuzzy fallback (Levenshtein ≤ 2, **only** for tokens ≥ 6 chars, **only** when no exact match
exists). Log every fuzzy hit — these are your taxonomy gaps and should be reviewed and
promoted to real aliases.

---

## 4. Gates — evaluated BEFORE scoring

Gates are eligibility, not fit. They never enter the weighted sum.

| Gate | Behavior on fail |
|---|---|
| Work authorization | `GATE_FAIL` |
| Required license / certification (e.g. RN, CPA, CDL) | `GATE_FAIL` |
| Security clearance | `GATE_FAIL` |
| Location / onsite requirement | `GATE_WARN` (soft — people relocate) |

On `GATE_FAIL`, return the failure with its reason and **do not compute or display a
percentage**. A "94% match" for a role the candidate legally cannot hold is worse than no
score at all.

---

## 5. Dimension scorers

Each returns a `DimensionScore` with `value ∈ [0,1]`, the weight applied, and a list of the
specific facts that produced it. All weights live in `config/weights.yaml` and are tunable
without a code change.

### 5.1 Required skills coverage — weight 0.35

For each required skill `r`, compute a match quality `m_r ∈ [0,1]`:

| Relationship (resume → requirement) | Base `m` |
|---|---|
| Exact or alias match | 1.00 |
| Resume skill is a **child** of the requirement (has React, wants "a JS framework") | 0.85 |
| Resume skill is a **sibling** via related edge | `edge_weight` (0.4–0.8) |
| Resume skill is a **parent** of the requirement (has JavaScript, wants React) | 0.40 |
| No path | 0.00 |

Then modulate by evidence and recency:

```
m_r_final = m_r × E × R

E (evidence strength):
  SKILLS_BLOB             0.65
  ROLE_BULLET             0.85
  ROLE_BULLET_QUANTIFIED  1.00
  ROLE_TITLE              1.00
  CERTIFICATION           1.00

R (recency decay):
  R = max(0.55, exp(-0.15 × years_since_last_used))
```

The floor on `R` matters: deep expertise from four years ago is still real expertise. The
evidence tiers are also our primary defense against keyword stuffing — a skill dumped in a
comma-separated blob earns meaningfully less than one demonstrated in an accomplishment.

Aggregate, weighted by IDF:

```
S_required = Σ(idf(r) × m_r_final) / Σ(idf(r))
```

**Critical-miss cap.** If a requirement with `idf > 0.7` scores `m_final < 0.2`, cap the
*final* score at 0.70. Two such misses cap it at 0.55. A candidate missing the one thing the
job is actually about should never show as a strong match, no matter how well they do
elsewhere. Implement this as a cap, not a subtraction — it's more interpretable and can't
drive scores negative.

### 5.2 Preferred skills coverage — weight 0.10

Same machinery, no critical-miss cap. Missing these is normal.

### 5.3 Relevant experience depth — weight 0.15

Don't use total years. Weight each role by how relevant it was:

```
role_relevance = cosine( idf_weighted_skill_vector(role),
                         idf_weighted_skill_vector(job) )

relevant_months = Σ (role.duration_months × role_relevance)
ratio = relevant_months / job.min_months
```

Then a saturating piecewise curve:

```
ratio < 0.5     →  0.60 × (ratio / 0.5)
0.5 ≤ ratio < 1 →  0.60 + 0.40 × ((ratio - 0.5) / 0.5)
ratio ≥ 1       →  1.00
```

**Edge cases that will bite you, handle them explicitly:**
- Overlapping date ranges (concurrent roles, consulting) — merge intervals, never double-count.
- `end = None` + `is_current = True` → use the `as_of` date parameter.
- Gaps between roles are **not** subtracted and **not** penalized. See §9.
- Internships: count at 0.5 duration weight.

**Do not penalize over-qualification.** It's tempting and it's a well-documented age-proxy.
If the business insists, make it a separate flag surfaced to the recruiter, never a score
deduction.

### 5.4 Seniority alignment — weight 0.10

Map titles to a two-track ordinal ladder — collapsing IC and management onto one axis is a
common and costly bug.

```
IC:   IC1 Junior → IC2 Mid → IC3 Senior → IC4 Staff → IC5 Principal → IC6 Distinguished
MGMT: M1 Lead → M2 Manager → M3 Senior Mgr → M4 Director → M5 VP → M6 C-level
```

```
same track:   score = max(0, 1 - 0.28 × |level_gap|)
cross track:  score = max(0, 1 - 0.28 × |level_gap| - 0.20)   # track-switch penalty
```

Title normalization is genuinely hard ("Software Engineer II", "SDE 2", "Engineer, Software
(L4)"). Build a rules table plus a learned-from-corpus mapping, and keep an
`unmapped_titles.log` that someone reviews.

### 5.5 Domain / industry overlap — weight 0.10

Use NAICS or ESCO sector codes with hierarchical partial credit:

```
exact sector match          1.00
same 3-digit NAICS parent   0.70
same 2-digit NAICS parent   0.40
unrelated                   0.15   # floor — domain transfer is real and common
```

The floor matters. Hard-zeroing cross-industry candidates is both inaccurate and a diversity
problem.

### 5.6 Education & certifications — weight 0.10

```
Degree level:  meets or exceeds requirement → 1.00
               one level below              → 0.55
               two or more below            → 0.20
               no requirement stated        → 1.00 (neutral, never penalize)

Field of study relevance:  multiplier 0.7 – 1.0 via the skill graph
Relevant certifications:   +0.1 each, capped at +0.3
```

**Explicitly forbidden inputs:** school prestige, ranking tier, selectivity, graduation year.
These are strong proxies for age and socioeconomic background and will not survive an
adverse-impact audit. Degree *level* and *field* only.

### 5.7 Tools & secondary keywords — weight 0.10

Same machinery as §5.1 at lower stakes. Absorbs the long tail.

---

## 6. Aggregation

```python
raw = Σ(weight_d × score_d)           # weights sum to 1.0
raw = apply_critical_miss_cap(raw)    # §5.1
raw = clamp(raw, 0.0, 1.0)
```

Require at test time that weights sum to exactly 1.0 (within float tolerance) — a failing
assertion at config load, not a silent drift.

---

## 7. Calibration

Raw scores cluster in an unintuitive range. A genuinely strong candidate landing at 0.41 and
being shown "41%" reads as an insult and destroys trust in the product.

**Phase 1 (pre-labels):** percentile-rank the raw score against the distribution of all
applicants to **that posting** (fall back to the job-family distribution when a posting has
fewer than 30 applicants). "82%" then means *"a better fit than 82% of applicants to this
role"* — a claim that is both defensible and true.

**Phase 2 (with outcome labels):** once you have advanced/rejected outcomes, fit **isotonic
regression** from raw score to P(advance). Isotonic preserves monotonicity, which protects
the explanation layer: a better candidate can never be shown a lower score. Refit monthly,
version each curve, and keep the previous curve available for rollback.

**Display the percentage. Three conditions attached.**

```
≥ 85  Strong match
70    Good match
55    Partial match
< 55  Limited match
```

Show the integer percentage as the primary number, with the band beside it as a plain-language
anchor and the §8 breakdown underneath. The number does work a band cannot — it sorts, it
gives the gap report something to move against, and it distinguishes a 71 from a 79 exactly
where a candidate choosing which five jobs to apply to needs it.

1. **Integers only.** Never decimals. `73.4%` claims precision the engine doesn't have.
2. **Calibrated, always.** An uncalibrated weighted sum rescaled to look like a probability is
   the failure mode to avoid — not the percentage itself. "70% chance of rain" is good UX
   because it's calibrated; apply the same standard here.
3. **Labeled.** Never a bare number. `"76 — better fit than 76% of applicants to this role"`.

**Naming.** Prefer `Fit score: 76` or `Match strength: 76` over `76% match`. The latter reads
as "76% chance of getting this job," which the engine does not and cannot claim, and which
feels like a broken promise when the rejection arrives.

Suppress the percentage only in the low-confidence case (§17) — a minority of scores, not the
default.

---

## 8. Explanation output

Every result must produce this. Build it alongside the scorer, not after.

```json
{
  "band": "Good match",
  "score": 76,
  "basis": "Better fit than 76% of applicants to this posting",
  "strengths": [
    {"label": "7 of 8 required skills", "impact": "+0.31"},
    {"label": "6 yrs relevant vs 4 required", "impact": "+0.15"}
  ],
  "gaps": [
    {"label": "Missing: Terraform (required)", "impact": "-0.08",
     "note": "Your Kubernetes and AWS experience partially covers this"}
  ],
  "to_improve": [
    "Adding demonstrated Terraform experience would move this to roughly 84%"
  ]
}
```

The `to_improve` field is computed by re-running the scorer with the gap filled — cheap,
because `score/` is pure. It's also the single highest-value feature for the user.

---

## 9. Compliance — build in from day one

A rules-based system is still an **automated employment decision tool** under NYC Local
Law 144, Illinois HB 3773, Colorado SB 24-205, and the EU AI Act (high-risk classification).
"Not AI" is not an exemption. Disparate-impact liability under Title VII attaches to
deterministic rules exactly as it does to models.

**Prohibited features — enforce with a test that fails the build:**

- Name, gender, age, date of birth, graduation year, photo
- Address, ZIP, or anything derived from them
- School prestige, ranking, or selectivity tier
- Employment gaps or gap duration
- Job tenure/"stability" scoring (a well-documented proxy for caregiving and disability)
- Any free-text field not resolved to a canonical skill, title, or sector ID

**Required infrastructure:**

- Persist every score with its full feature trace, config hash, and engine version.
- A monitoring job computing selection rates by demographic group **where that data is
  voluntarily and separately collected** — store it in a separate system, keyed by an opaque
  ID, never accessible to the scorer. Alert on four-fifths rule violations.
- A documented human-review path. The score ranks; it never auto-rejects.
- Annual bias audit artifacts, generated from the persisted traces.

Get an employment attorney to review before launch. I'm not one, and the above is a
starting checklist rather than legal advice.

---

## 10. Testing requirements

Treat these as part of the definition of done, not a follow-up ticket.
The full evaluation and tuning harness is specified in `EVAL.md` — read it alongside this file.

**Golden set.** 200+ hand-labeled resume/posting pairs with expected bands. Build this
*before* tuning weights, or you will tune to noise.

**Property tests (use Hypothesis):**
- *Monotonicity:* adding a matching skill never decreases the score.
- *Monotonicity:* removing a required skill never increases it.
- *Format invariance:* the same content as PDF vs. DOCX vs. plain text scores within ±2 points.
- *Boundedness:* output is always in `[0,1]`, for every generated input.
- *Determinism:* 1,000 repeated runs produce identical bytes.
- *Gate precedence:* a `GATE_FAIL` input never emits a percentage.

**Adversarial tests:**
- White-text keyword stuffing → caught at parse, flagged, not scored.
- The entire job posting pasted into the resume → must not score ≥ 0.85. The evidence tiers
  in §5.1 are what defends against this; verify they actually do.
- Skill listed 50 times → scores identically to listed once.

**Sensitivity analysis.** For each weight, perturb ±20% and report band-change rate on the
golden set. Any dimension where a 20% weight change flips more than 15% of bands is
over-leveraged — rebalance it.

---

## 11. Build order

Do not skip ahead. Each milestone ends with passing tests and a demo on real data.

1. **Data model + skill graph loader.** ESCO ingest, alias resolution, graph traversal with
   depth limits. Tests on traversal correctness.
2. **Scoring core against hand-built fixtures.** Skip parsing entirely — hand-write 30
   `Resume`/`JobPosting` objects in JSON. Get the math right in isolation. This is the part
   worth being slow and careful about.
3. **Aggregator, gates, critical-miss cap, explanation layer.**
4. **Corpus statistics.** IDF and PMI jobs over real postings. Snapshot and version outputs.
5. **Parsing.** PDF/DOCX → structured. Expect this to be 50–60% of total project effort —
   two-column layouts, tables, nine date formats, headers that aren't headers. Budget for it.
6. **Calibration layer**, percentile mode first.
7. **Audit and monitoring infrastructure.**
8. **Tuning** against the golden set, with sensitivity analysis.

---

## 12. Working agreement for Claude Code

- **Ask before assuming.** If this spec is ambiguous on a decision that affects the scoring
  math, stop and ask. Don't pick silently.
- **No stubs or `TODO: implement`** in anything presented as complete. If a piece isn't
  built, say so explicitly in the summary.
- **Tests before tuning.** The golden set exists before weights get touched.
- **Small commits**, one module per commit, each with its tests green.
- **Push back.** If something here is wrong, over-engineered, or will not work — say so and
  explain why. I would rather fix the spec than build the wrong thing carefully.
- **Config over code.** Any number a product manager might want to change belongs in
  `weights.yaml`, not in a Python literal.

### Stack

Python 3.11+, pydantic v2, pytest, Hypothesis, NumPy and scikit-learn (isotonic regression
only — offline, not in the scoring path), `pdfplumber` + `python-docx` for ingest.
No network calls anywhere in `score/`.

---

## 13. Totality contract

**The scorer is a total function.** There is no input — malformed, empty, hostile, or absurd —
that produces an exception, a `None`, a `NaN`, or an undefined state. Every input maps to one
of exactly four outcomes:

| Outcome | Meaning |
|---|---|
| `SCORED` | Full confidence, band + percentage shown |
| `SCORED_LOW_CONFIDENCE` | Band shown, percentage suppressed, reason attached (§17) |
| `NEEDS_REVIEW` | Not scorable; routed to a human with a specific reason code |
| `GATE_FAIL` | Ineligible; reason attached, no score computed (§4) |

Enforce with a Hypothesis test that generates arbitrary bytes, arbitrary malformed
dataclasses, and arbitrary unicode, and asserts one of the four outcomes every time.
This test should run on every commit and never be skipped.

**Numeric safety rules, enforced in code:**

- Every denominator is guarded. `Σ idf == 0` (a posting whose requirements are all unknown
  skills) → `NEEDS_REVIEW`, not a division.
- Every score is clamped to `[0,1]` at the boundary of every function, not just at the end.
- No float equality comparisons. Use `math.isclose` with explicit tolerance.
- Weighted sums use `math.fsum`, not `sum` — ordering-dependent float drift breaks
  determinism across Python versions.
- Any `NaN` or `inf` reaching the aggregator raises immediately in tests and degrades to
  `NEEDS_REVIEW` in production. It is never silently coerced to 0.

---

## 14. Input edge case catalogue

Each row needs a named test fixture and a defined behavior. Build the fixture directory
`tests/fixtures/edge/` first; it is the real specification.

### 14.1 Ingest

| Case | Behavior |
|---|---|
| Encrypted / password-protected PDF | `NEEDS_REVIEW: ENCRYPTED` |
| Scanned image-only PDF (no text layer) | OCR attempt → parse tier C; if OCR confidence < 0.6, `NEEDS_REVIEW: UNREADABLE` |
| Corrupt or truncated file | `NEEDS_REVIEW: CORRUPT` |
| Wrong document (cover letter, transcript, photo) | Resume-likelihood classifier on section markers; below threshold → `NEEDS_REVIEW: NOT_A_RESUME` |
| 90-page federal or academic CV | Cap at first 40k tokens of experience sections, flag `TRUNCATED` |
| Two-line resume | Score with low confidence; evidence density gate (§17) will likely abstain |
| Multi-column layout | Column detection via x-coordinate clustering; verify reading order with a sentence-continuity heuristic, downgrade to tier B if it fails |
| Repeated headers/footers, inline page numbers | Strip by cross-page line repetition detection |
| White or zero-opacity text, 1px fonts | Detect at ingest, strip, flag `STUFFING_SUSPECTED` |
| Zero-width chars, soft hyphens, ligatures (ﬁ), smart quotes | NFKC normalize at ingest, before any matching |
| Non-English resume | Detect language; if outside supported set → `NEEDS_REVIEW: UNSUPPORTED_LANGUAGE`. ESCO is multilingual — use it rather than translating |
| Mixed-language (French headers, English bullets) | Per-section language detection, per-section alias tables |
| RTL (Arabic, Hebrew) | Bidi-aware extraction, explicit test fixture |

### 14.2 Dates

Date parsing will produce more bugs than any other single component. Write it as a standalone
module with its own exhaustive test file.

| Case | Behavior |
|---|---|
| `Present`, `Current`, `Ongoing`, `—`, `to date` | Resolve to `as_of` parameter |
| `Summer 2020`, `Q3 2021`, `Fall '19` | Map to quarter midpoint, flag `IMPRECISE` |
| Two-digit years (`'19`, `98`) | Pivot at `as_of - 80 years` |
| No dates anywhere | `duration_months = None`; §5.3 contributes nothing and its weight **redistributes proportionally** across remaining dimensions — never silently scored as zero |
| End before start | Swap if that yields a plausible range, else drop the role and flag |
| Future end date | Clamp to `as_of` |
| Overlapping roles | Merge intervals before summing. Never double-count |
| Concurrent consulting (12 clients, same window) | Interval union, not sum. Cap total at calendar elapsed time |
| Gaps between roles | Ignored entirely. Not computed, not stored, not scored (§9) |
| Non-Gregorian calendar dates | Convert at parse; `NEEDS_REVIEW` if unconvertible |

### 14.3 Resume structure

| Case | Behavior |
|---|---|
| No section headers at all | Fall back to pattern-based block detection (date-anchored chunks) |
| Creative headers ("Where I've Been", "My Toolkit") | Header classifier over an embedding-free synonym table + positional heuristics |
| Functional resume (skills-first, no chronology) | All skills get `SKILLS_BLOB` evidence; §5.3 abstains; confidence drops. Correct outcome — we genuinely know less |
| Single 12-year tenure with internal promotions | Split on title changes within the employer block. Each becomes a `Role` |
| Contract-to-hire chains at one client | Merge consecutive same-employer intervals under 60 days apart |
| Founder / self-employed | Treat as a `Role`; no special scoring |
| Academic CV (publications dominate) | Publications section ignored for skill extraction unless it's a research role |
| Military (MOS / NATO codes) | Dedicated MOS→civilian-skill crosswalk table. O\*NET publishes one. This is a legally sensitive population — get it right |
| Bootcamp / self-taught / no degree | §5.6 returns neutral 1.00 when no requirement is stated. Never penalize absence of an unrequested credential |

### 14.4 Job posting pathologies

Postings are dirtier than resumes. Harden against the posting side explicitly.

| Case | Behavior |
|---|---|
| No requirements extractable | `NEEDS_REVIEW: POSTING_UNSCORABLE`. Do not score against a blank |
| 40+ requirements | Keep the top 25 by IDF, mark the rest `IMPLIED` at reduced weight |
| Impossible requirement (`10 years Kubernetes`, released 2014) | Validate `min_years` against the skill's first-release date in the taxonomy; clamp and flag `UNREALISTIC_REQUIREMENT` |
| Contradictory (`Junior` title, `10 years required`) | Flag; let §5.4 and §5.3 disagree openly in the explanation rather than averaging the contradiction away |
| One bullet containing six skills | Clause-split before extraction; each gets its own requirement |
| `X or equivalent`, `X or Y` | Parse as an OR-group: group scores `max(m)` over members, not the mean |
| Requirements buried in prose | Pattern extraction over modal phrases (`must`, `required`, `minimum`, `you have`) vs. (`nice to have`, `bonus`, `preferred`, `plus`) |
| Boilerplate-only posting | Low extractable-requirement count triggers the §17 abstention |

### 14.5 Scoring boundary conditions

| Case | Behavior |
|---|---|
| Zero skill overlap | `raw = 0.0`, valid output, band `Limited` |
| Resume lists 200 skills | IDF weighting handles it; verify with the keyword-stuffing adversarial test |
| All requirements `PREFERRED`, none `REQUIRED` | §5.1 weight redistributes to §5.2 |
| Exactly one requirement | Valid, but confidence penalty — a single-dimension score is fragile |
| Resume text == posting text | Must score **below** 0.85. Regression-tested (§10) |
| `as_of` earlier than all resume dates | `NEEDS_REVIEW: INVALID_AS_OF` |

**Weight redistribution rule.** Whenever a dimension abstains (insufficient data, not a
zero score), its weight redistributes proportionally across the dimensions that did score.
This is the difference between *"we don't know"* and *"they have none"*, and conflating the
two is the most damaging silent bug available in this system. Implement it once in the
aggregator and test it directly.

---

## 15. Ownership and negation resolution

This closes most of the negation gap from §18 and is fully rules-based.

### 15.1 Verb-strength lexicon

Attach the nearest preceding action verb within the same clause to each skill mention.
Clause boundaries: `.` `;` `,` and coordinating conjunctions.

```yaml
OWNERSHIP     1.00: [architected, designed, built, owned, led, founded, created,
                     spearheaded, established, pioneered, drove]
EXECUTION     0.85: [developed, implemented, engineered, delivered, migrated,
                     automated, optimized, deployed, maintained, scaled]
PARTICIPATION 0.65: [contributed, collaborated, participated, worked, used, utilized]
ASSISTANCE    0.45: [assisted, supported, helped, shadowed, observed]
EXPOSURE      0.30: [exposed, familiar, aware, "coursework in", "basic knowledge"]
NO_VERB       0.70: # default when no verb attaches — neither rewarded nor punished
```

This multiplier replaces the coarse `E` term in §5.1 for `ROLE_BULLET` evidence. Keep the
quantified-outcome bonus as a separate `×1.15` multiplier, capped at 1.0.

### 15.2 Negation and aspiration detection

Scan a 6-token window preceding the skill mention, **not crossing a clause boundary**:

```
Hard negation → m = 0.0
  not, no, without, lacking, never, "no experience"

Aspirational → m = 0.15 (interest is not experience)
  seeking, aspiring, "looking to", "interested in", "eager to learn",
  "currently learning", "studying", "hope to", "want to"
```

The aspirational case matters more than hard negation in practice. *"Seeking to transition
into machine learning"* scoring as ML experience is a common and embarrassing failure mode.

Test both against a fixture file of 100 hand-written bullets with labeled expected
multipliers. These lexicons are config, not code.

---

## 16. Taxonomy freshness pipeline

This closes the novel-skills gap. The goal is to convert an open-ended blind spot into a
bounded, measured lag.

**Weekly automated job:**

1. Collect all resume and posting tokens that failed canonical resolution.
2. Rank by frequency × recency-weighted growth rate.
3. Auto-promote any token appearing in > 50 distinct postings in 30 days to a provisional
   node, with parent inferred from its highest-PMI existing neighbor.
4. Queue the top 50 remaining for human review.
5. Emit a **coverage metric**: `% of posting requirement tokens resolved to canonical IDs`.

Track that coverage metric on a dashboard with an alert below 92%. It is the single best
early indicator that the engine is quietly degrading, and without it you will not notice
drift until users complain.

**Provisional nodes** get a 0.8 confidence multiplier until a human confirms placement, so a
mis-inferred parent cannot fully swing a score.

---

## 17. Confidence scoring and abstention

This is the honest answer to "remove the uncertainty": you cannot remove it, but you can
**measure it per-score and decline to show a number when it's too high.** An engine that
knows when it doesn't know is far more trustworthy than one that always emits a confident
percentage.

### 17.1 Confidence inputs

```python
confidence = weighted_geometric_mean(
    parse_quality,        # tier A=1.0, B=0.8, C=0.55 (OCR / failed column detection)
    taxonomy_coverage,    # % of posting requirement tokens resolved
    evidence_density,     # % of matched skills with evidence above SKILLS_BLOB
    temporal_completeness,# % of roles with parseable start+end dates
    requirement_count,    # saturating: 1 req → 0.4, 5 → 0.85, 10+ → 1.0
    dimension_coverage,   # % of total weight that actually scored vs. abstained
)
```

Geometric mean, not arithmetic — one catastrophically bad input should drag the result down
rather than being averaged away by five good ones.

### 17.2 Behavior by confidence band

```
≥ 0.80   SCORED                  band + percentage
0.60     SCORED_LOW_CONFIDENCE   band only, percentage suppressed,
                                 "limited information" note with the specific reason
< 0.60   NEEDS_REVIEW            no score, routed to human, reason codes listed
```

Surface the reason, always and specifically: *"We couldn't read the dates on this resume, so
experience depth isn't reflected in this score."* That sentence does more for user trust than
any amount of precision in the score itself.

### 17.3 Score intervals

Report a range, not a point, wherever uncertainty is material. Compute by re-running the
scorer under the plausible interpretations of each ambiguous input (imprecise dates at both
range endpoints, unresolved skills counted as matched and as missed). The spread **is** the
uncertainty, computed rather than estimated — cheap, because `score/` is pure (§1).

Display as "Good match (71–79%)" when the spread exceeds 6 points. Users read an honest range
as competence; they read a false decimal as a machine bluffing.

---

## 18. Residual limits — measured, not hidden

After §13–17, these remain. They are properties of the problem, not defects in the build.
Surface them rather than pretending to a confidence we don't have.

**Largely closed by §13–17:**

- ~~Negation and ownership~~ → §15 handles this with verb-strength and aspiration lexicons.
  Residual: sarcasm, unusual phrasings, and bullets with no attachable verb. Small.
- ~~Novel skills~~ → §16 converts this from a blind spot into a measured ≤ 7-day lag with a
  coverage alarm.
- ~~Unhandled inputs~~ → §13–14 make the scorer total. Every input has a defined outcome.

**Still open, by nature:**

- **Semantic gap.** "Built event-driven services in Go handling 40k req/s" against a
  requirement for "distributed systems experience" has near-zero lexical overlap and is a
  near-perfect match. The taxonomy and PMI edges recover maybe 60–70% of this class. Closing
  the rest requires generalizing over *unseen* phrasings, which is precisely the thing a
  lexical system cannot do. §17 at least detects when it's happening (low taxonomy coverage)
  and lowers confidence accordingly.

- **No ground truth.** This is the ceiling, and it is not an engineering problem. There is no
  oracle that says a given resume is a 78% match. Published interrater reliability for
  unstructured human resume screening sits around κ = 0.2–0.4 — two experienced recruiters
  scoring the same pair frequently disagree. "Accuracy" can therefore only ever be measured
  against *some* definition of correct, and all available definitions are noisy. Practical
  consequence: **validate against downstream outcomes** (did the candidate advance? get hired?
  stay 12 months?), not against anyone's opinion of the right percentage. That is the only
  target that is both objective and worth hitting.

- **Causal opacity of outcomes.** Even outcome-based validation learns from past hiring
  decisions, which embed past bias. §9's monitoring is what keeps this honest; it does not
  make the problem disappear. Isotonic calibration on biased labels produces a
  well-calibrated predictor of a biased process.

**What to tell users.** Show the number, but never alone. `"Fit score 84 — Strong match. 8 of
9 required skills, 6 years relevant experience."` The number sorts and motivates; the
breakdown is what makes it defensible and what the candidate can actually act on. A score
with no visible reasoning behind it is the version that gets you argued with — and, in an
audit, the version you can't explain.

If the semantic gap becomes the binding constraint, the smallest change that closes most of
it is a **static local embedding** (fastText or a small sentence-transformer) used *only* for
skill-to-requirement similarity. It runs in milliseconds, needs no API, is fully
deterministic given a pinned model file, and preserves the explanation layer because the
deterministic scoring structure sits unchanged on top. Worth revisiting after milestone 8
with real data on where the engine actually fails.
