# Match Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace every AI-based job match score in Tailrd with a deterministic, explainable,
non-LLM engine built exactly as `match-engine/SPEC.md` + `EVAL.md` + `BUILD.md` describe.

**Architecture:** A self-contained Python package (`match-engine/src/matcher`) with the
seven-stage pipeline of SPEC §1 (ingest → parse → normalize → extract → score → calibrate →
explain) plus audit, a skill graph seeded from ESCO + O*NET + Tailrd's curated synonyms with
IDF/PMI mined from our own postings, an evaluation harness (synthetic corpus, mutation suite,
metrics with bootstrap CIs) and a constrained tuning loop that only writes proposals. Tailrd's
backend calls it through one adapter; no scoring path calls an LLM.

**Tech Stack:** Python ≥3.11, pydantic 2.11.7, PyYAML 6.0.2 (runtime); pytest + Hypothesis
(tests); pdfplumber, python-docx, pymupdf, rapidocr-onnxruntime (ingest, lazily imported);
numpy, scikit-learn, optuna, reportlab, jinja2, cryptography (offline eval/tuning only).

**Spec:** `match-engine/SPEC.md`, `match-engine/EVAL.md`, `match-engine/BUILD.md` (milestone
checklist), `match-engine/docs/DECISIONS.md` (every interpretation; binding). `match-engine/CLAUDE.md`
holds the standing rules. ADVANCED.md is out of scope except the two sections BUILD.md cites
(§2.3 scope extraction, §7 counterfactual), which are quoted into the tasks that need them.

## Global Constraints

- `src/matcher/score/` is pure: no I/O, no clock, no config loading, no network, no randomness (SPEC §1).
- Identical inputs + config → byte-identical output; `math.fsum` for weighted sums; no float `==`; sort before iterating sets/dicts that reach output (SPEC §0, §13).
- Every function in `score/` clamps its returned score to [0, 1] (SPEC §13).
- `as_of` is an explicit parameter everywhere a date is needed (SPEC §0).
- Weights in `config/weights.yaml` sum to 1.0, asserted at load (SPEC §6). After M1, never edit it directly: write `proposals/` (CLAUDE.md).
- Prohibited features (SPEC §9) never reach `score/`; `tests/test_forbidden_features.py` is never weakened.
- Never read, decrypt or commit under `eval/corpora/holdout/` (EVAL §0).
- No stubs or `TODO: implement` in anything reported complete (SPEC §12).
- Public repo: never commit real résumés, user data, bulk posting text, or credentials (DECISIONS D-04).
- Core modules import only stdlib + pydantic + yaml; heavy libraries are imported inside functions (D-02).
- Runs on Python 3.11–3.13 (D-03). Engine venv: `match-engine/.venv` (pinned; Python 3.13 locally).
- Display copy: "Fit score 76 · Good match", integers only, never "76% match" (SPEC §7).
- Tailrd backend tests run with `DATABASE_URL=sqlite:///./ci_test.db` and a dummy `JWT_SECRET`, from an isolated CWD (never against Neon).

## Review Focus

