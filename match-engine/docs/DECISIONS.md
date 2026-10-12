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
(pydantic is pinned in `api/requirements.txt`; PyYAML is not there yet and Task 9.1 adds it). Heavy libraries (pdfplumber, python-docx, pymupdf,
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
precision)`; `imprecise` is derived from the precision: a season, a quarter or a year (a month or
a day is precise, even though a month's bounds span the month). This is what §17.3 intervals vary.
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
computer engineering). A label whose content words are all words of the curated label itself, up
to a plural ("analyses" is "analysis"), is that label rephrased and comes in although its words
are generic: "control system" and "system control" (Control systems), "business analyses" and
"analysis of businesses" (Business analysis), "science of data" (Data science). An alternative
label that a merge leaves to one unrelated concept is blocked by hand ("record information",
otherwise forensic "document evidence"), and a curated anchor gives an ESCO concept a spelling
ESCO lacks ("programme management" reaches `skill.program_management`, ESCO's "program
management"). A one-word French preferred label stays only when it is the English
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
"design reviews") and reads only as the field, in education lines. Industrial, interior and
product design are field names too, so an education line ("Diploma in Interior Design",
`include_fields=True`) reads the field and general text the skill; `skill.design` takes no parents
(`parents: []`), the merged ESCO "design thinking" concept sitting under arts and under marketing
and advertising. (2) Reads the curated
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
line ("$25/hr CAD", "Salary (CAD)"); before an amount or a rate unit on its line ("CAD $80k",
"CAD 2M", "CAD/hr"); inside a currency pair ("USD/CAD", "EUR/USD"); after an amount (thousands,
cents, a magnitude, four digits or more) unless the next word is one of the curated
`currency_skill_nouns` (plural nouns of CAD work: drawings, models, files, parts, sheets,
assemblies, licences, standards, ...), so "85,000 CAD plus benefits", "Base: 120,000 CAD | Bonus",
"a 2,500 CAD scholarship" and "120K CAD + equity" are money and "1,200 CAD drawings" is the skill;
after a smaller number when a rate phrase or a money or pay word ("25 CAD hourly", "a 500 CAD
prize"), a range to another number ("25 CAD - 30 CAD"), an addition ("40 CAD + HST") or a
separator ("500 CAD (monthly)", "20 CAD | weekends") follows. A rate phrase ("annually", "per
hour", "a year") needs a number before the code: "Taught CAD annually" and "Spent 10 hours in CAD
per week" keep CAD, while a rate unit on the code ("CAD/h") is money by itself. A four-digit year
is never a number ("the 2024 CAD standards manual", "SolidWorks 2022 CAD."), nor is a number glued
to letters ("CATIA V5 CAD") or inside the skill name the code follows ("Fusion 360 CAD (cloud)"),
and a number before a list comma ends the item before ("Office 365, CAD"); "Proficient in CAD
(SolidWorks)" keeps CAD too. Deviations kept: a small count or version before a clause end or ")"
keeps CAD ("Fusion 360 CAD.", "10 CAD models"), so "Paid 25 CAD." reads as the skill; and only
the very next word is checked for a noun, so "1,500 CAD shop drawings" reads as money. (6) Like
`resolve`, reads past a leading French
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
Junior and a Lead the same level. Candidate levels: when any role is current, the levels of the
current roles; else those of the roles with the latest end. Only those roles count: when none of
them carries a level the dimension abstains (`SENIORITY_UNKNOWN`) rather than fall back on an
older role's level, which would be a known-wrong (usually lower) score for exactly the candidates
whose current title the mapper missed (§14.5). Each candidate level is scored against the posting
and the best score is kept (equal scores: the level closest to the posting's, then the same
track, then the smaller code), so a current internship is not out-ranked by a concurrent club
"lead" title and a manager who also holds an IC role meets a management posting as a manager.
`candidate_level` (for display) is the highest of them with the tracks aligned; its offset is a
required argument, so no caller compares raw indices. Unknown posting level → abstain.

**D-38a Executive titles students hold. [Tailrd]** A founder, CEO, president, VP, director or head
(M4-M6) at a student organisation (club, society, students' union, hackathon, Enactus, "Women in
...") or at an employer that neither the curated company table nor the industry keyword rules know
has no level (the seniority dimension abstains) rather than M4-M6: a club president or the founder
of a side project is not an executive, and a wrong management track costs more than abstaining.
Other roles at a student organisation are IC0. A bare fellowship ("MLH Fellow") is IC0; only a
qualified one (distinguished, technical, senior, engineering fellow) is IC6.

**D-39 Over-qualification. [Deviation]** §5.4's `|level_gap|` would deduct for being *above* the
posting's level, which §5.3 explicitly forbids as an age proxy. Default
`seniority.penalize_overqualification: false`: being above level counts as gap 0 and raises an
`OVERQUALIFIED` flag in the explanation; the cross-track penalty still applies.

**D-40 Domain (§5.5).** Candidate credit = max over roles (any role, dated or not) of the NAICS
partial credit against any posting code: exact 1.00 when one code is a prefix of the other or
one is a sector-level code (two digits, or hyphenated as NAICS writes 31-33) standing for the
other's sector, with 31-33, 44-45 and 48-49 each treated as one sector (so 31, 32, 33 and 31-33
all match 336 exactly, as 33 does); same 3-digit 0.70; same 2-digit sector 0.40 (two codes in one
of those groups, such as 311 and 325, included); else 0.15. Blank codes are no codes. Unknown on
either side → abstain.

**D-41 Education (§5.6).** Levels NONE < HIGH_SCHOOL < ASSOCIATE (college diploma) < BACHELOR <
MASTER < DOCTORATE. In-progress degrees count at their level (graduation dates are prohibited
inputs, so completion timing is never inferred). Level and field pair per degree: value = the
best, over the candidate's degrees (none listed: NONE with no field), of `level tier × that
degree's field multiplier`, plus the credential bonus, clamped to 1. One degree's level is never
combined with another's field: a History PhD and a Software Engineering diploma against a
Bachelor's in CS score 1.00 × 0.70, not 1.00 × 0.955. Level tier: meets 1.00, one below 0.55,
two or more below 0.20, no requirement 1.00; "or equivalent", or a requirement that is not
REQUIRED (PREFERRED or IMPLIED), softens one tier, once (one below → 1.00, two or more below →
0.55). Field multiplier `0.7 + 0.3 × best relation credit` from the degree's fields to the
required ones (1.0 when no field is required). Relevant credential: an affirmed résumé credential
related (exact, child, sibling or parent) to a posting requirement skill or to a credential the
posting lists as required or preferred; relation to a listed credential, not only the listed id
itself, is a deliberate widening. +0.1 each in id order, cap +0.3; each `CERT_BONUS` fact shows
what that credential actually added after the cap.

