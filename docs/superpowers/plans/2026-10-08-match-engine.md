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

### Task 2.2: Taxonomy build (ESCO + O*NET + curated) and the 1,000 surface-form gate

Paths relative to `match-engine/`. Read SPEC §3 ("Seed it from ESCO and/or O*NET ... Do not
hand-write a taxonomy from scratch"), §14.3 (military crosswalk), §14.4 (release dates),
DECISIONS D-13, D-20, D-23, D-04. Builds on Task 2.1: `graph.loader.write_artifacts/load_graph`,
`graph.aliases.normalize_surface`, `normalize.resolver.Resolver` (read them first).

**Files:**
- Create: `scripts/fetch_sources.py` (downloads into `data/raw/`, records sha256 of each download)
- Create: `scripts/build_taxonomy.py` (reads `data/raw/` + curated config, writes `data/taxonomy/`)
- Create: `config/curated_taxonomy.yaml` (curated nodes, ambiguous casings, blocked aliases),
  `config/curated_edges.yaml` (hand-curated related edges, weights in [0.4, 0.8])
- Create (generated, committed): `data/taxonomy/{nodes.json.gz, idf.json, pmi_edges.json,
  resolver.json, titles.json, mos_crosswalk.json, naics.json, MANIFEST.json, SOURCES.md}`
- Modify: `src/matcher/graph/loader.py` — add `load_resolver_settings(taxonomy_dir) -> dict` (reads
  the hash-verified `resolver.json`) and `load_resolver(taxonomy_dir, config) -> Resolver`
  (graph + ambiguous map in one call)
- Test: `tests/fixtures/surface_forms.tsv`, `tests/test_surface_forms.py`, `tests/test_taxonomy_build.py`

**Sources** (record version, URL, licence and attribution in `data/taxonomy/SOURCES.md`):
1. ESCO v1.2.x classification CSV, English, plus French labels (`skills_fr.csv`) when obtainable.
   The download at `https://esco.ec.europa.eu/en/use-esco/download` is a Drupal form POST
   (fields `version`, `content`, `language`, `file_type`, `form_build_id`, `form_id=download_form`);
   fall back to the ESCO Web Service API (`https://ec.europa.eu/esco/api`) with polite rate
   limiting, or an official mirror. Files needed: skills, skill groups, broader relations (skill
   pillar hierarchy), transversal / language / digital skill collections, occupations,
   occupation–skill relations.
2. O*NET 30.0 text database `https://www.onetcenter.org/dl_files/database/db_30_0_text.zip`
   (CC BY 4.0): Technology Skills, Tools Used, Alternate Titles, Occupation Data, Knowledge, Job Zones;
   and O*NET's military crosswalk (MOC → O*NET-SOC; see onetcenter.org/crosswalks).
3. NAICS 2022 2–6 digit codes and titles (census.gov) → `naics.json` (`{"format":1,"codes":{"5112": "Software Publishers", ...}}`).
4. Tailrd's curated tables: `backend/services/structured_extraction.py` `_SKILL_SYNONYMS` and
   `backend/services/local_match.py` `IMPLIES` / `_DISPLAY` (copy their content into
   `config/curated_taxonomy.yaml`; nothing under `src/` may import `backend`).
If a source cannot be fetched, build from what was fetched, say so in SOURCES.md and the report,
and still meet the gate below. Never commit anything under `data/raw/`.

**Node rules:**
- Ids: curated and O*NET technology `skill.<slug>` (shared namespace: an O*NET example or ESCO
  label whose `normalize_surface` equals a curated node's label/alias MERGES into it — aliases
  unioned, curated label/kind/parents/release_year win); ESCO skills `esco.<slug>` (slug of the
  preferred label; on collision append `_` + 6 hex of the concept UUID); ESCO skill groups
  `esco.group.<code>`; UNSPSC commodity classes `cat.<code>`; credentials `cred.<slug>`; fields of
  study `field.<slug>`. Every id deterministic across rebuilds.
- Kinds: ESCO transversal → SOFT; ESCO knowledge → DOMAIN; ESCO skill/competence → METHODOLOGY;
  O*NET Technology Skills → TECHNOLOGY or TOOL by commodity class (programming/development/
  database/OS/network/cloud/web classes → TECHNOLOGY; office/spreadsheet/presentation/project
  management/CAD/accounting/ERP/CRM/graphics/video/analytics apps → TOOL; table in the script);
  O*NET Tools Used → TOOL; credentials → CREDENTIAL; fields → DOMAIN.
- Parents: ESCO broader relations; O*NET example → its `cat.<commodity>` node; curated parents
  (e.g. `skill.react` → `skill.js_framework` → `skill.javascript`; `IMPLIES` pairs become parents:
  postgresql → sql, typescript → javascript, next.js → react, ...).
- Aliases: ESCO preferred + alt + hidden labels (+ French ones); the label without a trailing
  parenthetical (`Python (computer programming)` → `Python`); O*NET examples with vendor prefix
  and trailing "software" removed (`Microsoft Excel` → `Excel`, `Atlassian JIRA` → `JIRA`,
  `Amazon Web Services AWS software` → `AWS`, `Amazon Web Services`). Drop a SINGLE-WORD ESCO
  alt/hidden label unless it equals that node's own preferred label or is curated (single generic
  words cause most false positives); drop every alias listed under `blocked_aliases`; drop aliases
  of ≤ 2 characters unless curated. Ambiguous English words that are also skills (Go, R, C, REST,
  Spring, Excel, Swift, Rust, Dart, React, Next, Express, Chef, Lean, Access, Word, Teams, Outlook,
  Spark, ...) go in `ambiguous` with their required casing → written to `resolver.json`
  (`{"format":1,"ambiguous":{"go":"Go",...}}`).
- `release_year` for technologies a posting might over-ask (SPEC §14.4): at least Kubernetes 2014,
  Docker 2013, React 2013, Vue 2014, Angular 2016, Svelte 2016, Next.js 2016, TypeScript 2012,
  Go 2009, Rust 2015, Swift 2014, Kotlin 2011, Flutter 2017, GraphQL 2015, Terraform 2014,
  FastAPI 2018, PyTorch 2016, TensorFlow 2015, Snowflake 2014, dbt 2016, Airflow 2015, Kafka 2011.
- Credentials: every id in `config/gates.yaml` `professional_credential_ids` must exist, plus
  common ones (AWS/Azure/GCP certifications, CompTIA A+/Network+/Security+, CISSP, CCNA, PMP,
  CAPM, CSM/PSM, Six Sigma belts, ITIL, CFA, CPA Canada, CMA, CHRP/CHRL, EIT, LEED, Red Seal,
  First Aid/CPR/BLS/ACLS, WHMIS, Food Handler, forklift, ordinary driver's licence
  `cred.drivers_license` incl. Ontario G/G2), each with a related edge to the skill it certifies.
- Fields of study (`field.*`, ~40: computer science, software/computer/electrical/mechanical/civil/
  chemical engineering, mathematics, statistics, data science, physics, business, commerce,
  finance, accounting, economics, marketing, psychology, biology, chemistry, nursing, health
  sciences, communications, design, law, political science, education, ...) with parents
  (`field.engineering`, `field.business_admin`, `field.science`) and curated related edges
  (CS–software engineering 0.80, CS–computer engineering 0.70, statistics–mathematics 0.70,
  data science–statistics 0.70, data science–CS 0.70, finance–accounting 0.60, economics–finance
  0.60, ...); aliases incl. "CS", "Comp Sci", "B.Comm", French ("génie logiciel", "informatique").
- `titles.json`: `{"format":1,"titles":{"<normalized title>":{"soc":"15-1252.00","family":"<SOC
  major group title>","job_zone":4}}}` from O*NET occupations + alternate titles.
- `mos_crosswalk.json`: `{"format":1,"codes":{"25B":{"branch":"army","title":"...","soc":[...],
  "skill_ids":[...]}}}`; skill_ids = the hot/in-demand technology examples and top knowledge areas
  of the mapped occupations that exist as nodes. If the crosswalk can't be fetched, a curated
  table of ≥ 40 common codes across branches, flagged as such in SOURCES.md.
- Write everything through `write_artifacts` (idf `{}` and edges = curated edges only for now;
  Task 2.3 fills real IDF/PMI) and list the extra JSON files in MANIFEST so they are hash-pinned.
- Hash pinning vs. line endings (from Task 2.1): this repository converts line endings on
  Windows checkouts, which would change the artifact bytes and fail the sha256 check. Add
  `match-engine/data/taxonomy/.gitattributes` with `* -text` (as
  `tests/fixtures/graph_small/.gitattributes` does) BEFORE the first commit of the artifacts.
- `Resolver.resolve()` requires the exact casing for ambiguous keys (`resolve("go")` → NONE,
  `resolve("Go")` → skill.go); skills-list items go through `extract(context="list")`. Write the
  ambiguous lines of the surface-form TSV accordingly (state the context in a 4th column if you
  test some through `extract`).
- When writing Python source that contains `\uXXXX` escapes, check the file afterwards: the
  file-writing tool has been seen to turn escapes into raw invisible characters
  (`grep -nP "[\x{200B}-\x{200D}\x{FEFF}\x{00AD}]"` must find nothing in source files).

**Gate (BUILD Milestone 2): 1,000 hand-picked surface forms resolve correctly.**
`tests/fixtures/surface_forms.tsv` — exactly 1,000 data lines `surface<TAB>expected_id<TAB>method`
(method ∈ EXACT, ALIAS, FUZZY, NONE), hand-picked across: programming languages and frameworks
with every common spelling (ReactJS / React.js / react, Node / NodeJS / Node.js, Postgres /
PostgreSQL / psql, k8s, Golang, C++ / cpp, C# / csharp, .NET / dotnet, JS, ES6, HTML5, CSS3,
SCSS, sklearn, PowerBI / Power BI, ...), data/ML, cloud/DevOps, design tools, office tools,
business/finance/accounting (QuickBooks, GAAP, IFRS, financial modeling, Bloomberg Terminal),
marketing/sales (SEO, HubSpot, Salesforce), healthcare (BLS, CPR, EMR), trades and engineering
(AutoCAD, SolidWorks, PLC, SCADA, LabVIEW, Verilog), soft skills, credentials, fields of study,
~50 French forms, ~40 typos that must FUZZY-match (≥ 6 chars, ≤ 2 edits), and ~40 strings that
must NOT resolve (NONE: "synergy", "rockstar", generic words). `tests/test_surface_forms.py`
loads the REAL `data/taxonomy` via `load_resolver` and asserts every line, reporting all failures
at once (one parametrised case per line). Also a test that loading + building the resolver takes
< 3 s and that every `professional_credential_ids` id exists in the graph.
`tests/test_taxonomy_build.py`: the builder is deterministic (build twice from the same raw
inputs → identical artifact hashes; use a tiny raw-input fixture under `tests/fixtures/raw_mini/`
so the test needs no network), merging rules, alias filtering rules, id collision rule.

- [ ] Fetch sources (network) → `data/raw/`; write build tests on `raw_mini` → FAIL; implement builder → PASS
- [ ] Run the full build; write the 1,000-line TSV; iterate on curated config until the gate passes
- [ ] Full suite once; ruff clean; commit by explicit paths (scripts, config, data/taxonomy, src/matcher/graph/loader.py, tests)

### Task 2.3: Corpus statistics (IDF, PMI) from Tailrd postings

Paths relative to `match-engine/`. Read SPEC §3.1–§3.2 ("computed offline from our own job
corpus ... pure counting"), DECISIONS D-21, D-22, D-04 and the ledger ruling on the corpus source.
Builds on Task 2.1 (`graph.idf.normalize_idf`, `graph.pmi.build_edges`, `write_artifacts`,
`load_graph`) and Task 2.2 (`data/taxonomy/*`, `graph.loader.load_resolver`).

**Files:** Create `scripts/pull_corpus.py`, `scripts/corpus_stats.py` (shared: iterate the corpus,
extract mentions, count df and co-df), `scripts/compute_idf.py`, `scripts/compute_pmi.py`;
regenerate (committed) `data/taxonomy/{idf.json, pmi_edges.json, MANIFEST.json}` and add
`data/taxonomy/corpus_report.md`; Test `tests/test_corpus_stats.py` with a synthetic in-repo corpus
`tests/fixtures/corpus_mini.jsonl` (~30 invented postings).
Modify: `pyproject.toml` eval extra — add `pg8000==1.31.2` (already installed in the venv).

**Behaviour:**
1. `pull_corpus.py --env-file <path> [--limit N]`: reads ONLY `DATABASE_URL` from the env file or
   the environment (never prints, logs or writes it), connects read-only with pg8000 (TLS), and
   streams `SELECT id, title, description, role_category, experience_level, first_seen_at,
   scraped_at FROM scraped_jobs WHERE length(coalesce(description,'')) > 200 AND duplicate_of IS
   NULL ORDER BY id` into `data/raw/corpus/postings.jsonl.gz` (git-ignored; scraped text never
   leaves `data/raw/`). Run it once against `C:\Users\elmas\Desktop\Tailrd\.env` (the Development
   branch, ~5,475 postings). Record the row count and date range (not the text) for SOURCES.md.
2. `corpus_stats.py`: for each posting, `resolver.extract(title + "\n" + description)` (prose
   context) → the SET of canonical ids (df counts each skill once per posting) and every unordered
   pair of ids in that set (co-df). Deterministic regardless of row order.
3. `compute_idf.py`: N = postings processed; `normalize_idf(df, N)` → `idf.json` (via
   `write_artifacts`, nodes and edges unchanged).
4. `compute_pmi.py`: `build_edges(df, co_df, N, threshold=taxonomy.pmi_threshold (0.4),
   min_cooccurrence=taxonomy.pmi_min_cooccurrence (5), max_out_degree=taxonomy.max_out_degree
   (20), curated=<config/curated_edges.yaml>)`; skip pairs where one id is an ancestor of the other
   (the hierarchy already relates them) → `pmi_edges.json` via `write_artifacts`.
5. `corpus_report.md` (committed; aggregates only, no posting text): corpus size and date range,
   the 100 most frequent ids with df and normalised idf, the 50 strongest PMI edges, and "noisy
   alias suspects" — ids with df share > 30 % that are not SOFT. For every suspect that is a
   false positive (a generic word mapped to a specific skill), add the alias to
   `blocked_aliases` in `config/curated_taxonomy.yaml`, rebuild with `scripts/build_taxonomy.py`,
   and recompute; the 1,000-form gate (Task 2.2) must stay green. Note the final suspect list and
   what you blocked in the report.
6. SOURCES.md gains a "Corpus statistics" section: source (Tailrd scraped postings, Development
   branch), N, date range, extraction method; and the one-line command to recompute against any
   database (`--env-file`).

**Tests:** on `corpus_mini.jsonl`: df/co-df counts equal hand counts for three named skills and
one pair; idf/pmi outputs deterministic under shuffled input; ancestor pairs excluded; curated
edges present; and the committed real artifacts load (`load_graph`), all idf ∈ [0,1], every edge
endpoint is a node, out-degree ≤ 20 except curated, and `kubernetes`'s normalised idf is greater
than `microsoft_office`'s (SPEC §3.1's motivating example), naming the real ids you produced.

