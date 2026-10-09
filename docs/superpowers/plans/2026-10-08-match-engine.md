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

### Task 2.1: Graph core (aliases, traversal, idf/pmi math, loader, resolver, fuzzy)

All paths below are relative to `match-engine/`. Read `SPEC.md` §3 and `docs/DECISIONS.md`
D-20..D-24 first. Existing contracts you build on (do not change them):
`src/matcher/graph/model.py` (`SkillNode`, `SkillGraph`), `src/matcher/models.py`
(`RelationKind`, `SkillKind`, `Frozen`), `src/matcher/config.py` (`EngineConfig`;
`config.scoring.relations.max_depth`, `config.scoring.taxonomy.fuzzy_max_distance` = 2,
`fuzzy_min_token_length` = 6, `config.scoring.idf.unseen_default` = 0.75).

**Files:**
- Create: `src/matcher/graph/aliases.py`, `src/matcher/graph/traversal.py`, `src/matcher/graph/idf.py`,
  `src/matcher/graph/pmi.py`, `src/matcher/graph/loader.py`
- Create: `src/matcher/normalize/__init__.py`, `src/matcher/normalize/fuzzy.py`, `src/matcher/normalize/resolver.py`
- Create: `tests/fixtures/graph_small/{nodes.json,idf.json,pmi_edges.json,MANIFEST.json}`
- Test: `tests/test_graph_traversal.py`, `tests/test_alias_resolution.py`, `tests/test_graph_stats.py`, `tests/test_graph_loader.py`

**Interfaces (Produces — later tasks import exactly these names):**
```python
# graph/aliases.py
def normalize_surface(text: str) -> str
def surface_variants(normalized: str) -> tuple[str, ...]      # lookup keys to try, in order

# graph/traversal.py
class Relation(Frozen): kind: RelationKind; depth: int; weight: Optional[float]
def ancestors(graph: SkillGraph, node_id: str, max_depth: int) -> Mapping[str, int]
def relations(graph: SkillGraph, have: str, want: str, max_depth: int) -> tuple[Relation, ...]
def distance(graph: SkillGraph, a: str, b: str, max_depth: int) -> Optional[int]

# graph/idf.py
def raw_idf(n_postings: int, df: int) -> float                 # ln(N / (1 + df)), SPEC §3.1
def normalize_idf(df: Mapping[str, int], n_postings: int) -> dict[str, float]   # D-21
def idf(graph: SkillGraph, skill_id: str, config: EngineConfig) -> float

# graph/pmi.py
def npmi(n: int, df_a: int, df_b: int, df_ab: int) -> Optional[float]           # D-22
def build_edges(df: Mapping[str, int], co_df: Mapping[tuple[str, str], int], n: int, *,
                threshold: float, min_cooccurrence: int, max_out_degree: int,
                curated: Mapping[str, Mapping[str, float]] = {}) -> dict[str, dict[str, float]]

# graph/loader.py
class ArtifactError(ValueError)
def load_graph(taxonomy_dir: str | Path) -> SkillGraph
def write_artifacts(taxonomy_dir, nodes: Iterable[SkillNode], idf: Mapping[str, float],
                    edges: Mapping[str, Mapping[str, float]], *, n_postings: int,
                    sources: Mapping[str, str]) -> None   # used by Tasks 2.2/2.3

# normalize/fuzzy.py
def bounded_levenshtein(a: str, b: str, max_distance: int) -> Optional[int]
class FuzzyHit(Frozen): surface: str; canonical_id: str; distance: int
class FuzzyLog: hits: list[FuzzyHit]; def record(hit) -> None; def lines() -> list[str]

# normalize/resolver.py
class Resolution(Frozen): surface: str; canonical_id: Optional[str]; method: Literal["EXACT","ALIAS","FUZZY","NONE"]; distance: int = 0
class Mention(Frozen): canonical_id: str; surface: str; start: int; end: int; method: Literal["EXACT","ALIAS"]
class Resolver:
    def __init__(self, graph: SkillGraph, config: EngineConfig, *,
                 ambiguous: Mapping[str, str] = {}, fuzzy_log: Optional[FuzzyLog] = None) -> None
    def resolve(self, surface: str) -> Resolution
    def extract(self, text: str, *, context: Literal["prose", "list"] = "prose") -> tuple[Mention, ...]
```