**D-42 Redistribution.** An abstaining dimension's weight goes proportionally to the dimensions
that scored. Special case (§14.5): when §5.1 abstains because the posting has no REQUIRED
requirements and §5.2 scored, §5.1's whole weight goes to §5.2. If nothing scores, the result is
`NEEDS_REVIEW: NOTHING_SCORABLE`. `Aggregate.scored_weight` is the configured weight the scoring
dimensions carry, the moved §5.1 weight included (it counts as scored): 1.0 when nothing
abstained, and for a posting that lists only PREFERRED skills, which is the posting's nature, not
missing data. It is the renormalisation denominator, and §17.1's dimension coverage reads it
(Task 3.5).

**D-43 Critical-miss cap** (§5.1) counts REQUIRED requirements/groups with `idf > 0.7` and
`m_final < 0.2`: one caps the final raw score at 0.70, two or more at 0.55. A `CapApplied` is
recorded only when the cap binds (the raw score was above it): "applied" means it lowered the
score. A critical miss under a lower score stays visible as the requirement's own fact and gap.

**D-44 Confidence (§17.1).** Weighted geometric mean; a component that is undefined for this pair
(e.g. evidence density when nothing matched, temporal completeness with no roles) is left out
and the weights renormalised, so "zero overlap" stays a valid `Limited` score (§14.5).
Components are floored at 1e-6 before the log, and reported as measured. Taxonomy coverage is
`(requirement_token_count - len(unresolved)) / requirement_token_count`, clamped to [0, 1]. Evidence
density is over the requirement groups of all of the posting's requirements (grouped together, as
`coverage.match_requirements` does): of the groups with any credit, the share whose matching résumé
skill (`via`) is shown beyond the skills list, i.e. has ANY affirmed mention whose evidence is not
SKILLS_BLOB (`PreparedResume.affirmed_evidence`), whatever mention has the best E x R; negated and
aspirational mentions show nothing. Requirement count is the curve at the number of distinct
requirement ids, a curve point taking its own value exactly. Dimension coverage is
`Aggregate.scored_weight` (D-42). Thresholds (0.80, 0.60, the weak line 0.70) are compared at 12
decimals, so float noise never moves a pair across one. Reasons: the LOW_* flag of every component
below 0.70, in component order; when the value is below 0.80 and no component is below 0.70, the
weakest component (and any tied with it) is named, so a result that suppresses its percentage
always says why (§17.2). A pair below 0.60 is NEEDS_REVIEW with LOW_CONFIDENCE first. On any
outcome other than SCORED, `calibrated_score` (and `raw_score`) are audit-only: kept for the trace
and the reviewer, never displayed, and adapters key on `outcome` and never rank or sort a
suppressed pair by them.

