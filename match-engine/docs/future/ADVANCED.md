# Advanced Architecture — V2 and V3

> Third file in the set. Read `SPEC.md` and `EVAL.md` first — this does not replace them.
> The V1 engine in `SPEC.md` becomes the **feature layer** for everything here. Nothing is
> thrown away.

---

## 0. The honest frame

Complexity is a cost, not a feature. Every technique below has to pay for itself in a metric
you track, or it's net negative — more failure modes, more maintenance, more that can't be
explained in an audit.

What actually separates a market-leading matcher from a competent one:

| | Competent | Leading |
|---|---|---|
| Weights | Hand-tuned | Learned from outcomes |
| Skill similarity | Hand-curated edges | Learned graph representation |
| Uncertainty | Heuristic confidence | Distribution-free guarantees |
| Resume coverage | What's written | What's written + principled inference |
| Scope | Skill fit today | Skill fit + career trajectory |
| Direction | Employer → candidate | Two-sided |
| Explanation | Which skills matched | Minimal path to the next band |

Rows 5 and 6 are where the market is weakest. If you want a defensible edge rather than
parity, that's where to spend.

**The constraint that changes.** V2 and V3 require relaxing "no AI" — but narrowly and in a
specific direction: **everything is trained offline, pinned as a versioned artifact, and
deterministic at inference.** No API calls, no network, no nondeterminism, no unexplainable
outputs. A pinned GBDT with monotonic constraints is far closer in spirit to your original
requirement than it is to calling an LLM. If that line can't move, stop at §2 — Layer A alone
is a meaningful upgrade and stays fully deterministic.

---

## 1. Architecture: two-stage retrieval and rerank

Scoring every resume against every posting does not scale, and it forces you to make the
scorer cheap. Split it.

```
Stage 1 — RETRIEVAL    ~1 ms    millions → ~1000
  Inverted index on canonical skill IDs, BM25 over the posting's requirement set.
  Hard gates (§4) applied here as filters. Recall-optimized: target recall@1000 ≥ 0.99.

Stage 2 — RANKING      ~15 ms   1000 → 100
  The V1 dimension scorers (SPEC §5) as a cheap feature vector + linear model.

Stage 3 — RERANK       ~80 ms   100 → 20
  Full feature set, learned ranker (§3), conformal intervals (§4),
  trajectory model (§5), counterfactual explanation (§7).
```

Measure **recall@K at each stage boundary**. A reranker cannot recover what retrieval
dropped, and retrieval recall is the ceiling on the entire system. This is the most common
place sophisticated matching pipelines quietly lose quality — everyone tunes the reranker and
nobody measures what stage 1 threw away.

---

## 2. Layer A — representation learning (still fully deterministic)

### 2.1 Skill graph embeddings via node2vec

Hand-curated edges plus PMI (SPEC §3.2) cover the obvious relationships and miss the rest.
Learn a dense representation instead.

```
Graph: ESCO hierarchy edges + PMI co-occurrence edges + alias edges
Walks: 10 per node, length 80, p=1, q=0.5 (structural equivalence bias)
Model: skip-gram, 128 dims, window 5, 5 epochs
Seed:  fixed. Walks serialized and committed. Artifact pinned by hash.
```

`q < 1` biases toward structural equivalence rather than community — you want Kubernetes and
Nomad close because they play the same *role*, not because they co-occur in the same job ads.

Replaces the lookup-table `edge_weight` in SPEC §5.1 with `cos(v_a, v_b)`, with hand-curated
edges still overriding where they exist. Fully deterministic given the pinned artifact.

### 2.2 Static text embeddings for the semantic gap

This is what closes SPEC §18's main open problem. Train fastText on your own posting +
resume corpus — domain-specific, so "Go" near "Kubernetes" rather than near "walk."

```
fastText skip-gram, 300d, subword n-grams 3-6, min_count 5
Phrase detection first (Gensim Phrases) so "machine learning" is one token
Artifact pinned; retrain quarterly with an A/B gate
```

Use it only for **requirement↔evidence similarity when lexical matching fails**, never as the
primary path. Subword n-grams matter here: they give you reasonable vectors for
`Kubernetes`/`K8s`/`kubernetes-native` without alias entries.