- [ ] Pull the corpus; tests → FAIL; implement → PASS; compute real artifacts; review suspects; full suite incl. the surface-form gate; ruff; commit by explicit paths

### Task 2.4: Gate — 1,000 surface forms
**Files:** `tests/fixtures/surface_forms.tsv` (1,000 lines: surface → expected id, spanning tech,
business, health, trades, soft skills, credentials, FR labels), `tests/test_surface_forms.py`.

## Milestone 3 — Scoring core (hand-built fixtures, no parsing)

Order inside the milestone (BUILD.md): one dimension at a time, test first; aggregator only after all seven pass.

**Common to every Milestone 3 task** (paths relative to `match-engine/`): read SPEC §1, §5, §13,
§14.5, §15 and DECISIONS D-23, D-30..D-46 first. Everything under `src/matcher/score/` is PURE:
no I/O, no clock, no config loading (config arrives as an `EngineConfig` argument), no randomness,
no imports of `matcher.parse`, `matcher.ingest`, numpy or anything outside stdlib + pydantic +
`matcher.{models,errors,config,graph}`. Every function that returns a score clamps it to [0,1];
weighted sums use `math.fsum`; no float `==` (use `math.isclose` or `<` with an epsilon); every
division guarded; a non-finite intermediate raises `EngineFault(Flag.NUMERIC_FAULT)` (helper
`score/numeric.py: finite(x) -> float`, `clamp01(x) -> float`). Tests use the hand-built graph
`tests/fixtures/graph_small/` (loaded with `graph.loader.load_graph`) and `load_config()`.
Expected values in tests are written as closed-form arithmetic from the spec, independent of
the implementation (EVAL §2.2: "verifies the implementation against the spec's math").

### Task 3.1: Evidence (§15) — `score/evidence.py`, `tests/test_evidence.py`, `tests/fixtures/bullets.yaml`

**Files:** Create `src/matcher/score/__init__.py`, `src/matcher/score/numeric.py`,
`src/matcher/score/evidence.py`; Test `tests/test_evidence.py`, `tests/fixtures/bullets.yaml`.

**Interfaces (Produces):**
```python
class BulletMention(Frozen): canonical_id: str; start: int; end: int     # char offsets into the bullet
class MentionEvidence(Frozen): canonical_id: str; start: int; end: int
    verb_class: VerbClass; polarity: Polarity; verb: str = ""; trigger: str = ""
def analyze_bullet(text: str, mentions: Sequence[BulletMention], config: EngineConfig) -> tuple[MentionEvidence, ...]
def is_quantified(text: str) -> bool
def evidence_multiplier(evidence: Evidence, verb_class: VerbClass, config: EngineConfig) -> float   # E, DECISIONS D-32
def recency_multiplier(last_used: Optional[date], as_of: date, config: EngineConfig) -> float      # R, §5.1/D-34
def apply_polarity(value: float, polarity: Polarity, config: EngineConfig) -> float               # D-33
```
**Behaviour:**
1. Clauses: split at `config.verbs.clause_punctuation` and at the coordinating conjunctions as
   whole words (case-insensitive). A mention belongs to the clause containing its start.
2. Verb: the nearest lexicon entry (single word or phrase such as "coursework in", matched on
   lower-cased whole words) that ENDS before the mention within its clause → its class.
   None in the clause → if `list_continuation_inherits_verb` and the clause holds only skill
   mentions, list words (`and`, `or`, `&`, `as well as`, `plus`, `including`, `with`), articles and
   punctuation, inherit the verb class of the previous clause (transitively); else `NO_VERB`.
3. Polarity: scan the `config.negation.window_tokens` tokens before the mention inside its clause;
   any hard-negation term → `NEGATED`; else any aspirational term → `ASPIRATIONAL`; else `AFFIRMED`.
   Multi-word terms ("no experience", "looking to") match as phrases.
4. `is_quantified` (bullet level, SPEC: ROLE_BULLET_QUANTIFIED): true when the bullet holds a
   quantified outcome — a number followed by `%`, preceded by a currency sign, followed by
   `k`/`K`/`M`/`B`/`x`/`×`/`+`/`times`, a number ≥ 2 followed within two tokens by a counting noun
   (users, customers, clients, people, engineers, members, students, requests, transactions,
   records, hours, days, weeks, projects, teams, servers, apps, services, tests, tickets,
   patients, employees, events, orders, ... — a list in the module), or a number after "by"/"to"
   following an outcome verb (increased, decreased, reduced, improved, grew, cut, saved, boosted,
   lowered, accelerated). Never: a 4-digit year 1900–2100 on its own, or a version number right
   after a tech name ("React 18", "Python 3", "ES6").
5. `evidence_multiplier`: SKILLS_BLOB → `scoring.evidence.SKILLS_BLOB` (0.65); ROLE_TITLE,
   CERTIFICATION → 1.00; ROLE_BULLET → `verbs.multiplier(verb_class)`; ROLE_BULLET_QUANTIFIED →
   `min(verbs.quantified_cap, verbs.multiplier(verb_class) × verbs.quantified_bonus)`.
6. `recency_multiplier`: `None` last_used → 1.0; years = max(0, (as_of − last_used).days / 365.25);
   `max(floor, exp(−decay_per_year × years))`.
7. `apply_polarity`: NEGATED → `negation.hard_negation.m` (0.0); ASPIRATIONAL →
   `min(value, negation.aspirational.m)`; AFFIRMED → value.

**Tests:** `bullets.yaml` holds exactly 100 hand-written bullets, each with its mentions (surface
strings located in the text by the test), the expected `verb_class` and `polarity` per mention,
the expected `is_quantified`, and the expected multiplier `apply_polarity(evidence_multiplier(
ROLE_BULLET[_QUANTIFIED], verb), polarity)` per mention. Cover every verb class and inflection
form, NO_VERB, clause boundaries (comma, semicolon, "and" between clauses), list continuation
("Built dashboards using React, Redux and D3" → all OWNERSHIP), "for" not splitting, negation
inside vs outside the 6-token window, negation not crossing a clause boundary, every aspirational
phrase ("Seeking to transition into machine learning" → ASPIRATIONAL, 0.15), quantified vs not
(versions, years). Plus unit tests: E table, quantified cap (EXECUTION 0.85 × 1.15 = 0.9775;
OWNERSHIP 1.0 × 1.15 → 1.0), R at 0 / 2 / 4 / 10 years (1.0, e^−0.3 = 0.740818, floor 0.55 since
e^−0.6 = 0.548812 < 0.55, 0.55).

- [ ] Write fixture + tests → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): evidence strength, verbs, negation (SPEC §15)`

### Task 3.2: Prepared résumé + skill coverage dimensions (§5.1, §5.2, §5.7)

**Files:** Create `src/matcher/score/prepare.py`, `src/matcher/score/coverage.py`,
`src/matcher/score/dimensions/__init__.py`, `.../dimensions/required_skills.py`,
`.../dimensions/preferred_skills.py`, `.../dimensions/tools.py`;
Test `tests/test_dimensions_required_skills.py`, `tests/test_dimensions_preferred_skills.py`,
`tests/test_dimensions_tools.py`, `tests/test_prepare.py`.

**Interfaces (Produces):**
```python
class SkillValue(Frozen): skill_id: str; value: float; evidence_mult: float; recency_mult: float
    evidence: Evidence; polarity: Polarity; source: SkillSource
class PreparedResume(Frozen):   # arbitrary_types_allowed not needed: holds only models
    resume: Resume; as_of: date; latest_activity: Optional[date]
    best: dict[str, SkillValue]            # per canonical id: the max-value mention
    child_cover: dict[str, tuple[float, str]]   # requirement id -> (best child credit × value, via id)
def prepare_resume(resume: Resume, graph: SkillGraph, config: EngineConfig, as_of: date) -> PreparedResume
class RequirementMatch(Frozen): group: str; members: tuple[str, ...]; best_member: str
    m: float; relation: RelationKind; via: Optional[str]; idf: float; weight_multiplier: float
    necessity: Necessity; critical: bool
def match_skill(prepared, requirement_id: str, graph, config) -> tuple[float, RelationKind, Optional[str]]
def match_requirements(prepared, requirements: Sequence[JobRequirement], graph, config) -> tuple[RequirementMatch, ...]
def coverage_value(matches: Sequence[RequirementMatch]) -> Optional[float]
def score_required_skills(prepared, posting, graph, config) -> DimensionScore
def score_preferred_skills(prepared, posting, graph, config) -> DimensionScore
def score_tools(prepared, posting, graph, config) -> DimensionScore
```
**Behaviour:**
1. `prepare_resume`: collect every SkillRef from roles (incl. ROLE_TITLE refs), projects,
   skills_blob, certifications. last_used for R: `ref.last_used`, else the role's end (as_of when
   current), else the project's end, else `latest_activity`; `latest_activity` = max over dated
   roles of (as_of if current else end.value) and over project end values, or None (→ R = 1.0).
   value = `apply_polarity(evidence_multiplier × recency_multiplier, polarity)`; `best[id]` keeps
   the max value (ties: deterministic by evidence enum order then source). `child_cover[a]` for
   every ancestor `a` (within `relations.max_depth`) of every `best` id = max `relations.child ×
   value` (× `provisional_multiplier` if either node is provisional), with the via id.
2. `match_skill(r)`: max of — EXACT `best[r].value`; CHILD `child_cover[r]`; SIBLING for each
   `s` in `best` with a related edge to r: `clamp(w, sibling_min, sibling_max) × best[s].value`;
   PARENT for each ancestor `a` of r present in `best`: `relations.parent × best[a].value`;
   × `provisional_multiplier` when r or the via node is provisional. Ties keep the order EXACT,
   CHILD, SIBLING, PARENT. Unknown r (not in graph) → exact only. Nothing → (0.0, NONE, None).
3. `match_requirements`: group by `or_group` (None → its own group keyed by skill_id); group
   `m` = max member m; `idf` = max member `graph.idf_of(id, idf.unseen_default)`;
   `weight_multiplier` = max member multiplier; `critical` = REQUIRED and idf >
   `critical_miss.idf_threshold` and m < `critical_miss.m_threshold`. Groups sorted by group key.
4. `coverage_value` = fsum(idf × wm × m) / fsum(idf × wm); None when the denominator ≤ 1e-12.
5. Dimensions: required uses REQUIRED requirements, preferred PREFERRED, tools IMPLIED
   (DECISIONS D-30). No requirements of that necessity → abstain with `NO_REQUIRED_REQUIREMENTS` /
   `NO_PREFERRED_REQUIREMENTS` / `NO_IMPLIED_REQUIREMENTS`; zero denominator → abstain with
   `NO_REQUIREMENTS_RESOLVED`. Scored → `DimensionScore(value=clamped, weight=configured,
   effective_weight=configured, contribution=configured×value, abstained=False)` (aggregate
   recomputes effective weights). Facts: one per group, code `REQ_MATCH` (m ≥ 0.5), `REQ_PARTIAL`
   (0 < m < 0.5), `REQ_MISS` (m < 1e-12), subject = best member, value = m, detail {relation, via,
   idf, weight_multiplier, necessity, members}; plus `Fact(code="CRITICAL_MISS", subject=best
   member, value=m, detail={"idf": idf})` for every critical group (required only).

**Closed-form tests (graph_small idfs read from the graph in the test):** required = react,
python, kubernetes; résumé: current role bullet "Built React apps" (ROLE_BULLET, OWNERSHIP),
skills blob python and docker, as_of = role still current → v(react)=1.0, v(python)=0.65,
kubernetes via SIBLING docker (edge 0.70) = 0.70 × 0.65 = 0.455 →
`(i_r×1.0 + i_p×0.65 + i_k×0.455)/(i_r+i_p+i_k)`. Also: CHILD (nextjs on résumé, react required
→ 0.85 × v), PARENT (javascript on résumé, react required → 0.40 × v), sibling clamp (edge 0.65 →
0.65; a test graph edge 0.95 → 0.80), OR-group max and max idf, critical flag at idf > 0.7 with
m < 0.2, recency (role ended 4 years before as_of → R = 0.55), quantified bonus, negated mention
→ 0, aspirational → 0.15, the same skill 50 times in the blob == once, provisional × 0.8,
overflow `weight_multiplier` 0.5 in tools, every abstain path.

- [ ] Tests → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): skill coverage dimensions (§5.1, §5.2, §5.7)`

### Task 3.3: Experience, seniority, domain, education dimensions (§5.3–§5.6)

**Files:** Create `.../dimensions/experience.py`, `seniority.py`, `domain.py`, `education.py`;
Test `tests/test_dimensions_experience.py`, `..._seniority.py`, `..._domain.py`, `..._education.py`.

**Interfaces:** `score_experience(prepared, posting, graph, config) -> DimensionScore` and the same
signature for `score_seniority`, `score_domain`, `score_education`. Helpers exported for reuse:
`role_relevance(role, posting, graph, config) -> float`, `relevant_months(prepared, posting, graph,
config) -> float`, `experience_curve(ratio, config) -> float`, `candidate_level(resume) ->
Optional[SeniorityLevel]`, `naics_credit(a, b, config) -> float`.

**Behaviour:** exactly DECISIONS D-35..D-41:
- relevance = cosine in the posting's requirement space over ALL requirement ids (each member of
  an OR-group individually, each id once): `v_job[r] = idf(r)`, `v_role[r] = idf(r) × best relation
  credit of the role's AFFIRMED skill mentions for r` (credit only: exact 1, child 0.85, sibling
  clamp(w), parent 0.40 — no E, no R); zero norm → 0.
- months = sweep over dated roles (end = as_of when current; end clamped to as_of): each
  elementary interval counts once at the max over active roles of `relevance ×
  (experience.internship_weight if is_internship else 1)`; months = days / 30.4375.
- min_months per D-37 (`MIN_YEARS_DEFAULTED` fact when the seniority default is used); ratio →
  `ratio < knee: value_at_knee × ratio/knee`; `knee ≤ ratio < 1: value_at_knee + (1−value_at_knee)
  × (ratio−knee)/(1−knee)`; `ratio ≥ 1: 1.0`.
- abstain `DATES_MISSING` (roles exist, none dated), `FUNCTIONAL_RESUME` (resume flag),
  `NO_EXPERIENCE_REQUIREMENT` (no years and no posting seniority); no roles at all → value 0.0
  with fact `NO_WORK_EXPERIENCE`.
- seniority per D-38/D-39: same track `gap = job.level − cand.level`; cross track: candidate
  mapped to the job's track with `cross_track_offset` (M n ≈ IC n+2), then the same gap and
  `− cross_track_penalty`; `gap < 0` (above level) counts as 0 unless
  `penalize_overqualification`, and adds fact `OVERQUALIFIED`; `max(0, 1 − step × gap − penalty)`.
  Candidate level = highest level among current roles, else the most recent role (latest end).
  Unknown either side → abstain `SENIORITY_UNKNOWN`.
