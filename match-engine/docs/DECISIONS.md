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
The corpus (Task 2.3) is Tailrd's own postings: the `scraped_jobs` rows with a description longer
than 200 characters and no `duplicate_of`, pulled read-only by `scripts/pull_corpus.py` into
`data/raw/corpus/` (git-ignored, D-04); the committed statistics come from the Development branch
(5,473 postings first seen 2026-05-12 to 2026-05-14). A posting is read as its title, a line break
and its description, decoded from HTML first (62 % are stored as escaped HTML, which glues a skill
to the markup: `&lt;li&gt;Python`), through `Resolver.extract` in prose context; a skill counts once
per posting. Boilerplate counts once (fix round 1): postings are taken in id order, an exact
duplicate of an earlier decoded description is left out (1,549 of 5,473, so N = 3,924), and a
description line of 30 characters or more found in 5 or more distinct postings (an employer's
"About us", a benefits block; 891 such lines) counts only in the first posting that has it. Without
this, one employer's blurb repeated in 43 postings made Next.js and PayPal a 0.99 edge. `scripts/compute_idf.py` and `scripts/compute_pmi.py` run again after every
`scripts/build_taxonomy.py`, which writes an empty IDF and the curated edges only. Known limits of
this corpus: 5,344 of its 5,473 descriptions are stored cut short, and it is tech-heavy, so SPEC
§3.1's motivating example does not hold on it (Kubernetes in 73 postings, Microsoft Office in 7);
`tests/test_corpus_stats.py` keeps that example as a strict xfail, which flags the day a fuller
corpus makes it hold.