Deterministic, ~2 ms, no API. This is the single cheapest large quality win available.

### 2.3 Scope and scale extraction

Resumes encode seniority in numbers that nobody parses. Extract them.

```
QUANTITY PATTERNS → normalized scope features
  "40k req/s", "2M users", "$12M budget", "team of 14", "petabyte"
  → scale_tier ∈ {personal, team, org, internet}  (log-bucketed)
  → people_managed: int
  → budget_managed: normalized currency
```

`scale_tier` is a genuinely strong seniority signal and far harder to game than a title. A
backend engineer who has operated at `internet` scale is a different candidate from one who
hasn't, and the current engine cannot see the difference.

---

## 3. Layer B — learned ranking (the biggest single jump)

Hand-tuned weights are a prior. Once you have outcome data, learn them.

### 3.1 The elegant part

**The V1 dimension scorers become the feature extractors.** You don't discard `SPEC.md` —
its outputs are the feature vector, which means features stay human-meaningful and the
explanation layer survives intact.

```python
features = [
    *v1_dimension_scores,          # 7 features, SPEC §5 — already interpretable
    *per_requirement_match_quality,# top-10 by IDF
    *graph_embedding_similarities, # §2.1
    *text_embedding_similarities,  # §2.2
    *scope_features,               # §2.3
    *trajectory_features,          # §5
    n_requirements, idf_mass_covered, idf_mass_missed,
    evidence_density, temporal_completeness, parse_tier,
]
```

### 3.2 Model

```python
LGBMRanker(
    objective="lambdarank",
    metric="ndcg", eval_at=[10],
    monotone_constraints=[+1 if f in COVERAGE_FEATURES else 0 for f in features],
    monotone_constraints_method="advanced",
    n_estimators=500, num_leaves=31, learning_rate=0.05,
    deterministic=True, force_row_wise=True, seed=42,
)
```

**`monotone_constraints` is non-negotiable.** It guarantees that adding a matched skill can
never lower the score — the property test in `EVAL.md` §2.3 that makes the system defensible.
Without it you will eventually ship a model that rewards *removing* a qualification, and you
will find out via a screenshot on social media.

**Grouping:** `group` = posting ID. LambdaMART optimizes ranking *within* a posting, which is
exactly the product's job.

**Labels, in order of preference:** 90-day retention > hired > interviewed > advanced to
screen > recruiter click. Graded relevance (0–4), not binary. If you only have clicks,
position-bias correct them (inverse propensity weighting) or you'll learn the ranking you
already had.

### 3.3 Explainability is preserved, not lost

```python
shap_values = explainer.shap_values(features)
# feature names are already "required_skill_coverage", "relevant_experience_ratio"
```

TreeSHAP is exact for tree ensembles (not an approximation), and because the features are the
V1 dimensions, SHAP values map straight into the `SPEC.md` §8 explanation with no translation
layer. This is the reason for keeping V1 as the feature layer rather than going end-to-end
neural — an end-to-end model would beat this by a few NDCG points and cost you the entire
audit story.

### 3.4 Ensemble

Keep V1 running. Blend:

```
final = w·learned + (1-w)·v1_rule_based,  w ramping 0.3 → 0.8 as label volume grows
```

Rule-based is your floor for cold-start postings, new job families, and the day the model
artifact fails to load. Never let the learned path be a single point of failure.

---

## 4. Layer C — conformal prediction

This replaces the heuristic intervals in `SPEC.md` §17.3 with a **distribution-free coverage
guarantee**, and it is the most intellectually serious upgrade in this document.

### Split conformal, Mondrian by job family

```
1. Hold out a calibration set (~1000 pairs), disjoint from training.
2. Nonconformity score: s_i = |y_i - ŷ_i|
3. For each job family g, compute q_g = ⌈(n_g+1)(1-α)⌉-th smallest s_i in g
4. Prediction interval: [ŷ - q_g, ŷ + q_g]
```

This gives you, at α = 0.1, a mathematically guaranteed 90% coverage — the true relevance
lands in the stated interval 90% of the time — **with no distributional assumptions and no
assumption that the model is correct**. The guarantee holds even if the model is badly
misspecified. That property is rare and worth a great deal in a regulated domain.