- domain per D-40: `naics_credit(a, b)`: one code a prefix of the other → exact (1.00); common
  prefix ≥ 3 → 0.70; common prefix ≥ 2 or same `sector_groups` member → 0.40; else 0.15. Value = max
  over (role code, posting code). Abstain `INDUSTRY_UNKNOWN` when either side has no codes.
- education per D-41: no requirement → `no_requirement` (1.0). Candidate level = max rank over
  `resume.education` (none listed → NONE). Shortfall 0 → meets, 1 → one_below, ≥2 →
  two_or_more_below; `or_equivalent` or PREFERRED necessity → one tier more lenient. Field
  multiplier `field_multiplier_min + (1 − field_multiplier_min) × best credit` (exact 1, child,
  sibling, parent as above) between candidate field ids and required field ids; 1.0 when no field
  is required; 0.7 when required but the candidate lists none. Certification bonus:
  `cert_bonus_each` per résumé credential related (exact/child/sibling/parent) to any posting
  requirement skill or listed in `certifications_preferred`/`_required`, capped at
  `cert_bonus_cap`. Value = min(1, level × field + bonus).

**Closed-form tests:** relevance with r1 (idf .8, exact) and r2 (idf .6, parent .4):
`(0.64 + 0.144) / (sqrt(0.64 + 0.0576) × 1.0) = 0.784 / 0.835225 = 0.938669`; a role Jan 2020–Dec
2021 fully relevant vs a 36-month minimum: months = (2021-12-31 − 2020-01-01).days / 30.4375 =
730 / 30.4375 = 23.983573 → ratio 0.666210 → 0.6 + 0.4 × 0.166210/0.5 = 0.732968; two identical overlapping roles count once;
an internship at 0.5; concurrent consulting union never exceeds calendar time; overqualified
ratio 3 → 1.0; curve at 0.25 → 0.30. Seniority: IC2 vs IC3 → 0.72; IC1 vs IC3 → 0.44; IC0 vs IC4
→ 0.0; IC4 vs IC2 → 1.0 + OVERQUALIFIED (0.44 with penalize on); M2 vs IC3 → 0.80; IC3 vs M2 →
0.52; IC1 vs M1 → 0.24. Domain: "541511" vs "5415" → 1.0; "541511" vs "541330" → 0.70; "5112"
vs "5191" → 0.40; "311" vs "325" (both manufacturing 31-33) → 0.40; "52" vs "62" → 0.15.
Education: meets → 1.0; one below → 0.55; two below → 0.20; or_equivalent one below → 1.0;
related field credit c → (0.7 + 0.3c); two relevant certs on a one-below candidate → 0.55 + 0.2.

- [ ] Tests → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): experience, seniority, domain, education dimensions`

### Task 3.4: Gates, aggregation, redistribution, critical-miss cap (§4, §6, §14.5)

**Files:** Create `src/matcher/score/gates.py`, `src/matcher/score/aggregate.py`;
Test `tests/test_gates.py`, `tests/test_aggregate.py`, `tests/test_weight_redistribution.py`.

**Interfaces:**
```python
def evaluate_gates(resume: Resume, posting: JobPosting, graph: SkillGraph, config: EngineConfig) -> tuple[GateStatus, tuple[GateResult, ...]]
class Aggregate(Frozen): raw: float; dimensions: dict[str, DimensionScore]; caps: tuple[CapApplied, ...]; scored_weight: float
def aggregate(dimensions: Sequence[DimensionScore], config: EngineConfig) -> Optional[Aggregate]   # None = nothing scored
```
**Behaviour:** gates per DECISIONS D-60 and `config/gates.yaml` (work authorisation: candidate
`work_auth` None → WARN `WORK_AUTH_UNKNOWN`; needs sponsorship and posting
`sponsorship_available is False` → FAIL `SPONSORSHIP_UNAVAILABLE`; citizenship required and the
candidate's known citizenships miss every posting country → FAIL `CITIZENSHIP_REQUIRED`
(unknown → WARN); known authorised countries missing the posting's countries → FAIL when the
posting says no sponsorship, else WARN `WORK_AUTH_MISMATCH`. Licence: any credential id (or a
descendant) among résumé certifications → PASS; else FAIL `LICENSE_MISSING` if any id is in
`professional_credential_ids`, else WARN `LICENSE_UNVERIFIED`. Clearance: candidate has it →
PASS; active required → `active_missing_status` `CLEARANCE_MISSING`; else
`obtainable_status` `CLEARANCE_OBTAINABLE`. Location: onsite required and the candidate's known
locations share no country+region or city with the posting's → WARN `LOCATION_MISMATCH`; unknown
→ PASS. A disabled gate kind → PASS. Overall = FAIL if any FAIL, else WARN if any WARN, else PASS.)
Aggregate per DECISIONS D-42/D-43: effective weights = configured weights renormalised over the
non-abstaining dimensions; the §14.5 special case moves the whole required weight to preferred
when required abstained with `NO_REQUIRED_REQUIREMENTS` and preferred scored; returns new
`DimensionScore`s (effective_weight, contribution = ew × value, clamped); raw = fsum; then the
critical-miss cap from the required dimension's `CRITICAL_MISS` facts (1 → `min(raw, cap_one)`,
≥ 2 → `min(raw, cap_two_or_more)`, recorded as `CapApplied`); clamp. All abstained → None.
Non-finite anywhere → `EngineFault(NUMERIC_FAULT)`.

**Closed-form tests:** all seven scored with values (.8, .5, .6, .72, .15, 1.0, .4) → raw =
.35×.8+.10×.5+.15×.6+.10×.72+.10×.15+.10×1.0+.10×.4 = 0.647; domain abstains → (0.647 − 0.015)/0.9
= 0.702222; required abstains with NO_REQUIRED and preferred scored → preferred weight 0.45;
the silent-bug test: experience abstaining ≠ experience scoring 0.0 ((0.647 − 0.09)/0.85 =
0.655294 vs 0.647 − 0.09 = 0.557);
one critical miss caps 0.95 → 0.70, two → 0.55, cap never raises a lower raw; nothing scored →
None; every gate rule above.

- [ ] Tests → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): gates and aggregation with redistribution and critical-miss cap`

### Task 3.5: Confidence, intervals, calibrator protocol, engine entry point (§13, §17)

**Files:** Create `src/matcher/score/confidence.py`, `src/matcher/score/intervals.py`,
`src/matcher/score/engine.py`, `src/matcher/calibrate/__init__.py`, `src/matcher/calibrate/base.py`;
Test `tests/test_confidence.py`, `tests/test_intervals.py`, `tests/test_engine.py`.

**Interfaces:**
```python
# calibrate/base.py
class Calibrator(Protocol):
    def calibrate(self, raw: float) -> tuple[int, CalibrationInfo]: ...
class FixedCalibrator:            # tests only: round(100 × raw), CalibrationInfo(method="identity", scope="test", n=0, version="test")
# score/confidence.py
def compute_confidence(resume, posting, prepared, dimensions, matches, config) -> Confidence
def requirement_count_factor(n: int, config) -> float
def outcome_for(confidence: Confidence, config) -> Outcome      # SCORED / SCORED_LOW_CONFIDENCE / NEEDS_REVIEW
# score/engine.py
def score(resume: Any, posting: Any, *, as_of: Any, config: EngineConfig, graph: SkillGraph,
          calibration: Calibrator, options: ScoreOptions = ScoreOptions()) -> MatchResult
def review_result(reasons: Sequence[Flag], *, config, graph, as_of: date, input_digest: str = "") -> MatchResult
```
**Behaviour:** confidence per §17.1 and D-44 (components: parse_quality from the résumé tier;
taxonomy_coverage = (requirement_token_count − len(unresolved)) / requirement_token_count,
None when the count is 0; evidence_density = share of matched requirement groups whose via/exact
skill's best evidence is not SKILLS_BLOB, None when nothing matched; temporal_completeness = dated
roles / roles, None without roles; requirement_count = piecewise-linear `requirement_count_curve`
on the number of distinct requirement ids (clamped to the end points); dimension_coverage = sum of
configured weights of scoring dimensions); value = exp(Σ wᵢ ln max(cᵢ, floor) / Σ wᵢ) over defined
components; reasons = the LOW_* flag of every component below `weak_component`. Outcome: ≥ 0.80
SCORED, ≥ 0.60 SCORED_LOW_CONFIDENCE, else NEEDS_REVIEW with `LOW_CONFIDENCE` + those reasons.
Engine order: validate (anything not a valid Resume/JobPosting/date → `review_result(
[INVALID_INPUT])`); resume flags in `NEEDS_REVIEW_REASONS` → NEEDS_REVIEW with them; posting with
no requirements → NEEDS_REVIEW `POSTING_UNSCORABLE` if flagged else `NO_REQUIREMENTS_RESOLVED`;
`INVALID_AS_OF` when as_of precedes every date on the résumé; gates (FAIL → GATE_FAIL result, no
score computed); prepare; seven dimensions; aggregate (None → NEEDS_REVIEW `NOTHING_SCORABLE`);
calibrate → calibrated int, band = `bands.band_for`; confidence → outcome (display_score only
when SCORED); `with_interval` → intervals; `engine_version` = config.engine_version;
`config_hash` = `combined_hash(config, graph.version)`; `input_digest` = first 16 hex of sha256
over canonical résumé JSON + "|" + canonical posting JSON + "|" + as_of ISO. Non-strict mode
turns any exception into NEEDS_REVIEW (`NUMERIC_FAULT` for that EngineFault, else
`INTERNAL_ERROR`); strict re-raises. Explanation and improvements stay empty until Milestone 4.
Intervals per D-45: pessimistic and optimistic variants of the résumé (imprecise dates moved to
their earliest/latest bounds so durations are shortest/longest and last-use oldest/newest) and
of the posting (each unresolved token as an extra REQUIRED pseudo-requirement
`unresolved:<normalized token>` scored as missed / matched with idf
`intervals.unresolved_token_idf`); low/high = min/max calibrated score over base and both
variants; `display` = (high − low) > `intervals.display_spread`.

**Closed-form tests:** components (1.0, 0.8, 0.5, 1.0, 0.85, 0.9) → 0.820893 → SCORED; one
component undefined → mean over five; requirement_count_factor 1 → 0.40, 3 → 0.625, 5 → 0.85,
7 → 0.91, 10 and 20 → 1.0; outcome thresholds; engine routes INVALID_INPUT (bytes, dict junk,
NaN), INVALID_AS_OF, POSTING_UNSCORABLE, GATE_FAIL (no raw score), NOTHING_SCORABLE; a GATE_FAIL
input never carries a percentage; strict vs non-strict on an injected fault; digest stable; an
interval wider than 6 points displays.

