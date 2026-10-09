# Engine decisions log

Every place where SPEC.md / EVAL.md / BUILD.md is silent, ambiguous, or (in our judgment) wrong
for this product is resolved here, once, with the reason. Implementers follow this file; a
change to a decision is a change to this file first.

Context: the engine replaces ALL AI-based job scoring in Tailrd (a job-seeker product: students
looking for internships and new-grad roles; ~47k scraped postings, a handful of users). SPEC.md
was written for a recruiter-side tool, so a few rules are adapted below and marked **[Tailrd]**.

Status legend: **[Spec]** clarifies the spec, **[Deviation]** departs from the literal text,
**[Tailrd]** is a product adaptation.

---

## Process

**D-00 Autonomous build.** The project owner instructed (2026-10-08) that every milestone be
built without stopping for confirmation while they are away. The CLAUDE.md "Before you start ...
Do not write code until I confirm" step is satisfied by that standing instruction. Anything we
would have asked is decided here and listed in the final report instead. The hard rules in
CLAUDE.md (pure `score/`, never touch the holdout, never tune around a fairness failure, never
edit the forbidden-features test, proposals instead of direct `weights.yaml` commits after M1)
are NOT relaxed.

**D-01 Layout.** The engine lives in `match-engine/` inside the Tailrd monorepo; that directory
is the "repo root" the docs talk about (`match-engine/src/matcher`, `match-engine/config`,
`match-engine/eval`, ...). The directory name has a hyphen on purpose: it can never shadow the
`matcher` package on `sys.path`. The Tailrd backend imports the package through one adapter
(`backend/services/fit_engine.py`) that puts `match-engine/src` on `sys.path`.

**D-02 Dependencies.** Core modules (`models`, `errors`, `config`, `graph`, `normalize`, `score`,
`explain`, `calibrate`, `parse`, `audit`) need only the standard library, pydantic v2 and PyYAML
(both already deployable on Vercel). Heavy libraries (pdfplumber, python-docx, pymupdf,
rapidocr, numpy, scikit-learn, optuna, reportlab) are imported lazily, inside the functions of
`ingest/`, `eval/` and `scripts/` that need them. Importing `matcher.score` must never import
any of them.

**D-03 Python.** Code runs on 3.11, 3.12 (Vercel) and 3.13 (local). No 3.12+-only syntax.

**D-04 Public repo.** The GitHub repository is public. Never commit real résumés, user data,
scraped posting text in bulk, connection strings or keys. Committed: code, config, synthetic
corpora, and *derived* corpus statistics (document frequencies, PMI edges). Raw downloads and
corpus pulls go to `match-engine/data/raw/` (git-ignored).

## Data model