Mondrian (per-group) conditioning matters: marginal coverage can hide a family where coverage
is 60%. Condition on job family, and separately **audit coverage by demographic group** —
systematically wider intervals for one group is a fairness problem that aggregate metrics
will not surface.

Interval width then drives the §17 abstention policy, now with a real guarantee behind it
rather than a weighted geometric mean of heuristics.

---

## 5. Layer D — career trajectory modeling (your differentiator)

Everyone scores "can this person do this job." Almost nobody scores "is this a sensible next
move for this person." The second question is what candidates actually care about, and it's
where the market is weakest.

### 5.1 Transition graph

From the resume corpus, mine actual career moves:

```
P(role_j | role_i, skills, tenure, industry)
```

Nodes = canonical (title × seniority × industry). Edges weighted by observed frequency,
Laplace-smoothed, with a recency half-life of 3 years so the graph reflects the current
market rather than 2012's.

### 5.2 Three derived features

```
transition_likelihood   P(target | current) from the graph
progression_delta       seniority(target) - seniority(current), with
                        trajectory velocity: this candidate's historical levels/year
skill_bridge_distance   graph-embedding distance from current skill centroid to
                        the target role's centroid — "how far is this reach?"
```

### 5.3 Products this unlocks

- **"Reach / match / safe"** classification alongside the fit score.
- **"You're 2 skills from this role"** — the counterfactual in §7, aimed at a career step
  rather than a single posting.
- **"People with your background moved into these roles"** — recommendation from the
  transition graph, not the matcher. High engagement, genuinely useful, nobody does it well.
- **Compensation trajectory**: expected comp delta over 3 years along each path.

**Fairness caveat, important.** The transition graph is mined from historical career moves,
which encode historical discrimination. If women have historically moved from engineering into
PM rather than into staff engineering, the graph learns that and will recommend it. Audit
transition recommendations by group and consider reweighting toward the *skill bridge*
distance rather than observed frequency. This feature can entrench the exact pattern your
product should be dissolving.

---

## 6. Layer E — two-sided matching

The employer-fit score is half the problem. Model candidate fit too.

```
candidate_utility = f(
    comp_offered vs. comp_expectation,     # the dominant term
    commute / remote alignment,
    seniority_progression_delta,           # is this a step up?
    skill_growth_opportunity,              # new skills this role would build
    company_stage_preference,
    tenure_risk,                           # over-qualification → early exit
)

match = harmonic_mean(employer_fit, candidate_utility)
```

Harmonic mean, not arithmetic — it punishes imbalance. A candidate the employer loves who
would take a pay cut and a commute is a bad match, and an arithmetic mean hides that.

This directly improves the metric that matters commercially: **application-to-interview rate**
and 90-day retention, not raw match quality. It's also the feature most likely to differentiate
the product in a demo.

---

## 7. Layer F — counterfactual explanation as optimization

Formalize `SPEC.md` §8's `to_improve` as a constrained minimization:

```
minimize    Σ_{s ∈ S} learning_cost(s)
subject to  score(resume ⊕ S) ≥ next_band_threshold
            |S| ≤ 4
```

`learning_cost(s)` from the skill graph: distance from the candidate's existing skill
centroid, scaled by typical time-to-proficiency. Solve as a small ILP (PuLP/OR-Tools) or
greedy with a swap-improvement pass — the search space is tiny because `score()` is pure and
fast (SPEC §1).

Output:

> **Two steps from a Strong match:** Terraform (≈3 weeks given your Kubernetes background)
> and a CI/CD pipeline project (≈2 weeks). Estimated fit: 84.

This is the single highest-engagement feature in a job-seeker product, and it is only
possible because the scoring core is deterministic and cheap to re-run. It falls out almost
free from the V1 architecture — which is the payoff for the discipline in `SPEC.md` §1.

---

## 8. Posting-side intelligence

Garbage postings produce garbage matches regardless of algorithm.