- [ ] Tests → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): confidence, intervals and the engine entry point`

### Task 3.6: Thirty hand-built pairs and the global properties (§10, §13, §9)

**Files:** Create `tests/fixtures/profiles/*.json` (30), `tests/test_fixture_pairs.py`,
`tests/test_totality.py`, `tests/test_determinism.py`, `tests/test_forbidden_features.py`,
`tests/test_properties.py`, `tests/strategies.py` (Hypothesis strategies for valid Resume /
JobPosting over graph_small ids).

Each fixture: `{"name","description","as_of","resume":{...},"posting":{...},"expected":{
"outcome", "raw_score" (or null), "derivation": "<the arithmetic, in words>"}}` with the raw
score computed BY HAND from the spec (abs_tol 0.001). The 30 cover: strong all-round match;
zero overlap (raw 0.0, Limited, valid); `student_projects_only` (skills + projects, no roles →
valid score with NO_WORK_EXPERIENCE); internship applicant; career changer (domain floor 0.15);
OR-group; one critical miss (cap 0.70); two (cap 0.55); all-PREFERRED posting (weight moves to
preferred); single requirement (confidence penalty); no dates (experience abstains,
redistributes); functional résumé; overlapping roles; concurrent consulting; internship at half
weight; over-qualified (no deduction); cross-track; GATE_FAIL sponsorship; GATE_WARN location;
licence FAIL; the posting's skills pasted as a skills blob (< 0.85 raw); 200-skill blob (IDF
handles it); one skill ×50 (== once); negated mention; aspirational mention; four-year-old
skills (R floor); imprecise season dates; degree or-equivalent; certification bonus; unrelated
industry.
- `test_totality.py`: Hypothesis over arbitrary bytes, arbitrary unicode, arbitrary JSON-ish
  dicts and valid-but-random models; every call returns a MatchResult whose outcome is one of the
  four; for VALID random models the outcome is never INTERNAL_ERROR/NUMERIC_FAULT and raw ∈ [0,1].
  `max_examples` from env `TOTALITY_EXAMPLES` (default 2,000; the gate runs 50,000).
- `test_determinism.py`: 1,000 runs of every fixture → identical canonical bytes; the same pairs
  scored in two subprocesses with different `PYTHONHASHSEED` → identical bytes.
- `test_forbidden_features.py` (SPEC §9; never weaken later): AST scan of `score/` modules other
  than `gates.py` for attribute access to `title_raw`, `employer`, `surface_form`, `field_raw`,
  `locations`, `work_auth`, `name`, `institution`, `school`, `address`, `birth`, `age`,
  `gender`, `photo`; behavioural invariance: changing employer / title_raw / surface_form /
  field_raw / project name / locations (without onsite gates) leaves every byte of the result
  except `input_digest` unchanged; moving roles apart in time (gaps) while keeping durations and
  last-use dates leaves the raw score unchanged.
- `test_properties.py` (Hypothesis): adding a matching skill never lowers raw; removing a
  résumé skill a requirement needs never raises it; raw ∈ [0,1]; GATE_FAIL never has a score.

**Gate (BUILD Milestone 3):** all seven dimensions match closed-form values; `TOTALITY_EXAMPLES=50000` green.

- [ ] Fixtures + tests → run → fix engine bugs found (each bug gets a regression test) → PASS; gate run; commit `test(match-engine): 30 hand-built pairs, totality, determinism, forbidden features`

**Gate:** all seven dimensions match closed-form values; totality green over 50k fuzzed inputs.

## Milestone 4 — Explanation
### Task 4.1: `explain/{breakdown,gaps,counterfactual}.py`, `tests/test_explanation_completeness.py`

Paths relative to `match-engine/`. Read SPEC §7 (display rules), §8 (explanation output), §17.2
("surface the reason, always and specifically"), §18 ("What to tell users") and DECISIONS D-52.
ADVANCED §7, the only part of ADVANCED.md in scope, is quoted here — do not open that file:

> Formalize SPEC.md §8's `to_improve` as a constrained minimization: minimize Σ_{s ∈ S}
> learning_cost(s) subject to score(resume ⊕ S) ≥ next_band_threshold, |S| ≤ 4.
> `learning_cost(s)` from the skill graph: distance from the candidate's existing skill
> centroid, scaled by typical time-to-proficiency. Solve as a small ILP or greedy with a
> swap-improvement pass — the search space is tiny because `score()` is pure and fast.
> Output: "Two steps from a Strong match: Terraform (≈3 weeks given your Kubernetes background)
> and a CI/CD pipeline project (≈2 weeks). Estimated fit: 84."

**Files:** Create `src/matcher/explain/{__init__,labels,breakdown,gaps,counterfactual}.py`;
Modify `src/matcher/score/engine.py` (fill `explanation` when `options.with_explanation`,
`improvements` + `explanation.to_improve` when `options.with_counterfactual`), `config/scoring.yaml`
+ `config.py` (new `counterfactual:` section: `weeks_by_kind` {TECHNOLOGY: 4, TOOL: 2,
METHODOLOGY: 4, DOMAIN: 8, SOFT: 2, CREDENTIAL: 8}, `weeks_per_hop` 1.0, `unreachable_hops` 4,
`added_evidence` ROLE_BULLET, `added_verb` EXECUTION); Test
`tests/test_explanation_completeness.py`, `tests/test_counterfactual.py`, `tests/test_breakdown.py`.

**Interfaces:**
```python
def skill_label(graph: SkillGraph, skill_id: str) -> str                        # labels.py, display casing
def find_gaps(result: MatchResult, posting: JobPosting, prepared: PreparedResume, graph, config) -> tuple[Gap, ...]
class Gap(Frozen): requirement_id: str; necessity: Necessity; m: float; impact: float; covered_by: tuple[str, ...]
def improve(resume, posting, *, as_of, config, graph, calibration, max_steps: int = 4) -> tuple[Improvement, ...]
def build_explanation(result: MatchResult, resume, posting, prepared, graph, config,
                      improvements: Sequence[Improvement] = ()) -> Explanation
```
**Behaviour:**
1. Headline: SCORED → `"Fit score 76 · Good match"` (`bands.display.label`); with a displayed
   interval → `"Fit score 71–79 · Good match"`; LOW_CONFIDENCE → the band only; GATE_FAIL →
   `"Not eligible: <FLAG_TEXT of the failing gate>"`; NEEDS_REVIEW → `"We couldn't score this
   match"`. Never "%" after the number (SPEC §7 naming). `basis` = `bands.basis[scope]` with the
   score (empty when no score is displayed).
2. Strengths (≤ 4, by contribution): required coverage "`k` of `n` required skills" (+ the
   dimension contribution, signed, 2 decimals, e.g. "+0.31"); preferred likewise; experience
   "`x` yrs relevant vs `y` required" (years to one decimal; "vs a typical `y`" when defaulted);
   seniority/education/industry when they scored ≥ 0.8.
3. Gaps: every REQUIRED group with m < 0.5 ("Missing: Terraform (required)" or "Partly covered:
   …"), impact = −(effective required weight × idf×wm/Σ(idf×wm) × (1 − m)), with a note naming the
   candidate skills that partially cover it ("Your Kubernetes and AWS experience partially covers
   this") from `covered_by` (siblings/children/parents present on the résumé); then PREFERRED
   gaps, experience shortfall, seniority gap, education shortfall; ordered by impact.
4. Notes: one specific sentence per low-confidence reason, gate warning, cap and informational flag
   (FLAG_TEXT), e.g. DATES_MISSING → "We couldn't read the dates on this résumé, so experience
   depth isn't reflected in this score." OVERQUALIFIED is a note, never a gap.
5. `improve`: candidates = skills of REQUIRED/PREFERRED/IMPLIED groups with m < 0.5; adding a
   skill = a new mention in the most recent role (or a new undated project when there is no
   role) with evidence `added_evidence`/`added_verb`; target = the next band's minimum above the
   current calibrated score (already Strong → no suggestions). learning_cost(s) = `weeks_by_kind`
   × (1 + `weeks_per_hop` × min hops from any résumé skill via `traversal.distance`, capped at
   `unreachable_hops`). Greedy by score gain per week, ≤ `max_steps`, then one swap pass that
   lowers total weeks while still reaching the target; every candidate re-scored with
   `engine.score(..., options=ScoreOptions(with_explanation=False, with_counterfactual=False))`.
   Return the chosen set (if it reaches the target) and up to three single-skill improvements;
   each `Improvement.text` reads "Adding demonstrated Terraform experience would move this to
   roughly 84" (multi-skill: "Adding Terraform and CI/CD would move this to roughly 86 (Strong
   match)"). Deterministic: ties broken by skill id.
6. Totality: explanation building never raises; every outcome gets an Explanation.

**Tests:** completeness over the 30 fixtures and Hypothesis-random valid pairs (every outcome has
an explanation; SCORED has score, band, basis and ≥ 1 strength or gap; every REQUIRED group
appears in strengths or gaps; LOW_CONFIDENCE has a band, no score, ≥ 1 specific note;
GATE_FAIL has the gate reason and no score); impact arithmetic on a closed-form fixture;
counterfactual: suggested skills re-scored actually reach the stated score; the swap pass never
raises total weeks; already-Strong → no suggestions; determinism.

- [ ] Tests → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): explanation, gaps and counterfactual improvements (SPEC §8)`

## Milestone 5 — Eval harness
### Task 5.1 ∥: Generator + renderers — `eval/generate/{profiles,render}.py`, `eval/generate/templates/*.j2` (8 templates × 6 formats)

Paths relative to `match-engine/`. Read EVAL.md §2 (the synthetic corpus, "the main workhorse")
first. Offline code (numpy, reportlab, jinja2, python-docx, pymupdf, Pillow allowed); deterministic
from a seed (`random.Random(seed)` / numpy `default_rng(seed)`), never the wall clock; synthetic
people only ("Alex Example" style names, example.com emails) — no PII.

**Files:** Create `eval/generate/__init__.py`, `eval/generate/vocabulary.py`,
`eval/generate/profiles.py`, `eval/generate/postings.py`, `eval/generate/render.py`,
`eval/generate/templates/{chronological,functional,two_column,academic_cv,ats_plain,designer_heavy,federal_long,military_transition}.j2`,
`eval/generate/corpus.py` (CLI: `python -m eval.generate.corpus --profiles 500 --seed 7 --out eval/corpora/synthetic`);
Test `tests/eval/test_generate.py`.

**Interfaces (Produces):**
```python
class VocabSkill(Frozen): id: str; label: str; kind: SkillKind; families: tuple[str, ...]; aliases: tuple[str, ...] = ()
class Vocabulary(Frozen): skills: tuple[VocabSkill, ...]; families: tuple[str, ...]
def default_vocabulary() -> Vocabulary                 # ~150 skills over ~8 families, ids "skill.<slug>"
def vocabulary_from_graph(graph: SkillGraph, ids: Sequence[str]) -> Vocabulary
class GroundTruthBullet(Frozen): text: str; skill_ids: tuple[str, ...]; verb_class: VerbClass; quantified: bool
class GroundTruthRole(Frozen): title: str; seniority: SeniorityLevel; employer: str; industry: str
    start: date; end: Optional[date]; is_internship: bool; bullets: tuple[GroundTruthBullet, ...]
class CandidateProfile(Frozen):
    profile_id: str; family: str; target_posting_id: str; fit_level: int        # 0..3 by construction
    skills: tuple[tuple[str, float, Evidence, Optional[date]], ...]             # (skill_id, years, tier, last_used)
    roles: tuple[GroundTruthRole, ...]; projects: tuple[...]; education: tuple[Education, ...]
    certifications: tuple[str, ...]; skills_section: tuple[str, ...]; summary: str
def generate_postings(vocab, n_per_family: int, seed: int) -> tuple[JobPosting, ...]   # with REQUIRED/PREFERRED/IMPLIED, posting ids
def generate_profiles(vocab, postings, n: int, seed: int) -> tuple[CandidateProfile, ...]  # spread over fit levels 0..3 per posting
def expected_resume(profile: CandidateProfile, vocab, as_of: date) -> Resume      # what a perfect parser recovers
TEMPLATES: tuple[str, ...]   # the 8 names above
FORMATS: tuple[str, ...]     # ("pdf_latex", "pdf_word", "pdf_canva_like", "docx", "txt", "pdf_scanned_300dpi")
def render(profile: CandidateProfile, template: str, fmt: str, vocab: Vocabulary, *, seed: int = 0) -> bytes
```
**Behaviour:**
1. Fit levels are constructed, not guessed: for a target posting, level 3 covers ≥ 90 % of the
   REQUIRED skills with ROLE_BULLET evidence and meets years/seniority/education; level 2 ~70 %;
   level 1 ~40 %; level 0 a different family. Record `fit_level` as the graded relevance label.
2. Bullets are written from verb templates per VerbClass ("Architected {skill} services...",
   "Assisted with {skill} migrations...") with quantified variants ("…reducing latency by 35%").
3. `expected_resume` maps the ground truth to the engine `Resume` exactly (evidence tiers, verb
   classes, month-precision dates, internship flags, education levels/fields, certifications).
4. Templates are Jinja2 producing one neutral intermediate document (headings, paragraphs,
   bullet lists, side-blocks); per-format renderers lay it out:
   `pdf_latex` (reportlab, serif, LaTeX `article`-like margins), `pdf_word` (reportlab, Calibri-like
   sans default margins), `pdf_canva_like` (two columns, coloured sidebar with skills/contact),
   `docx` (python-docx, real headings and list styles; two_column template uses a table),
   `txt` (plain), `pdf_scanned_300dpi` (render `pdf_word`, rasterise every page at 300 dpi with
   PyMuPDF, small seeded rotation ≤ 0.5° and noise, embed as an image-only PDF).
   `functional` omits the chronology (skills grouped by theme; employers listed without dates);
   `academic_cv` adds publications/teaching; `federal_long` adds hours/week, supervisor, duties
   (multi-page); `military_transition` lists an MOS/MOC code with a civilian translation;
   `designer_heavy` uses icons (unicode), skill bars and a sidebar; `ats_plain` is single-column,
   no tables; `two_column` puts skills/education in a left column.
5. Corpus CLI writes `eval/corpora/synthetic/<profile_id>/<template>__<format>.<ext>` plus
   `profiles.jsonl`, `postings.jsonl`, `manifest.json` (seed, counts, sha256 of each file); same
   seed → byte-identical files (set reportlab `invariant=1` and fixed document metadata/dates).
   Generating 500 profiles × 48 renderings must work (report the wall time).

**Tests:** generation determinism (same seed → identical profiles and identical rendered bytes for
a sample of template×format), every template × format renders non-empty and opens (PDF via
PyMuPDF with ≥ 1 page; DOCX via python-docx; scanned PDF has no text layer), the text of
`txt` contains every skills_section label, fit levels spread 0..3 per posting, `expected_resume`
validates as a `Resume` and has one SkillRef per ground-truth bullet mention.

- [ ] Tests → FAIL; implement → PASS; generate the 500-profile corpus once (not committed — `eval/corpora/synthetic/` is ignored) and report time + size; full suite; ruff; commit `feat(match-engine): synthetic profile generator and renderers (EVAL §2.1)`
### Task 5.2 ∥: Metrics — `eval/metrics/{ranking,calibration,fairness,bootstrap}.py` with tests on known values

Paths relative to `match-engine/`. Read EVAL.md §3 (metrics, guardrails, "always report
confidence intervals") and §4 (summary table verdicts) first. This is offline evaluation code
(DECISIONS D-02): numpy is allowed; nothing under `src/matcher` may import it. Deterministic:
every random draw comes from `numpy.random.default_rng(seed)` with an explicit seed argument.

**Files:** Create `eval/__init__.py`, `eval/metrics/__init__.py`, `eval/metrics/ranking.py`,
`eval/metrics/calibration.py`, `eval/metrics/fairness.py`, `eval/metrics/bootstrap.py`;
Test `tests/eval/test_ranking.py`, `tests/eval/test_calibration.py`, `tests/eval/test_fairness.py`,
`tests/eval/test_bootstrap.py` (+ `tests/eval/__init__.py` if needed for imports).

**Data shape used by every function:** a `PairScore` record (frozen dataclass or pydantic Frozen):
`posting_id: str, pair_id: str, score: float | None` (None = abstained / no score),
`relevance: int` (graded label 0..3: 0 limited, 1 partial, 2 good, 3 strong), `advanced: bool | None`
(binary outcome when known), `band_pred: int | None` and `band_true: int | None` (0..3, same
order), `group: str | None` (opaque fairness group id), `hard: bool = False`.

**Interfaces (Produces):**
```python
# ranking.py
def ndcg_at_k(pairs: Sequence[PairScore], k: int = 10) -> float | None       # mean over postings
def precision_at_k(pairs, k: int = 10, good_threshold: int = 2) -> float | None
def auc_roc(pairs) -> float | None                                           # uses .advanced
def kendall_tau_b(xs: Sequence[float], ys: Sequence[float]) -> float | None
def per_posting(pairs, metric: Callable[[Sequence[PairScore]], float | None]) -> dict[str, float]
# calibration.py
def expected_calibration_error(probs: Sequence[float], outcomes: Sequence[bool], bins: int = 10) -> float | None
def band_accuracy(pairs) -> dict[str, float]          # {"exact", "off_by_one", "off_by_two_plus"}
def abstention_stats(pairs) -> dict[str, float | None] # {"rate", "precision"} precision = share of abstentions with hard=True
# fairness.py
def selection_rates(pairs, selected: Callable[[PairScore], bool], min_group_n: int = 100) -> dict[str, tuple[int, float]]
def four_fifths(pairs, selected, min_group_n: int = 100) -> FairnessResult   # min_ratio, per_group, passed (min_ratio >= 0.80), groups_excluded
# bootstrap.py
def bootstrap_ci(pairs, metric, *, n_resamples: int = 1000, seed: int = 0, alpha: float = 0.05) -> CI    # point, low, high
def bootstrap_delta(baseline_pairs, current_pairs, metric, *, n_resamples: int = 1000, seed: int = 0,
                    min_effect: float = 0.01, higher_is_better: bool = True) -> DeltaCI   # point, low, high, verdict
```

**Behaviour:**
1. NDCG@k per posting: rank pairs with a non-None score by score descending, ties broken by
   `pair_id` ascending (deterministic); DCG = Σ_{i=1..k} (2^rel_i − 1)/log2(i + 1); IDCG from the
   ideal ordering of the same posting's pairs (all pairs, including abstained ones, count toward
   IDCG — an abstention cannot improve NDCG); postings with IDCG == 0 are skipped; mean over the
   rest; None when no posting qualifies.
2. Precision@k: per posting, share of the top-k (by the same ordering) with relevance ≥
   `good_threshold`; denominator = min(k, number of scored pairs); mean over postings.
3. AUC-ROC over pairs with non-None score and non-None `advanced`: Mann–Whitney U with ties
   counted 0.5; None if either class is empty.
4. Kendall τ-b with the standard tie correction; None for fewer than 2 points or a constant input.
5. ECE: equal-width bins on [0,1] (`bins` of them, last bin closed), Σ (n_b/N)·|mean(prob) −
   mean(outcome)|; probs outside [0,1] raise ValueError; empty input → None.
6. Band accuracy over pairs with both bands: exact share, |diff| == 1 share, |diff| ≥ 2 share.
7. Selection rates per group with n ≥ `min_group_n` (smaller groups are reported in
   `groups_excluded` and never decide pass/fail); ratio = rate_g / max rate; `min_ratio`; passed =
   min_ratio ≥ 0.80 (EVAL §3 guardrail). No eligible groups → passed=True with min_ratio=None.
8. Bootstrap resamples POSTINGS with replacement (pairs within a posting are correlated, EVAL §3),
   recomputing the metric on the concatenated pairs each time; percentile CI at alpha/2 and
   1 − alpha/2; resamples where the metric is None are skipped; same seed → identical output.
   `bootstrap_delta` pairs baseline/current by posting id (only postings present in both), resamples
   the same posting ids for both sides, and returns verdict: `PASS` when the CI excludes 0 in the
   good direction AND |point| ≥ min_effect; `FAIL` when the CI excludes 0 in the bad direction;
   otherwise `NOISE` (EVAL §4/§5.3: "an NDCG improvement of 0.004 with a CI of ±0.02 is noise").

**Tests (hand-computed values stated in the tests):** NDCG for a 3-pair posting with a known
answer (write the arithmetic in a comment); perfect ranking → 1.0; reversed → known value; ties
by pair_id; abstained pairs lower NDCG; P@k; AUC with ties (e.g. scores [0.1,0.4,0.35,0.8] and
labels [F,F,T,T] → 0.75); τ-b against a textbook example; ECE on a 2-bin toy; band accuracy;
four-fifths with one group at 0.79 ratio → passed False, a 40-pair group excluded; bootstrap
determinism (same seed twice → identical), CI contains the point, delta verdicts PASS/NOISE/FAIL
on constructed data.

- [ ] Write the tests → run → FAIL; implement → PASS; full suite once; ruff clean
- [ ] Commit by explicit paths: `git add match-engine/eval match-engine/tests/eval`
### Task 5.3: Suites, report, run, baseline — `eval/suites/{properties,synthetic,mutations}.py`, `eval/{report,run,baseline}.py`, Makefile targets

Paths relative to `match-engine/`. Read EVAL.md §1–§4 and §7 first. Builds on: `matcher.score.engine.score`
(+ `calibrate.base.FixedCalibrator` until Milestone 7 lands), `eval.metrics` (import `PairScore`
from `eval.metrics`; NEVER create `tests/eval/__init__.py`), `eval.generate` (Task 5.1:
`generate_postings`, `generate_profiles`, `expected_resume`, `render`, `TEMPLATES`, `FORMATS`).
Consumer notes from Task 5.2's review: pass `min_effect` per metric (0.01 is the NDCG guard;
band off-by-two uses 0.005); a delta with `point is None` and `four_fifths` with `min_ratio is
None` render as "not assessed", never green; `band_accuracy` may return None values; report
the abstention rate next to P@10.

**Files:** Create `eval/suites/{__init__,properties,closed_form,synthetic,mutations,golden}.py`,
`eval/report.py`, `eval/run.py`, `eval/baseline.py`, `eval/baselines/main/` (committed baseline:
metrics.json + per-pair scores of the synthetic corpus — synthetic, so no PII),
`eval/corpora/golden/README.md` (labelling format + dual-labelling protocol), `eval/corpora/dev/README.md`;
Modify `Makefile` only if a target needs a flag; Test `tests/eval/test_suites.py`, `tests/eval/test_report.py`.

**Behaviour:**
1. Layer 1 (`properties.py`, < 30 s): fixed-seed checks over generated pairs — boundedness,
   monotonicity (add a matching skill never lowers raw; remove a required skill never raises it),
   gate precedence, determinism (1,000 runs of a sample → identical sha256), totality (0
   exceptions over 50,000 fuzzed inputs in `make eval`, 5,000 in `eval-fast`), latency p95 per pair
   (< 150 ms guardrail).
2. Layer 2 (`synthetic.py`): (a) analytic correctness — `closed_form.py` re-implements the SPEC §5
   arithmetic independently (no imports from `matcher.score` beyond models/config) for synthetic
   pairs constructed with exact matches only (known evidence/verb/recency, non-overlapping dated
   roles, known seniority/education/industry), and asserts `isclose(engine raw, closed form,
   abs_tol=0.001)` (EVAL §2.2 "the strongest test"); (b) ranking over the corpus with
   `fit_level` as graded relevance: NDCG@10, P@10, AUC (advanced = fit ≥ 2), band accuracy,
   ECE (calibrated/100 vs fit ≥ 2), abstention rate/precision, all with bootstrap CIs over
   postings; (c) parser fidelity and format invariance with an injected `parse(bytes, filename)`
   callable: recovered skills ≥ 95 %, date error ≤ 1 month, and max − min raw over the 48
   renderings ≤ 0.02 (p99 guardrail). Until the real parser exists (Milestone 6), (c) reports
   "skipped: parser not available"; `eval-fast` samples 10 profiles × 48 renderings, `eval` runs all.
3. `mutations.py`: one parametrised case per EVAL §2.3 row on constructed pairs whose arithmetic
   makes the stated range exact (e.g. four REQUIRED skills of equal idf 0.8, so removing one drops
   required coverage by 0.25 → raw by 0.35 × 0.25 = 0.0875 ∈ [0.06, 0.12]): remove one required
   skill; remove two high-idf required skills (cap ≤ 0.55); shift all dates back 4 years
   (0.02–0.06); "architected" → "assisted" (the mention's multiplier ratio 0.45/1.00); add 40
   irrelevant skills (|Δ| ≤ 0.01); swap an IC3 title for M2 (cross-track penalty visible); blank
   all dates (experience abstains, weight redistributes, no silent zero). The module docstring
   says: add a case for every bug fixed.
4. Layer 3 (`golden.py`): runs when `eval/corpora/golden/labels.jsonl` exists (Kendall τ vs
   human ordering, band accuracy incl. off-by-two alarm, inter-rater agreement when dual
   labels exist); otherwise prints `GOLDEN: skipped (no labels)`. Layer 4 prints
   `HOLDOUT: skipped (no key)` unless Task 8.1's tooling finds the key.
5. `report.py` writes `reports/<UTC timestamp>/{summary.md, metrics.json, regressions.md,
   failures/<case>.json (full feature trace, reproducible), config_hash.txt}`; `summary.md`
   exactly in EVAL §4's table format (Metric | Baseline | Current | Δ | 95% CI | Verdict) with
   PASS / NOISE / FAIL / not assessed, a guardrail section (any guardrail failure → **FAIL**
   section, exit code 1), and the top regressions (pairs that got worse vs baseline, worst first).
6. `baseline.py`: `--save-baseline NAME` writes `eval/baselines/NAME/`; `--baseline NAME` diffs
   against it (per-pair deltas feed regressions.md; metric deltas use `bootstrap_delta`).
7. `run.py`: `python -m eval.run --layers 1,2[,3,4] [--baseline main] [--save-baseline main]
   [--sample N]`; `make eval-fast` must finish in < 2 minutes on this machine (report the time).

**Gate (BUILD Milestone 5):** `make eval-fast` < 2 min; 500 synthetic profiles generated.

- [ ] Tests → FAIL; implement → PASS; run `make eval-fast` and `make eval`; save the `main` baseline; full suite; ruff; commit `feat(match-engine): eval harness layers 1-3, report, baselines`

## Milestone 6 — Parsing (largest milestone)
### Task 6.1 ∥: Dates — `parse/dates.py`, `tests/test_dates.py` (every SPEC §14.2 row)

Paths relative to `match-engine/`. Read SPEC §14.2 and §11 ("nine date formats") first.
Contracts you build on (do not change): `models.DateValue(value, earliest, latest, precision)`
with `imprecise` = precision in {SEASON, QUARTER, YEAR}; `models.DatePrecision`; `errors.Flag`
(you MAY add `Flag.FUTURE_START` with a `FLAG_TEXT` line if you need it — nothing else in errors.py).
"Date parsing will produce more bugs than any other single component. Write it as a standalone
module with its own exhaustive test file" (SPEC §14.2). It must never raise on any input.

**Files:** Create `src/matcher/parse/__init__.py`, `src/matcher/parse/dates.py`; Test `tests/test_dates.py`.

**Interfaces (Produces):**
```python
class DateRange(Frozen):
    start: Optional[DateValue]; end: Optional[DateValue]; is_current: bool
    flags: tuple[Flag, ...] = (); raw: str = ""
def parse_date(text: str, *, as_of: date, role: Literal["start","end","point"] = "point",
               lang: Literal["en","fr"] = "en") -> tuple[Optional[DateValue], tuple[Flag, ...]]
def parse_range(text: str, *, as_of: date, lang: Literal["en","fr"] = "en") -> Optional[DateRange]
def find_date_ranges(text: str, *, as_of: date, lang: Literal["en","fr"] = "en") -> tuple[tuple[int, int, DateRange], ...]
def months_between(a: date, b: date) -> float          # (b - a).days / 30.4375, may be negative
```
`find_date_ranges` returns `(start_offset, end_offset, range)` spans located inside a longer line
(e.g. `"Software Intern, Acme — May 2023 – Aug 2023, Ottawa"`), non-overlapping, sorted, so the
role parser (Task 6.3) can find and cut the date out of a header line.

**Behaviour (every row is a test):**
1. Point formats (en): `Jan 2020`, `January 2020`, `Jan. 2020`, `Sept 2020`, `01/2020`, `1/2020`,
   `2020-01`, `2020/01`, `01.2020`, `2020`, `Jan '20`, `'19`, `2020-03-15`, `03/15/2020`,
   `15/03/2020` (day > 12 ⇒ D/M/Y), `March 15, 2020`, `15 March 2020`. Ambiguous `03/04/2020`
   ⇒ M/D/Y in `en`, D/M/Y in `fr`. Precision: day forms DAY; month forms MONTH; bare year YEAR.
   French: `janvier 2020`, `févr. 2021`, `fevrier 2021`, `août 2019`, `déc. 2018`, `juil. 2022`.
2. Month precision as `start` → first day of the month; as `end` → last day; `point` → first day;
   `earliest`/`latest` = first/last day of the month. Day precision: earliest = latest = value.
3. Seasons and quarters (SPEC: "map to quarter midpoint, flag IMPRECISE"): Winter=Q1, Spring=Q2,
   Summer=Q3, Fall/Autumn=Q4 (fr: hiver, printemps, été/ete, automne); `Q3 2021`, `2021 Q3`,
   `Fall '19`, `Summer 2020`. value = quarter midpoint (Q1 Feb 15, Q2 May 16, Q3 Aug 16, Q4 Nov 16),
   earliest/latest = quarter start/end, precision SEASON (seasons) or QUARTER (Qn), flag
   `IMPRECISE_DATE`. Bare year: value = Jul 1 (start/point) or Jul 1 (end), earliest Jan 1, latest
   Dec 31, flag `IMPRECISE_DATE`.
4. A range written as ONE period (`Summer 2020`, `2020`, `Q3 2021`) means the role spanned it:
   start = DateValue(value=period start, earliest=period start, latest=period midpoint),
   end = DateValue(value=period end, earliest=period midpoint, latest=period end), IMPRECISE.
5. Ranges: separators `–`, `—`, `-`, `to`, `through`, `until`, `till`, `à`, `au`, `jusqu'à`;
   `Jan 2020 – Mar 2021`, `2019-2021`, `2019 to 2021`, `May–Aug 2020` (start month inherits the
   end's year), `Sept 2019 - Apr. 2020`, `Since 2019` / `Depuis 2019` (current), `2018/19`
   academic year ⇒ Sep 2018 – Apr 2019 (MONTH precision).
6. Present markers resolve to `as_of` with `is_current=True` and `end=None`: `Present`, `Current`,
   `Now`, `Ongoing`, `To date`, `Today`, a dangling dash (`Jan 2020 –`, `2019 —`), fr `Présent`,
   `Aujourd'hui`, `À ce jour`, `En cours`, `Actuel`, `Maintenant`.
7. Two-digit years (`'19`, `98`, `Fall '19`): the unique year in `[as_of.year − 80, as_of.year + 19]`
   with that last-two-digits; flag `TWO_DIGIT_YEAR`.
8. Future end date (end.value > as_of): end becomes `DateValue.exact(as_of, DatePrecision.DAY)`,
   flag `FUTURE_END_CLAMPED`. Start after as_of: the range is dropped (returns a
   DateRange with start=None, end=None and flag `FUTURE_START`; the caller drops the role).
9. End before start: swap when the swapped range is plausible (start ≥ as_of − 80 years and
   duration ≤ 50 years), flag `DATE_RANGE_SWAPPED`; otherwise start=end=None, flag `DATE_RANGE_DROPPED`.
10. Non-Gregorian with an explicit marker: Buddhist Era `2563 BE`/`พ.ศ. 2563` → year − 543; Solar
    Hijri `1399 SH`/`هـ.ش` → +621; Hijri `1441 AH`/`هـ` → round(year × 0.970229 + 621.5643);
    Hebrew `5780 AM` → −3760; flag `NON_GREGORIAN_CONVERTED`. A marker with a year that converts
    outside [1900, as_of.year + 1] → no date, flag `UNSUPPORTED_CALENDAR`.
11. Years outside [as_of.year − 80, as_of.year + 10] without a calendar marker are not dates.
    Numbers that are clearly not dates (`$2020`, `2020 users`, phone numbers, `40k`) never parse.
12. Totality: `parse_date`, `parse_range` and `find_date_ranges` never raise for any `str`
    (Hypothesis over `st.text()`), and `start.value <= end.value` whenever both are present.

- [ ] Write `tests/test_dates.py` (one parametrised case per row above, plus Hypothesis totality) → run → FAIL
- [ ] Implement `parse/dates.py` → run → PASS; full suite once; ruff clean
- [ ] Commit by explicit paths: `git add match-engine/src/matcher/parse match-engine/tests/test_dates.py` (+ errors.py if you added FUTURE_START)
### Task 6.2 ∥: Ingest — `ingest/{detect,pdf,docx,ocr,normalize_text,quality}.py`, `tests/fixtures/edge/` (~60), `tests/test_edge_ingest.py`

Paths relative to `match-engine/`. Read SPEC §14.1 (every row is a requirement), §17.1 (parse
tiers A=1.0, B=0.8, C=0.55) and DECISIONS D-02, D-70 first. Ingest turns bytes into normalised
text + layout lines, a parse tier, a language and flags — or a NEEDS_REVIEW reason. It never
raises. pdfplumber, pymupdf (`fitz`), python-docx and rapidocr are imported INSIDE functions (the
core package must import without them). "Build the fixture directory tests/fixtures/edge/ first;
it is the real specification" (SPEC §14).

**Files:** Create `src/matcher/ingest/{__init__,detect,pdf,docx,ocr,normalize_text,quality,language}.py`;
`tests/fixtures/edge/make_edge_fixtures.py` (deterministic generator: reportlab, pymupdf,
python-docx, Pillow) and its committed outputs (total < 3 MB); Test `tests/test_edge_ingest.py`,
`tests/test_normalize_text.py`, `tests/test_language.py`.
Modify: `src/matcher/config.py` `IngestConfig` + `config/scoring.yaml` `ingest:` — add
`max_pages: 100` and `resume_likelihood_threshold: 0.5` (nothing else in config).

**Interfaces (Produces):**
```python
class TextLine(Frozen): text: str; page: int; x0: float; y0: float; x1: float; y1: float
    size: float = 0.0; bold: bool = False; column: int = 0
class IngestResult(Frozen):
    ok: bool; text: str; lines: tuple[TextLine, ...]; tier: ParseTier; language: Optional[str]
    languages: dict[str, float]          # share of lines per detected language
    flags: tuple[Flag, ...]; reasons: tuple[Flag, ...]   # reasons non-empty exactly when ok is False
    page_count: int; ocr_confidence: Optional[float]; resume_likelihood: float
def ingest_bytes(data: bytes, filename: str = "", *, config: EngineConfig) -> IngestResult
def ingest_text(text: str, *, config: EngineConfig) -> IngestResult
def normalize_text(text: str) -> str                        # normalize_text.py
def detect_language(text: str) -> tuple[Optional[str], float]   # language.py; ISO 639-1 or None
def resume_likelihood(text: str) -> float                   # quality.py, in [0, 1]
SUPPORTED_LANGUAGES = ("en", "fr")
```
**Behaviour (one test per row, each with a fixture under `tests/fixtures/edge/`):**
1. Type detection by magic bytes first, extension second: PDF, DOCX (zip with
   `word/document.xml`), plain text (UTF-8, UTF-8 BOM, UTF-16 BOM, Windows-1252 fallback), images
   (PNG/JPEG); anything else → NEEDS_REVIEW `CORRUPT`. Zero bytes → `CORRUPT`.
2. Encrypted PDF that needs a password → `ENCRYPTED`. Truncated/garbled PDF or a bad DOCX zip →
   `CORRUPT`.
3. PDF text: PyMuPDF words/spans with coordinates (pdfplumber as fallback). Hidden text is
   removed and flags `STUFFING_SUSPECTED`: white or near-white fill (all channels ≥ 0.95), opacity
   0, invisible render mode (PyMuPDF `page.get_texttrace()` span `type == 3` or `opacity == 0`),
   font size < 2 pt, text drawn outside the page box.
4. Columns: cluster line x-starts per page; for 2+ columns read column by column; verify reading
   order with a sentence-continuity heuristic (a line ending mid-sentence should continue in the
   next line read); failure → tier B + `COLUMN_ORDER_UNCERTAIN`. Single column clean → tier A.
5. Repeated headers/footers and page numbers ("Page 2 of 3", "2", "- 2 -") are removed by
   cross-page repetition of normalised lines in the top/bottom 8 % of the page.
6. DOCX: paragraphs, tables (row cells joined in order), text boxes (`w:txbxContent`), header and
   footer parts once (not per page); hidden runs (`w:vanish`), white font colour, size < 2 pt →
   removed + `STUFFING_SUSPECTED`. Text boxes present → tier B unless the order is unambiguous.
7. Scanned / image-only PDF or image file: OCR via rapidocr-onnxruntime (pages rendered with
   PyMuPDF at 300 dpi, single-threaded session for determinism) → tier C + `OCR_USED`; mean line
   confidence < `ingest.ocr_min_confidence` (0.6) or OCR unavailable → `UNREADABLE`. An image with
   no text → `NOT_A_RESUME`.
8. More than `ingest.max_pages` pages: only the first `max_pages` are read, flag `TRUNCATED`
   (the 40k-token experience cap is the parser's job, Task 6.4).
9. `normalize_text`: NFKC (ligatures ﬁ ﬂ ﬀ), remove zero-width chars and soft hyphens, join a
   word hyphenated across a line break, smart quotes → straight, bullet glyphs (• ▪ ◦ ● ‣ ∙ ➢ ✓ –
   at line start) → "- ", non-breaking spaces → space, collapse runs of spaces, keep line breaks.
10. Language: stop-word profiles (en, fr, es, de, pt, it, nl) + script detection (Arabic, Hebrew,
    Cyrillic, CJK, Devanagari, Thai...). Document language = the majority of lines; `languages` =
    shares; `MIXED_LANGUAGE` when two supported languages each hold ≥ 20 %; a document whose
    majority language is outside `SUPPORTED_LANGUAGES` → `UNSUPPORTED_LANGUAGE` (RTL included:
    Arabic and Hebrew fixtures extract in logical order and then route here).
11. `resume_likelihood`: section markers (experience, education, skills, projects,
    certifications, summary; en + fr), date ranges, bullet density raise it; cover-letter markers
    ("Dear", "Sincerely", "I am writing to", "Hiring Manager") and transcript markers
    ("Transcript", "Course Code", "Credits", "Term GPA", "Cumulative") lower it; below
    `ingest.resume_likelihood_threshold` → `NOT_A_RESUME`.
12. Every input in `tests/fixtures/edge/` and arbitrary bytes (Hypothesis) → an IngestResult, never
    an exception; the same bytes twice → identical results.
Fixtures (≥ 60): clean one-column PDF, two-column, three-column, Canva-like sidebar, repeated
header/footer + page numbers, ligatures, zero-width chars, soft hyphens, smart quotes, white
text, 1 pt text, invisible render mode, text off-page, encrypted, owner-password-only (openable →
readable), truncated, random bytes, zero bytes, scanned 300 dpi (readable), scanned noisy 72 dpi
(UNREADABLE), photo without text, a 90-page CV (read in full; its 40k-token experience cap is
Task 6.4's) and a 120-page one (`TRUNCATED` at 100 pages), two-line résumé, cover letter, transcript, DOCX with tables / text boxes / header-footer /
hidden text / white text / corrupt zip, UTF-8 / UTF-8 BOM / UTF-16 / cp1252 text, French résumé,
Spanish résumé, German résumé, French headers with English bullets (MIXED_LANGUAGE), Arabic,
Hebrew. Synthetic names/emails only (e.g. "Alex Example"), never real people.

- [ ] Generator + fixtures + tests → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): ingest (SPEC §14.1)`
**Common to Tasks 6.3–6.5** (paths relative to `match-engine/`): read SPEC §14.3, §14.4, §15,
DECISIONS D-12, D-14, D-25, D-34a, D-37, D-60, D-70, D-80. Parsing is deterministic and never
raises (any input → a result or a reason). It uses: `parse.dates` (`parse_range`,
`find_date_ranges` — call it on HEADER lines only: it has known in-line false positives such as
"Microsoft Office 2019", "ISO 9001:2015", street numbers), `normalize.resolver.Resolver`
(`extract(text, context="prose"|"list")`; ambiguous keys need exact casing in `resolve()`;
a newline is a hard break, so unwrap soft-wrapped lines inside one bullet before extracting),
`score.evidence` (`analyze_bullet`, `is_quantified` — the only `score` import allowed),
`graph.loader` (taxonomy dir: `titles.json`, `naics.json`, `mos_crosswalk.json` formats are in
Task 2.2). Parse-time configuration lives in `config/parse/*.yaml`, loaded by
`parse/config.py` (NOT part of `EngineConfig`; its content hash is recorded in diagnostics).
Unit tests use the `graph_small` fixture plus small fixture copies of the extra taxonomy files;
synthetic people and invented employers only. Free text never reaches `score/` except through
canonical ids (D-14): institution names, graduation dates, addresses are never put in a model.

### Task 6.3: Sections, roles, education, scope, industry, title seniority

**Files:** Create `src/matcher/parse/{config,taxonomy_extras,sections,seniority,industry,roles,education,scope}.py`,
`config/parse/{sections.yaml,seniority.yaml,industries.yaml}`; Test `tests/test_taxonomy_extras.py`, `tests/test_sections.py`,
`tests/test_parse_seniority.py`, `tests/test_industry.py`, `tests/test_roles.py`,
`tests/test_education.py`, `tests/test_scope.py`, `tests/fixtures/parse_taxonomy/` (tiny
titles/naics/mos files).

**Interfaces (Produces):**
```python
class ParseConfig(Frozen): version: str  # + typed sections/seniority/industries tables
def load_parse_config(config_dir: str | Path | None = None) -> ParseConfig
# parse/taxonomy_extras.py — the hash-pinned extra artifacts of Task 2.2
class TitleInfo(Frozen): soc: str; family: str; job_zone: Optional[int]
class TitleIndex:   # normalised title → TitleInfo; lookup tries the full title, then without seniority modifiers
    def lookup(self, title: str) -> Optional[TitleInfo]
    def canonical_id(self, title: str) -> Optional[str]       # "title.<soc>"
class MosIndex:
    def find(self, text: str) -> tuple[tuple[str, tuple[str, ...]], ...]   # (code, skill_ids) found in text
def load_title_index(taxonomy_dir: str | Path) -> TitleIndex
def load_mos_index(taxonomy_dir: str | Path) -> MosIndex
class SectionKind(str, Enum): CONTACT, SUMMARY, EXPERIENCE, EDUCATION, SKILLS, PROJECTS,
    CERTIFICATIONS, COURSEWORK, PUBLICATIONS, AWARDS, VOLUNTEER, LANGUAGES, INTERESTS, REFERENCES, OTHER
class Section(Frozen): kind: SectionKind; header: str; lines: tuple[str, ...]; start_line: int; language: Optional[str]
def segment(lines: Sequence[str], *, parse_config: ParseConfig, layout: Sequence[TextLine] | None = None) -> tuple[tuple[Section, ...], tuple[Flag, ...]]
def title_seniority(title: str, parse_config: ParseConfig, *, meta_level: str | None = None) -> Optional[SeniorityLevel]
def classify_industry(name: str, context: str, parse_config: ParseConfig) -> tuple[str, ...]   # NAICS codes
class RoleDraft(Frozen): title_raw: str; employer: str; location: str; date_range: Optional[DateRange]; bullets: tuple[str, ...]
def split_roles(section: Section, *, as_of: date, lang: str = "en") -> tuple[RoleDraft, ...]
def build_role(draft: RoleDraft, *, as_of: date, resolver: Resolver, titles: TitleIndex, mos: MosIndex,
               parse_config: ParseConfig, config: EngineConfig) -> Role
def parse_education(section: Section, *, resolver: Resolver, parse_config: ParseConfig) -> tuple[Education, ...]
class ScopeFeatures(Frozen): scale_tier: Optional[Literal["personal","team","org","internet"]]; people_managed: Optional[int]
    budget_usd: Optional[float]; evidence: tuple[str, ...]
def extract_scope(bullets: Sequence[str]) -> ScopeFeatures
```
**Behaviour:**
1. `segment`: a header is a short line (≤ 6 words, no final period) that matches the en/fr synonym
   table (case/punctuation-insensitive, trailing ":" ignored; creative headers included: "Where
   I've Been" → EXPERIENCE, "My Toolkit" → SKILLS, "Expérience professionnelle", "Compétences",
   "Formation"), or is styled as a header (ALL CAPS / bold / larger font when `layout` is given)
   and is followed by content. No header anywhere → `NO_SECTIONS` and the date-anchored fallback
   (SPEC §14.3): lines whose header contains a date range open role blocks (EXPERIENCE); the rest
   is OTHER.
2. `title_seniority` (SPEC §5.4 rules table in `config/parse/seniority.yaml`): intern / co-op /
   student / stagiaire / apprentice → IC0; junior, jr, entry-level, associate, graduate, new grad,
   "I", "1", L3, SDE I → IC1; II, 2, intermediate, L4, SDE II → IC2; senior, sr, III, 3, L5, SDE III
   → IC3; staff, L6 → IC4; principal, L7, L8 → IC5; distinguished, fellow → IC6; "tech lead", "lead
   engineer/developer" → IC4; "team lead", "lead" alone, supervisor → M1; manager → M2; senior
   manager → M3; director, head of → M4; VP, vice president → M5; chief/CxO, president → M6.
   No modifier → `meta_level` (posting metadata: internship → IC0, new_grad → IC1) or None.
3. `classify_industry`: curated company → NAICS table first (normalised names; include the ~100
   employers most frequent in Tailrd's catalogue and well-known Canadian/US employers), then
   keyword rules over name + context (bank → 5221, software/SaaS → 5112/5415, hospital → 6221,
   university/college → 6113, government/ministry/federal → 92, consulting → 5416, insurance →
   5241, telecom → 517, retail/store → 44-45, restaurant → 7225, construction → 23, automotive →
   3361, aerospace → 3364, pharma/biotech → 3254/5417, energy/utilities → 22/211, nonprofit →
   8133, media/news → 5111/5161, logistics → 4931/4841, accounting firm → 5412, law firm → 5411,
   school board → 6111, ...). Unknown → `()`.
4. `split_roles`: header lines carry the date (via `find_date_ranges` on that line only);
   patterns "Title — Employer, Location | dates", "Employer \n Title \n dates", "Title, Employer
   (dates)", "Employer (dates) \n Title"; bullets are the following lines (bullet glyph or
   indentation) until the next header. Several titles with their own dates under one employer →
   one draft each (promotions, SPEC §14.3). Consecutive same-employer drafts with a similar title
   and < 60 days between them → merged (contract-to-hire chains). Founder/self-employed → a
   normal role. A dropped/future range keeps the role with no dates.
5. `build_role`: `title_canonical` from the titles index; seniority from `title_seniority`;
   industry from `classify_industry(employer, first bullet)`; `is_internship` from the title or
   date-range text (intern, co-op, stage, stagiaire); skills: `extract(title)` → ROLE_TITLE refs;
   each bullet (soft-wraps joined) → `extract` + `analyze_bullet` + `is_quantified` →
   ROLE_BULLET or ROLE_BULLET_QUANTIFIED refs with verb_class/polarity, source ROLE,
   first_used = start, last_used = end (as_of when current); an MOS/MOC code found in the title or
   bullets (e.g. "25B", "0311") adds the crosswalk's skills as ROLE_TITLE refs (SPEC §14.3).
6. `parse_education`: degree level en/fr (BSc, B.Sc., BA, BEng, BASc, BCom, B.Comm, Bachelor,
   Baccalauréat → BACHELOR; MSc, MEng, MASc, MBA, Master, Maîtrise → MASTER; PhD, Doctorate,
   Doctorat → DOCTORATE; Diploma, Associate, College diploma, DEC, AEC → ASSOCIATE; High school,
   Secondary, DES → HIGH_SCHOOL); fields = `field.*` ids extracted from the degree line;
   `in_progress` from "expected", "candidate", "in progress", "present", "current"; never the
   institution or any date in the output.
7. `extract_scope` (ADVANCED §2.3, quoted here — do not open ADVANCED.md): "QUANTITY PATTERNS →
   normalized scope features: '40k req/s', '2M users', '$12M budget', 'team of 14', 'petabyte'
   → scale_tier ∈ {personal, team, org, internet} (log-bucketed); people_managed: int;
   budget_managed: normalized currency". Log buckets: users/requests < 1e3 personal, < 1e5 team,
   < 1e7 org, ≥ 1e7 or petabyte-scale internet. Extraction only: scope is reported in parse
   diagnostics and is NOT used by scoring.

- [ ] Tests (realistic student/co-op/promotion/French/military/academic examples) → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): résumé sections, roles, education, scope`

### Task 6.4: Résumé assembly (`parse/resume.py`), adversarial checks

**Files:** Create `src/matcher/parse/resume.py`; Test `tests/test_parse_resume.py`,
`tests/test_resume_document.py`, `tests/test_adversarial_resume.py`.

**Interfaces (Produces):**
```python
class ParsedResume(Frozen): resume: Optional[Resume]; reasons: tuple[Flag, ...]; flags: tuple[Flag, ...]
    unresolved_tokens: tuple[str, ...]; unmapped_titles: tuple[str, ...]; scope: tuple[ScopeFeatures, ...]
    sections: tuple[SectionKind, ...]; parse_config_version: str
def parse_resume_text(text: str, *, as_of: date, resolver: Resolver, config: EngineConfig, parse_config: ParseConfig,
                      lines: Sequence[TextLine] | None = None, tier: ParseTier = ParseTier.A,
                      language: str | None = None, flags: Sequence[Flag] = ()) -> ParsedResume
def parse_resume_bytes(data: bytes, filename: str, *, as_of, resolver, config, parse_config) -> ParsedResume
def resume_from_document(doc: Mapping[str, Any], *, as_of, resolver, config, parse_config,
                         raw_text: str | None = None) -> ParsedResume
```
**Behaviour:**
1. Text path: `segment` → EXPERIENCE (and VOLUNTEER) → roles; PROJECTS → `Project`s (name line +
   bullets, ROLE_BULLET[_QUANTIFIED] refs with verbs, source PROJECT); SKILLS → `extract(...,
   context="list")` → SKILLS_BLOB refs; CERTIFICATIONS → credential refs (CERTIFICATION);
   EDUCATION → `parse_education`, plus coursework lines → ROLE_BULLET refs with verb EXPOSURE
   (source EDUCATION); SUMMARY → SKILLS_BLOB refs with polarity from `analyze_bullet`
   ("seeking a role in machine learning" → ASPIRATIONAL); PUBLICATIONS, INTERESTS, REFERENCES
   ignored for skills; text outside any recognised section → SKILLS_BLOB only. Roles are created
   ONLY from experience-like sections or date-anchored blocks — never from unstructured text.
2. Experience text beyond `ingest.max_experience_tokens` (40,000 whitespace tokens) is cut,
   flag `TRUNCATED`. `FUNCTIONAL_RESUME` when the résumé has skills/summary content and
   experience entries but no dated role (or "Summary of Qualifications"/"Areas of Expertise"
   dominate with no chronology). `total_experience_months` = union of dated role intervals.
   Locations: city/region/country only from contact lines; work-authorisation phrases
   ("Canadian citizen", "permanent resident", "open work permit", "US citizen", "require
   sponsorship") → `WorkAuth`. Unsupported language (ingest) → `reasons=(UNSUPPORTED_LANGUAGE,)`.
3. `parse_resume_bytes` = `ingest_bytes` + `parse_resume_text` (ingest failure → its reasons).
4. `resume_from_document` (D-80, Tailrd's stored résumé document): keys `experience[{company,
   title, location, start_date, end_date, bullets}]`, `education[{school, degree, location,
   start_date, end_date, gpa, achievements, coursework}]` (school, gpa, dates ignored),
   `projects[{name, link, organization, location, start_date, end_date, bullets}]`,
   `skills[str]`, `technologies{category: [str]}`, `custom_sections[{title, kind, text, bullets,
   items}]` (certification-like titles → credentials; others → SKILLS_BLOB), `summary`
   (`summary_title` is a heading — ignored). Dates: `parse_range(f"{start} – {end}")`. A document
   with no experience/education/projects/skills content and a `raw_text` → `parse_resume_text(raw_text)`.
5. Adversarial (SPEC §10): white-text stuffing is stripped at ingest and flagged; a résumé that IS
   a pasted job posting (the posting's text, end to end through `parse_resume_text` →
   `score`) scores raw < 0.85 against that posting; one skill listed 50 times == once.

- [ ] Tests (incl. the Review Focus #2 end-to-end pasted-posting test and a `resume_from_document` round trip from a realistic Tailrd-shaped document) → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): résumé assembly from text, files and stored documents`

### Task 6.5: Postings (`parse/posting.py`) and every §14.4 pathology

**Files:** Create `src/matcher/parse/posting.py`, `config/parse/posting.yaml` (section and modal
phrase tables en/fr, gate phrase tables); Test `tests/test_parse_posting.py`,
`tests/test_posting_pathologies.py`.

**Interfaces (Produces):**
```python
class PostingMeta(Frozen): company: str = ""; company_description: str = ""; locations: tuple[Location, ...] = ()
    work_type: Literal["remote","hybrid","onsite",""] = ""; employment_type: str = ""
    experience_level: str = ""; job_family: Optional[str] = None
def parse_posting(title: str, description: str, *, meta: PostingMeta, as_of: date, resolver: Resolver,
                  graph: SkillGraph, config: EngineConfig, parse_config: ParseConfig, titles: TitleIndex) -> JobPosting
```
**Behaviour (one test per SPEC §14.4 row, plus the rest):**
1. Sections: requirements / qualifications / what you'll need / must have / basic or minimum
   qualifications (fr: exigences, qualifications requises) → REQUIRED context; preferred / nice to
   have / bonus / pluses / assets (Canadian "assets", fr: atouts) → PREFERRED; responsibilities /
   what you'll do / the role → IMPLIED unless a modal phrase says otherwise; about us / benefits /
   perks / EEO / how to apply → ignored. Prose with no sections: sentences with must / required /
   minimum / you have / you'll need → REQUIRED, nice to have / bonus / preferred / plus / asset →
   PREFERRED, other mentions → IMPLIED (a prose-only posting is scorable — Review Focus #5).
2. Clause-split bullets; every skill mention → a requirement with its context's necessity; the
   same skill in several contexts keeps the strongest (REQUIRED > PREFERRED > IMPLIED).
3. OR-groups: "X or Y", "X/Y" (both resolving), "one of X, Y, Z", "such as X, Y or Z" → one
   `or_group`; "X or equivalent" → one-member group with `or_equivalent`.
4. Years: "3+ years of Python" → that requirement's `min_years`; "3+ years of experience" /
   "minimum 2 years" / "at least 5 years" / "1-2 years" (→ 1) with no skill → `min_years_total`;
   `min_years` above (as_of.year − node.release_year) → clamped to that span, flag
   `UNREALISTIC_REQUIREMENT`.
5. Education requirement (degree level + `field.*` ids; "or equivalent experience" →
   `or_equivalent`; "currently pursuing/enrolled" → the level, in progress allowed; "Master's
   preferred" → PREFERRED necessity). Credentials in REQUIRED context → `certifications_required`
   (+ a `LicenseGate`); PREFERRED → `certifications_preferred`.
6. Gates: "must be legally authorized to work in <country>", "no sponsorship", "unable to
   sponsor", "citizenship required", "Canadian citizen or permanent resident" → `WorkAuthGate`;
   "Secret/Top Secret clearance required", "active clearance" (active_required) vs "able to obtain"
   → `ClearanceGate`; `meta.work_type == "onsite"` with locations → `LocationGate`.
7. Seniority = `title_seniority(title, meta_level=meta.experience_level)`; `is_internship` from
   meta/title; `job_family` = meta.job_family or the titles index family; `industry_codes` =
   `classify_industry(meta.company, meta.company_description)`; `remote_policy` from work_type.
8. More than `posting.max_weighted_requirements` (25) → keep the top 25 by IDF (ties by id), the
   rest become IMPLIED with `overflow_weight_multiplier`, flag `REQUIREMENTS_TRUNCATED`.
   Contradiction (IC0/IC1 title with ≥ 5 years required) → `CONTRADICTORY_REQUIREMENTS` (both
   facts kept; nothing averaged). No resolvable requirement → empty requirements +
   `POSTING_UNSCORABLE`; < 3 requirements and mostly boilerplate → `BOILERPLATE_ONLY`.
9. `requirement_token_count` and `unresolved_requirement_tokens`: noun phrases in REQUIRED and
   PREFERRED clauses after "experience with/in", "knowledge of", "proficiency in", "familiarity
   with", plus capitalised tech-looking tokens, that did not resolve (SPEC §16 coverage input).

- [ ] Tests → FAIL; implement → PASS; full suite; ruff; commit `feat(match-engine): job posting parser with §14.4 pathologies`

**Milestone 6 gate:** format invariance ≤ 0.02 spread across the 48 renderings (run by Task 5.3's
synthetic suite with the real parser injected).
**Gate:** format invariance ≤ 0.02 spread across all 48 renderings.

## Milestone 7 — Calibration & audit
### Task 7.1: `calibrate/{percentile,isotonic}.py` (D-50, D-51), reference-panel builder `scripts/build_reference_panel.py`

Paths relative to `match-engine/`. Read SPEC §7 and DECISIONS D-50..D-52 first. Runtime code is
pure Python (no numpy). Builds on `calibrate/base.py` (`Calibrator` protocol: `calibrate(raw) ->
(int, CalibrationInfo)`).

**Files:** Create `src/matcher/calibrate/{reference,percentile,isotonic}.py`,
`scripts/build_reference_panel.py`, `data/calibration/{panel.json,MANIFEST.json}` (+ `* -text`
`.gitattributes`); Test `tests/test_percentile.py`, `tests/test_isotonic.py`, `tests/test_panel.py`.

**Interfaces:**
```python
class ReferenceDistribution(Frozen): scope: Literal["posting","family","global","panel"]; key: str
    n_candidates: int; n_pairs: int; samples: tuple[float, ...]   # sorted raw scores, ≤ 5,000 (deterministic quantile downsample)
    version: str
def build_reference(scope, key, raw_scores: Sequence[float], candidate_ids: Sequence[str], version: str) -> ReferenceDistribution
def percentile(raw: float, ref: ReferenceDistribution) -> int      # round(100 × (below + 0.5 × equal)/n), clamped
class PercentileCalibrator:
    def __init__(self, references: Iterable[ReferenceDistribution], *, min_reference_n: int)
    def bind(self, *, posting_key: str | None, family: str | None) -> Calibrator   # chain posting → family → global → panel
class IsotonicCurve(Frozen): xs: tuple[float, ...]; ys: tuple[float, ...]; version: str; n_labels: int; previous: Optional[str]
def fit_isotonic(pairs: Sequence[tuple[float, bool]], version: str) -> IsotonicCurve   # PAVA, offline
class IsotonicCalibrator: ...   # implements Calibrator; refuses (ValueError) a curve with n_labels < isotonic_min_labels
def load_panel(path) -> tuple[ReferenceDistribution, ...]
```
**Behaviour:** the chain uses the first reference whose `n_candidates` (DISTINCT people, not pairs —
"applicants" are people) ≥ `calibration.min_reference_n` (30); the result's `CalibrationInfo.scope`
names it so `bands.basis[scope]` tells the truth; percentile is monotone non-decreasing in raw.
Isotonic: pool-adjacent-violators on (raw, advanced) pairs → step function; `apply` interpolates
monotonically; every curve versioned with `previous` kept for rollback; inactive below 500 labels
(D-51). `build_reference_panel.py`: for each job family in the corpus (`data/raw/corpus`,
`role_category`), sample up to 60 postings (seeded), parse them with `parse_posting`, generate
panel profiles targeted at each posting with fit levels spread uniformly (Task 5.1's generator,
`vocabulary_from_graph`), score them, and write per-family + global `ReferenceDistribution`s
(n_candidates = distinct panel profiles) to `data/calibration/panel.json` — aggregates of
synthetic scores only.

- [ ] Tests (hand-computed percentiles incl. ties; chain fallback order; PAVA on a textbook
      example; monotonicity property) → FAIL; implement → PASS; build the panel; commit `feat(match-engine): percentile and isotonic calibration, reference panel`
### Task 7.2: `audit/{trace,fairness,drift}.py`, `scripts/refresh_taxonomy.py` (SPEC §16 coverage metric + provisional nodes)
Plus `scripts/bias_audit.py` (annual audit artifacts generated from persisted traces, SPEC §9) and
`docs/compliance.md` (the documented human-review path: the score ranks, never rejects;
NEEDS_REVIEW routes to a person with its reason codes; demographic data lives outside the
scorer, keyed by opaque id).

Paths relative to `match-engine/`. Read SPEC §0.4, §9, §16 and EVAL §6 first. `audit/` is runtime
code: pure Python (no numpy; do not import `eval`).

**Files:** Create `src/matcher/audit/{__init__,trace,fairness,drift}.py`, `scripts/refresh_taxonomy.py`,
`scripts/bias_audit.py`, `docs/compliance.md`; Test `tests/test_audit_trace.py`,
`tests/test_audit_fairness.py`, `tests/test_drift.py`, `tests/test_refresh_taxonomy.py`.

**Interfaces:**
```python
def build_trace(result: MatchResult) -> dict          # compact, JSON-safe: outcome, raw, calibrated, band, dims
    # (value, weight, effective_weight, abstain_reason), caps, gates, confidence components,
    # per-requirement [skill_id, necessity, m, relation], flags, reasons, engine_version,
    # config_hash, input_digest, as_of — never free text (D-14)
def trace_digest(trace: dict) -> str                  # sha256 of canonical JSON, 16 hex
def opaque_id(user_key: str, salt: bytes) -> str      # HMAC-SHA256, 24 hex; demographics are joined on this, never on user ids
def four_fifths(records: Iterable[tuple[str, str, bool]], *, min_group_n: int = 100) -> FairnessReport  # (opaque_id, group, selected); exact Fractions vs 4/5
def psi(reference: Sequence[float], current: Sequence[float], *, bins: int = 10) -> float
class DriftAlarm(Frozen): code: str; value: float; threshold: float; message: str
def drift_alarms(*, coverage: float | None, abstention_rate: float | None, abstention_baseline: float | None,
                 psi_value: float | None, min_fairness_ratio: float | None, p95_latency_ms: float | None) -> tuple[DriftAlarm, ...]
```
**Behaviour:** EVAL §6 thresholds — coverage < 0.92; abstention > 20 % or > 2× the 30-day
baseline; PSI > 0.25; any four-fifths ratio < 0.80; p95 latency > 300 ms — each an alarm with a
plain message. `refresh_taxonomy.py` (SPEC §16): from a corpus file (`data/raw/corpus/...`, each
posting with `first_seen_at`), parse postings, collect `unresolved_requirement_tokens`, rank by
frequency × recency-weighted growth; tokens in > `auto_promote_min_postings` (50) distinct postings
within `auto_promote_window_days` (30) become provisional nodes (`provisional=True`,
`source="provisional"`, parent = the existing node with the highest PMI against the token's
co-mentions) → rebuild artifacts; write the top `review_queue_size` (50) remaining to
`data/taxonomy/review_queue.csv`; print and write the coverage metric (% of requirement tokens
resolved) to `reports/taxonomy/<date>.json` and exit non-zero below 0.92. `bias_audit.py`: inputs
= persisted traces (JSONL) + a separate demographics file keyed by opaque id (never user ids) →
markdown + JSON: selection rates per group per band threshold, four-fifths ratios, score
distributions per group, abstention per group. `docs/compliance.md`: the human-review path
(Tailrd: the candidate sees their own score; NEEDS_REVIEW shows "we couldn't score this" with the
reason and is logged for a person to check; the score never hides or rejects a job), what is
persisted per score (trace, config hash, engine version), where demographics live and who can
join them, the annual audit procedure, and the SPEC §9 caveat that an employment attorney should
review before any employer-facing use.

- [ ] Tests → FAIL; implement → PASS; run refresh_taxonomy once on the corpus (report coverage); commit `feat(match-engine): audit trace, fairness and drift monitoring, taxonomy freshness`

## Milestone 8 — Tuning
### Task 8.1: `eval/tune/{search,constraints,proposal}.py`, holdout encryption tooling, `.github/workflows/match-engine-eval.yml`, `docs/evaluation_budget.md`

Paths relative to `match-engine/` (the workflow file lives at the repo root). Read EVAL.md §0,
§5, §7 and SPEC §10 (sensitivity analysis) first. CLAUDE.md hard rules apply verbatim: never
write `config/weights.yaml`; never read/decrypt `eval/corpora/holdout/` outside the CI path; a
fairness guardrail failure is terminal (never tuned around, never re-thresholded).

**Files:** Create `eval/tune/{__init__,search,constraints,proposal,sensitivity}.py`,
`eval/holdout.py`, `docs/evaluation_budget.md`, `docs/evaluation_budget.ledger.csv`,
`eval/HOLDOUT.md` (the holdout format and procedure; nothing is ever committed under
`eval/corpora/holdout/`, and the commit guard is never bypassed),
`.github/workflows/match-engine-eval.yml`; Test `tests/eval/test_tune.py`, `tests/eval/test_holdout.py`.

**Behaviour:**
1. `search.py`: Optuna TPE (seeded) over the 7-weight simplex (sample in a transformed space,
   normalise to sum exactly 1.0, reject any weight outside [0.03, 0.45]); objective NDCG@10 on the
   chosen split (`dev` when labels exist, else `synthetic` with the proposal clearly marked
   "synthetic-only: do not adopt"); constraints: fairness ratio ≥ 0.80 where assessable
   (infeasible otherwise), abstention rate ≤ 0.15; `--budget` trials.
2. `proposal.py` writes `proposals/<date>-tune/{weights.diff, rationale.md, report.md,
   sensitivity.md}` — the diff shows current vs proposed `weights.yaml`; rationale lists which
   dimensions moved, which cases improved and which broke (from the eval regressions); it never
   writes `config/`. Accept rules from EVAL §5.3 (improvement CI excludes 0 and point > +0.01
   NDCG; dev gain > 2× holdout gain → `OVERFIT_SUSPECTED`, reject) are evaluated and printed.
3. `sensitivity.py` (SPEC §10): for each weight, ±20 % (renormalised), report the band-change rate
   on the evaluation pairs; > 15 % → flag that dimension as over-leveraged.
4. `holdout.py`: AES-GCM encrypt/decrypt of `labels.enc` with a key from env `MATCH_HOLDOUT_KEY`;
   without the key `make eval-holdout` prints `HOLDOUT: skipped (no key)` and exits 0; every
   decrypting run appends a row to `docs/evaluation_budget.ledger.csv` (date, ref, purpose) and
   refuses past 10 looks in the current quarter.
5. Workflow: on pull requests touching `match-engine/**` run `make test` and `make eval-fast`
   (Ubuntu, Python 3.11 and 3.12 matrix, pinned deps); on `release/**` branches additionally
   `make eval-holdout` with `MATCH_HOLDOUT_KEY` from secrets.
6. Run `make tune BUDGET=50` once on the synthetic split and leave the resulting proposal
   (marked synthetic-only) in `proposals/`.

- [ ] Tests (constraint enforcement, simplex normalisation, proposal never touches config, holdout skip/ledger/refusal, sensitivity arithmetic on a toy) → FAIL; implement → PASS; commit `feat(match-engine): constrained tuning, proposals, holdout tooling, CI workflow`

## Milestone 9 — Tailrd integration (beyond BUILD.md; the reason this exists)
**Common to Milestone 9** (paths relative to the REPO root): read DECISIONS D-80..D-85 and
`docs/ai-cost-controls.md`. Backend tests run from an isolated CWD with
`DATABASE_URL=sqlite:///./ci_test.db JWT_SECRET=test` exported (never against Neon — the app
lifespan would migrate whatever database `DATABASE_URL` names); e.g.
`cd <scratch dir> && DATABASE_URL=sqlite:///./ci_test.db JWT_SECRET=test PYTHONPATH=<worktree> python -m pytest <worktree>/backend/tests/<file> -q`
with the system Python that has the backend requirements. Integration map (from the codebase
survey; verify line numbers, they drift): producers `backend/services/local_match.py` (whole
file), `jobs_sidebar.py:42-43,73-125`, `routers/jobs.py:73-98,154-342 (strong/sort 287-339),
1029-1030, 1089-1100`, `routers/ai.py:126-221 (breakdown), 373-387 (analyze-fit), 397-480
(job-analysis/custom-resume/rewrite), 483-537 (batch-score), 662-675 (cron-match-alerts)`,
`routers/resumes.py:449-537`, `services/match_notifier.py:65-135,182-209,402-510,513-818`,
`services/match_engine.py:90-102,190-358`, `services/openai_service.py:665-708`,
`services/resume_tailor.py:150-162`, `routers/tailor.py:46-118`; schemas `schemas/match.py:8-24`,
`schemas/jobs.py:11-72`, `schemas/ai.py:35-74`, `schemas/tailor.py:18-26`; storage
`db/models.py:197-200,298-322`, `migrations/add_job_match_terms.py`, `main.py:60-117`; cron
`routers/github_sources.py:810-932`; email `services/email_service.py:209-285`; web
`frontend/src/pages/Jobs.tsx`, `components/JobDetailView.tsx`, `components/JobsSidebar.tsx`,
`components/CustomResumeModal.tsx`, `lib/skillLabel.ts`; extension `chrome-extension/src/content/tailorCard.ts`.

### Task 9.1: Adapter, storage and calibration references

**Files:** Create `backend/services/fit_engine.py`, `backend/migrations/add_fit_engine.py`;
Modify `backend/db/models.py` (new columns/tables), `backend/main.py` (register the migration),
`api/requirements.txt` (+ `PyYAML==6.0.2`), `vercel.json` (`excludeFiles` also excludes
`match-engine/{tests,eval,scripts,proposals,reports,docs}/**` and `match-engine/data/raw/**`);
Test `backend/tests/test_fit_engine_adapter.py`.

**Behaviour:**
1. `fit_engine.py` puts `match-engine/src` on `sys.path` (D-01) and exposes a lazily built,
   process-wide `FitEngine` (config, graph, resolver, parse config, title/MOS indexes, panel
   references, combined hash); measure and report cold build time.
2. `resume_for_profile(profile, settings) -> ParsedResume` per D-80: the stored document when it has
   experience/education/projects/skills content, else `raw_text`; work authorisation and location
   from the user's saved answers/settings (find where `needs_sponsorship` and the location live;
   unknown stays None).
3. `posting_for_job(job) -> JobPosting` per D-84: cached in `scraped_jobs.match_terms` as
   `{"v": "<engine posting-parser version>", "dl": <len(description)>, "posting": <canonical JSON>}`,
   recomputed when `v` differs or `dl` differs from `length(description)` selected in SQL (the
   description is fetched only when recomputing); `PostingMeta` from the job's columns (company,
   company_description, locations_json, work_type, employment_type, experience_level,
   role_category as job family).
4. Fingerprint per D-82: `"fit" + major + ":" + sha256(canonical Resume JSON + combined hash)`
   truncated to 64 chars. `current_fingerprints()` returns ONLY this (old AI and `local1:` rows
   become stale everywhere).
5. Storage (additive migration, idempotent, works on SQLite and Postgres): `job_match_scores`
   gains `raw_score FLOAT`, `band VARCHAR(20)`, `outcome VARCHAR(30)`, `confidence FLOAT`,
   `engine_version VARCHAR(20)`, `config_hash VARCHAR(20)` (all nullable); new `match_traces`
   (user_id, job_id unique pair, trace JSON from `audit.build_trace`, input_digest,
   engine_version, config_hash, scored_at) — written beside every banked score and never selected
   by feed queries (egress); new `match_calibration` (scope, key, n_candidates, n_pairs, samples
   JSON, version, created_at) holding the per-family/global references built from banked raw
   scores by `refresh_calibration(db)` (distinct résumé fingerprints count as candidates; < 30 →
   the panel stays in charge, D-50).
6. `bank_scores(db, user_id, profile, job_ids) -> dict[int, MatchResult]`: score every job without
   a current row (one prepared résumé, many postings), store `score` = calibrated int (sorting),
   raw/band/outcome/confidence/version/hash, and the trace; GATE_FAIL and NEEDS_REVIEW rows store
   `score` NULL-equivalent (choose a sentinel that sorts last, documented) so they never rank high.
7. `explain_job(db, user, job) -> dict`: one full result with explanation, interval and
   counterfactual for the detail view.

- [ ] Tests (sqlite): conversion from a Tailrd-shaped document and from raw text, posting cache
      hit/miss/invalidation by length, fingerprint changes on résumé edit and on config change,
      banking writes scores + traces, calibration refresh uses distinct candidates → FAIL → implement → PASS; commit

### Task 9.2: Every producer switched; every AI scoring path removed (D-83, D-85)

**Files:** Modify `backend/services/jobs_sidebar.py`, `backend/routers/jobs.py`,
`backend/routers/ai.py`, `backend/routers/resumes.py`, `backend/services/match_notifier.py`,
`backend/routers/github_sources.py` (sweep summary fields), `backend/services/email_service.py`
(subject/body naming: "Strong match: <title> · fit score 88", never "88% match"),
`backend/services/match_engine.py` (delete `compute_breakdown`, `analyze_fit`, `queue_analysis`;
keep what tailoring still needs), `backend/services/openai_service.py` (delete `match_job`,
`extract_experience_years`), `backend/schemas/match.py` (MatchBreakdown keeps its old fields for
compatibility and gains `band`, `headline`, `basis`, `outcome`, `display_score`, `strengths`/`gaps`
as structured items, `to_improve`, `notes`, `interval`), `backend/schemas/jobs.py` (`match_band`,
`match_outcome` overlaid like `match_score`), `docs/ai-cost-controls.md`; Delete
`backend/services/local_match.py` (move `display_skill` where its callers need it) and the
`MATCH_SCORING` / `MATCH_AI_DAILY_PER_USER` / `MATCH_AI_CONFIRM_MIN` settings; Test: rewrite
`test_local_match.py` → `test_fit_scoring_paths.py`, update `test_jobs_sidebar.py`,
`test_match_notifier.py`, `test_match_sweep_cost.py`, `test_ai_router.py`,
`test_match_engine_basic.py`, `test_ai_web_flow.py`, `test_jobs_properties.py`.

**Behaviour:** feed overlay, `sort=match`, `strong=1` (= band Strong, outcome SCORED), sidebar
counts and `GAP_POOL` (Good or better), upload pre-score (newest 300 visible jobs), the cron sweep
(no LLM call of any kind; alerts only for SCORED Strong matches; `refresh_calibration` once per
sweep; drift alarms from `audit.drift_alarms` in the summary), `/ai/match-breakdown` (engine
explanation, never charges the AI daily limit), `/ai/batch-score` and `/ai/analyze-fit` removed
(404) — all read/write engine fingerprints only. Old rows are never shown:
`test_stale_fingerprints_are_never_served` seeds AI and `local1:` rows and asserts the feed,
sidebar, breakdown and alerts ignore them. A grep in the tests asserts no scoring module calls
OpenAI (`op="match.score"`, `"match.fit"` gone).

- [ ] Tests → FAIL → implement → PASS (all touched suites + the full backend suite once, isolated); commit

### Task 9.3: Tailoring shows engine scores (D-83)

**Files:** Modify `backend/services/resume_tailor.py`, `backend/routers/tailor.py`,
`backend/routers/ai.py` (job-analysis / custom-resume-analysis / rewrite / custom-resume),
`backend/services/match_engine.py` (`analyze_job` no longer produces scores or keyword lists with
the LLM); Test the tailoring suites (`test_tailor_api.py`, `test_tailor_document.py`,
`test_resume_rewrite_regression.py`, `test_analysis_memo.py`).
**Behaviour:** `overall_score`, `ats_score` (= the engine's calibrated score; documented),
`match_label` (band), `keyword_coverage` (required+preferred coverage), `matched_keywords` /
`missing_keywords` (engine facts, display labels) and before/after scores come from scoring the
original and the tailored document with the engine; the LLM is used only for the rewrite text
itself, fed the engine's gaps where it used to be fed its own analysis. Response shapes unchanged
(the extension's tailor card keeps working on the same 0–100 scale).

- [ ] Tests → FAIL → implement → PASS; commit

### Task 9.4: Web app shows the engine's result (D-52, D-85)

**Files:** Modify `frontend/src/pages/Jobs.tsx`, `frontend/src/components/JobDetailView.tsx`,
`frontend/src/components/JobsSidebar.tsx`, `frontend/src/components/CustomResumeModal.tsx`, their
tests. **Behaviour:** card badge = band from the API with "Fit 76" (no "%" and no hard-coded
80/60); low confidence shows the band only; GATE_FAIL shows "Not eligible" with the reason;
NEEDS_REVIEW shows nothing scary ("Not scored"). The detail view ALWAYS requests the breakdown
(it is free now) and renders headline, basis, strengths and gaps with impacts, "to improve"
suggestions, notes, and the interval when displayed. Sidebar copy follows the 85/70/55 bands.
Run `npx tsc --noEmit` and the touched vitest files locally (vitest is not in CI).

- [ ] Tests → FAIL → implement → PASS; commit
### Task 9.6: Docs + real-data dry run on the dev DB (latency, abstention rate, distribution, agreement with the old local scorer)
Includes EVAL §6 hooks: optional shadow scoring (`FIT_SHADOW_CONFIG` dir → score with both, persist
only the live one, log the distribution diff) and the drift alarms wired to the sweep summary.
