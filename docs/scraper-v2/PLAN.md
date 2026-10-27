# Scraper v2: plan

Spec: `docs/scraper-v2/SPEC.md` (the owner's overnight-build prompt, completed 2026-10-25). Where this plan and the spec disagree, the spec wins unless DECISIONS.md records why.

Branch `feat/scraper-v2` (worktree `.worktrees/scraper-v2`), from `origin/main` at f9d4738. Neon branch `scraper-v2-dev` (`br-summer-sunset-b7t9a1u0`), copied from production at 2026-10-25 04:47 UTC. Started 2026-10-25 00:47 EST; 12-hour budget.

## What exists (Phase 0)

The spec's "What already exists" section was verified against the code and Neon on 2026-10-25 and still holds. The parts this plan builds on:

- `backend/services/ats_scraper.py`: `ATSJob` (the raw job), `BoardSnapshot` (filtered jobs plus every live URL, `complete`, `rejected`), `ATSScraper.scrape_board()` routing five platforms, `rejection()` ("level", "location", "unplaced"), `entry_tier()`, `experience_level_for()`, `HARD_SENIOR` / `SOFT_SENIOR` / `FRONTLINE`.
- `backend/routers/github_sources.py` `cron_ats()`: the per-board write path (refresh known rows, insert new rows with descriptions and logos, cross-source twins, reconcile or confirm, `source_health`). Orchestration, normalization and writes live in one 230-line function.
- `backend/services/listing_freshness.py`: `title_fields()`, `location_derived_fields()`, `build_new_row_fields()`, `refresh_known_listings()` (column-only reads), `reconcile_board()`, the sweeps, `record_liveness()`, `HIDDEN_LISTING_STATUSES` (closed + off_target; `stale` visible).
- `backend/services/platform_liveness.py` (per-platform API checks), `cross_source_dedup.py`, `structured_extraction.py`, `location_parser.py`, `na_location.py`, `role_classifier.py` (17 categories, read by the fit engine).
- `backend/data/ats_companies.json` + `company_registry.py`: 278 entries, 252 enabled; `SUPPORTED_PLATFORMS` is a fixed set of five.
- Boot migrations: idempotent modules in `backend/migrations/`, imported in `backend/main.py`, guarded by lock timeouts and an advisory lock (see `add_listing_probe_columns.py`). New ORM tables are created by `Base.metadata.create_all` at boot.
- `experience_years_required` exists on `scraped_jobs` and nothing fills it.

## Decision: refactor

| Rewrite criterion | Today | True? |
|---|---|---|
| Parses HTML where a JSON endpoint exists | All five connectors use public JSON APIs | No |
| Source logic, normalization and DB writes tangled | `cron_ats()` mixes orchestration, normalization and writes | Yes |
| No tests and no fixtures | Connector fixtures and lifecycle, liveness and dedup tests exist | No |
| Cannot run partially | One failing board is a `source_health` row | No |
| Adding a source copies more than 50 lines | A new fetcher is new code (~60 lines), not a copy | No |

One of five is true, so v2 refactors: it keeps `ATSJob` / `BoardSnapshot` / reconciliation, moves the write path out of the router into `backend/ingest/core/pipeline.py`, adds platforms as adapter modules, and replaces the entry-level gate with tags.

## Architecture (target)

```
backend/ingest/
  taxonomy.py            # experience levels + career families (rules), legacy maps
  data/taxonomy.json     # keyword rules, boundary rules, legacy category map
  data/limits.json       # storage caps (per-company cap, description cap, stop size)
  core/http.py           # UA, per-host pacing, retries/backoff, robots.txt cache
  core/pipeline.py       # ingest one BoardSnapshot (shared by cron-ats and the runner)
  core/queue.py          # scrape_tasks: enqueue, claim (SKIP LOCKED), lease, finish
  run.py                 # runner CLI: lock, plan, crawl, ingest, sweep, report
  report.py              # last run summary
  adapters/__init__.py   # registry: discovers adapter modules
  adapters/<platform>.py # one per new platform
```

Adapters return what the existing fetchers return, `(list[ATSJob], complete, total)`, because `BoardSnapshot` needs the whole list and the `complete` flag to reconcile. `ATSJob` is the spec's RawJob.

## Storage and transfer plan

- `main` 302 MB logical of 512 MB; gate at 450 MB on both branches.
- Reclaim first: strip descriptions from rows dead over 30 days that no user table references (about 48 MB).
- Caps in `backend/ingest/data/limits.json`: newest postings first, at most `max_live_per_company` live rows per employer, descriptions cut at `description_max_chars`, and the runner stops inserting when `pg_database_size` passes `stop_inserting_at_mb`.
- Transfer: full re-list once a day, not every hour; the refresh reads columns only; the build measures `data_transfer_bytes` at start, after each full run and at the end.

## Global constraints (every task)

1. Read the spec section your task cites. Exact values in this plan are binding.
2. Python code must run on 3.12 (Vercel) and 3.13 (CI, local). Add nothing to `backend/requirements.txt`.
3. Never write the em dash character (U+2014) anywhere: code, comments, tests, docs, commit messages.
4. Database changes are additive only. Never rewrite `scraped_jobs` (no ALTER COLUMN TYPE, no volatile default, no VACUUM FULL, no table copy). Never delete a job row.
5. Tests: pytest under `backend/tests/` (new ingest tests under `backend/tests/ingest/`, fixtures under `backend/tests/fixtures/ingest/`). Run them from a scratch working directory so the CWD-relative SQLite files never collide with another process: `cd <your scratch dir>`, set `DATABASE_URL=sqlite:///./ci_test.db`, `JWT_SECRET=x`, `PYTHONPATH=<worktree>`, then `python -m pytest <worktree>/backend/tests/<path> -q`. Never point a test at Neon.
6. Tests that make real network requests carry `@pytest.mark.network` and are skipped unless `RUN_NETWORK_TESTS=1`.
7. Every HTTP request identifies as `TailrdBot/2.0 (+https://www.tailrd.ca)`, paces per host (default 1 request per second), backs off on 429 and 5xx, and never logs in, bypasses a bot check, or uses a headless browser.
8. Match the surrounding code: comments explain why, in plain sentences; names follow the module you are in.
9. Touch only the files your task lists (plus tests and fixtures you add). Commit in your own worktree, staging by explicit path. Never change branches, stash, reset, rebase or push.
10. Experience levels stored in `scraped_jobs.experience_level` are exactly: `internship`, `new_grad`, `entry`, `mid`, `senior`, `lead`, `director`, `executive`.
11. UI experience options (value, label), in this order: (`intern_new_grad`, "Intern/New Grad"), (`entry`, "Entry Level"), (`mid`, "Mid Level"), (`senior`, "Senior Level"), (`lead`, "Lead/Staff"), (`director`, "Director/Executive"). Filter expansion (backend `EXPERIENCE_FILTER_MAP`): `intern_new_grad` -> internship, new_grad; `entry` -> entry; `mid` -> mid; `senior` -> senior; `lead` -> lead; `director` -> director, executive; legacy `internship` -> internship; legacy `new_grad` -> new_grad, entry (the old "New Grad / Entry Level" option); `executive` -> executive. Default when a user has no saved choice: `intern_new_grad` + `entry`.
12. Career families are stored in `scraped_jobs.career_family` as slugs (labels carry commas, which break comma-separated filter params). Slug: label, in this order:
    `software_engineering`: Software Engineering; `data_ai_analytics`: Data, AI & Analytics; `it_security_infrastructure`: IT, Security & Infrastructure; `product_design`: Product & Design; `engineering`: Engineering; `legal`: Legal; `healthcare_clinical`: Healthcare, Clinical; `healthcare_admin_support`: Healthcare, Administration & Support; `finance_accounting`: Finance & Accounting; `banking_insurance_investment`: Banking, Insurance & Investment; `sales_bizdev`: Sales & Business Development; `marketing_communications`: Marketing & Communications; `operations_supply_chain`: Operations, Supply Chain & Logistics; `hr_recruiting`: Human Resources & Recruiting; `education_research`: Education & Research; `government_public_sector`: Government & Public Sector; `skilled_trades`: Skilled Trades, Construction & Manufacturing; `customer_support_admin`: Customer Support & Administrative; `hospitality_retail_food`: Hospitality, Retail & Food Service; `other`: Other.
13. Adapter contract (`backend/ingest/adapters/<module>.py`): module constants `PLATFORM` (the registry `ats_platform` value), `TIER` (1 to 4), `COUNTRIES` (tuple), `RATE_LIMIT` (seconds between requests to one host), `REQUIRED_ENV` (tuple of env var names; the adapter is disabled when any is missing), `HOST_PATTERNS` (tuple of regexes for posting URLs it owns); `async def list_jobs(client, slug, company_name, *, config, deadline=None) -> tuple[list[ATSJob], bool, int]` returning every listing unfiltered (all levels, all countries), whether the list is complete, and the total the source reported; optional `async def fetch_job(client, slug, ref, *, config) -> dict` returning `{"description": str, "employment_type": str}`; optional `async def check_alive(client, url) -> platform_liveness.LivenessResult`. `ATSJob.external_id` is `"{PLATFORM}:{slug}:{source id}"`; `ATSJob.url` is the public posting URL without tracking parameters. Requests go through `backend.ingest.core.http`.
14. Registry entries (`backend/data/ats_companies.json`) may carry `hq_country` (`CA` or `US`: where the employer is based and mostly hires; every entry gets one), `country` (unchanged meaning: the board posts in that one country only, which switches off the location filter, so it is set only for single-country boards such as hospitals, universities and governments), `sector`, `careers_url`, and an adapter-specific `config` object; `company_registry.load_board_config(platform, slug)` returns that object.
15. Legacy job-function map (old `role_category` value -> family slugs), identical in `taxonomy.LEGACY_CATEGORY_TO_FAMILIES` and the frontend: Software Engineering -> software_engineering; Engineering and Development -> engineering, it_security_infrastructure; Data Analysis -> data_ai_analytics; Machine Learning and AI -> data_ai_analytics; Accounting and Finance -> finance_accounting, banking_insurance_investment; Management and Executive -> operations_supply_chain; Sales -> sales_bizdev; Marketing -> marketing_communications; Human Resources -> hr_recruiting; Product Management -> product_design; Business Analyst -> data_ai_analytics; Creatives and Design -> product_design, marketing_communications; Legal and Compliance -> legal; Customer Service and Support -> customer_support_admin; Operations -> operations_supply_chain; Consultant -> other; Other -> other.

## Tasks

### Task 1: Foundation: schema, models, adapter registry, HTTP helper, taxonomy stub

Spec sections: "Data model (Neon Postgres)", "Target architecture", "Scraping conduct". Model: opus. Runs first; everything else builds on it.

Files: `backend/migrations/add_scraper_v2.py` (new), `backend/main.py` (register it), `backend/db/models.py`, `backend/ingest/__init__.py`, `backend/ingest/core/__init__.py`, `backend/ingest/core/http.py`, `backend/ingest/adapters/__init__.py`, `backend/ingest/taxonomy.py` (stub), `backend/services/ats_scraper.py` (`scrape_board` routing only), `backend/data/company_registry.py`, `backend/services/platform_liveness.py` (adapter hook only), tests under `backend/tests/ingest/`.

1. Migration `add_scraper_v2.py`, in the style of `add_listing_probe_columns.py` (inspect first, send no DDL when everything exists, `lock_timeout`, an advisory xact lock with a new unique key, `IF NOT EXISTS` on Postgres, plain on SQLite):
   - `scraped_jobs` new nullable columns, no defaults: `career_family VARCHAR(48)`, `career_family_secondary VARCHAR(48)`, `remote_scope VARCHAR(16)`, `enrichment_version INTEGER`, `fingerprint VARCHAR(64)`, `company_id INTEGER`, `closed_reason VARCHAR(32)`, `consecutive_failures INTEGER`, `salary_is_estimated BOOLEAN`, `apply_url VARCHAR(2048)`, `expires_at TIMESTAMP`.
   - indexes: `ix_scraped_jobs_status_family (listing_status, career_family)`, `ix_scraped_jobs_status_level (listing_status, experience_level)`, `ix_scraped_jobs_status_geo (listing_status, country, region)`, `ix_scraped_jobs_company_id (company_id)`, `ix_scraped_jobs_status_probed (listing_status, last_probed_at)`, `ix_scraped_jobs_fingerprint (fingerprint)` (not unique; see the spec's Dedupe section).
   - `source_health` new nullable columns: `disabled_until TIMESTAMP`, `jobs_7d_avg FLOAT`.
2. `models.py`: the new columns on `ScrapedJob` and `SourceHealth`; new models `Company` (`companies`: id, name, domain, country, sector, ats, ats_token, careers_url, config JSON, enabled, created_at, updated_at; unique (ats, ats_token)), `ScrapeRun` (`scrape_runs`: id, started_at, finished_at, trigger, status, tasks_total, tasks_ok, tasks_failed, jobs_seen, jobs_new, jobs_closed, notes TEXT, stats JSON), `ScrapeTask` (`scrape_tasks`: id, run_id, kind, payload JSON, status, attempts, lease_until, last_error TEXT, created_at, finished_at; index (status, lease_until)). `create_all` makes the tables at boot.
3. `backend/ingest/core/http.py`: `USER_AGENT`, `make_client(**kwargs)` (UA header, 30 s timeout, follows redirects), `async request(client, method, url, *, rate_limit=1.0, retries=3, **kwargs)` (per-host pacing, exponential backoff with jitter on 429/5xx and timeouts, honours `Retry-After`, raises the last error), `async robots_allowed(client, url)` (urllib.robotparser, cached per host per process; a robots.txt that cannot be fetched allows).
4. `backend/ingest/adapters/__init__.py`: discovers every module in the package that defines `PLATFORM` and `list_jobs`; `get_adapter(platform)`, `adapter_platforms()`, `adapter_enabled(platform)` (False when a `REQUIRED_ENV` var is missing), `adapter_for_url(url)` (first adapter whose `HOST_PATTERNS` match). A broken adapter module logs and is skipped, never breaks the import.
5. `ats_scraper.scrape_board()`: a platform not among the five built-ins goes to its adapter (`list_jobs(client, slug, company_name, config=load_board_config(platform, slug), deadline=self.deadline)`); an unknown or disabled platform keeps today's empty incomplete snapshot.
6. `company_registry.py`: `SUPPORTED_PLATFORMS` stays the built-in five, and `load_companies(supported_only=True)` also accepts platforms with an enabled adapter; `load_board_config(platform, slug)`; `load_board_countries()` keeps working for the new `country` field.
7. `platform_liveness`: before the generic page check, a URL an adapter owns (`adapter_for_url`) is checked by that adapter's `check_alive` when it has one.
8. `backend/ingest/taxonomy.py` stub with the final public API (Task 2 fills it in): `LEVELS` (constraint 10), `FAMILIES` (ordered dict slug -> label, constraint 12), `ENRICHMENT_VERSION = 1`, `classify_experience(title, department="", employment_type="", years_required=None) -> str` (stub: `experience_level_for` when `entry_tier` says entry level, else "mid"), `classify_family(title, department="", description="") -> tuple[str, str | None]` (stub: ("other", None)), `years_required(text) -> int | None` (stub: None), `LEGACY_CATEGORY_TO_FAMILIES: dict[str, list[str]]` (stub: {}).
9. Tests: the migration runs twice on SQLite and on a second run sends no DDL; `scrape_board` reaches a fake adapter; `load_board_config`; the HTTP helper retries a 429 then succeeds (httpx MockTransport) and sends the UA; the liveness hook routes to a fake adapter. The existing suite still passes.

### Task 2: Taxonomy: experience levels and career families (rules) with a labeled sample

Spec sections: "Experience levels", "Career families", "Testing and acceptance gates" (enrich). Model: opus. After Task 1.

Files: `backend/ingest/taxonomy.py`, `backend/ingest/data/taxonomy.json`, `backend/tests/ingest/test_taxonomy.py`, `backend/tests/fixtures/ingest/labels.json`.

1. Label first. Start from the collected sample `<scratch>/labels-raw.json` (real postings: title, company, department, employment_type, description snippet, url). Add `label_family` (constraint 12 slug) and `label_level` (constraint 10 value) to at least 200 of them, at least 5 per family and 15 per level, judging only the posting, never the classifier. Commit `labels.json` before writing classifier code; any later label change goes in the task report with its reason.
2. `classify_experience`: reuse `ats_scraper`'s patterns (`entry_tier`, `experience_level_for`, `HARD_SENIOR`, `SOFT_SENIOR`, `FRONTLINE`, `INTERNSHIP_TITLE`) so today's internship and new-grad rows keep their labels; the strong tier stays internship / new_grad, the weak tier (junior, entry-level, level I, associate, analyst, 0-2 years) becomes `entry`; frontline hourly work without a level is `entry`; mid-level words and level II are `mid`; senior words, III/IV and 5+ years are `senior`; staff, lead, principal, architect, manager and supervisor are `lead`; director, head of, VP are `director`; chief, president (not vice president), C-suite, EVP/SVP are `executive`; no signal falls back to `years_required` (0-1 entry, 2-4 mid, 5-7 senior, 8+ lead), then `mid`. Boundary rules live in `taxonomy.json`.
3. `years_required(text)`: the minimum years a description asks for ("3+ years", "minimum of 5 years", "2-4 years of experience"), ignoring ages, dates and "years in business"; None when absent.
4. `classify_family`: keyword rules over title, department and the first 600 characters of the description, priority-ordered in `taxonomy.json`; one primary and an optional secondary; `other` only when nothing matches.
5. `LEGACY_CATEGORY_TO_FAMILIES`: each of the 17 `role_classifier.CANONICAL_CATEGORIES` mapped to one or more family slugs.
6. Tests: the accuracy test over `labels.json` (family >= 90%, level >= 85%), per-rule unit tests, and a test that every row `experience_level_for` labels internship or new_grad keeps that label.

### Task 3: Tier 1 adapters: Workable, Recruitee, Teamtailor, BambooHR, Jobvite

Spec section: "Sources to cover" (Tier 1). Model: sonnet. After Task 1; parallel with Tasks 2, 4, 5, 6, 7.

Files: `backend/ingest/adapters/{workable,recruitee,teamtailor,bamboohr,jobvite}.py`, fixtures, `backend/tests/ingest/test_adapters_tier1.py`.

For each platform: verify the current public endpoint shape with a live request first; implement the adapter contract (constraint 13), including `check_alive` when the platform has an API that can answer for one posting; save a sanitized real payload as a fixture and test parsing (titles, URLs, external ids, locations, posted dates, employment type, description when the list carries it); a `network` test listing at least 1 job for 3 real companies. A platform with no public feed (Jobvite may not have one) is documented in the report and left out. In the report, list every board you verified (company, platform, slug, country, open postings) as JSON for the registry.

### Task 4: Tier 2 adapters: Oracle Cloud Recruiting, iCIMS, Taleo, SuccessFactors

Spec section: "Sources to cover" (Tier 2). Model: sonnet. After Task 1; parallel.

Files: `backend/ingest/adapters/{oracle_hcm,icims,taleo,successfactors}.py`, fixtures, `backend/tests/ingest/test_adapters_tier2.py`.

Same requirements as Task 3. Oracle Cloud Recruiting: the public `hcmRestApi` requisition finder (`recruitingCEJobRequisitions`, `findReqs` with `siteNumber`), config `{"host": ..., "site": ...}`, detail via `recruitingCEJobRequisitionDetails`; JPMorgan Chase (`jpmc.fa.oraclecloud.com`, `CX_1001`) is a known tenant. iCIMS: public search pages (`/jobs/search?pr=N&in_iframe=1`) and JobPosting JSON-LD on posting pages; respect robots.txt. Taleo and SuccessFactors: attempt each within 45 minutes; if blocked, stop, keep what you learned in the report, and ship nothing for it.

### Task 5: Public-sector and aggregator adapters: USAJOBS, Job Bank Canada, Adzuna, Jooble

Spec sections: "Sources to cover" (Tier 2 public sector, Tier 3). Model: sonnet. After Task 1; parallel.

Files: `backend/ingest/adapters/{usajobs,jobbank_ca,adzuna,jooble}.py`, fixtures, `backend/tests/ingest/test_adapters_public.py`, `.env.example` (new, key names with comments only).

USAJOBS (`REQUIRED_ENV` USAJOBS_API_KEY, USAJOBS_USER_AGENT), Adzuna (ADZUNA_APP_ID, ADZUNA_APP_KEY; countries ca and us), Jooble (JOOBLE_API_KEY): built against documented response shapes with hand-made fixtures that follow the documentation exactly; they ship disabled until the keys exist. A board's `slug` and `config` describe the query (keyword set, location, page cap). Aggregator rows set `apply_url` to the employer link when given. Job Bank Canada: look for an official feed or open-data export first; else respectful HTML of public search results plus JobPosting JSON-LD on posting pages, robots.txt respected. Report which of these returned live data.

### Task 6: Ingestion pipeline, runner, queue, caps and report

Spec sections: "Target architecture", "Scheduling and running within platform limits" (concurrency, observability), "Storage and transfer budget", "Dead-job detection". Model: opus. After Task 1; parallel with Tasks 2 to 5 and 7.

Files: `backend/ingest/core/pipeline.py`, `backend/ingest/core/queue.py`, `backend/ingest/run.py`, `backend/ingest/report.py`, `backend/ingest/data/limits.json`, `backend/routers/github_sources.py` (`cron_ats` calls the pipeline), `backend/services/listing_freshness.py`, `backend/services/ats_scraper.py` (`RETIRABLE_REJECTIONS`), `backend/scripts/reclaim_dead_descriptions.py`, tests.

1. Move `cron_ats()`'s per-board body into `pipeline.ingest_snapshot(db, client, snapshot, ctx)`; `cron_ats` keeps its response shape and calls it. Behaviour stays the same except the v2 changes below. The existing cron-ats tests pass.
2. Every level: crawls run `ATSScraper(filter_entry_level=False)`; `RETIRABLE_REJECTIONS` becomes `{"location"}`; `title_fields()` sets `experience_level` with `taxonomy.classify_experience` (fed `years_required` of the description when the title has no level), `career_family`, `career_family_secondary`, `enrichment_version`; `role_category` keeps `role_classifier.classify`. LinkedIn/Indeed ingest and `retire_senior_aggregator_rows` stay exactly as they are (spec decision 6). New rows also get `fingerprint` (spec Dedupe), `experience_years_required`, `company_id` when the companies table has the employer, `closed_reason` stamped when a row is removed or expires (`delisted`, `404`, `redirect_home`, `closed_text`, `ttl`).
3. Caps from `limits.json` (`max_live_per_company`, `skip_posted_older_than_days`, `description_max_chars`, `max_new_rows_per_run`, `stop_inserting_at_mb`): new jobs are inserted newest first; a board whose employer already holds the cap of live rows inserts no more; the runner checks `pg_database_size` every 500 inserts and stops inserting (still reconciling) past the stop size.
4. Queue: `enqueue(db, run_id, kind, payloads)`, `claim(db, run_id, limit, lease_seconds)` (`FOR UPDATE SKIP LOCKED` on Postgres; a plain select on SQLite), `finish(task, ok, error)`, expired leases reclaimed, at most 3 attempts, finished tasks older than 7 days deleted at run start.
5. Runner `python -m backend.ingest.run` (`--boards`, `--platforms`, `--limit`, `--concurrency` 20, `--per-host` 2, `--budget-minutes` 170, `--dry-run`, `--trigger`): a session advisory lock on `DATABASE_URL_DIRECT` when set (else the main URL) so two runs never overlap; one `scrape_runs` row; registry mirrored into `companies`; one `list` task per enabled board not in breaker cooldown; crawl with the concurrency limits, ingest each snapshot through the pipeline, `source_health` per board (with `jobs_7d_avg` and a yield warning when a board drops more than 60% below it); then the existing freshness sweeps; then the run summary. JSON log lines, one per task. `ALERT_WEBHOOK_URL` gets the yield warnings when set.
6. `report.py`: last run summary, per-source yield, live jobs by family, level, country and top 15 regions, storage size; usable as `python -m backend.ingest.report`.
7. `reclaim_dead_descriptions.py`: dry run by default; `--apply` nulls `description`, `description_sections` and `change_log` on rows `removed`/`expired` for more than `--days` (30) with no row in `application_records`, `user_saved_jobs`, `tailored_resumes`, `job_match_notifications`, `connection_requests`, `pending_questions`; batches of `--batch` (2000); prints the size before and after.
8. Tests: pipeline idempotency (a second ingest of the same snapshot adds 0 rows), caps, the queue (claim, lease expiry, attempts), a runner end to end on SQLite with a fake adapter, the closure simulation (50 postings dropped from a complete fixture board are hidden and closed, `removed` with `closed_reason` `delisted`, by the first reconcile, stay closed after a liveness pass, and one that reappears is revived), reclaim dry run vs apply.

### Task 7: App integration: experience levels, career families, feed visibility, alerts

Spec sections: "Experience levels", "Career families", "Dead-job detection" (stale hidden), "Mission" (in-scope UI). Model: sonnet. After Task 1; parallel.

Files: `frontend/src/components/JobFilterBar.tsx`, `frontend/src/pages/Jobs.tsx` (defaults), `frontend/src/setup/steps/ExperienceStep.tsx`, the onboarding job-function step, `frontend/src/setup/answersToFilters.ts`, `frontend/src/setup/types.ts` if needed, frontend tests, `backend/services/job_filters.py`, `backend/routers/jobs.py` (list filters, stats), `backend/services/listing_freshness.py` (`HIDDEN_LISTING_STATUSES` only), `backend/services/match_notifier.py`, `backend/services/jobs_sidebar.py` if it counts by category or level, backend tests.

1. Experience options and filter map exactly as constraint 11; `normalizeExperienceLevels()` maps saved values onto the new options (internship -> intern_new_grad; new_grad -> intern_new_grad + entry; intern_new_grad and the five new keys stay; anything else is dropped). A visitor or user with no saved experience filter starts at Intern/New Grad + Entry Level.
2. Job functions become the 20 career families (constraint 12): a `career_family` filter (comma-separated slugs) on `GET /jobs` filtering `scraped_jobs.career_family`, labels in the filter bar and onboarding; saved `role_category` filters migrate to families through a legacy map identical to `taxonomy.LEGACY_CATEGORY_TO_FAMILIES` (a backend test reads both and checks they agree). Keep the `role_category` param working for old clients.
3. Feed visibility: `stale` joins `HIDDEN_LISTING_STATUSES`; `match_notifier._alertable()` stops including `stale`.
4. Match alerts: the sweep's scoring window keeps only jobs whose `experience_level` is in the user's levels (`user_settings.experience_levels`, expanded by `EXPERIENCE_FILTER_MAP`; empty means internship, new_grad, entry).
5. Tests: backend filter expansion, the career_family filter, stale hidden, alerts never score a director job for an Intern/New Grad user; frontend vitest for the normalizers, the filter bar options and the onboarding mapping. `npx tsc --noEmit` clean.

### Task 8: Scheduling, CI and cut-over

Spec section: "Scheduling and running within platform limits". Model: sonnet. After Task 6.

Files: `.github/workflows/ingest.yml` (new), `.github/workflows/ingest-network-tests.yml` (new), `.github/workflows/ci.yml`, `requirements-ingest.txt` (new, exact pins with hashes), `pytest.ini` or `pyproject.toml` (the `network` marker), `.github/workflows/scrape-jobs.yml` and `backend/tests/test_scrape_jobs_workflow.py` (cut-over), `backend/routers/github_sources.py` (cut-over flag only).

1. `ingest.yml`: daily at an off-peak minute plus `workflow_dispatch`; `permissions: contents: read`; actions pinned by SHA; installs only `requirements-ingest.txt` with `--require-hashes`; runs `python -m backend.ingest.run --trigger cron` with `DATABASE_URL` and `DATABASE_URL_DIRECT` from the secret `INGEST_DATABASE_URL`; when the secret is empty it prints a notice and exits 0; never installs python-jobspy.
2. `ingest-network-tests.yml`: nightly, runs the `network` tests with `RUN_NETWORK_TESTS=1`.
3. CI: the per-PR run deselects `network` tests; a job with a `postgres:17` service runs the Postgres-only ingest tests.
4. Cut-over: with `INGEST_V2_RUNNER=1` set on Vercel, `cron-ats` skips boards the v2 runner crawled in the last 20 hours (from `source_health`), so the two never crawl a board twice in a cycle. Default off.

### Task 9: Company discovery: registry to 600+ employers, ats-detect

Spec section: "Company discovery". Controller merges the research output; one implementer writes the script. After Tasks 3 to 5 (the platforms must exist).

Files: `backend/data/ats_companies.json`, `scripts/ats_detect.py`, `backend/tests/test_ats_detect.py`.

1. Merge the verified seed files (`<scratch>/seeds/*.json`) and the adapter tasks' verified boards into the registry: no duplicates, `hq_country` on every entry (backfill the existing 278), `country` only on single-country boards (constraint 14), `sector` and `careers_url` where known, at least 600 enabled, at least 250 CA and 350 US by `hq_country`.
2. `scripts/ats_detect.py <careers url>`: detects the ATS from URL patterns and known script tags (patterns reused from `chrome-extension/src/content/siteRegistry.ts`) and prints a registry entry.

### Task 10: Full runs, gates, docs, report, rollout

Controller-led. Reclaim on `scraper-v2-dev`, first full run, fixes, second run (idempotency), gates, README "Ingestion", rewrite of `docs/ingestion-pipeline.md`, `REPORT.md`, final review, then the production rollout if every gate passes.

### Task 11: Follow-ups: tags on every ingest path, backfill, myworkdaysite, ats-detect

Collected from the task reviews and reports. Model: sonnet. After Task 6 merges.

Files: `backend/services/aggregator.py`, `backend/routers/jobs.py` (`ingest_batch` only), `backend/scripts/backfill_v2_tags.py` (new), `frontend/src/setup/setupConfig.tsx` (the experience question's headline only), `backend/services/ats_scraper.py` (`_HARD_SENIOR_WORDS`, `workday_public_base` only), `backend/data/ats_companies.json` (the 23 held myworkdaysite boards only), `backend/tests/test_connector_fixtures.py` (the template test's host pattern only), `.gitignore`, `scripts/ats_detect.py` (new), tests.

1. GitHub-list rows (`aggregator.py`) and LinkedIn/Indeed `ingest_batch` rows also get `career_family`, `career_family_secondary` and `enrichment_version` from `taxonomy.classify_family`. Their `experience_level` logic stays exactly as it is (spec decision 6; the lists are student lists).
2. `backend/scripts/backfill_v2_tags.py`: dry run by default. With `--apply`, it tags rows whose `listing_status` is active, stale or off_target and whose `enrichment_version` is NULL: `career_family`, `career_family_secondary` and `enrichment_version`. ATS rows (`source_platform` 'ats') also get `experience_level` recomputed with `taxonomy.classify_experience`. It reads columns only (title, department-equivalent fields; never description in bulk), updates in batches of 1,000, and prints counts per family and per level.
3. The experience question's headline in `setupConfig.tsx` fits six levels, for example "What level of role are you looking for?"
4. Add "dean" to `_HARD_SENIOR_WORDS`, with a test ("Associate Dean" is not entry level).
5. `workday_public_base` builds `https://{host}/recruiting/{tenant}/{site}` for `wdN.myworkdaysite.com` CxS bases. Verify that pattern live on one posting first. Then move the 23 `held_workday_url_template` values back to `workday_url_template`, enabled, and let the template test accept that host.
6. `.gitignore`: `!backend/ingest/data/` and `!backend/ingest/data/**`, like the existing `backend/data` exception.
7. `scripts/ats_detect.py <careers url>`: detects the ATS from URL patterns and known script tags (patterns reused from `chrome-extension/src/content/siteRegistry.ts`) and prints a registry entry. Tests use offline HTML fixtures.

### Task 12: Liveness fixtures

Spec section: "Dead-job detection and job lifecycle" (Required fixtures). Model: sonnet. After Task 4's fix.

Files: `backend/tests/fixtures/ingest/liveness/` (new), `backend/tests/ingest/test_liveness_fixtures.py` (new).

For each Tier 1 and 2 source with a liveness check (greenhouse, lever, ashby, smartrecruiters, workday, workable, recruitee, bamboohr, oracle_hcm, icims, jibe, jobbank_ca; teamtailor, taleo and successfactors fall back to the generic page check), save 3 alive and 3 dead real responses. Take the URLs from the dev branch: `active` rows on that platform for alive, `removed` rows for dead. Re-check each one live today, and keep only those whose state is unambiguous. Record each HTTP exchange the check makes (status, headers that matter, body trimmed to what the check reads, redirects) as a fixture. The test replays them through `platform_liveness.check_listing` with an httpx MockTransport and asserts every verdict: 100%, no network. Where a source cannot yield 3 dead examples today, say so in the report with the reason. Never weaken a check to make a fixture pass.
