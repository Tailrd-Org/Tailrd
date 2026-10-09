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

**D-25 Ambiguous skill words inside slash/hyphen tokens. [Spec: clarified]** In extraction, an
ambiguous key ("Go", "R", "Spring", "Excel") that is one part of a slash token matches only when
another part of the same token resolves to a non-ambiguous skill, and then under the usual rules
(exact casing; in prose not the first word of a sentence, the token's first part taking the
token's position): "Python/R" and "Java/Spring" yield both skills, while "A/R", "Go/No-Go", "R/W",
"Excel/Word" and "Spring/Summer 2027" (a co-op term) yield no ambiguous skill. An ambiguous key
whose casing is an all-capital acronym of two letters or more (AI, ML, BI, OS, QA, UI, REST, CAD)
needs no companion, its casing being evidence enough: "AI/ML" yields both, "BI/reporting" BI,
"OS/networking" OS; single letters and word-like keys keep the rule, and an acronym is no
companion for them ("Go/REST" yields REST only). A token whose parts are all currency codes is a
currency pair and yields nothing ("USD/CAD", "EUR/USD"; D-27). As part of a hyphen token an
ambiguous key matches only in a two-part `<key>-<suffix>` compound with suffix based, powered,
driven, backed, first, centric, enabled, native, heavy or only: "Go-based" and "AI-powered" yield
the skill; "Go-Live", "Go-to-market" and "bi-weekly" yield nothing. Both rules hold in prose and
in skills lists.

**D-26 Taxonomy build rules (Task 2.2). [Tailrd]** `scripts/build_taxonomy.py` seeds the graph from
ESCO and O*NET under `config/curated_taxonomy.yaml`. A source concept merges into a curated node by
its full name, its vendor-free name or an acronym expansion, never by a bare acronym or a "<word>
software" remainder. A merged ESCO concept brings its preferred labels (English; French when two
words or more). Into a skill, never into a field of study, it also brings an alternative or hidden
label that has several words, one of which is a specific content word of the curated node's own
label or aliases (normalised; English and French function words, one-letter words, light verbs
such as show, apply and perform, and generic words in either language such as system/système,
planning/planification, mechanics/mécanique, programme, management/gestion, process/processus,
financial, data/données, analysis/analyse, tool/outil and machine aside; a plural "s" or "es"
removed): "supervised learning" and "machine learning models" reach machine learning, "customer
support" customer service, "manage projects" project management, "manier des outils à main" hand
tools, while "web design" under computer programming, "regulatory systems" under control systems,
"financial planning" under forecasting, "plasma mechanics" under fluid mechanics, "farming
machines" under milling machines, "show courtesy" under empathy, "entrepôt de données" under
databases and "systèmes électroniques et informatiques" under embedded systems stay out, and no
paraphrase lands in a field ("computer software development" is software development, not
computer engineering). A one-word French preferred label stays only when it is the English
one: otherwise it is often an ordinary French word ("morale", "saisie"). One-word names with a
capital on nodes that are not curated are automatic `cased` keys in `resolver.json`: they need
their casing everywhere, skills lists included ("Excel (pivot tables)" never yields the O*NET
class "Tables"); only the curated `ambiguous` keys get the skills-list leniency of SPEC §3.3. ESCO
programming languages (labelled "(computer programming)" or filed under computer programming) are
TECHNOLOGY. Generic titles (curated `generic_titles`: intern, co-op, student, trainee, apprentice,
volunteer, engineer, analyst, ...) name no occupation and stay out of `titles.json`. Each reader
verifies what it reads: `load_graph` hashes the nodes, IDF and edges; `load_json_artifact` the
document it returns, so a cold start never hashes the 11 MB of titles and military codes it does
not use.