**D-45 Intervals (§17.3)** are two extra runs: pessimistic (imprecise dates at shortest
durations / oldest last-use, unresolved requirement tokens counted as missed) and optimistic
(the reverse, tokens counted as matched at `intervals.unresolved_token_idf`). Shown when the
spread exceeds 6 points. Computed on request (detail views), not in bulk sweeps, and for SCORED
results only: the other outcomes suppress the percentage, and an interval is a range of them.
Dates: only imprecise ones move (D-11), and only their `value`. Pessimistic: `end = min(max(end lo,
start lo), end hi)`, `start = max(start lo, min(start hi, end))`, a precise date's lo and hi being
its value: crossing bounds shrink a role to a point at its oldest end, and no value ever leaves its
own bounds (a project, which may end before it starts, keeps each date in its own range).
Optimistic: starts at their earliest, ends at their latest. Projects move like roles. Tokens: each
distinct `unresolved:<normalize_surface(token)>` (cut to 200 characters) is a pseudo-requirement of
the posting's strongest stated necessity: REQUIRED when it lists one; else PREFERRED, because D-42
then moves §5.1's weight to §5.2 and the tokens go where the weight lives (making §5.1 score would
undo that transfer); else IMPLIED. They join that coverage dimension through `score_coverage`'s
extra groups (the base run's path, critical-miss rule included for REQUIRED) and no other
dimension. low/high are the min/max over the base score and both readings.

**D-46 Totality and numeric safety.** The public entry point accepts anything; invalid input →
`NEEDS_REVIEW: INVALID_INPUT`. A NaN/inf reaching the aggregator raises in `strict` mode (tests)
and becomes `NEEDS_REVIEW: NUMERIC_FAULT` otherwise. No other exception escapes in non-strict
mode. Details of `score/engine.py`:
- Input: the résumé and the posting may be models (validated again), dicts or JSON; `as_of` must
  be a `date`, not a `datetime`. Anything that cannot be read is INVALID_INPUT in both modes, with
  no input digest; an `as_of` that is not a date is recorded as `date.min`.
- A NEEDS_REVIEW reason flag on the résumé or on the posting routes the pair to NEEDS_REVIEW with
  those flags, the résumé's first. INVALID_AS_OF compares `as_of` with every role and project date
  at its earliest bound and every skill date; a résumé with no date never triggers it.
- A NEEDS_REVIEW returned before the gates run reports `gate_status` PASS with no gate results,
  meaning "not evaluated"; consumers key on `outcome`.
- Faults: `EngineFault(NUMERIC_FAULT)` becomes NUMERIC_FAULT, anything else INTERNAL_ERROR. No
  exception class is kept on the result: the engine is deterministic, so a caller replays the pair
  in strict mode to get it (Task 9.1's adapter does this and logs the class).
- Wiring: a calibrator without `calibrate`, or `options` that are not `ScoreOptions`, is NEEDS_REVIEW
  INTERNAL_ERROR (strict mode raises TypeError for the calibrator; bad options cannot say the mode,
  so the default, non-strict, applies). The one exemption from totality: an unusable `config` or
  `graph` raises TypeError in both modes, since no result could carry an engine version or a
  config hash. The calibrator's answer must be an int in [0, 100] and a `CalibrationInfo`.
- `flags` are informational, each once: the inputs' own non-reason flags, then the scoring's
  informational facts (MIN_YEARS_DEFAULTED, NO_WORK_EXPERIENCE, OVERQUALIFIED). `review_result`
  takes the same informational `flags` keyword.
- The seven scorers are one registry (`score.dimensions.SCORERS`), used by the base run and by the
  interval readings alike.

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
always a warning. Every gate result names what the posting asks in its detail, never the
candidate's citizenship, places or credentials.

**D-61 Work authorisation: one decision table. [Tailrd]** C, the posting's countries, is the
gate's `countries`, else the countries of the posting's `locations` ("We are unable to sponsor"
names no country); C may be empty. A is `authorized_countries` with `citizenships` (a citizenship
implies authorisation there, never the reverse). A country missing from A is not evidence that the
candidate cannot work there: a FAIL needs an explicit statement, a sponsorship need or a stated
citizenship that misses a citizenship requirement. One row decides, in order:
0. Nothing stated (no work authorisation, or one with no country, no citizenship and no
   sponsorship answer): WARN `WORK_AUTH_UNKNOWN`.
1. Citizenship required: a citizenship in C passes; known citizenships that all miss C fail
   (`CITIZENSHIP_REQUIRED`); no known citizenship, or C empty, warns (`CITIZENSHIP_REQUIRED`).
2. A meets C: PASS (a Canadian citizen who needs sponsorship for US roles passes in Canada).
3. Otherwise: the posting sponsors, PASS. The posting refuses sponsorship, or the gate names
   countries: a stated sponsorship need fails (`SPONSORSHIP_UNAVAILABLE` when sponsorship is
   refused, else `WORK_AUTH_MISMATCH`), except that it warns (`SPONSORSHIP_UNAVAILABLE`) when C is
   empty and A is not, since the job may be where they are authorised; no sponsorship needed
   passes; an unknown need warns (`WORK_AUTH_MISMATCH`, or `WORK_AUTH_UNKNOWN` when C is empty).
   A gate with no country and no sponsorship policy passes.
Every WARN of the table is an unknown (D-60) and takes `work_authorization.unknown_status`.

**D-62 Licence evidence. [Tailrd]** Held credentials are the AFFIRMED mentions with
CERTIFICATION or ROLE_TITLE evidence (never a bullet such as "administered medications under RN
supervision", a skills-list entry, a negated or an aspirational mention). A gate passes when a held
credential is one it lists or sits below one on the is-a chain, at any depth: parent links are
eligibility facts, so the walk is the full closure (cycle-safe), never the scoring knob
`relations.max_depth`. Related edges are never followed. The formal in-progress designations of
taxonomy D-28 are an explicit list, `license.in_progress_credential_ids` (never guessed from an
id's spelling; a test checks it covers the taxonomy's `in_progress` VALUES); the walk never climbs
past one, so a designation, or anything below it, meets only a gate that lists the designation
itself ("P.Eng. or EIT"). Not shown: FAIL `LICENSE_MISSING` when the gate lists a professional
licence, else WARN `LICENSE_UNVERIFIED`. Which credentials are gated licences is
`license.professional_credential_ids` alone, never the graph and never the keys of the taxonomy's
`in_progress` map (cred.cfa is one of those keys and is no gated licence). No id is on both lists.

**D-63 Clearance ladders. [Tailrd]** `security_clearance.ladders` holds ordered lists, lowest
first, one country each: CA reliability, enhanced reliability, secret, top secret; US public
trust, confidential, secret, top secret, TS/SCI. A held clearance meets the gate's level when it is
the same token or at or above it on the same ladder; a token on no ladder matches only itself; a
gate that states no level takes any clearance. Tokens are `<CC>_<LEVEL>`, read upper-case with each
run of other characters as one underscore, and config validation refuses a malformed token, a
token listed twice and a ladder that is empty or spans two countries. The reserved
`security_clearance.unknown_level_token` (`ACTIVE_UNKNOWN_LEVEL`, named in config only, never on a
ladder) is an active clearance of unknown country and level, all Tailrd stores: it meets an "able
to obtain" gate and an active gate that states no level, and gives WARN `CLEARANCE_UNVERIFIED`
(unknown, never FAIL) against an active clearance of a stated level.

**D-64 Location. [Tailrd]** Only an onsite gate is checked, against its places, else the
posting's. The candidate is elsewhere only when each of their known places is known to differ from
each posting place: another country, another region, or another country than a country-wide
posting place. The same city in a compatible country, or the same country and region, is the same
area; what cannot be told (a place known only by its country, a city without its region) is no
mismatch, so the warning never says "based elsewhere" without evidence.

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

## Posting parser (Task 6.5) [Tailrd]

The guiding rule for every entry below: a phantom requirement lowers every candidate's score on
the posting, so where a reading is uncertain the softer one is taken (IMPLIED over REQUIRED, a
warning gate over a failing one, no level over a guessed one). Tables: `config/parse/posting.yaml`
(part of `ParseConfig`, so of its version).

**D-100 Sections and necessity.** A short header line, a "Label: text" line, or a line ending in
":" that contains a header phrase ("Requirements for this role include:") opens REQUIRED,
PREFERRED, RESPONSIBILITIES, ABOUT or IGNORED; a label inside a bullet holds for that bullet only.
Necessity is per clause (cut at commas, brackets, ":" and but/while): the clause's own modal phrase
decides, in the order implied (learning, interest, opportunity, "if you have"), negated ("not
required", "no ... required"), preferred, required; implied and negated phrases are read over
every word, the other two only outside skill mentions ("asset management" is no "asset"). A
required phrase inside a PREFERRED section stays PREFERRED. A clause without a phrase takes the
next clause's when the clauses between are bare lists and that clause continues the list ("Java,
Python or Go is required"), else the previous clause's when it is itself a bare list ("Must have:
Python, SQL"), else the section default (prose and responsibilities: IMPLIED). So "Strong Python
skills, experience with Docker is a plus" leaves Python at the section default. ABOUT and IGNORED
text yields no requirement. A statement that is only a list ("- Rust", "Go, Rust or C++") is read
in the resolver's list context, so an ambiguous name may open it; "- Excel at problem solving" is a
sentence and stays prose.

**D-101 OR-groups.** Runs of mentions joined only by list separators. A run after a lead-in ("such
as", "e.g.", "one of") or written "X, Y or Z" is one group; inside an "and" list only the "X or Y"
and "X/Y" pairs are; "or equivalent/similar/other ..." after a run (or a lone mention) flags the
group `or_equivalent`. Groups sharing a member merge (more ways in, never fewer). A group one of
whose members is also required alone at least as strongly is redundant and its other members fall
to IMPLIED: "Python" plus "Python or Go" must not leave Go as a lone REQUIRED skill the posting never
asked for. Labels `or:<n>` by first appearance. A skill keeps its strongest occurrence; on a tie a
standalone occurrence beats a group membership.

**D-102 Years.** A years phrase sets `min_years` on the run of mentions right after it (8 words at
most, same sentence), else on a mention right before it ("Python (3+ years)"), else, when an
experience word is near, `min_years_total`, from REQUIRED clauses only and the smallest stated (an
alternative such as "5 years, or 3 with a Master's" must not become the higher bar). "1-2 years"
is 1, "0-2 years" 0. Never experience: a following age/degree/program/contract/term word, or a
preceding within/past/last/next/for. A skill's years read only from its chosen necessity's
occurrences. Clamped to `as_of.year - release_year` with UNREALISTIC_REQUIREMENT. Contradiction:
IC0/IC1 with `min_years_total`, else the largest REQUIRED per-skill years (D-37's order), at or
above `contradiction.min_years` (5).

**D-103 Education and credentials.** Degree words in REQUIRED/PREFERRED clauses; a bare
"Master"/"Bachelor" counts only beside a schooling word or a field ("Scrum Master", "master data
management" are none); posting phrases ("undergraduate degree", "graduate degree",
"post-secondary program") and "degree in <field>" (bachelor's) add levels; alternatives take the
lowest; REQUIRED wins over PREFERRED; fields are the clause's `field.*` ids, and in a schooling
statement a skill that is also the field is the field ("degree in Statistics" asks for no
statistics skill). Credentials are never skill requirements. In a REQUIRED clause that shows the
credential is held (a holding word anywhere in the clause, its own name's words included, or the
credential opening the clause) it is `certifications_required` and a `LicenseGate`; alternatives
share one gate and one held member holds the group ("P.Eng. or EIT designation"); a credential with
a non-credential alternative or "or equivalent" is required but gates nothing (the posting accepts
another way in). PREFERRED credentials are `certifications_preferred`. "Under RN supervision" is
neither.

**D-104 Text, truncation, empty descriptions.** Entities are decoded (twice at most: Tailrd stores
escaped HTML), tags become line breaks ("<li>" a bullet) or vanish, script/style bodies drop,
inline bullet glyphs split lines, then `normalize_text`. A description whose length is within
`truncation.tolerance` of a cap (2,000 or 3,000) and that ends in "..." or "…" is TRUNCATED: its
last word is dropped when the cut fell inside it ("Java..." from "JavaScript"), BOILERPLATE_ONLY is
never concluded from it, and the scorer caps the pair's parse-quality component at tier B
(`score/confidence.py`; the requirements may go on past the cut). An empty description is title and
metadata only: seniority, internship, family, industry, remote policy and the location gate are
kept, nothing is read from the title as a requirement, and POSTING_UNSCORABLE sends the pair to
NEEDS_REVIEW. `max_chars` (60,000) bounds absurd inputs (also TRUNCATED).

**D-105 Gates from posting text.** Read from every sentence (footer and EEO text included) except
preferred ones. Work authorisation: "authorized/eligible to work in <country>" (the country named,
else none: the gate falls back to the posting's places, D-61), "work permit", "right to work";
sponsorship refused or offered (both stated: unknown). Citizenship: "<country> citizen(ship)",
"citizenship required", "citizens only", with a requirement word, never negated; it ALWAYS carries
countries: the one named, else the posting's (Task 3.4 review). "Citizen or permanent resident",
"green card" and "U.S. person" are authorisation in the country, not citizenship. "US" is matched
with its casing ("work for us" names no country). Clearance: only in a sentence about a security
clearance (never "customs clearance"), never a negated one; levels become `<CC>_<LEVEL>` ladder
tokens (TS/SCI, Public Trust, Confidential: US; Reliability, Enhanced Reliability: CA; Secret, Top
Secret: the country named, else the posting's single country, else no level); alternatives take
the lowest level; `active_required` only when the sentence says active/current/hold/possess/valid
and no "able to obtain" ("Secret clearance required" alone warns, D-60). Onsite with places: a
`LocationGate`.

**D-106 Overflow, boilerplate, coverage.** Overflow ranks REQUIRED/PREFERRED units (a group is one
unit) by IDF, ties by the smallest member id; the rest become IMPLIED at the overflow multiplier.
BOILERPLATE_ONLY: fewer than 3 distinct requirements and at least half the text in ABOUT/IGNORED
sections. `requirement_token_count` is the distinct ids mentioned in REQUIRED/PREFERRED clauses
plus the unresolved tokens there (phrases after "experience with", "knowledge of", ... with lead
and generic words dropped, and tech-shaped words: inner capitals, ".js"/".NET", "C++", unlisted
acronyms), sorted by their normal form.

## Audit and monitoring

**D-90 Audit trace. [Spec: "full feature trace" undefined]** `audit.build_trace(result)` is the
record persisted with every score (SPEC §0.4): the outcome and gate status; the raw and calibrated
score and the band; per dimension the value, weight, effective weight and abstain reason; the
caps, gates and confidence (value, components, reasons); one `[skill_id, necessity, m, relation]`
row per requirement group; flags and reasons; and the engine version, config hash, input digest
and `as_of`. Three fields go beyond the task brief: `trace_version`, `gate_status` and
`calibration{method, scope, n, version}` (a percentile cannot be read without its reference).
Never free text (D-14). The defence is structural: the trace reads only fields that hold canonical
ids, enums, numbers and dates (the scorer writes the ids) and refuses explanations, improvements,
intervals, fact details and gate details. As a backstop, an id-position string that is not
`[A-Za-z0-9][A-Za-z0-9._:/+#-]{0,199}` becomes `<redacted>`, and every confidence component name
that is not safe gets a numbered placeholder instead (`<redacted:1>`, `<redacted:2>`, numbered
over the sorted original names, so a lone one is `<redacted:1>`), so two never merge into one key.
A stray string thus shows up in the record instead of being copied; that is a backstop, not a
proof. `audit/` imports nothing from `score/`: the requirement fact codes are duplicated and a
test pins them to `score/coverage.py`.

**D-91 Opaque ids.** `opaque_id(user_key, salt)` is HMAC-SHA256 of the user key under the salt,
first 24 hex characters. Demographic data is joined to traces on this id only (SPEC §9), never on
a user id: `scripts/bias_audit.py` refuses any key that is not 24 lowercase hex. Tailrd's user ids
are small integers, so a short or guessable salt would make every id enumerable: `opaque_id`
raises ValueError for a salt under 16 bytes. The salt is a secret kept outside the repository and
outside the scorer's environment; rotating it severs every earlier link.

**D-92 Four-fifths counts people; rates count pairs. [Deviation from eval/metrics/fairness.py]**
The runtime monitor `audit.four_fifths(records)` takes `(opaque_id, group, selected)` records. A
group is assessed only when it holds at least `min_group_n` (100, EVAL §3) DISTINCT opaque ids;
its selection rate is selected records over records, so every scored pair of every person counts
and an abstained or gated pair is a record that was not selected. One person scored against 2,000
postings is one person (the reasoning of D-50). The offline harness `eval/metrics/fairness.py`
gates on PAIRS instead (its `PairScore` carries a group but no person id), so "n ≥ 100" means
pairs in the harness and people in the runtime monitor. Rates and ratios are exact `Fraction`s and
the verdict is `min ratio ≥ 4/5`: 5/100 against 4/100 passes though 0.04 / 0.05 is
0.7999999999999999 in floats. 4/5 is not a parameter (CLAUDE.md). A person in two groups, an empty
id or group, or a `selected` that is not a bool raises ValueError.

**D-93 Not assessed is not a pass.** A `FairnessReport` is `assessed` only when at least two
groups are eligible. Otherwise `passed`, `min_ratio` and `min_ratio_exact` are None, as is each
group's ratio: nothing was compared, so there is no verdict. (The offline harness reports
`passed=True` there, which suits a build guardrail and misleads a monitor.) A monitor reports "not
assessed" and never reads None as a pass or as a failure; `bias_audit.py` mirrors it with status
NOT_ASSESSED and exit status 3.

**D-94 PSI. [Spec: binning unspecified]** `audit.psi(reference, current, bins=10)` bins by the
reference's quantile edges (nearest rank, so each bin holds about 1/bins of the reference;
duplicate edges collapse when ties make bins indistinguishable), right-closed with open ends, and
sums `(a - e) ln(a / e)` with every share floored at 1e-4. The reference defines the bins, so PSI
is not symmetric; the 0.10 / 0.25 rules of thumb come from decile PSI of this kind. PSI is noise
on small samples: `drift_alarms` and `evaluate_drift` raise no SCORE_DRIFT alarm unless both
windows hold at least 200 scores (`PSI_MIN_SAMPLE`; the caller passes the sizes), and
`evaluate_drift` reports the skipped check with the reason "insufficient sample". `psi` itself is
plain arithmetic.

**D-95 Drift alarms (EVAL §6).** Strict comparisons: coverage < 0.92, abstention > 0.20 or > 2×
the 30-day baseline, PSI > 0.25, four-fifths ratio < 0.80, p95 > 300 ms; a value on its limit is
healthy. None skips a check; NaN, infinite, negative or out-of-range input raises (a percentage
passed as a fraction would never alarm). Abstention means no percentage was shown, NEEDS_REVIEW or
SCORED_LOW_CONFIDENCE; GATE_FAIL is an ineligibility finding. `COVERAGE_FLOOR` is pinned to
`taxonomy.coverage_alert_threshold` by a test; the fairness floor is `float(FOUR_FIFTHS)` and its
input must be `FairnessReport.min_ratio` (None when not assessed, which raises nothing).

**D-96 Bias-audit artifacts (SPEC §9).** `scripts/bias_audit.py` reads persisted lines
`{"opaque_id": <24 hex>, "digest": <16 hex, optional>, "trace": {...}}` (a digest that does not
match excludes the line and is counted) and a UTF-8 demographics CSV keyed by an `opaque_id`
column, one column per attribute (blank = not disclosed; "Prefer not to say" counts as blank; a
column in which every person has a different value, or that holds email addresses, is refused as
an identifier). It writes `bias_audit.json` and `bias_audit.md`: per attribute and band threshold
(the minimums in `bands.yaml`) the selection rates, exact four-fifths ratios, score distributions
and abstention per group. A trace is selected at threshold T when its outcome is SCORED or
SCORED_LOW_CONFIDENCE and its calibrated score is ≥ T; any other trace is never selected but stays
in the denominator. Groups under the floor are named with their size only. Exit status: 0 pass, 1
unusable input, 2 violation, 3 not assessed. A run with a floor under 100, thresholds other than
the band minimums or a subset of attributes is marked EXPLORATORY in both reports and in the
summary line, because each could hide a violation; a floor above 100 is refused for the same
reason (a fairness failure is terminal: nobody tunes around it or drops the failing group).

**D-97 Compliance document: facts versus owner decisions.** `docs/compliance.md` opens with
"Status and owner decisions": what this package provides; what the Tailrd integration must still
build, keeping apart what Milestone 9 of the plan schedules (trace storage, the web display, the
drift report) and what it does not (the review log, the demographics form and store and the salt
on that side, the audit export, the scheduled four-fifths job: building those, in particular
collecting demographics, needs the owner's decision first); and the policy calls and the product
premise that are only PROPOSED until the owner confirms them: candidate-facing only, a missing
score never hides a job, a named person reads the review log weekly, a candidate may ask a person
to look at any score, no hand edits to a score, autofill EEO answers are not an audit source, who
may join demographics, retention, cadence. Not proposals: "The score ranks; it never
auto-rejects." and "Get an employment attorney to review before launch." are SPEC §9's own
sentences.

## Language

**D-70** Language detection is a stop-word profile plus script detection (no model). Supported:
English, plus French where the taxonomy carries French labels. Anything else →
`NEEDS_REVIEW: UNSUPPORTED_LANGUAGE`. RTL scripts get an explicit fixture and the same route.