**Artifact formats (Tasks 2.2/2.3 write these; the loader reads them):** every file is UTF-8 JSON
with `"format": 1`. `nodes.json.gz` (gzip; the loader also accepts an uncompressed `nodes.json`):
`{"format":1,"nodes":[<SkillNode.model_dump(mode="json")>, ...]}` sorted by id.
`idf.json`: `{"format":1,"n_postings":<int>,"idf":{"<id>":<float in [0,1]>}}`.
`pmi_edges.json`: `{"format":1,"edges":{"<id>":{"<id>":<float in [0,1]>}}}`.
`MANIFEST.json`: `{"format":1,"artifacts":{"<file name>":"<sha256 hex of the file bytes>"},"sources":{...}}`.
`load_graph` verifies every listed artifact's sha256 and raises `ArtifactError` on mismatch or a
missing file; `SkillGraph.version` = first 16 hex of sha256 over the sorted `name:hash` lines of
`artifacts` (sources excluded, so rebuilding identical artifacts yields the same version). The
loader may build nodes with `SkillNode.model_construct` after the hash check (artifacts were
validated when written) — load time matters (Vercel cold starts).

**Behaviour:**
1. `normalize_surface`: NFKC; remove zero-width chars (U+200B–U+200D, U+FEFF) and soft hyphens
   (U+00AD); delete apostrophes (straight and curly: `'` `’` `‘`), map curly double quotes to `"`
   and en/em dashes to `-`; casefold; turn `( ) [ ] { } , ; : ! ? " * ® ™ ©` and `|` into spaces
   (so `"O'Reilly"→"oreilly"`, `"Google's"→"googles"`); strip leading/trailing `-` `/` `_` and a
   trailing `.` from each whitespace token while keeping inner and leading-dot forms (`.net`,
   `node.js`, `c++`, `c#`, `ci/cd`, `scikit-learn`); collapse whitespace; strip.
   Table to pin in tests: `"React.js"→"react.js"`, `" Node.JS "→"node.js"`, `"C++"→"c++"`,
   `"C#"→"c#"`, `".NET"→".net"`, `"CI/CD"→"ci/cd"`, `"Scikit-Learn"→"scikit-learn"`,
   `"Microsoft Office®"→"microsoft office"`, `"“Agile”"→"agile"`, `"Python,"→"python"`,
   `"Node.js."→"node.js"`, `"Python (computer programming)"→"python computer programming"`,
   `"ﬁnance"→"finance"`, `"soft­ware"→"software"`, `"zero​width"→"zerowidth"`,
   `"Front–End"→"front-end"`.
   `surface_variants("front-end")` → `("front-end", "front end", "frontend")`; tokens without
   `-` return just themselves.
2. `ancestors`: BFS over `parents`, returns `{ancestor_id: min_depth}` for depth 1..max_depth,
   never includes the node itself even when a cycle leads back to it; terminates on cycles;
   memoised in `graph.cache` keyed by `("anc", node_id, max_depth)`. Unknown id → `{}`.