**D-10 Models.** pydantic v2 `BaseModel`, `frozen=True`, `extra="forbid"`, NaN/inf rejected at
validation. Dates are `datetime.date`. Canonical bytes of any model:
`json.dumps(m.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.

**D-11 Dates carry their precision.** A parsed date is a `DateValue(value, earliest, latest,
precision)`; `imprecise` is derived (`earliest != latest`). This is what §17.3 intervals vary.
`Role.start` is optional because §14.2 "no dates anywhere" must be representable.

**D-12 Projects are evidence, not employment. [Tailrd]** Students' strongest evidence is often
a project. `Resume.projects` holds skill mentions with bullet-level evidence (same verb and
quantification rules as role bullets, `source=PROJECT`). Projects never count toward experience
months (§5.3) and never set seniority.

**D-13 Credentials are graph nodes.** Certifications and licences are nodes of kind `CREDENTIAL`
(an addition to SPEC §3's five kinds), related to the skills they certify. Gates (§4) reference
credential ids.

**D-14 What scoring may read.** Free-text fields (`title_raw`, `employer`, `surface_form`,
`field_raw`) exist for display and explanation only. `score/` reads canonical ids, enums, dates
and numbers. School / institution is not part of the engine model at all (SPEC §5.6, §9).
Graduation dates are never parsed into the model.

## Skill graph

**D-20 Sources.** ESCO (skills, knowledge, transversal skills, hierarchy, alt labels) and O*NET
(Technology Skills, Tools Used, alternate titles, occupations) are ingested; Tailrd's curated
synonym table (`backend/services/structured_extraction.py:_SKILL_SYNONYMS`) is merged in as
hand-curated aliases and edges, which win on conflict (§3.2).

**D-21 IDF normalisation. [Spec: method unspecified]** `idf_raw(s) = ln(N / (1 + df(s)))` over
postings. The normalised value is the **mention-weighted CDF** of `idf_raw`: the share of all
skill mentions in the corpus whose `idf_raw` is lower than this skill's. It is monotone in
`idf_raw` (same ordering), bounded to [0, 1], and gives the critical-miss threshold (`idf > 0.7`)
a readable meaning: rarer than 70 % of the skills postings ask for. Min-max or `/ln N` scaling
puts almost every skill in a narrow band and the 0.7 threshold would almost never fire.
A skill never seen in the corpus gets `idf.unseen_default` (0.75), not 1.0, so a mis-resolved
rare token cannot dominate a posting.

**D-22 PMI normalisation. [Spec: PMI_max unspecified]** Edge weight is normalised PMI:
`NPMI(a,b) = PMI(a,b) / -ln P(a,b)` (the per-pair maximum PMI), kept when `NPMI >= 0.4` and the
pair co-occurs in at least `pmi.min_cooccurrence` (5) postings; out-degree capped at 20 by weight
(ties broken by id). A global PMI_max would rank by rarity, not association.

**D-23 Relation credits.** For a requirement `r` and one résumé skill mention `s`:
exact/alias 1.00; `s` is a descendant of `r` within depth 3 → 0.85; related edge →
`clamp(w, 0.40, 0.80)` (the §5.1 table's stated range); `s` is an ancestor of `r` within depth 3
→ 0.40; else 0. Shared parent without an edge earns nothing. `m_r` is the max over all of the
candidate's mentions of `credit × E × R` (after §15.2 overrides), so a skill listed 50 times
equals a skill listed once.

**D-24 Provisional nodes** (§16) multiply the credit by 0.8 until confirmed.

## Scoring

**D-30 Requirement partition. [Spec: "tools & secondary keywords" undefined]** §5.1 scores
`REQUIRED` requirements, §5.2 `PREFERRED`, §5.7 `IMPLIED`: skills the posting mentions outside
its required/preferred statements, plus the overflow beyond the top 25 by IDF (§14.4). Node
kind does not route a requirement.

**D-31 OR-groups.** Members share `or_group`. A group scores `max(m)` over members; its IDF is
the max member IDF (importance is a property of the group, not of the candidate). "X or
equivalent" is a one-member group flagged `or_equivalent`; graph relations already give partial
credit to equivalents.

**D-32 Evidence multiplier E.** `SKILLS_BLOB` 0.65, `ROLE_TITLE` 1.00, `CERTIFICATION` 1.00.
`ROLE_BULLET` uses the §15.1 verb class (OWNERSHIP 1.00 … EXPOSURE 0.30, NO_VERB 0.70);
`ROLE_BULLET_QUANTIFIED` is the same verb multiplier ×1.15, capped at 1.0 (§15.1).
`scoring.evidence.ROLE_BULLET` (0.85) and `ROLE_BULLET_QUANTIFIED` (1.00) are informational
only: the verb multiplier replaces them, so tuning them changes nothing.

**D-33 Negation (§15.2).** A hard-negated mention contributes 0. An aspirational mention
contributes `min(value, 0.15)`. Another, non-negated mention of the same skill still counts.

**D-34a Clause rules (§15.1). [Spec: clarified]** Boundaries are `. ; , ! ?` and the coordinating
conjunctions *and, but, or, nor, yet, so* ("!" and "?" end sentences like "."). "for" is excluded:
in bullets it is a preposition ("built dashboards for the sales team using Tableau"). A clause that
holds only skill mentions and list words ("…using React, Redux and D3") is a list continuation and
inherits the previous clause's verb; otherwise every comma-separated tool after "using" would fall
to NO_VERB. Punctuation inside a word or number ("Node.js", "1,000") or a skill name, and the
period of an abbreviation ("U.S.", "e.g.", "i.e.", "Inc."), are not boundaries.

The guiding intent of D-34b..D-34f (SPEC §15): credit demonstrated work by its verb and stop
interest or negation from counting as experience. A literal reading that zeroes a skill the
bullet demonstrates defeats it.

**D-34b Polarity across clauses.** Only a pure-list clause (D-34a) takes the previous clause's
polarity, only from a mention there: "Not familiar with Docker or Kubernetes" negates both,
"Studying Japanese and Korean" is aspirational for both. A clause without a mention hands on its
verb class only. Polarity never crosses a sentence end (`. ; ! ?`) or "but"/"yet": in "Seeking a
data analyst internship. Python, SQL, Tableau" and "No experience with Java, but Python and Go"
the listed skills are affirmed.

**D-34c Negation scope. [Deviation]** Why: SPEC §15.2's 6-token window zeroes skills a bullet
demonstrates ("Deployed with no downtime using Kubernetes"). A hard-negation term negates a mention
only when every token between them is a scope filler (`negation.hard_negation.scope_fillers`:
experience, prior, hands-on, familiar, used, with, the, ...; also ordinary modifiers such as
direct, production, very, currently, previously), another mention, an aspiration term ("Not
interested in Java" stays negated) or a "yet" right after the term. Otherwise the term governs the
noun it opens: "Automated deployments with no manual steps using Terraform" and "no production
downtime using Kubernetes" affirm the tool. A deferring participle right after a mention opens a
new tool: "Built the cache without Redis using Nginx" negates Redis only. "use", "work" and "have"
join the ruled fillers so "didn't use", "haven't worked" and "don't have experience" negate. An
`exceptions` phrase ("not only") and a term joined by a hyphen ("no-code", "not-for-profit",
"never-used") never negate; "n't" counts as "not"; "yet" right after a negation is not a clause
boundary. Aspiration keeps the 6-token window inside the clause.

**D-34d Verb chains. [Deviation]** Why: §15.1's nearest verb credits "Helped build X" as OWNERSHIP
and "Built dashboards using React" as PARTICIPATION. A verb followed, directly or across only
"to", "in", "with" or "on", by another verb heads the chain and gives its class: "Helped to build"
is ASSISTANCE, "Contributed to building" and "Participated in designing" PARTICIPATION, "Assisted
in developing" ASSISTANCE. An instrument participle (`verbs.defer_to_earlier`: using, utilizing,
utilising and the leverage family) defers to an earlier verb of its clause: "Built dashboards
using React" is OWNERSHIP.

**D-34e Nouns that look like verbs. [Deviation]** Why: §15.1 matches word forms, so nouns ("design
docs", "support tickets", "5 engineers") would attach as verbs. Base and third-person forms
(design, designs, support, engineers, work, use, lead, drive, scale, ...: a form whose -s/-es
sibling is listed, or that sibling) attach only in verb position, the first word of a clause or
right after "to". Noun entries (`verbs.noun_entries`: exposure, familiarity, awareness) attach only
before "of", "to", "with" or "in". Past and -ing forms attach anywhere, and the "using" deferral
only skips to verbs that attach. So "Used design patterns in Java" is PARTICIPATION, "Wrote design
docs for the Kafka migration" NO_VERB and "Shadowed senior engineers using Kubernetes" ASSISTANCE.

**D-34f Conjunction clauses.** A verbless clause produced by "and", "or" or "nor" (not after a
sentence end) inherits the previous clause's verb class, and only the verb class, whatever it holds:
"Built dashboards and reports in Tableau" is OWNERSHIP, while "Not familiar with Docker and
proficient in Kubernetes" affirms Kubernetes. A clause whose only verb is a deferring participle
counts as verbless ("Built dashboards and reports using Tableau" is OWNERSHIP); an inherited
NO_VERB never hides that participle. After punctuation only a pure list inherits (D-34a).

**D-34 Recency.** `years = (as_of - last_used).days / 365.25`; current roles are 0 years.
Skills-blob and certification mentions take the résumé's latest activity date (latest role end,
or `as_of` if a role is current); a résumé with no dates gets `R = 1.0` (we do not penalise what
we cannot date).

**D-35 Role relevance (§5.3). [Deviation: cosine space]** Cosine is computed in the posting's
requirement space: `v_job[r] = idf(r)`, `v_role[r] = idf(r) × max credit of the role's mentions
for r` (negated/aspirational mentions excluded). A plain cosine over the union of skills would
lower a role's relevance for every unrelated skill the person also used there, i.e. it
penalises breadth. Role titles that resolve to skills enter as `ROLE_TITLE` mentions.

**D-36 Relevant months.** Interval sweep over all dated roles: each elementary segment counts
once, weighted by the max `relevance × (0.5 if internship else 1.0)` among roles active in it.
Overlaps and concurrent consulting are never double-counted and the total can never exceed
calendar time. Gaps are never computed.

**D-37 Experience minimum.** `min_months = 12 × min_years_total`; else the max per-skill
`min_years` of REQUIRED requirements; else a default by posting seniority from config
(`experience.default_min_months`, e.g. intern 6, IC1 12), flagged `MIN_YEARS_DEFAULTED`.
Unknown seniority and no stated years → the dimension abstains.
Abstain also when roles exist but none is dated (`DATES_MISSING`) or the résumé is functional.
A résumé that simply lists no employment scores 0 here (they have none; it is not unknown).

**D-38 Seniority (§5.4).** Levels: `IC0` intern/co-op/student **[Tailrd]**, `IC1`–`IC6`,
`M1`–`M6`. Cross-track gaps align tracks by `seniority.cross_track_offset` (2: Lead≈Senior,
Manager≈Staff, Director≈Distinguished) instead of comparing raw indices, which would call a
Junior and a Lead the same level. Candidate level = the highest level among current roles,
else the most recent role. Unknown on either side → abstain.

**D-39 Over-qualification. [Deviation]** §5.4's `|level_gap|` would deduct for being *above* the
posting's level, which §5.3 explicitly forbids as an age proxy. Default
`seniority.penalize_overqualification: false`: being above level counts as gap 0 and raises an
`OVERQUALIFIED` flag in the explanation; the cross-track penalty still applies.

**D-40 Domain (§5.5).** Candidate credit = max over roles of the NAICS partial credit against
any posting code (exact 1.00, same 3-digit 0.70, same 2-digit sector 0.40 — with 31-33, 44-45,
48-49 treated as one sector — else 0.15). Unknown on either side → abstain.

**D-41 Education (§5.6).** Levels NONE < HIGH_SCHOOL < ASSOCIATE (college diploma) < BACHELOR <
MASTER < DOCTORATE. In-progress degrees count at their level (graduation dates are prohibited
inputs, so completion timing is never inferred). Field multiplier `0.7 + 0.3 × best relation
credit` between the candidate's fields and the required ones (1.0 when no field is required).
Relevant credential +0.1 each, cap +0.3; value clamped to 1. "or equivalent" softens one tier
(one below → 1.00, two below → 0.55).

**D-42 Redistribution.** An abstaining dimension's weight goes proportionally to the dimensions
that scored. Special case (§14.5): when §5.1 abstains because the posting has no REQUIRED
requirements and §5.2 scored, §5.1's whole weight goes to §5.2. If nothing scores, the result is
`NEEDS_REVIEW: NOTHING_SCORABLE`.

**D-43 Critical-miss cap** (§5.1) counts REQUIRED requirements/groups with `idf > 0.7` and
`m_final < 0.2`: one caps the final raw score at 0.70, two or more at 0.55.

**D-44 Confidence (§17.1).** Weighted geometric mean; a component that is undefined for this pair
(e.g. evidence density when nothing matched, temporal completeness with no roles) is left out
and the weights renormalised, so "zero overlap" stays a valid `Limited` score (§14.5).
Components are floored at 1e-6 before the log.

**D-45 Intervals (§17.3)** are two extra runs: pessimistic (imprecise dates at shortest
durations / oldest last-use, unresolved requirement tokens counted as missed) and optimistic
(the reverse, tokens counted as matched at `intervals.unresolved_token_idf`). Shown when the
spread exceeds 6 points. Computed on request (detail views), not in bulk sweeps.

**D-46 Totality and numeric safety.** The public entry point accepts anything; invalid input →
`NEEDS_REVIEW: INVALID_INPUT`. A NaN/inf reaching the aggregator raises in `strict` mode (tests)
and becomes `NEEDS_REVIEW: NUMERIC_FAULT` otherwise. No other exception escapes in non-strict
mode.

## Calibration and display

**D-50 Phase-1 reference. [Tailrd]** "Applicants to that posting" becomes "candidates scored
against that posting". With few users the chain is: posting (n ≥ 30) → job family (n ≥ 30) →
all postings (n ≥ 30) → the shipped reference panel (synthetic profiles scored offline). The
`basis` sentence names the reference actually used; it never claims "applicants" when it is a
panel. Percentile = `(below + 0.5 × equal) / n`, rounded to an integer in [0, 100].

**D-51 Phase 2** (isotonic, pure-Python PAVA at runtime, fitted offline) is built and versioned
but stays off until `calibration.isotonic.min_labels` (500) outcome labels exist. Today there are
two tracked applications.

**D-52 Bands and naming.** Bands come from the calibrated integer: ≥85 Strong, ≥70 Good, ≥55
Partial, else Limited. Display "Fit score 76 · Good match", never "76% match".

## Gates (§4) [Tailrd]

**D-60** Unknown candidate status never fails a gate; it warns. Work authorisation fails only
when the candidate's stated status contradicts the posting (e.g. needs sponsorship, posting says
none). Licence gates fail only for professional licences listed in `config/gates.yaml` (RN, CPA,
CDL, P.Eng, ...) that the résumé does not show; "valid driver's licence" warns. Clearance fails
only for an *active* clearance the résumé does not show; "able to obtain" warns. Location is
always a warning.

## Tailrd integration [Tailrd]

**D-80 Résumé source.** The engine's `Resume` is built deterministically from the stored résumé
*document* (the editable `experience` / `education` / `projects` / `skills` rows) when it has
sections, because that is what the user sees and edits (`raw_text` is never updated on edit).
When the document has no sections (the upload-time section extraction is an LLM call and fails
when OpenAI is unavailable), the engine parses `raw_text` with its own deterministic parser.
Scoring therefore never calls, and never waits on, any AI. Upload-time extraction itself is a
résumé feature, not job scoring, and is out of scope.

**D-81 Which résumé.** Unchanged from today: the user's newest résumé with text
(`jobs_sidebar` / `match_notifier` selection), so this change does not also move users between
résumés.

**D-82 Fingerprints.** A stored score is current only if its fingerprint is
`"fit<engine major>:" + sha256(canonical engine Resume JSON + config hash)`. Every older row
(the sha256 "AI" rows and the "local1:" rows) is stale and is overwritten by the next sweep or
feed view, so no AI-made score is ever shown again. Résumé edits change the fingerprint.

**D-83 No AI in any score.** Removed: the notifier's "LLM confirms the top candidates" step,
`MATCH_SCORING=ai`, `/ai/batch-score`, `/ai/analyze-fit`, `MatchEngine.compute_breakdown` and the
dead `queue_analysis` / `OpenAIService.match_job`. The tailoring flow keeps its LLM *rewrite*,
but every score it shows (before/after, label, matched/missing keywords) comes from the engine.

**D-84 Posting cache.** The parsed `JobPosting` is cached per job in `scraped_jobs.match_terms`
with `{"v": <posting parser version>, "dl": <description length>}`; it is recomputed when the
version changes or `length(description)` (computed in SQL, so the description is not
transferred) differs, which fixes today's never-invalidated terms.

**D-85 Thresholds.** Every hard-coded 80/60 in the backend and web app is replaced by the band
from the engine (85/70/55). Alerts go out for confident (`SCORED`) Strong matches only.

## Language

**D-70** Language detection is a stop-word profile plus script detection (no model). Supported:
English, plus French where the taxonomy carries French labels. Anything else →
`NEEDS_REVIEW: UNSUPPORTED_LANGUAGE`. RTL scripts get an explicit fixture and the same route.