1. **"We don't know" vs "they have none"** (SPEC §14.5): an abstaining dimension must redistribute, never score 0. Test owner: Task 3.4 (`test_weight_redistribution.py`) and Task 3.3 (each dimension's abstain path).
2. **Posting text pasted into a résumé** must score < 0.85 raw, end to end through the real parser, not just with hand-built objects. Test owner: Task 6.4 (`test_posting_pasted_into_resume_end_to_end`).
3. **A résumé with only a skills section and projects (typical first-year student)** must get a valid score (not NEEDS_REVIEW) with a "no work experience listed" fact, not a crash or an abstention cascade. Test owner: Task 3.6 fixture `student_projects_only.json`.
4. **Old AI/local scores must never be displayed after deploy**: the feed, sidebar, breakdown and alerts read only engine fingerprints. Test owner: Task 9.3 (`test_stale_fingerprints_are_never_served`).
5. **Postings with no qualifications section (prose only)** must still produce requirements (IMPLIED or modal-phrase REQUIRED) and a score, not POSTING_UNSCORABLE. Test owner: Task 6.5 (`test_prose_only_posting_is_scorable`).

---

## Execution model

- Orchestrator = the session that owns this plan. It implements Milestone 1 itself (the contracts
  every later task consumes) and dispatches one fresh implementer subagent per task afterwards,
  then a reviewer pass (spec compliance, then code quality) before committing.
- Subagents work in `C:\Users\elmas\Desktop\Tailrd\.worktrees\match-engine` on disjoint paths and
  **do not commit**; the orchestrator commits each reviewed task by explicit path.
- Tasks inside one milestone that touch disjoint files may run in parallel (marked ∥).
- Each milestone ends at its BUILD.md gate; the orchestrator ticks BUILD.md.
- Commands (from `match-engine/`): tests `./.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider`,
  `make eval-fast`, `make eval`.

## Cross-milestone interfaces (binding)

```
matcher.models            (M1)  all SPEC §2 models + enums (see src/matcher/models.py)
matcher.errors            (M1)  Flag enum (reason codes + flags), FLAG_TEXT, EngineFault
matcher.config            (M1)  EngineConfig, load_config(dir) -> EngineConfig, config_hash(cfg) -> str
matcher.graph.model       (M1)  SkillNode, SkillGraph(nodes, related, idf, version)
matcher.graph.traversal   (M2)  ancestors(graph, node_id, max_depth) -> dict[str, int]
                                relation(graph, have, want, max_depth) -> tuple[RelationKind, int]
matcher.graph.loader      (M2)  load_graph(taxonomy_dir) -> SkillGraph
matcher.graph.aliases     (M2)  normalize_surface(text) -> str
matcher.normalize.resolver(M2)  Resolver(graph, cfg); .resolve(surface) -> Resolution;
                                .extract(text) -> tuple[Mention, ...]
matcher.score.engine      (M3)  score(resume, posting, *, as_of, config, graph, calibration,
                                      options=ScoreOptions()) -> MatchResult      (total function)
matcher.score.prepare     (M3)  prepare_resume(resume, graph, config, as_of) -> PreparedResume
matcher.explain.*         (M4)  build_explanation(...) -> Explanation; improve(...) -> tuple[Improvement, ...]
matcher.calibrate.*       (M7)  ReferenceDistribution, percentile(raw, ref) -> int, IsotonicCurve
matcher.parse.*           (M6)  parse_resume_text(text, *, as_of, resolver, config) -> ParsedResume
                                resume_from_document(doc, *, as_of, resolver, config) -> ParsedResume
                                parse_posting(title, description, *, meta, resolver, config) -> JobPosting
matcher.ingest.*          (M6)  ingest_bytes(data, filename) -> IngestResult
```

---

## Milestone 1 — Foundations (orchestrator)

### Task 1.1: Package skeleton, config, models, errors

**Files:**
- Create: `match-engine/pyproject.toml`, `match-engine/Makefile`, `match-engine/.gitignore`,
  `.pre-commit-config.yaml` (repo root), `match-engine/scripts/guard_holdout.py`
- Create: `match-engine/config/{weights,gates,bands,verbs,negation,scoring}.yaml`, `config/engine_version.txt`
- Create: `match-engine/src/matcher/{__init__,models,errors,config}.py`, `src/matcher/graph/{__init__,model}.py`
- Test: `match-engine/tests/test_models.py`, `tests/test_config_integrity.py`
- Modify: root `.gitignore` (un-ignore `match-engine/data/taxonomy/`)

**Gate:** config loads, weights validate (sum 1.0, each in [0.03, 0.45]), every model round-trips
through JSON byte-identically, a holdout path is rejected by `guard_holdout.py`.

## Milestone 2 — Skill graph

### Task 2.1: Graph core (aliases, traversal, idf/pmi lookups, loader, resolver, fuzzy)
**Files:** `src/matcher/graph/{loader,aliases,traversal,idf,pmi}.py`, `src/matcher/normalize/{__init__,resolver,fuzzy}.py`,
`tests/fixtures/graph_small/` (hand-built JSON graph, ~40 nodes incl. a cycle), `tests/test_graph_traversal.py`, `tests/test_alias_resolution.py`.
Behaviour: alias normalisation per SPEC §3.3 (case-fold, NFKC, punctuation strip that keeps
`+ # . /` inside tech names such as C++, C#, .NET, Node.js, CI/CD); longest-match multi-word
extraction; bounded Levenshtein ≤ 2 only for tokens ≥ 6 chars and only without an exact hit,
every fuzzy hit logged to a `FuzzyLog`; traversal depth-limited and cycle-safe.

### Task 2.2 ∥: Taxonomy build from ESCO + O*NET + curated synonyms
**Files:** `scripts/fetch_sources.py`, `scripts/build_taxonomy.py`, `data/taxonomy/{nodes.json.gz,MANIFEST.json}`, `config/curated_aliases.yaml`, `config/curated_edges.yaml`.
Sources: ESCO v1.2 CSV (form POST at esco.ec.europa.eu/en/use-esco/download; fallback: ESCO API),
O*NET 30.0 text db (`https://www.onetcenter.org/dl_files/database/db_30_0_text.zip`): Technology
Skills, Tools Used, Alternate Titles, Occupation Data; Tailrd `_SKILL_SYNONYMS` + `IMPLIES`.
Raw downloads in `data/raw/` (ignored). Node schema per SPEC §3 + D-13 (CREDENTIAL kind) + `release_year`.
Also builds `data/taxonomy/mos_crosswalk.json` (SPEC §14.3: military MOS/MOC code → civilian
skill ids, from O*NET's military crosswalk + the occupations' technology/skill links) and
`data/taxonomy/titles.json` (O*NET alternate titles → occupation code + job family).

### Task 2.3: Corpus statistics (IDF, PMI) from Tailrd postings
**Files:** `scripts/pull_corpus.py` (reads `DATABASE_URL`, writes `data/raw/corpus/postings.jsonl.gz`),
`scripts/compute_idf.py`, `scripts/compute_pmi.py`, `data/taxonomy/{idf.json,pmi_edges.json}`.
Behaviour: D-21 (mention-weighted CDF), D-22 (NPMI ≥ 0.4, co-occurrence ≥ 5, out-degree 20, curated wins).

### Task 2.4: Gate — 1,000 surface forms
**Files:** `tests/fixtures/surface_forms.tsv` (1,000 lines: surface → expected id, spanning tech,
business, health, trades, soft skills, credentials, FR labels), `tests/test_surface_forms.py`.

## Milestone 3 — Scoring core (hand-built fixtures, no parsing)

Order inside the milestone (BUILD.md): one dimension at a time, test first; aggregator only after all seven pass.

### Task 3.1: Evidence (§15) — `score/evidence.py`, `tests/test_evidence.py` (100 labelled bullets fixture `tests/fixtures/bullets.yaml`)
### Task 3.2: Prepared résumé index — `score/prepare.py` (per-skill best mentions, latest activity date, relation cache)
### Task 3.3: The seven dimensions — `score/dimensions/{required_skills,preferred_skills,experience,seniority,domain,education,tools}.py`, `tests/test_dimensions_<name>.py` each with closed-form expected values
### Task 3.4: Gates, aggregate, redistribution, critical-miss cap — `score/{gates,aggregate}.py`, `tests/test_aggregate.py`, `tests/test_weight_redistribution.py`
### Task 3.5: Confidence, intervals, engine entry point — `score/{confidence,intervals,engine}.py`
### Task 3.6: Fixtures and global properties — `tests/fixtures/profiles/*.json` (30 pairs incl. `student_projects_only.json`), `tests/test_totality.py` (50k fuzzed), `tests/test_determinism.py` (1,000 runs), `tests/test_forbidden_features.py`

**Gate:** all seven dimensions match closed-form values; totality green over 50k fuzzed inputs.

## Milestone 4 — Explanation
### Task 4.1: `explain/{breakdown,gaps,counterfactual}.py`, `tests/test_explanation_completeness.py`
Counterfactual per ADVANCED §7 (quoted in the task prompt): greedy + one swap pass, |S| ≤ 4,
learning cost = graph distance from the candidate's skills; output strings like
"Adding demonstrated Terraform experience would move this to roughly 84".

## Milestone 5 — Eval harness
### Task 5.1 ∥: Generator + renderers — `eval/generate/{profiles,render}.py`, `eval/generate/templates/*.j2` (8 templates × 6 formats)
### Task 5.2 ∥: Metrics — `eval/metrics/{ranking,calibration,fairness,bootstrap}.py` with tests on known values
### Task 5.3: Suites, report, run, baseline — `eval/suites/{properties,synthetic,mutations}.py`, `eval/{report,run,baseline}.py`, Makefile targets
**Gate:** `make eval-fast` < 2 min; 500 synthetic profiles generated.

## Milestone 6 — Parsing (largest milestone)
### Task 6.1 ∥: Dates — `parse/dates.py`, `tests/test_dates.py` (every SPEC §14.2 row)
### Task 6.2 ∥: Ingest — `ingest/{detect,pdf,docx,ocr,normalize_text,quality}.py`, `tests/fixtures/edge/` (~60), `tests/test_edge_ingest.py`
### Task 6.3: Sections, roles, education, scope — `parse/{sections,roles,education,scope}.py` (scope per ADVANCED §2.3, quoted)
### Task 6.4: Résumé assembly — `parse/resume.py` (`parse_resume_text`, `resume_from_document`), stuffing + pasted-posting tests
### Task 6.5: Postings — `parse/posting.py`, `tests/test_posting_pathologies.py` (every SPEC §14.4 row)
**Gate:** format invariance ≤ 0.02 spread across all 48 renderings.

## Milestone 7 — Calibration & audit
### Task 7.1: `calibrate/{percentile,isotonic}.py` (D-50, D-51), reference-panel builder `scripts/build_reference_panel.py`
### Task 7.2: `audit/{trace,fairness,drift}.py`, `scripts/refresh_taxonomy.py` (SPEC §16 coverage metric + provisional nodes)
Plus `scripts/bias_audit.py` (annual audit artifacts generated from persisted traces, SPEC §9) and
`docs/compliance.md` (the documented human-review path: the score ranks, never rejects;
NEEDS_REVIEW routes to a person with its reason codes; demographic data lives outside the
scorer, keyed by opaque id).

## Milestone 8 — Tuning
### Task 8.1: `eval/tune/{search,constraints,proposal}.py`, holdout encryption tooling, `.github/workflows/match-engine-eval.yml`, `docs/evaluation_budget.md`

## Milestone 9 — Tailrd integration (beyond BUILD.md; the reason this exists)
### Task 9.1: Adapter `backend/services/fit_engine.py` (engine singleton, résumé/posting conversion per D-80/D-84, fingerprints D-82)
### Task 9.2: Storage — migration adding `job_match_scores.{raw_score,band,outcome,engine_version,config_hash}`, tables `match_traces`, `match_calibration`
### Task 9.3: Replace every producer (feed overlay, breakdown, upload pre-score, sweep, sidebar) and delete the AI scoring paths (D-83); tests incl. `test_stale_fingerprints_are_never_served`
### Task 9.4: Tailoring scores from the engine (D-83)
### Task 9.5: Web app — bands/labels from the engine (D-85), full breakdown (strengths, gaps, to-improve, basis, confidence note, interval)
### Task 9.6: Docs + real-data dry run on the dev DB (latency, abstention rate, distribution, agreement with the old local scorer)
Includes EVAL §6 hooks: optional shadow scoring (`FIT_SHADOW_CONFIG` dir → score with both, persist
only the live one, log the distribution diff) and the drift alarms wired to the sweep summary.