```
Requirement inflation detection
  Compare stated min_years against the market distribution for that title.
  >90th percentile → down-weight, flag "this requirement is unusual for this role"

Ghost job detection
  Reposting frequency, time-to-first-response, posting age vs. family median,
  requirement incoherence. Suppress or label. Protects candidate trust.

Duplicate detection
  MinHash/LSH over normalized requirement sets. Aggregators create massive duplication.

Implicit requirement inference
  Requirements the posting omits but the role always has, from the corpus:
  P(skill | title, industry, seniority) > 0.8 → add as IMPLIED at reduced weight
```

Requirement inflation is also a fairness lever worth knowing about: inflated requirements
disproportionately deter candidates from underrepresented groups from applying, a
well-documented effect. Down-weighting them is both more accurate and better for your
marketplace.

---

## 9. Training infrastructure

Where sophisticated systems actually fail. Non-negotiables:

**Temporal splits only.** Train on postings before date D, validate after. Random splits leak
future information through shared postings and will overstate your metrics by a wide margin.

**Group-aware splitting.** Never split a posting's applicants across train and test. Same
failure mode, less obvious.

**Feature store with point-in-time correctness.** IDF, PMI, and transition probabilities as of
*the scoring date*, not today's values. Getting this wrong is the single most common
production ML bug and it is silent — the model looks excellent offline and mediocre live.

**Active learning for the golden set.** Label where it pays:
```
priority = predictive_variance × conformal_interval_width × |v1_score - learned_score|
```
Pairs where the models disagree and the interval is wide carry the most information per
labeling dollar — typically 3–5× the efficiency of random sampling.

**Multi-objective tuning.** Optimize the Pareto front, don't collapse to a scalar:
```
maximize NDCG@10, minimize ECE, subject to four_fifths ≥ 0.80 ∀ groups
```
Present the frontier to a human for selection. The weight you'd use to scalarize these is
itself a policy decision, and it should be made explicitly by a person, not buried in a
config file.

---

## 10. Honest cost ledger

| Layer | Needs | Build | Ongoing | Gain (est.) |
|---|---|---|---|---|
| A — representations | Corpus only | 3–4 wk | Quarterly retrain | +0.03–0.06 NDCG |
| B — learned ranker | ~50k labeled outcomes | 6–8 wk | Monthly retrain, monitoring | +0.08–0.15 NDCG |
| C — conformal | 1k calibration pairs | 1–2 wk | Low | No NDCG gain — trust and defensibility |
| D — trajectory | Large resume corpus | 6–10 wk | Quarterly | Product differentiation, not NDCG |
| E — two-sided | Comp + preference data | 4–6 wk | Low | Interview rate, retention |
| F — counterfactual | None (V1 suffices) | 2 wk | Low | Engagement |

**Layer B's prerequisite is the binding one.** Fifty thousand labeled outcomes means real
applicant volume over real time. You cannot buy or synthesize your way past it, and attempting
to train a ranker on 500 examples will produce something measurably worse than the hand-tuned
weights. Build A, C, and F first — none of them need labels.

**What you're signing up for ongoing:** roughly one engineer's continuous attention for model
monitoring, retraining, and drift response, plus legal review on every change to the decision
logic. If that headcount isn't real, build Layers A, C and F and stop. A well-maintained V1
beats a decayed V3, and a decayed V3 is what you get without that person.

---

## 11. What I'd still refuse to add

Cataloged so nobody adds them later thinking they were an oversight:

- **End-to-end neural matching (cross-encoder on raw text).** Would likely beat this on NDCG.
  Costs the entire explanation and audit story. Not a trade worth making in a regulated
  employment context, where you must describe your decision logic on demand.
- **Personality / culture-fit inference from resume text.** No validity evidence, severe
  disparate-impact exposure, and in several jurisdictions probably unlawful.
- **Video, voice, or photo signals.** Illegal in Illinois without consent (AIVIA), heavily
  restricted elsewhere, and the validity literature is poor.
- **Company-prestige tiering.** Strong socioeconomic proxy. Low marginal signal once skills
  and scope (§2.3) are extracted properly.
- **Any feature you cannot explain to a rejected candidate in one sentence.** That's the
  practical test. If the answer requires describing a latent space, it doesn't ship.