**D-27 Extraction precision. [Tailrd]** `extract`: (1) leaves fields of study (`field.*`) out
unless `include_fields=True` (education lines, posting education requirements); a field still
consumes its words, so no shorter skill surfaces inside "Computer Science". A concept that is
both a field and a professional domain has a DOMAIN skill node of its own that owns the prose
names (`skill.statistics`, `skill.accounting`, `skill.marketing`, `skill.finance`,
`skill.economics`, `skill.data_science`, `skill.nursing`), related at 0.8 to its field, which keeps
its label and degree names: the skill in general text ("Strong knowledge of accounting principles
and statistics"), the field when `include_fields=True` (education lines), and in `resolve` the
skill unless `prefer_fields=True`. A source concept so named merges into the skill. `skill.design`
(related at 0.8 to `field.design`) has multi-word names only ("design thinking", "product design",
"industrial design", "interior design"; the disciplines graphic, UX, UI, web and mechanical design
are nodes of their own): bare "design" is the verb in most postings ("Design, build and maintain",
"design reviews") and reads only as the field, in education lines. (2) Reads the curated
`blocked_phrases` as non-skills that consume their words, such as American Express, Express
Entry, Express Employment Professionals, Swift Transportation, Statistics Canada, Department of
Finance, Sous Chef, Vitamin C, Building A-Z, Swift Current, Spring Break, Go-Live, Go/No-Go, R&D,
the NCLEX-RN exam (not the licence), and section headers that would read as a skill ("Technical
Skills", "Language Skills", "Platforms"). The longest match wins and a blocked phrase wins a tie;
`resolve` gives NONE. (3) Never reads a season word as a skill when a year or a term word follows
it, perhaps after "of" ("Spring 2024", "Spring '24", "Spring of 2024", "Spring term", "Spring
co-op"), or another season stands next to it, across at most one link word ("Fall and Spring",
"Summer, Fall, Spring"). (4) Never reads one capital letter as a skill when it stands between two
capitalised words that are not skills ("Mary C Jones") or is followed by a period between such
words ("John R. Smith", "Dr. Mary C. Jones"); the word before may be a title or an initial with its
own period ("Dr. R. Smith", "J. R. R. Tolkien"). "Wrote firmware in C." keeps C. (5) Never reads a
currency code (CAD, USD, EUR, GBP) as a skill in a money context: after "$" or a salary word on its
line ("$25/hr CAD", "Salary (CAD)"), before an amount or a rate ("CAD $80k", "CAD 2M",
"CAD/hr"), inside a currency pair ("USD/CAD", "EUR/USD"), or after a number that a rate or a
money word follows ("25 CAD per hour", "a 2M CAD budget") or, when the number is an amount
(thousands, cents, a magnitude, four digits or more), that ends its clause ("The budget was
80,000 CAD."). A four-digit year is never a number ("the 2024 CAD standards manual", "SolidWorks
2022 CAD."), a count or a version is no amount ("1,200 CAD drawings", "10 CAD models", "Fusion
360 CAD.") and a number before a list comma ends the item before ("Office 365, CAD"): all keep
CAD, as does "Proficient in CAD (SolidWorks)". (6) Like `resolve`, reads past a leading French
elision (l' d' qu' j' n' s' c' m' t', straight or curly apostrophe, any case): "d'Excel" gives
Excel, the mention starting at "Excel". Inside a window the elision stays, as labels have it
("gestion de l'information"); a label that opens with one is indexed past it.
Curated one-word aliases with an ordinary other sense are gone or need their casing, audited
against both languages' source prose (English: ESCO and O*NET descriptions and tasks; French:
ESCO's French skill, occupation and group descriptions). Gone: ml, networking, containers,
trades, regression, black/green/yellow belt, cloud, selling, prospecting, presenting, mills,
mentor, paie, vérification; recherche ("à la recherche d'un stagiaire"), vente and ventes ("point
de vente" is now the POS node), prévisions, achats, approvisionnement, recrutement ("processus de
recrutement"), fiabilité; bare "Teams" (only "Microsoft Teams", "MS Teams") and bare "Chef" (only
"Chef Infra", "Chef configuration management"; "Chef (configuration management)" is a `resolve`
name only, since extraction stops at the bracket and reads "configuration management"). Multi-word
forms replace them ("recherche scientifique", "ventes B2B", "prévisions financières", "gestion
des achats", "recrutement de personnel"). Casing needed: ML, BEd, AI ("j'ai"), BI ("bi-weekly"),
OS (French "os"), MIS, Maven, Tailwind, AJAX ("Ajax, ON"), Ruby, Elixir, Bash, Hibernate,
Anaconda, Solidity, Cerner (French "cerner"), Initiative, Reliability, Final Cut, Lambda
functions. Agile matches in lower case. Rejected: a technology mention required in the same
sentence, and a capitalised neighbour read as a proper noun; both cost recall ("Built backend
services in Go.", "Advanced Excel").

**D-28 Credential status is context, not vocabulary. [Tailrd]** Whether a credential is held,
pursued or required depends on the words around it, and they mean different things in a résumé
and in a posting ("candidate" there is the applicant: "Ideal candidate: RN with 3+ years"). The
taxonomy therefore resolves the credential CONCEPT the same way everywhere: "RN student",
"R.N. (in progress)", "CPA, in progress", "RN (pending NCLEX)", "Professional Engineer in
training", "Registered Nurse (RN) candidate" all give the licence; reading the status ("pursuing",
"(in progress)", "(pending ...)", "student/candidate/trainee" after a licence, "expected <year>")
is the résumé parser's job (Task 6.4), and the licence gate counts only certification and title
evidence (Task 3.4), so a mentioned licence ("RN supervision") is never a held one. Only FORMAL
in-progress designations are credentials of their own, curated per professional licence
(`in_progress`): `cred.cpa_in_progress` ("CPA candidate", "CPA student", "CPA PEP", "CPA PEP Core
1/2", "CPA Professional Education Program", "CPA <Canada or a provincial body> PEP", "writing the
CFE"), `cred.eit` (EIT, Engineer-in-Training), `cred.articling_student` (articling student,
student-at-law, Law Society licensing candidate), `cred.nursing_student` (nursing student, student
nurse, BScN student), `cred.teacher_candidate`, `cred.medical_student`, `cred.pharmacy_student`
(incl. intern pharmacist), `cred.paramedic_student`, `cred.electrician_apprentice` (309A
apprentice, apprentice electrician), `cred.cdl_trainee` (commercial learner's permit); the map
also gives the CFA charter, no gated licence, its `cred.cfa_candidate` (CFA candidate, CFA Level
I/II/III). Each is related (0.5-0.7) to its licence and to its field or skill and never is a
licence; the licence gate matches the licence id itself, never through a related edge. A formal
designation is read only as written, never across ":" or a dash ("Elementary Teacher - Candidate
must hold OCT" names no teacher candidate). The build fails when a professional licence has no
in-progress node, when a formal designation reaches a licence (the CFA charter included) through
`resolve` or `extract`, or when one of the curated posting probes (`licence_probes`, eight lines
such as "Ideal candidate: RN with 3+ years of acute care experience.", "The ideal candidate:
P.Eng. or EIT ..." and "Graduate of an accredited RN program ...") yields other credentials than
its licence and those it names. The period closing an initialism ("C.P.A.", "B.Ed.") does not
end a window before a lower-case word ("C.P.A. candidate"); before a capital or after any other
word it does ("ASP.NET. Core"). Superseded (fix round 2): names generated from every licence name
with a status word, the bracketed-status reading and the absorption of a licence next to its
in-progress form; they leaked through punctuation and turned posting requirements into
candidacies.

**D-29 Military crosswalk skills. [Spec §14.3: clarified]** A code's `skill_ids` come only from
the O*NET occupations it maps to: their hot or in-demand technologies and top five knowledge areas
(importance >= 3). Never a sibling occupation's skills (the earlier "All Other" family union gave
25B, IT Specialist, Epic, AutoCAD and Marketo), never a field of study, never a knowledge area more
than 25 % of all codes would get (`common_knowledge_left_out`: English, customer service,
mechanics, management principles). A knowledge area named like a field of study counts through
its domain skill where one exists (Economics and Accounting: accounting; Sales and Marketing:
marketing; D-27). An entry without skills carries a `note` saying why; most map
only to military-specific occupations (SOC 55) or to civilian "All Other" catch-alls O*NET
publishes no skill data for (counts in `data/taxonomy/SOURCES.md`). SPEC §14.3 calls veterans a
legally sensitive population: an empty list with its reason is honest, an inferred one is not.

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

**D-33 Negation (§15.2).** A hard-negated mention contributes 0. An aspirational mention
contributes `min(value, 0.15)`. Another, non-negated mention of the same skill still counts.

**D-34a Clause rules (§15.1). [Spec: clarified]** Boundaries are `. ; ,` and the coordinating
conjunctions *and, but, or, nor, yet, so*. "for" is excluded: in bullets it is a preposition
("built dashboards for the sales team using Tableau"). A clause that holds only skill mentions
and list words ("…using React, Redux and D3") is a list continuation and inherits the previous
clause's verb; otherwise every comma-separated tool after "using" would fall to NO_VERB.

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
panel. Percentile = `(below + 0.5 × equal) / n`, rounded to an integer in [0, 100]. The n ≥ 30
rule counts DISTINCT candidates (people), not (candidate, posting) pairs: "applicants" are people,
and five users × 2,000 jobs would otherwise pass the threshold while really comparing a user
mostly with themselves. With Tailrd's current handful of résumés, every score therefore uses the
panel until 30 people have scored résumés.

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