**D-22 PMI normalisation. [Spec: PMI_max unspecified]** Edge weight is normalised PMI:
`NPMI(a,b) = PMI(a,b) / -ln P(a,b)` (the per-pair maximum PMI), kept when `NPMI >= 0.4` and the
pair co-occurs in at least `pmi.min_cooccurrence` (5) postings; out-degree capped at 20 by weight
(ties broken by id). A global PMI_max would rank by rarity, not association. A pair where one id
is an ancestor of the other, at any depth, is left out before `build_edges` (Task 2.3): the
hierarchy relates them already (PostgreSQL under SQL, Kubernetes under Docker). Mined weights are
NPMI in [0.4, 1]; scoring clamps them (D-23). Pairs are counted over the corpus with its
boilerplate counted once (D-21); genuine pairs that only the repeated postings had carried (CPR and
First Aid, Objective-C and Swift, FPGA and Verilog, Ansible and Puppet, GraphQL and REST) are curated.

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
not use. The corpus review of Task 2.3 read the most frequent ids of Tailrd's postings that are
not SOFT in context and blocked the generic words that reached one specific node: O*NET "Act!"
("act as", "the Clean Water Act"), O*NET "Google" (the company), the curated alias "translating"
("translating business needs into ..."), ESCO "ecosystems" ("partner ecosystems"), the ESCO
alternatives "shape the future" and "work-life balance" (a benefit), and the bare "SDK" of the
Microsoft Windows SDK ("iOS SDK"). A blocked label drops its node (`skill.act`, `skill.google`,
`esco.ecosystems`). No id that is not SOFT reaches the 30 % suspect line in that corpus (the highest
is `skill.ai` at 24.8 %), so the review read the watch list of `corpus_report.md` (2 % and up) and
the next ranks by hand. Fix round 1 read every id that is not SOFT and has mined edges (320) and
blocked more: "ML systems" (machine learning, not the ML language), "EMI" (interference), "drive
forklifts" (no agricultural machines), "fabricate metal", "quality checks", "digital communications",
"optical systems", "optical products", "work with engineers", "data infrastructure", ESCO "logic"
("business logic", "PLC logic"; node dropped) and the bare "MOS" of multi-object spectrographs ("MOS
11B"); and as blocked phrases "building blocks" (an O*NET toy class), "Access Management", "Access
Control", "Access Engineer", "Ropes Access" and "Rope Access" (no Microsoft Access) and "child care
benefits" (no early childhood education).

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
line ("$25/hr CAD", "Salary (CAD)"), or after a "$" amount, perhaps with its rate, before the code's
bracket ("$80,000 (CAD)", "$30.00 per hour (CAD)"); after a pay word earlier in its clause (pay,
rate, range, salary, compensation, wage, stipend, bonus, amount, currency, figures; French salaire,
rémunération, montant, devise, taux) with only link words, numbers and brackets between ("Pay range
(CAD)", "Hourly rate (CAD)", "Pay range in CAD", "All amounts are in CAD", "Currency: CAD", "All
figures cited below are in CAD"), while a content word between keeps the skill ("a wide range of CAD
tools", "Pay attention to detail in CAD"); before an amount, a range, a rate unit or "per <unit>" on
its line ("CAD $80k", "CAD 2M", "CAD 25-30", "CAD/hr", "CAD per hour", French "CAD par mois"), a range
of years, hours or times excepted ("CAD 2-3 years"); inside a currency pair ("USD/CAD", "EUR/USD");
after an amount (thousands, also grouped by a plain, no-break or narrow no-break space as in French
"70 000", cents, a magnitude, four digits or more) or a number that closes a range, which is never a
version ("18 - 20 CAD", "25 CAD to 30 CAD"), unless the next word, or the word after one modifier that
is no function or rate word, is one of the curated `currency_skill_nouns` (plural nouns of CAD work:
drawings, models, files, parts, components, documents, sheets, assemblies, licences, standards, ...),
so "85,000 CAD plus benefits", "Base: 120,000 CAD | Bonus", "a 2,500 CAD scholarship", "120K CAD +
equity" and "5,000 CAD on parts" are money and "1,200 CAD drawings", "1,500 CAD shop drawings" and
"1,200 CAD part files" the skill; after a smaller number when a rate phrase or a money or pay word
("25 CAD hourly", "a 500 CAD prize"), a range to another number ("25 CAD - 30 CAD"), an addition ("40
CAD + HST") or a separator ("500 CAD (monthly)", "20 CAD | weekends") follows. "per <unit>" (hour,
week, month, year, annum, day; French "par heure, semaine, mois, année, an, jour") right after the
code is money by itself, like a rate unit on the code ("CAD/h"): Task 2.3 reverted round 4's "needs a
number", so "Rates quoted in CAD per hour" is money and so is "Spent 10 hours in CAD per week"; the
other rate phrases ("annually", "hourly", "a year") need a number before the code ("Taught CAD
annually" keeps CAD). A four-digit year is never a number ("the 2024 CAD standards manual",
"SolidWorks 2022 CAD."), nor is a number glued to letters ("CATIA V5 CAD") or inside the skill name
the code follows ("Fusion 360 CAD (cloud)"), and a number before a list comma ends the item before
("Office 365, CAD"); "Proficient in CAD (SolidWorks)" keeps CAD too. Deviation kept: a small count or
version before a clause end or ")" keeps CAD ("Fusion 360 CAD.", "10 CAD models"), so "Paid 25 CAD."
reads as the skill. Costs taken (Task 2.3): "Prepared figures in CAD" and "Bonus: CAD, SolidWorks"
read as money (a pay word with only link words before the code), and so does "Used 2-3 CAD tools" (a
range is an amount, and "tools" is no noun of CAD work). (6) Like
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
services in Go.", "Advanced Excel"). A key that needs its casing never matches text holding an
apostrophe (fix round 1): normalisation drops the apostrophe, so the company's "Scale's" read as
O*NET's "Scales"; the possessive's bare form is still tried ("Rust's" gives Rust). (7) Bare "PID"
yields nothing, deliberately, even where no controls context is near (Task 2.3): in Tailrd's postings
it is PID control ("PID loops", "tuning PID loops"), which reached O*NET's "Photoionization detectors
PID" through a derived alias. PID control, controls, controller(s), loop(s), control loop(s), tuning
and loop tuning are aliases of `skill.control_systems`, so a controls requirement and a PID
requirement credit each other in full; O*NET's three photoionization detector examples merge into one
curated `skill.photoionization_detectors` ("photoionization detector", "PID detector"), and
"photoionization detector (PID)" keeps the detector. (8) "control system" inside a compound that
names no control system is a blocked phrase: document, change, pest, cash, fire alarm and climate
control system or systems ("vehicle climate control systems" is the most frequent in Tailrd's
postings); "access control system" reaches `skill.access_control_systems` through a curated anchor,
as its plural always did. (9) The fuzzy fallback of `resolve` (SPEC §3.3; Task 2.3) never reads a
word of `common_words.json` as a typo: every normalised word (letters, perhaps hyphen-joined) used in
at least 20 postings that does not itself resolve exactly. A typo is rare in postings, a real word
frequent: "salary" no longer reaches SOLAR-2, "analysis" Analyse-it, "security" CompTIA Security+,
"engineer" nginx. A word this tech-heavy corpus rarely uses still fuzzes ("patients" reaches
"patents"), and a right fuzzy hit on a frequent word is lost too ("proactively" reached Initiative):
such a word becomes an alias when it is worth one.

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

**D-28a Reading credential status on a résumé (Task 6.4). [Tailrd]** `parse/resume.py`
`credential_status` reads the status D-28 leaves to the parser, for every mention (role titles and
bullets included). A status word marks a credential only next to that credential's own mention,
because a licence GATE_FAIL needs explicit evidence and a loose word must never turn a held licence
into a pursued one. Noun words (student(s), candidate(s), trainee(s), apprentice(s), apprenticeship;
étudiant, candidat, stagiaire, apprenti) count only right after the credential, with nothing but
brackets, a hyphen or an acronym between ("RN students", "Primary Care Paramedic (PCP) student",
"CDL-A trainee", "Electrician (309A) Apprentice"). Following words (in progress, pending, expected,
anticipated, in training, writing the, Level I/II/III, en cours, en formation, prévu) count after it
with only punctuation (never ; | •), connectors, issuer names, months, years or the credential's
program word (diploma, program, certificate, designation, exam, licence, ...) between ("CPA, in
progress", "RN (pending NCLEX)", "Primary Care Paramedic Diploma, Centennial College (expected
2026)"); a degree ("Master of Nursing", "BScN"), another skill or another lower-case word
("renewal pending", "Hazmat endorsement pending") stops them. Leading words (pursuing, completing,
currently completing, working towards, enrolled, studying for) count before it with only articles and
prepositions between ("Pursuing a CPA designation", "enrolled in RN bridging"). The nearest
credential is marked, with the same credential's alias right before it ("Registered Nurse (RN)
candidate"). A marked credential becomes its formal in-progress designation, stays when it is one,
and is dropped when it has none. A held mention elsewhere is never downgraded: status marks only the
mention it stands next to.

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

**D-98 Taxonomy freshness (SPEC §16). [Spec: growth rate, parent rule and write path unspecified]**
`scripts/refresh_taxonomy.py` parses the corpus with the posting parser, each exact duplicate and
each repeated blurb line counted once (D-21), over the postings first seen in the
`auto_promote_window_days` days ending at `as_of` (default: the corpus's latest `first_seen_at`, so
a rerun on the same file gives the same report). Coverage has two figures (fix round 1): raw,
resolved requirement tokens over all of them (`requirement_token_count`,
`unresolved_requirement_tokens`); and recurring, the same with the unresolved tokens found in fewer
than 2 distinct postings left out (a one-off is more often a typo, a fragment or a name than a
missing skill; resolved tokens all stay, since the parser exposes their count, not their set). The
alert and the exit code (1 below 0.92 or with no token) follow the recurring figure; the report
also gives the per-posting mean (what the `taxonomy_coverage` confidence input sees). A token's
rank is `recent x (recent + 1) / (earlier + 1)`: distinct postings in the window times growth
against the window before it. Promotion needs more than 50 distinct postings in the window
(strictly, as §16 says), a skill-shaped token (a phrase, a word in lower case or in capitals, or a
capitalised word with a technical suffix or form: SaaS, MLOps, GraphQL, GraphDB, Vue.js; "SpaceX",
"OpenAI" or "LangChain" go to the queue noted "possible employer or product", since no frequency
rule tells an employer from a skill) and a parent: the node with the highest NPMI (D-22) against
the token over the window, among the ids required in at least `pmi_min_cooccurrence` of its
postings and by at least `taxonomy.parent_min_df` (20) postings in all (PMI rewards rarity: the
low-df tail Task 2.3 parked), never a SOFT node or one under a SOFT node (ESCO's "perform services
in a flexible manner" sits under adaptability); no parent means the queue noted "no parent". The
committed taxonomy changes only with `--apply --approve <file>`, and only for the promoted tokens
the file lists (a person accepted each); `--apply` alone is refused. Then `compute_idf.py` and
`compute_pmi.py` must run, as after a build. The queue (`data/taxonomy/review_queue.csv`, top 50
not promoted) holds short tokens and counts only (D-04). On Tailrd's corpus (2026-05-14) after the
parser fix below: recurring 86.80 %, raw 78.71 %, per-posting mean 79.61 % (before: 83.28 %,
73.98 %, 73.28 %); the alert still fires, which is a real vocabulary gap, not noise; SaaS is the
one promotion proposed.

**D-98a Posting noise tokens (Task 7.2B fix round 1). [Tailrd]** The posting parser's unresolved
requirement tokens (SPEC §16, §17.1) leave out: a token whose folded form is a configured stop word
(`posting.unresolved.stop_words`: local, must, live, team, remote, perks, skills, hq, jd, li, et, ...)
or a generic word; a word in capitals whose lower case is a common word of the posting corpus
(`common_words.json`: "HIRING", "TODAY"), unless allow-listed; a two- or three-letter word in
capitals unless it is in `acronym_allowlist` (RAG, IAM, GPU, SDK, PCB, NDT, ...) or written in a
technical form ("/" before it; "#", "+", "/" or ".js" after it); every tech-shaped word of a
statement in capitals (more than half, and more than two, of its words outside resolved mentions
are capitals of two letters or more, judged per statement, not per clause). A trigger phrase ends
before its first function word (`phrase_stop_words`: is, are, to, through, join, considered, a,
the, of, for, with, ...), at most 4 words remain, and a phrase of generic, lead or stop words only
is dropped: "rim repair to join our crew" gives "rim repair", "languages is considered a plus"
nothing. Each unresolved token is an IDF 0.5 pseudo-requirement in the score interval and lowers
`taxonomy_coverage`, so noise cost every posting confidence; the token count falls by the same
amount as the unresolved list.

**D-140 Measured taxonomy gaps closed in the curated layer (Task 7.3). [Tailrd]** Gaps measured on
the eval vocabulary (31 common skills with no spelling that resolves), parked by earlier tasks
(SaaS, B2B, "CNO registration", "GN", AWS certification names, "patients"), and on Tailrd's
posting corpus (`review_queue.csv`) are closed as curated nodes and aliases in
`config/curated_taxonomy.yaml`, with reviewed parents, so every rebuild keeps them; generated
artifacts are rebuilt with `build_taxonomy.py`, `compute_idf.py` and `compute_pmi.py`, never edited.
Where an ESCO concept or O*NET example names the same thing, its name is a curated alias and it
merges (D-26): apply case management, conduct ICT code review, perform cost accounting activities,
manage the general ledger, develop plans related to client discharge, distributed computing,
non-destructive testing, printed circuit boards, OpenAI ChatGPT, Platform as a service PaaS, Time
series modeling TSM software. A new name that is also an ordinary word keeps to its sense:
Amplitude, HTTP, HTTPS and RAG are curated ambiguous keys (their casing; a URL's "https" or a
cleaning rag is no skill), and a blocked phrase keeps "RAG status" out. Fix round 1: a name whose
bare form has another common sense is kept only in its qualified forms. Reconciliation resolves only
as account, bank, balance sheet, GL or intercompany reconciliation ("equity, diversity, inclusion and
reconciliation" names no accounting skill), so the eval label "Reconciliation" stays unresolved.
The nursing node is cardiac telemetry monitoring (cardiac telemetry, telemetry nursing, telemetry
unit); bare "telemetry monitoring" is as often software observability and stays unresolved, so the
eval label "Telemetry Monitoring" does too. "RL" is no alias of reinforcement learning ("RL
circuits"). Bare "ASNT" (the society) names non-destructive testing, the domain; only certification
wording ("ASNT Level II", "ASNT certified") is the credential. "Miro" is list-only (D-144). Time
series analysis is its own node, the parent of time series forecasting, and case management has no
parent (social work, insurance and healthcare alike). Bare "Go-to-market"
stays blocked (D-27), so only "Go-to-market strategy" and its spellings are the skill and bare
"GTM" is not one. "CNO registration" names no credential (fix round 1): the College of Nurses of
Ontario registers RNs, RPNs and NPs, so reading it as the RN licence gated RPN postings on RN and let
an RPN résumé pass RN gates; the RN and RPN names themselves remain the only source of nursing gates.
"Graduate Nurse"
is a name of the formal in-progress node `cred.nursing_student` (D-28, D-62), so it never reaches
the licence. Bare "GN" is not an alias yet: in "RN or Graduate Nurse (GN) status" the posting parser
gives the restated designation in brackets a second gate of its own (it does the same today for "P.Eng.
or EIT (Engineer in Training)"), and it already reads "RN or GN" by phrase (D-103); "GN" can become an
alias once the parser folds a bracketed restatement into its group. "Identity & Access Management"
is no alias of identity and access management (a team name in a job title, pinned in
tests/test_extract_precision.py); the "and" spelling is. An AWS certification is named only with certification wording ("AWS
Solutions Architect certification"), never by the job title "AWS Solutions Architect", which D-62
would read as a held credential in a role title. `fuzzy_exclusions` (curated, written to
`resolver.json`) are ordinary words `resolve` never fuzzy-matches, where the posting corpus is too
small to make them common words (Task 2.3): patient, patients, parents, analyse, analysed,
supervisors, heating, tenants, seniors; each reached an unrelated node by edit distance
("patients" -> patents, "supervisors" -> hypervisors, "heating" -> betting).

**D-141 A sentence-initial name in a skill list or about languages (Task 7.3). [Tailrd]** An
ambiguous or cased key never opens a prose sentence (D-25: "Go above and beyond"). The keys listed
under `sentence_initial` (Go, French, English) may, when the sentence lists skills: right after
the key comes ",", "/", "&", "and" or "or", and a skill starts there ("Go, Python and Rust",
"Go/Python services"); and a listed language may also open a sentence that speaks of languages: a
cue word (fluent, bilingual, proficiency, language, spoken, written, asset, required, ...) or
another listed language stands in it ("English and French required", "French is an asset"). The
casing rule still holds ("go, Python" opens nothing), skills lists and `resolve` are unchanged,
and "English Bay" or "French doors" stay words unless the sentence has a cue: the cues include
general requirement words, so "French doors required." reads French (a capitalised "French" in mid
sentence resolved before this rule too). Other languages (Spanish, Mandarin) are not listed:
the rule widens only for the names the posting parser lane found missed.

**D-142 PhD is a degree level, not a node (Task 7.3). [Tailrd]** "PhD" (21 postings in the queue)
gets no taxonomy node: a node would make it a skill requirement or a field of study, and the
education parser already reads it as DOCTORATE (`config/parse/sections.yaml`). That the posting
parser still records "PhD" as an unresolved requirement token is a parser rule
(`posting.unresolved` checks its acronym stop list only for words in capitals), left to the
posting parser's lane.

**D-143 Review-queue approvals (Task 7.3). [Tailrd]** The tokens approved from `review_queue.csv`
are listed in `data/taxonomy/approvals/<date>-<who>.txt`, whose header says who approved them; the
2026-10-13 list was approved by the build controller on the owner's standing instruction and is for
the owner to audit. An approved token becomes a curated label or alias with a reviewed parent, not
a provisional node: `refresh_taxonomy.py --apply` writes provisional nodes only for tokens in more
than 50 postings (D-98), and a later `build_taxonomy.py` run would drop them. Rejected: employers
and organisations (SpaceX, DoD); job titles (CIO/CTO, HRBP); generic words and phrases (analytics,
automation, factory, manufacturing, monitoring, operations, production environment, technical
project delivery, full-cycle quota-carrying, LOB, SDK, scripting); acronyms with several common
senses (CI, DSS, FP, IPC, NAS, RT, SOC, UT, GTM, networking, and in fix round 1 RL), whose unambiguous long forms are
aliases instead (ultrasonic testing, radiographic testing, NAS 410, FP&A); STEM (an umbrella of
fields); a phrase whose skill already resolves ("building data processing pipelines"); PhD (D-142).
SparkTM was withdrawn in fix round 1: it was "Spark™" spelled out by NFKC, and the ingest
normaliser now drops the trademark, service-mark and registered signs before NFKC.

**D-144 List-only names (Task 7.3 fix round 1). [Tailrd]** A key listed under `list_only` (Miro) is
also a given name. In prose it stands only inside a skill list in its sentence: ",", "/", "&",
"and" or "or" joins it to another skill before or after it ("Ran workshops in Figma and Miro",
"Miro, Figma and Jira"), or (fix round 2) right after a tool cue ("with", "in", "using", "via",
"on": "Experience with Miro", "Facilitated workshops in Miro") or a tools label ("Tools: Miro",
"Tooling: Miro"); never when a capitalised word that is no skill follows it, which makes it a name.
"Worked with Miro Kovac" and the résumé name line "Miro Petrov" name no tool. Longer keys ("Miro
board") are unrestricted, and skills lists and `resolve` are unchanged.

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

**D-35 Role relevance (§5.3). [Deviation: requirement space; norm ratio, not cosine]** Relevance is
computed in the posting's requirement space: `v_job[r] = idf(r)`, `v_role[r] = idf(r) × max credit of
the role's mentions for r` (negated/aspirational mentions excluded), and relevance = `|v_role| /
|v_job| = sqrt(Σ idf² c² / Σ idf²)`. A plain cosine over the union of skills would lower a role's
relevance for every unrelated skill the person also used there, i.e. it penalises breadth. Role
titles that resolve to skills enter as `ROLE_TITLE` mentions. Why not §5.3's cosine (Task 3.6 fix
round 1): a cosine is not monotone in one component, so adding a matching skill to a role could
lower the score (SPEC §10; a verified pair lost raw 0.356657 -> 0.356106), and it is scale-invariant
(uniform parent credits of 0.40 scored 1.0). The norm ratio equals the cosine whenever every credit is
0 or 1, is monotone in every credit and bounded by [0, 1]; on that pair relevance rises 0.1848 ->
0.1977. The D-36 sweep is unchanged.

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

**D-47 Zero skill overlap (§14.5 read with §5.6).** §14.5's "zero skill overlap → raw = 0.0" cannot
hold literally while §5.6 never abstains (no requirement 1.00; floor 0.20 × 0.70 = 0.14). It is read
as: the skill dimensions (§5.1, §5.2, §5.7) and §5.3 score valid zeros, never abstentions, the output
is a valid score and the band is Limited. The minimum case (tests/fixtures/profiles/02_zero_overlap)
scores raw 0.0175 (education 0.14 over the scored weight 0.80), calibrated 2, Limited.

**D-48 Totality example counts. [Deviation from the Task 3.6 brief's 2,000]** `tests/test_totality.py`
runs 300 Hypothesis examples per test by default (env `TOTALITY_EXAMPLES`), on every commit and never
skipped (§13), so `make eval-fast` stays inside its budget. The Milestone 3 gate is `make gate`, which
runs `TOTALITY_EXAMPLES=50000` and the slow tests (1,000-run determinism); it passed in 51 min.

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

**D-50 addendum (Task 9.1). [Tailrd]** Live references are per job family (key = the posting's
`role_category`, always passed as a `str`: a non-str key would silently skip the family tiers) and
global (key `*`), rebuilt by `fit_engine.refresh_calibration` from the banked raw scores of the
current config hash (every outcome that produced a raw score, the panel builder's rule) and stored
in `match_calibration`; a candidate is a distinct person (see the note below). The committed panel is checked
against its MANIFEST (sha256 and version) when the engine is built, and a mismatch stops the build.
Known limits, accepted for now: small families ride on synthetic counts (Product Management has 24
panel candidates and falls through to the pooled panel; Business Analyst has 56); live pairs (every
user against every job, most of them unrelated) will skew lower than the panel's median raw score
of about 0.57, where each synthetic profile was scored only against its own target posting, so a
live family reference, once in charge, reads the same raw score as a higher percentile than the
panel did. Recheck the panel against the live distribution when any family reaches 30 real
candidates. A stored score is calibrated when it is banked and is not re-read when the references
change; a refresh moves scores only as rows are re-banked.

**D-50 note: "fingerprints" means people (Task 9.1 fix round 1). [Tailrd]** The plan's "distinct
résumé fingerprints count as candidates" is read as distinct people: a user who edits a résumé
gets a new fingerprint, and rows under the old one survive on jobs not re-banked since, so
counting fingerprints let one person count several times and opened the 30-candidate gate early.
`refresh_calibration` keys each candidate by a sha256 of the user id (counted, never stored),
samples only the rows under each user's newest fingerprint (latest `scored_at`), and only on
active jobs (listing status not hidden from the feed).

**D-51 Phase 2** (isotonic, pure-Python PAVA at runtime, fitted offline) is built and versioned
but stays off until `calibration.isotonic.min_labels` (500) outcome labels exist. Today there are
two tracked applications.

**D-52 Bands and naming.** Bands come from the calibrated integer: ≥85 Strong, ≥70 Good, ≥55
Partial, else Limited. Display "Fit score 76 · Good match", never "76% match".

## Explanation (§8) [Task 4.1]

**D-53 Wiring.** `score()` runs the scorer, then `matcher.explain.attach_explanation`: an
`explanation` for every outcome when `options.with_explanation` (the default), `improvements` and
`explanation.to_improve` when `options.with_counterfactual`. The import is late (explain/ re-runs
`score()` for counterfactuals), so score/'s purity allowlist includes `matcher.explain`, and explain/
carries the same purity guard (no I/O, no clock, stdlib + pydantic + matcher only). Options that are
not `ScoreOptions` explain with the defaults. Bulk sweeps pass `with_explanation=False`.
`build_explanation` and `improve` take `strict`, passed from `options.strict`: tests see a defect,
production degrades to the headline and the reason sentences (never an empty headline: a scored
result falls back to its band) and to no improvements.

**D-54 What each outcome shows.** SCORED: "Fit score 76 · Good match" (a displayed interval
replaces the number: "Fit score 71–79 · Good match"), the basis, strengths, gaps, notes and
improvements, with signed raw-scale impacts ("+0.31"; a value that rounds to zero is "0.00").
SCORED_LOW_CONFIDENCE: the band as headline, strengths and gaps WITHOUT impacts (a column of raw
contributions would be the suppressed number under another name, D-44), notes. GATE_FAIL: "Not
eligible: <sentence of the first failing gate>", no strengths or gaps, the other reasons and warnings,
and a line that the profile facts can be updated. NEEDS_REVIEW: "We couldn't score this" and one
sentence per reason, no strengths or gaps.

*Addendum (Task 9.4, web display) [Tailrd].* The engine still emits the signed raw-scale impacts;
the web app does not print them. It shows each impact as a word with a small bar sized against the
largest |impact| shown on the page: at least 2/3 of it is "Big plus" / "Big gap", at least 1/3 is
"Plus" / "Gap", below that "Small plus" / "Small gap". The raw value ("+0.31") stays in the item's
`title` and `aria-label` for traceability. A raw figure on an unlabelled scale read as points or
percent; the words keep the order and relative size without inventing a scale. Because the jobs list
carries no interval (it is not banked), a job card shows the band only, never the number; the
number, or the range when the interval is displayed, appears only in the detail view, which also
hides the basis sentence when the range replaces the number (the basis names the point).

**D-55 Truthful names.** A matched requirement is named as held only when the résumé shows it (or
one of its OR-group members); credit earned through a related skill reads "Terraform (through your
Amazon Web Services experience)". The required-skills line ("k of n required skills") is always
kept, so every REQUIRED group is named in a strength or a gap. Gaps are REQUIRED and PREFERRED groups
with m < 0.5, impact `-(effective weight × idf×wm / Σ idf×wm × (1 − m))`; IMPLIED groups are never
gaps (missing a keyword the posting only mentions is normal) but may be suggestions. With no work
experience and a stated minimum of zero, the experience item is the strength "This role doesn't
require prior work experience". Education is a strength only when the posting states a requirement.

**D-56 Which notes.** Low-confidence and review reasons, gate warnings, caps that BOUND (D-43:
"Missing Terraform, a skill this role centres on, limits this score."; a non-binding critical miss
is only its own gap), the résumé-side abstentions DATES_MISSING and FUNCTIONAL_RESUME, and the
informational flags, except MIN_YEARS_DEFAULTED (the experience item already says "vs a typical")
and NO_WORK_EXPERIENCE when an experience item is shown. Posting-side abstentions are not notes.
Two reasons are said from the data instead of FLAG_TEXT: LOW_EVIDENCE_DENSITY names the matched
skills whose only affirmed evidence is the skills section ("JavaScript is only listed in your skills
section; showing it in a project or role would count for more.", up to three then "and N more";
projects are bullet-level evidence, D-12), and LOW_DIMENSION_COVERAGE names the parts that abstained.
No note says "most".

**D-57 Suggestions (`to_improve`).** SCORED only (the other outcomes show no number to move). Target:
the next band's minimum above the calibrated score; already Strong gets none. Candidates: members of
REQUIRED, PREFERRED and IMPLIED groups with m < 0.5. A skill is added as one AFFIRMED
`counterfactual.added_evidence` / `added_verb` mention in the most recent role (current, latest
start; else latest end; else the first listed), or a new undated project when there are no roles.
`learning_cost = weeks_by_kind[kind] × (1 + weeks_per_hop × hops)`, hops from the nearest résumé
skill capped at `unreachable_hops` (also for a skill absent from the graph, costed as TECHNOLOGY).
Every candidate is re-scored alone; the best `counterfactual.max_candidates` (16) by raw gain per week
form the pool; greedy by raw gain per week up to `max_counterfactual_steps`; one pass that drops or
swaps for a cheaper skill while the target is still reached (total weeks never rise). The chosen set
(when it reaches the target) and up to three single skills that raise the integer score; the band in
parentheses only when it changes. Retuning any `counterfactual` value changes the config hash and so
re-fingerprints every stored score (D-82), although no score moves: change it deliberately.

**D-58 Skill names.** Graph labels are display text, except ESCO's lower-case phrases, which get a
capital first letter unless the second letter is a capital ("pH meters"). An id the graph does not
hold is shown from its own spelling ("skill.event_sourcing" → "Event sourcing").

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

**D-86 A missing score is not a low score. [Tailrd]** `job_match_scores.score` holds the displayed
integer of a `SCORED` result and is NULL for every other outcome: `GATE_FAIL` and `NEEDS_REVIEW` show
no number, and `SCORED_LOW_CONFIDENCE` shows only its band (D-54; its calibrated integer is audit-only
and is never sorted). Never a numeric sentinel (0, -1), which could leak into a display, an average
or a threshold. The row keeps the raw score, band, outcome, confidence, engine version and config
hash for audit. Match-sorted queries put NULL after every scored row with an explicit, portable
ordering (`fit_engine.score_order`: a `CASE` on NULL, then the score descending; NULLS LAST is spelt
and defaulted differently in SQLite and Postgres). The UI labels them ("We couldn't score this" /
"Not eligible", compliance.md section 2), and no count, alert or "strong" filter treats them as
low or as matches. The migration makes the column nullable (catalog-only on Postgres).

**D-87 The scoring date. [Tailrd]** Every score is as of the first day of the current UTC month
(`fit_engine.fit_as_of`), for parsing the résumé, parsing postings and scoring. The parsed résumé
depends on the date ("Present", months of experience), so a daily date would change every
fingerprint (D-82), and so re-score every user against every job, every day. Months are the
precision résumé dates have. A résumé whose only dates start later in the month than the first is
NEEDS_REVIEW INVALID_AS_OF until the next month; accepted as rare.

**D-88 Profile facts to the engine. [Tailrd]** Work authorisation and location are read from the
saved answers through `backend/services/profile_facts.py` (the port of the extension's reading),
each of its ten text fields cut to 2,000 characters first. A `settings.location` equal to the
column default "United States" is dropped before the facts are read: it is the bot's search region,
never a residence. `authorized_countries`: US, CA, the residence and any country the answer names,
where `authorized_in` says True. `needs_sponsorship`: `needs_sponsorship_in` over US, CA and the
residence minus the authorised ones; True when any says True, False when all say False, else None
(so a general "I need sponsorship" beside a Canada-scoped status stays unknown for the US: a
warning, never a fail, exactly as the extension reads it). Citizenships are never inferred from
the profile (a stated citizenship that misses a posting's country fails a citizenship gate, D-61,
and the profile cannot say "only"). "Active clearance" is
`gates.security_clearance.unknown_level_token`, never a literal. The candidate's place is the
profile's country only at high confidence, with its region and city. The résumé's own statements
fill what the profile leaves unknown (authorised countries are united, the profile's sponsorship
answer wins, the résumé's citizenships are kept); with no profile place the résumé's contact place
stands. Unknown stays None.

**D-89 Posting cache version. [Tailrd]** The `v` of D-84 is `"pv1-"` + 16 hex of sha256 over the
engine version, the parse-config version, the combined config hash and the year of the scoring
date (a requirement's years-since-release clamp reads the year). Any of them changing re-parses a
posting the next time it is read; a parser fault is an INTERNAL_ERROR review and is never cached.
Until Task 9.2 removes `local_match`, its `{"v": 1, "top", "tags"}` entries and these share the
column and would overwrite each other, so 9.2 must switch every reader at once. Because the
version holds the year, every cached posting is re-parsed in the first sweeps after 1 January
(about 47k postings at about 20 ms each, roughly 16 minutes of CPU once a year). The same version
and the description length form `job_match_scores.posting_key`: a banked score is current only
while both its fingerprint and its posting key match, so a description backfill or edit, a
parse-table change or a year change rescores it, and an INTERNAL_ERROR result is stored with a
NULL key and retried by the next sweep.

**D-107 Reading banked scores without the engine. [Tailrd]** A fit fingerprint (D-82) needs the
built engine (about 10 s cold per process), which the feed, the sidebar and the job list must
never pay. `backend/services/fit_reads.py` holds everything those paths need and imports nothing
from `matcher`. `bank_scores` records, per user, the fingerprint it banked under and an
`input_key` (sha256 of the résumé document, its raw text, the ten profile fields and the
clearance answer: every stored input the fingerprint depends on) in `match_fingerprints`. A
request serves rows under that fingerprint while its `input_key` still matches the stored inputs,
plus the exact fingerprint when its process has already built the engine; rows are one per
(user, job), so two fingerprints never count a job twice. An edited résumé or answer changes the
key, so its old scores stop being served at once ("not scored yet" until the next sweep, upload or
breakdown). The row also stores the scoring month it was banked for (`as_of`, D-87); a
remembered fingerprint from another month is not served, so a new month shows "not scored yet"
until the sweep rescores (the month is a date rule, computed without the engine; a NULL month
from before the column is served). A request path calls `bank_scores` only when
`engine_ready()`, for at most 50 not-yet-scored jobs of its page, on a short-lived session of its
own (a commit on the request's session would expire, and so reload, every job row it loaded).
Accepted: between a config-changing deploy and the next sweep, a cold process serves the previous
config's fit rows for an unchanged résumé (they are engine rows, never AI or "local1:" ones).

**D-108 The sweep, without AI. [Tailrd]** The cron sweep keeps its scope (the newest 2,000 jobs
the feed shows per user, in the user's region, not yet alerted) and its 120 s scoring box, and
calls `bank_scores` in chunks of at most 500 job ids, each committed on its own, yielding to the
event loop between chunks (the cron-poll wall clock can only cut at an await). An IntegrityError
(a concurrent sweep or upload banked the same pair first) rolls back and skips that chunk only. A
`FitTraceSaltError` stops scoring for the rest of the sweep (`fit_trace_salt_missing: true`, an
ERROR log, banked Strong matches still emailed); there is no fallback scorer. Alerts go out only
for SCORED rows in the Strong band (D-85); `MATCH_NOTIFY_THRESHOLD` is gone. Once per sweep with
at least one user scanned: `refresh_calibration`, then `audit.evaluate_drift` with the 30-day
baseline (current-config rows banked before the sweep started, at most 5,000 raw scores, newest
first) as the PSI reference and this sweep's raw scores as current, both sample sizes passed (an
empty side is reported as skipped, never evaluated); abstention is NEEDS_REVIEW plus
SCORED_LOW_CONFIDENCE over all pairs (D-95). Coverage, four-fifths and latency are not measured
by the sweep and are listed as `not_measured`, so no check is silently dropped. The upload
pre-score banks the newest 300 visible jobs in a worker thread on its own session; it sends no
alert.

**D-109 The breakdown and the feed fields. [Tailrd]** `/ai/match-breakdown` is
`fit_engine.explain_job` (a sync route, so the first call's engine build runs in a worker thread),
never charges the daily AI limit, and also banks the job's score so the card agrees with the
panel. `MatchBreakdown` keeps its seven old fields (`strengths` / `weaknesses` stay lists of
strings: the labels of the structured items) and adds `outcome`, `band`, `display_score`,
`headline`, `basis`, `strength_items` and `gaps` (label, impact, note, code), `to_improve`,
`notes` and `interval`. Only SCORED carries numbers: every other outcome has 0 in the old number
fields and no `display_score`; `match_label` is the band, else "Not eligible" (GATE_FAIL) or "We
couldn't score this" (NEEDS_REVIEW). The three old sub-scores are the engine's required-skills,
experience and domain dimension values on 0..100. The job list overlays `match_score` (the
displayed score for SCORED, else 0), `match_band` and `match_outcome` per user; the global
`scraped_jobs.match_score` column (an AI-era leftover) is never shown on a card again. No
reader serves any AI-era per-job value still on `scraped_jobs` (fix round 1): the card resets
`experience_score`, `skill_score`, `industry_score`, `requirements_met`, `requirements_total`,
`requirements_detail` and `match_summary`, and `match_label` is the band or ""; `min_score`
filters on the user's own SCORED engine score (NULL never passes; no résumé keeps nothing);
`sort=match` without a résumé is newest first; `/jobs/stats` `avg_match_score` is the user's
SCORED engine average or null; the extension's apply session carries the user's engine score or
0. No response or email says "% match": the alert subject is "Strong match: <title> · fit score
88". A sweep that scored nothing because the salt is missing skips calibration and drift
(listed as "skipped: no scores"), and the scrape workflow warns on `fit_trace_salt_missing` and
`engine_unavailable`.

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
statistics skill). Credentials are never skill requirements, except as an alternative (below). A
`LicenseGate` needs plainly held wording in a REQUIRED clause (a holding word anywhere in the
clause, its own name's words included, or the credential opening it: "must hold a valid P.Eng.",
"licensed RN", "RN license in good standing"); the credential is then `certifications_required`.
Not-held wording anywhere in the clause (eligible, eligibility, registrable, obtain, pursue,
pursuing, working towards, willingness, upon hire, "within <N> months") never gates: a new grad
working towards the licence must not fail; the credential is `certifications_preferred` (fix round
1, binding ruling). "<licence> or <its in-progress form>" ("Registered Nurse or eligible for
registration", "RN or Graduate Nurse (GN)", "P.Eng. or EIT") gates on the licence or its formal
in-progress designation (D-62; the designation is the configured in-progress id related to the
licence in the graph), and gates nothing when the licence has none. Alternatives share one gate
and one held member holds the group. A credential with a non-credential alternative in its clause
("AWS Certified Solutions Architect or hands-on AWS experience", "... or equivalent experience") is
an OR-group with that alternative (a one-member `or_equivalent` group for bare experience), never a
gate, and only a preferred credential for §5.6. "CPA designation or equivalent" is required but
gates nothing. "Under RN supervision" is neither.

**D-104 Text, truncation, empty descriptions.** Entities are decoded (twice at most: Tailrd stores
escaped HTML), tags become line breaks ("<li>" a bullet) or vanish, script/style bodies drop,
inline bullet glyphs split lines, then `normalize_text`. A description whose length is within
`truncation.tolerance` of a cap (2,000 or 3,000) and that ends in "..." or "…" is POSTING_TRUNCATED: its
last word is dropped when the cut fell inside it ("Java..." from "JavaScript"), BOILERPLATE_ONLY is
never concluded from it, and the scorer caps the pair's parse-quality component at tier B
(`score/confidence.py`; the requirements may go on past the cut) and POSTING_TRUNCATED is always
among the confidence reasons and the result's reasons, SCORED included, so the penalty is named
(fix round 1; an interim rule pending the owner's view of how strongly truncation should weigh).
The résumé page cap keeps its own TRUNCATED flag. An empty description is title and
metadata only: seniority, internship, family, industry, remote policy and the location gate are
kept, nothing is read from the title as a requirement, and POSTING_UNSCORABLE sends the pair to
NEEDS_REVIEW. `max_chars` (60,000) bounds absurd inputs (also POSTING_TRUNCATED).

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
`LocationGate`. Every recognised gate phrase (clearance context and levels, citizenship,
residence, sponsorship, work authorisation) is masked from skill extraction and from the
unresolved-token count: "TS/SCI" is no TypeScript, "Reliability status" no reliability skill.

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
outside the scorer's environment; rotating it severs every earlier link. On Vercel the trace
writer (`fit_engine.bank_scores`, `delete_user_traces`) and the scorer share one serving process,
so that separation is procedural: only the two writers read `FIT_TRACE_SALT`, and `score()` never
receives it.

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

## Evaluation harness (Task 5.3) [Tailrd]

**D-110 Guardrails and exit code.** `python -m eval.run` writes `reports/<UTC timestamp>/` and exits 1
when any guardrail has `passed = False`. A guardrail that cannot be measured has `passed = None` and
reads "not assessed", never PASS. This covers a delta with an undefined point and a four-fifths check
with no group of n >= 100 (D-93). Four-fifths is a guardrail on every suite whose pairs carry a group
(selected = raw >= 0.70, the Good band), the golden set included through its optional `group` field.
The synthetic corpus has no groups, so it always reads "not assessed" there.

**D-111 Vocabulary ids in the taxonomy's namespace.** The generator's vocabulary (`skill.<slug>`) is
translated to taxonomy ids by resolving each label or alias with the real resolver, EXACT or ALIAS
only (the methods the extractor uses). Translation can merge: on the default vocabulary Market
Research and Competitive Analysis both become `skill.market_research`, User Research and Usability
Testing become `skill.user_research`, and Visual Design and Graphic Design become
`skill.graphic_design`. Merged mentions count as one skill. Two merged posting requirements become
one requirement with the stronger necessity. 31 of 178 vocabulary skills have no resolvable
spelling. Translated résumés drop them, translated postings carry them as unresolved requirement
tokens, and their labels are listed in metrics.json (`taxonomy_unknown_labels`). They are reported,
not hidden: the guardrails "Parser fidelity: recovered skills (all vocabulary)" (>= 0.95, every
skill the rendering shows) and "Taxonomy coverage (posting requirement tokens)" (>= 0.92, SPEC §16)
both count them, and both fail on the default vocabulary (0.867 and 0.833 on the eval-fast sample).
Recovery over taxonomy-known skills only (0.984) is a diagnostic.

**D-112 M6 format invariance has three parts.** This supersedes BUILD.md Milestone 6's "<= 0.02
spread across all 48 renderings" (orchestrator ruling; SPEC §14.3 makes a functional résumé score
differently by design). The three parts: (a) max - min raw over the 6 formats of each template;
(b) the same over the 42 renderings of the 7 information-equivalent templates (all but
`functional`); (c) every `functional` rendering against `expected_resume(template="functional")`.
Each must be <= 0.02 at p99. A rendering with no résumé or no raw score has an unbounded spread.
Industry codes are stripped from both sides of oracle-versus-parse comparisons, because no page
prints the oracle's NAICS codes. Parser fidelity requires recovered skills (all vocabulary) >= 0.95,
and date error <= 1 month on every dated rendering (max, not a percentile; a role the parser misses
counts as unbounded).

**D-113 Abstention precision.** "Hard" pairs are fit levels 1 and 2, the borderline candidates an
abstention should concentrate on. Observation on the synthetic corpus: all 45 abstentions (9 %) are
fit-0 candidates from another family, abstaining with LOW_EVIDENCE_DENSITY, so abstention precision
is 0 %. The engine abstains on easy negatives, not on borderline pairs.

**D-114 Pairs as metric records.** NEEDS_REVIEW is an abstention (score None). GATE_FAIL is
ineligible: it ranks last (score 0.0), and its band counts as Limited (0) for band accuracy, so a
gate never hides a pair from that metric. SCORED and SCORED_LOW_CONFIDENCE rank by their raw score.

**D-115 Regressions.** A pair regresses against a baseline when its score moves against the advanced
direction by more than 0.005 (a drop for an advanced pair, fit >= 2; a rise for a pair that is not),
or when it newly abstains. Regressions are listed worst first. Metric deltas use `bootstrap_delta`
with each metric's own `min_effect` (0.01; 0.005 for band off-by-two).

**D-116 Parser bugs get unit regression tests.** A bug fixed in parsing (sections, entries, dates,
ingest) gets a regression test next to the code it fixes (tests/test_sections.py,
tests/test_parse_resume.py, ...), not a mutation-suite case. `eval/suites/mutations.py` is the
regression suite for scoring arithmetic (EVAL 2.3) and grows with every scoring bug fixed.

**D-117 Mutation constructions.** Every EVAL 2.3 row runs on a constructed pair whose arithmetic
makes the stated range exact, and the closed form is checked before and after the change. Example:
"remove one required skill (idf 0.8)" starts from a moderate candidate (raw 0.772). From a stronger
base the miss would trip the critical-miss cap (idf 0.8 > 0.7, D-43), and the drop would be the cap's.

**D-118 eval-fast budget. [Deviation]** BUILD.md M5's "under 2 minutes" applies to `make eval-fast`
as the eval harness: `pytest tests/eval` plus `eval.run --layers 1,2`. The full non-slow unit suite
is `make test`, which CI runs separately. Measured 2026-10-13 on the development machine (8 cores,
shared with other work): warm parse cache 52 s (tests/eval 23 s, eval 27 s); cold cache 8 min 27 s,
of which 457 s is OCR of the 80 scanned renderings. The old target, which ran the whole unit suite
first, took 2 min 48 s warm. The parse cache (`.cache/eval/parse/`, git-ignored) is keyed by render
identity and by what the parser reads (`synthetic.parser_inputs`): its modules, config/parse/**,
verbs.yaml, negation.yaml, the curated taxonomy files, the taxonomy manifest and resolver tables,
and the ingest and taxonomy sections of scoring.yaml. A weights or scoring-module change keeps it
warm. A cold full `make eval` (24,000 renderings) takes about 6-7 hours.

## Tuning and holdout (Task 8.1) [Tailrd]

**D-119 Fairness that cannot be assessed leaves a tuning trial feasible. [Deviation]** EVAL 5.1
makes `fairness_ratio >= 0.80` a hard constraint, infeasible if violated, and the Task 8.1 brief
adds "infeasible otherwise" where fairness is not assessable. Taken literally, every trial on the
synthetic corpus (whose pairs carry no group) would be infeasible and no proposal could exist.
Ruling: a trial whose four-fifths guardrail is "not assessed" (D-93) is feasible, and the proposal
then states prominently "fairness not assessed: proposal cannot be adopted until a grouped labelled
set exists", with the verdict NOT ADOPTABLE at best. A fairness FAIL is still infeasible, and a FAIL
on the current weights is terminal: the tuner stops before any trial and exits 1 (CLAUDE.md, EVAL 7).

**D-120 What a tuning trial scores.** Weights reach neither the parser nor the posting parser, so a
trial re-scores fixed inputs and parses nothing: on the synthetic split, the 500 oracle résumés
(`corpus_pairs`) against their target postings, the pairs the synthetic layer ranks; on the dev split,
`eval/corpora/dev/pairs.jsonl` (relevance 3 hired, 2 advanced, 0 otherwise). The sampler draws a
point in (0, 1]^7; `-log(u)` normalised is a flat Dirichlet draw `d`, and `w = 0.03 + 0.79 d` keeps
every weight at or above the lower bound. Weights live on a 4-decimal grid summing to exactly 1 as
decimals (the rounding residual goes to the largest weight); a vector with a weight above 0.45 is
rejected without scoring. Optuna TPE, seed 0 by default, constraints through `Trial.set_constraint`.
`make tune BUDGET=50` on the synthetic split takes about one minute (2026-10-13). Sensitivity
(SPEC 10) runs on the proposed vector; a perturbed vector outside [0.03, 0.45] is still scored
(an unchecked in-memory config, never persisted) and marked.

**D-121 Holdout format and look budget.** `labels.enc` is AES-256-GCM (`MHO1 | nonce | ciphertext`),
key 32 bytes, strict base64, in `MATCH_HOLDOUT_KEY`: an environment secret of the `holdout` GitHub
environment (deployment branches `release/**`, required reviewers, no repository-level copy), visible
only to the `make eval-holdout` step. The bundle (inputs and `labels.enc`) lives outside the public
repository and is fetched into the runner from `MATCH_HOLDOUT_BUNDLE_URL`; `eval/corpora/holdout/` is
git-ignored, and the `holdout-guard` job checks every path touched by any commit of a pull request or
push (a required status check). `make eval-holdout` decrypts only on `release/**` refs. Each look is
claimed in a durable ledger before decryption (review I-1, option A): `evaluation_budget.ledger.csv`
on the `holdout-ledger` branch, which every release branch shares. The claim fetches that branch,
refuses the 11th look of the calendar quarter (UTC) and a second look at the same commit SHA (EVAL 0:
once per release candidate), and pushes the `date,ref,sha,purpose` row as a fast-forward commit (a
concurrent claim is rejected). An unreadable branch or a failed push refuses the run, and nothing is
decrypted. `docs/evaluation_budget.ledger.csv` is only the format reference. The tool prints aggregates
only; a malformed input is reported by index and exception type, never by content. Layer 4 inside
`eval.run` only reports whether the key is present.

**D-122 CI dependency pins.** CI installs `-c constraints-ci-<python>.txt -e ".[ingest,eval,dev]"`. The
two constraint files are compiled with `uv pip compile` for Linux x86_64 (manylinux_2_28) and Python
3.11 and 3.12 respectively, so every transitive dependency is pinned for the platform CI runs on.
numpy 2.5 and scipy 1.18 have no Python 3.11 wheels, so 3.11 gets numpy 2.4.6 and scipy 1.17.1
(environment markers in pyproject.toml). The OCR (rapidocr-onnxruntime, pymupdf) is pure pip; the
runner adds only `libgl1` and `libglib2.0-0` for OpenCV. The parse cache is restored by a key built
from `generator_fingerprint` and `parser_fingerprint` (D-118) and saved even when `make eval-fast`
fails (split restore and save), so the next run is warm while guardrails are red. Actions are pinned
by full commit SHA.

## Format invariance (Task 5.4) [Tailrd]

**D-130 OCR text repair.** Scanned résumés are repaired after ingest and before parsing
(`parse.resume.parse_ingested` runs `ingest.ocr_repair.repair_ocr`), only when ingest flagged
OCR_USED; typed text is never touched. The rules are tables in `config/parse/ocr.yaml` (a fifth parse
config file, part of the parse-config version): a misread roman level right after a title word of the
role parser ("Analyst Ill," -> "III"; "Il" reads as II, never guessed up to III), a misread bullet glyph
at a line start (": ", ". ", ";", "。", or "e"/"o" and a space, then a capital or a digit; a stray quote
after it is the same speck), a dot read for a space before a capitalised word ("to.Present") and a dot
touching a range dash ("2024. - Present"), a region code read in lower case at a line end ("Denver, co",
only when the capitals make the line a place), a speck read as a final period after a line that is
exactly a listed header ("Education."), and misread acronyms ("Bl" -> "BI", "Cl/CD" -> "CI/CD") or a
lost letter of a pair ("Ilustrator"). An acronym or letter repair applies only when the repaired word is
an exact key of the resolver (EXACT or ALIAS, one list-context mention spanning the whole word, never a
fuzzy match, so the check has no side effect) and the word as read is not. [Deviation] The brief says
"resolves EXACT"; most acronyms the taxonomy knows are aliases of a longer label (BI, IaC, GCP), so EXACT
alone would repair almost nothing; ALIAS is still an exact key. The first line (the person's name, "Al")
is never repaired. Nothing is ever added: every rule restores a spelling the page shows.

**D-131 Undated entries of a functional résumé are roles.** In an experience-like section with no dated
line and no bullet at all, a head line naming a job or an organisation is an undated entry without
bullets; a line naming a title and an employer is a whole entry, and the place lines below it join it
("Title, Employer / City"). The role has no dates: its title is evidence (ROLE_TITLE, undated
`first_used`/`last_used`), it adds no months (`total_experience_months` stays the union of dated roles,
None when none is dated), as undated roles already were (D-34). A section with bullets keeps the old rule
(an undated entry needs bullets under it), so a bullet whose glyph a scan lost is not a role. The
FUNCTIONAL_RESUME qualifications share counts words, not lines (a paragraph wrapped over four PDF lines
weighs what it weighs on one line), and a résumé with undated roles and listed skills is functional
whatever its layout.

**D-132 A line opening with a past-tense action verb is no head line.** The verb lexicon's forms ending
in "ed" and its irregular past forms (`roles.past_verbs`: built, led, ...) open bullet text ("Optimized
audience segments ..."), so such a line never names a role, even when a scan lost its bullet glyph. Base
forms are left out because titles open with them ("Lead Data Analyst", "Engineer").

**D-133 A header word inside a list.** Inside a section that is no entry section (skills, a
"Qualifications" sidebar, a summary), a listed header opens a section only in a strong form (ALL CAPS, a
trailing colon, a rule under it), or when a line follows it that is not another header (an empty
section is the tail of a wrapped item: "Market / Research / Employment History") and that does not
continue the list. A line continues the list when it is written like the line before the header (or,
for the first item, like the header line itself): a "Label: items" line, items between separators, or
one short item (at most four words, no digit, no final period, no job title, degree or school). Lines
and words only, never the layout. [Deviation] The brief names SKILLS blocks; the same sidebar list is
also written under "Other Qualifications" (a SUMMARY header), where "Leadership" opened a VOLUNTEER
section, so the rule covers every non-entry section.

**D-134 Lines a narrow column wrapped.** Read as the unwrapped line, layout-independent: a skills-list
line that is no bullet and no label carries on the list line before it when that line does not end with
a separator ("Methods: A/B" / "Testing, Agile"); a field of study that runs over a degree line's end into
the next line is read over both ("in Business" / "Administration, ..."); a short line naming no job
right below a complete header line that ends with its dates, followed by a place, a bullet or nothing,
is the end of that line's last piece ("Redwood    Sep 2025 – Present" / "Creative"); a place line
between a dates-only line and the head line above it is passed over (a dates column read after the
place); and "following", "within", "between", "through", "throughout", "during" and "among" end a line
mid-sentence like the other function words.

**D-135 Header vocabulary.** "Badges" and "Digital Badges" (CERTIFICATIONS) are listed headers: creative
headers of real designer résumés as much as of the synthetic designer template. [Fix round 1] "Learning"
was listed too and is withdrawn: a wrapped "Machine / Learning" bullet opened an EDUCATION section inside
an experience entry. The designer template's "Learning" section stays unread (a known M6 cost).

**Fix round 1 amendments (review of 8c75b0b..17ea24c; precision on real résumés outranks the M6
number).**
- D-130: OCR provenance is per page. Ingest marks each line OCR read (`TextLine.ocr`: every line of
  an image file, the lines of each PDF page read by OCR) and only those lines are repaired, so a PDF
  with one scanned page among typed pages leaves the typed lines alone. Finer (per block) provenance
  does not exist: a page is OCRed whole. Tightened rules: a bullet glyph needs a space after it
  (".NET Developer" stays); an acronym or doubled-letter repair is skipped when the next word is
  capitalised (a name: "Al Smith", "Al Ain", "Mel Studio"); a level token needs a title word directly
  before it (no comma between: "Server, Il Fornaio" stays) and nothing word-like after it (the end, a
  separator, a spaced dash or a digit; "Ill-health" and "Chef Il Fornaio" stay). A repair that fails
  leaves its line as read, and a failure of the whole step parses the text as read; neither fails
  the résumé.
- D-131: the listing rule applies only when the whole résumé has no dated role (no experience-like
  section holds a dated line), in experience and volunteer sections alike (the brief named
  experience; volunteer listings are read the same way). A listing entry needs a title piece that
  names a job (`title_score`), in at most six words, opening with no pronoun ("Sorted donations" and
  "I worked as a Software Engineer" are no roles). Its roles carry `Role.listed`; they are evidence
  only and are left out of temporal completeness (`score/confidence.py`: a résumé whose roles are all
  listed has none, as with no roles at all). This is the one change under `score/`, by ruling.
- D-132: past forms that open titles as adjectives (assisted, automated, founded, registered,
  certified, licensed, chartered, ...) are not bullet openers ("Assisted Living Coordinator").
- D-133: the list rule runs only inside SKILLS and SUMMARY sections, and never holds back a header of
  an entry kind (experience, education, projects, certifications, volunteer): "Experience" after a
  one-per-line skills list always opens its section, so "Leadership" and "Research" in a sidebar open
  theirs again. A header of another kind is judged by its run up to the next listed header: an empty
  run opens nothing; any line in the run not written like the line before the header (a dated line,
  a title, a degree, a school, a place, contact details, a sentence) opens the section.
- D-134: a wrapped employer tail joins only when it names an organisation itself (an employer word)
  or starts in lower case or with "&"/"and"; "Payments Analytics" under "Acme Corp" is a team.

**D-123 Tailoring scores. [Tailrd]** Task 9.3 completes D-83 for the tailoring flow. Every number,
band and keyword in `/ai/job-analysis`, `/ai/custom-resume-analysis`, `/ai/rewrite`,
`/ai/custom-resume` and the extension's `/api/tailor-resume`, `/api/custom-resume-analysis` and
`/api/custom-resume` comes from scoring a résumé DOCUMENT against one parsed posting with the
engine (`fit_engine.score_document`). The original and the tailored document are scored against the
same posting: the job row's cached posting (D-84), or pasted text parsed by the engine
(`posting_from_text`, memoised per input and posting version). The document is read in the
stored-profile shape (`resume_document.document_to_record`, the inverse of `db_record_to_document`),
with the user's saved answers, through the same calibration, so the "before" of a stored résumé
equals its feed score. Nothing is banked and no trace is written. Field mapping:
`overall_score` is the displayed (calibrated) score. `ats_score` is the SAME number: the engine has
no separate ATS model, and the field stays for the clients that read it. `match_label` is the band,
"We couldn't score this" (NEEDS_REVIEW) or "Not eligible" (GATE_FAIL, with the gate's reason
leading `weaknesses` and in `reasons`). `keyword_coverage` is the engine's idf-weighted coverage over
the required and preferred skill requirements. `matched_keywords` are requirements at REQ_MATCH;
`missing_keywords` are those at REQ_PARTIAL or REQ_MISS, as display labels ("A or B" for an
OR-group). Only SCORED shows a number: SCORED_LOW_CONFIDENCE shows the band with null scores (D-86).
An additive `outcome` (and `reasons`) field lets clients key on the outcome. The LLM is called once
per rewrite, for the rewrite text, and is fed the engine's missing requirements; the LLM analysis
prompt, its memo and the 80/60 `score_to_label` are removed. The two analysis routes call no AI, so
they no longer count against the daily AI limit. Consequence: a rewrite that only LISTS a skill (no
evidence in a role or project) can lower the engine's evidence density, so the "after" can be
SCORED_LOW_CONFIDENCE (band only) where the "before" had a number.
Fix round 1 (review of 9.3):
- *Failures.* Any failure of the rewrite's LLM call (connection, timeout, HTTP error, an
  unexpected response, a reply that is not JSON or not a résumé document) is a `RewriteFailedError`
  (a `ConnectionError`). The rewrite routes answer it with a 503 and save no version. A failed
  rewrite is never a 200 that returns the original as if it were the rewrite.
- *Limits.* The two analysis routes call no AI, but each is CPU work, so they share a cheap
  per-user rate limit (`usage_limiter.fit_analysis_guard`: 20 a minute and 1,000 a day by default,
  `FIT_ANALYSIS_PER_MINUTE` / `FIT_ANALYSIS_PER_DAY`, on the same database-backed counters). It
  never charges the daily AI quota. A pasted job description is capped at 20,000 characters (title
  and company at 2,000), and longer input is a 422. `posting_from_text` cuts to the same length for
  any other caller.
- *Reasons.* The rewrite responses carry an additive `reasons` list for the tailored side: the
  engine's plain sentences (compliance.md section 2, `matcher.errors.FLAG_TEXT`). When a SCORED
  original became SCORED_LOW_CONFIDENCE, nothing is added: fix round 2 removed a fixed sentence of
  our own that was added whatever the real cause (LOW_EVIDENCE_DENSITY already says when skills
  are only listed). The list is exactly the engine's sentence for each reason code. Clients show
  the reasons whenever the outcome is not SCORED.
- *What is woven in.* `missing_keywords` stays the full chip list (REQ_PARTIAL and REQ_MISS,
  required then preferred; "A or B" for an OR-group). A one-click rewrite (no `add_keywords`) weaves
  in only the additive `suggested_keywords`: the REQUIRED groups at REQ_MISS, by best member, at
  most 10.
- *`gaps`.* `gaps` stays LLM prose by decision. It is honest-gap text written with the rewrite, not
  a score, a label or a keyword list, so it can differ from the engine's `missing_keywords`.

## Shadow scoring and the real-data dry run (Task 9.6) [Tailrd]

**D-150 Shadow scoring in the cron sweep (EVAL section 6). [Tailrd]** When `FIT_SHADOW_CONFIG` names
a config directory (the layout of `match-engine/config`: the six YAML files and
`engine_version.txt`), the sweep scores every chunk it banked a second time with that config and puts
`shadow` in its summary: the outcome and band histograms of both sides, the display-score delta
(shadow minus live, over jobs SCORED on both sides: n, mean, p50), band agreement over jobs with a
band on both sides, and the jobs that cross the alert bar (a SCORED Strong match) into or out of it.
- *Only live is persisted.* `fit_engine.shadow_scores` scores in memory: no score row, no trace, no
  posting cache entry, no remembered fingerprint. It reads only the user's settings and the live
  calibration references.
- *What the shadow varies.* Only the `EngineConfig` (weights, gates, bands, verbs, negation, scoring)
  and a resolver built for it. The skill graph, the parse tables, the title and MOS indexes and the
  calibration panel are the live ones. The résumé is parsed again with the shadow config (one parse
  per user, cheap). The postings are the ones the live pass just used (`bank_scores(postings_out=)`),
  NOT parsed again: a second parse costs about 20 ms a posting plus the description egress, which
  the 300 s cron cannot afford. So a candidate whose change works through posting parsing (posting
  negation or verbs) is under-measured; the summary says `"postings": "live parse"`. A candidate of
  that kind is judged offline (`make eval`), as EVAL section 5 requires anyway.
- *One calibrator.* Both sides use the live calibrator, so the display delta is the config's alone,
  not a change of reference group.
- *Deadline.* The shadow pass runs inside the sweep's scoring budget (`CRON_MATCH_BUDGET_S`): its time
  counts toward the check before the next live chunk, and once the budget is spent the shadow pass is
  skipped and its jobs counted (`skipped_deadline`). The shadow engine is built lazily on the first
  scored chunk (about 3 s: the resolver).
- *Failures.* A directory that is missing or a config that fails to load sets `status: "error"`
  with the error class and message, and no further shadow pass runs that sweep; a fault while
  scoring a chunk counts in `errors`. Neither ever stops or changes the live sweep.

**D-151 The calibration panel rebuilt for the merged parser and taxonomy (Task 9.6). [Tailrd]** The
panel's MANIFEST recorded config hash `b44170942753a66f` and graph `1c13153c7e3c7a93`; after the
parser and taxonomy work of Tasks 5.4, 6.5 and 7.3 the engine's are `48335e3c488c5fe9` and
`e27a2eda90711e67`, so `build_engine` was calibrating against references scored by an older engine.
Rebuilt with `scripts/build_reference_panel.py` on the same corpus (sha256 `bd943d2d...`), seed and
parameters: version `panel-6c0ff8f59781` became `panel-bed969e57383`. The same postings were sampled
(two families changed one posting each) and every family kept its candidate count. The shift is
small: the pooled group's mean raw score by fit level moved by at most 0.005 (level 0 0.3907 to
0.3859, level 3 0.8487 to 0.8453); the percentile of a fixed raw score (0.3 to 0.9) moved by a median
of 0.8 points across families, at most 6.2 points in the families that are in charge (DevOps at raw
0.6); Product Management (24 candidates, under the minimum, so it falls back to the pooled group)
moved 20.8 at raw 0.5 and is never used. Over the new pooled samples, old-minus-new percentile is
-0.31 at the median (p05 -0.85, p95 +0.08). No test pins the panel to the current config: a rebuild
needs the corpus, which is not committed, so a CI check would block every config or taxonomy change
for contributors without it. The rebuild stays a release step (docs/fit-engine.md).