3. `relations(have, want)`: every applicable relation: `EXACT` (have == want, depth 0);
   `CHILD` (want ∈ ancestors(have), depth = that depth); `PARENT` (have ∈ ancestors(want));
   `SIBLING` (`graph.related_weight(have, want)` is not None, depth 1, weight = edge weight).
   Return them sorted by `(kind order EXACT, CHILD, SIBLING, PARENT)`; none → `()`. Credit values
   are NOT computed here (that is score/'s job with config).
4. `distance(a, b)`: undirected hop count over parent links (both directions) and related edges,
   BFS bounded by max_depth; `0` when equal; `None` when farther or unknown. Children index built
   once and memoised in `graph.cache`.
5. `normalize_idf` (DECISIONS D-21): `raw = ln(N/(1+df))` per skill; weight each skill by its
   mention count `df`; normalised value = (total df of skills with strictly lower raw idf
   + 0.5 × df of skills with equal raw idf) / total df. Skills with df == 0 are omitted (they get
   `unseen_default` at lookup). Deterministic for any input order. `idf()` = `graph.idf_of(id,
   config.scoring.idf.unseen_default)`.
6. `npmi` (D-22): `p_a=df_a/n`, `p_b=df_b/n`, `p_ab=df_ab/n`; `PMI=ln(p_ab/(p_a·p_b))`;
   `NPMI=PMI/(-ln p_ab)`; return None when `df_ab == 0` or `p_ab == 1`; clamp to [-1, 1].
   `build_edges`: keep pairs with `df_ab >= min_cooccurrence` and `npmi >= threshold`, weight =
   `clamp(npmi, 0, 1)`; add both directions; per node keep the `max_out_degree` highest weights
   (ties broken by target id ascending); then overlay `curated` (curated weight wins, both
   directions, never dropped by the degree cap); omit self-pairs.
7. `bounded_levenshtein`: classic DP with early exit once every cell in a row exceeds
   `max_distance`; returns the distance or None.
8. `Resolver.__init__` indexes `normalize_surface(label)` and every alias of every node →
   node id. When two nodes claim the same key, the curated node (`source == "curated"`) wins,
   then the lexicographically smallest id (deterministic). `ambiguous` maps a normalised key to
   its required exact casing (e.g. `{"go": "Go", "r": "R", "rest": "REST", "spring": "Spring",
   "excel": "Excel", "swift": "Swift", "rust": "Rust"}`): such a key resolves only if the
   original text equals the required casing and, in `prose` context, is not the first word of
   the text or of a sentence; in `list` context only the casing check is waived (any case).
9. `resolve(surface)`: try each of `surface_variants(normalize_surface(surface))` as an exact key
   (method `EXACT` if it equals a node's normalised label, else `ALIAS`); else, only if the
   normalised surface is a single token (no space) of length ≥ `fuzzy_min_token_length`, the
   closest key within `fuzzy_max_distance` among keys whose length differs by ≤ 2 (tie: smaller
   distance, then smallest key) → method `FUZZY`, recorded in `fuzzy_log`; else `NONE`.
   Fuzzy never runs when an exact/alias hit exists. Keys shorter than 6 never fuzzy-match.
10. `extract(text)`: tokens are maximal runs of `[A-Za-z0-9+#._/'-]` (plus any Unicode letter)
    with character offsets into the ORIGINAL text; greedy longest match from each position over
    windows of up to 8 tokens, comparing the space-joined normalised tokens via
    `surface_variants`; a matched span consumes its tokens (no overlaps); a single token that
    contains `/` or `-` and does not match as a whole is retried as its parts
    (`React/Redux` → React, Redux; `React-based` → React). No fuzzy matching inside `extract`
    (bounded cost on free text). Output sorted by `start`; `surface` is the original slice.
11. Fixture `tests/fixtures/graph_small/` (~40 nodes) covers: `skill.javascript` ←
    `skill.js_framework` ← {`skill.react`, `skill.vue`, `skill.angular`, `skill.svelte`};
    `skill.nextjs` (parent react); `skill.react_native` (parent react); `skill.typescript`
    (parent javascript); python family (`skill.python` ← `skill.django`, `skill.flask`,
    `skill.pandas`); `skill.sql` ← `skill.postgresql`, `skill.mysql`; cloud (`skill.aws`,
    `skill.kubernetes`, `skill.docker`, `skill.terraform`); `skill.go` (alias "golang", ambiguous
    "go"); `skill.r`; a few SOFT skills; `cred.aws_saa`, `cred.registered_nurse`; and a parent
    cycle `skill.cycle_a → skill.cycle_b → skill.cycle_c → skill.cycle_a`. Related edges:
    react–vue 0.65 (curated), react–nextjs 0.80, kubernetes–docker 0.70, terraform–aws 0.55.
    Aliases include ReactJS/React.js, Node.js/NodeJS, Postgres, k8s, Golang, "React Native".
    It is written in the artifact format above with a correct MANIFEST (write it with
    `write_artifacts` from a small builder in the test module or a committed generator
    script, so hashes are never hand-typed).

**Tests to write first (TDD), at minimum:**
- traversal: ancestors with depth 1/2/3 limits; the cycle terminates and excludes the start node;
  `relations` for EXACT, CHILD (react→javascript, depth 2), PARENT (javascript→react), SIBLING
  (react→vue, weight 0.65), multiple kinds at once, NONE → `()`; `distance` for siblings, parent
  chains, unreachable → None; memoised and non-memoised calls agree.
- idf: `raw_idf(1000, 9) == ln(100)`; `normalize_idf` on a hand-computed 4-skill example
  (state the expected numbers in the test); ties get the half-weight rule; input order doesn't
  matter; all outputs in [0,1].
- pmi: `npmi` closed-form example; independent pair → 0; always-together pair → 1; `build_edges`
  threshold, min co-occurrence, out-degree cap with id tie-break, curated override both ways.
- loader: round trip `write_artifacts` → `load_graph`; tampered byte → `ArtifactError`; missing
  file → `ArtifactError`; version stable across rebuilds of identical content.
- aliases/resolver: the normalisation table above; `resolve` EXACT vs ALIAS vs variant
  (`"front end"` finds a `front-end` alias); fuzzy `"kubernets"` → kubernetes (distance 1, logged);
  no fuzzy for `"reac"` (short) or when an exact key exists; `extract` longest match
  (`"React Native"` → react_native, not react), slash and hyphen splitting, trailing period,
  ambiguous "Go" (prose: "Built services in Go." → go; "Go to market strategy" at sentence start
  → nothing; list context "python, go, sql" → go), offsets slice the original text exactly,
  deterministic output for repeated calls.

- [ ] Write the fixture builder and failing tests; run `./.venv/Scripts/python.exe -m pytest tests/test_graph_traversal.py tests/test_alias_resolution.py tests/test_graph_stats.py tests/test_graph_loader.py -q` → FAIL (modules missing)
- [ ] Implement the modules; rerun → PASS; run the full suite once → PASS; `ruff check` clean
- [ ] Commit by explicit paths: `git add match-engine/src/matcher/graph match-engine/src/matcher/normalize match-engine/tests && git commit -m "feat(match-engine): skill graph core (aliases, traversal, idf/pmi, loader, resolver)"`

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
