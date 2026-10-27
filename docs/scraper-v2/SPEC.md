# Agent Prompt: Job Scraper v2 (overnight build)

Oct 24, 2026 · @Wissam · completed Oct 25, 2026 against the live repo, Neon and Vercel

## Fill in before sending

Filled in and checked on 2026-10-25. Look these over, then paste this whole document as the first message to the agent. The counts in "What already exists" drift daily; the agent re-measures them in Phase 0.

- REPO: `Tailrd-Org/Tailrd` (public), default branch `main`. Local checkout `C:\Users\elmas\Desktop\Tailrd` on Windows 11 (PowerShell and Git Bash). `gh` is not installed: use the GitHub MCP (signed in as `WisHonor`) for pull requests.
- APP NAME: `Tailrd` (https://www.tailrd.ca), the Jobright-style job search app.
- AUDIENCE: every experience level, filtered by Jobright's six options: Intern/New Grad, Entry Level, Mid Level, Senior Level, Lead/Staff, Director/Executive. Today's catalogue is internship and new-grad only, and that audience must not lose jobs.
- NEON PROJECT: `tailrd` (`summer-hill-50886625`, org "Tailrd"), production branch `main` (`br-dry-hat-b7rsamst`). The other branch, `Development` (`br-gentle-darkness-b7yddqts`), serves the root `.env` and every Vercel preview: leave it alone.
- NEON PLAN: Free. Enforced cap of 512 MB logical size per branch (`main` was at 302 MB on 2026-10-25). 5 GB of data transfer and 100 CU-hours per project per month. 10 branches (2 used). Running out of transfer or compute suspends the database until the 1st of next month, which takes the whole site down. If you upgrade to Launch before sending, say so here; the storage budget below then follows the new limit.
- VERCEL PROJECT: `tailrd` (`prj_OkjGYKqNOlpUGPX5NA9YezI8yUGp`, team `tailrd-team`, `team_ZQx3HisOonkEeaeLYWnBMim5`), Hobby plan, Python 3.12 functions in `iad1`. Production domain www.tailrd.ca. Pushing to `main` deploys production.
- LLM FOR CLASSIFICATION: `none`, so use the rules-based classifier. The only key, `OPENAI_API_KEY`, belongs to a free-tier org whose rate limits live autofill shares, and ingestion has been model-free on purpose since the July cost incident.
- OPTIONAL API KEYS (none yet): to switch these adapters on the first night, register and add the values to the root `.env` before sending. Without them, those adapters are built against fixtures and ship disabled.
  - `USAJOBS_API_KEY` and `USAJOBS_USER_AGENT` (your email): https://developer.usajobs.gov/apirequest/
  - `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`: https://developer.adzuna.com/signup
  - `JOOBLE_API_KEY`: https://jooble.org/api/about
- PRODUCTION DEPLOY POLICY:
  - If every gate in "Testing and acceptance gates" passes, merge to main and deploy to production. Migrations must be additive.
  - Merging is the deploy. Vercel builds `main`, and the app runs its boot migrations against the production database on the first cold start. So the storage and transfer gates are gates too, and "Rollout to production" is part of the deploy.
- TIME BUDGET: 12 hours. Stop there, write the morning report, and leave the branch clean and runnable even if incomplete.

## What already exists (checked 2026-10-25)

Tailrd does not have one scraper that does everything. It runs a hardened, freshness-first pipeline, documented in `docs/ingestion-pipeline.md` (read all of it in Phase 0). What v2 changes is scope: today's crawl keeps only internship and new-grad roles, mostly in tech.

**Stack.** Python 3.12, FastAPI, SQLAlchemy 2 and psycopg2 on Vercel (`api/index.py` loads `backend/main.py`). React, Vite and TypeScript frontend. pytest and hypothesis (133 backend test files). The Chrome extension is separate and out of scope. There is no Alembic: schema changes are idempotent Python modules in `backend/migrations/`, run in the `lifespan` of `backend/main.py`, so they apply on the first cold start of every deploy, production included.

**Ingestion today**

| Piece | Where | Notes |
|---|---|---|
| Board connectors: Greenhouse, Lever, Ashby, SmartRecruiters, Workday (CxS) | `backend/services/ats_scraper.py` (`scrape_board()`, `BoardSnapshot`) | Public JSON APIs only. Partial snapshots confirm rows but never remove them. |
| Board registry | `backend/data/ats_companies.json`, `backend/data/company_registry.py` | 278 entries, 252 enabled (Greenhouse 129, Ashby 60, Workday 33, Lever 17, SmartRecruiters 13). `country` unset on 250. Mostly tech. |
| Curated GitHub lists (internship, new grad) | `aggregator.py`, `markdown_parser.py`, `routers/github_sources.py` (`cron-poll`) | 50 sources (11 active), 1,475 visible jobs |
| LinkedIn guest API, JobSpy (Indeed, LinkedIn) | `scripts/scrape_linkedin.py`, `scripts/scrape_jobspy.py`, then `POST /jobs/ingest-batch` | Entry-level search terms only. 1,769 visible jobs (31% of the feed) |
| Lifecycle, liveness, ghost scoring | `listing_freshness.py`, `platform_liveness.py` | Per-platform API checks, the 403 rule, click-time `POST /jobs/{id}/check-live` |
| Dedup | `cross_source_dedup.py`, `job_deduplication.py` | URL identity, `external_id`, exact twins, difflib >= 0.93 fuzzy match. Direct rows never merge with each other. |
| Extraction (salary, visa, skills, employment type) | `structured_extraction.py` | Regex and taxonomy, deliberately no model calls |
| Location | `location_parser.py`, `na_location.py`, `country_filter.py` | |
| Role taxonomy | `role_classifier.py` | 17 categories, shared with the Jobs filter and read by the fit engine as `family` |
| Board health | `source_health.py` (table `source_health`) | Breaker opens after 5 failures, 24 h cooldown |
| Metrics | `GET /jobs/ingest-metrics` | Logged by every workflow run |

**Schedule.** `.github/workflows/scrape-jobs.yml` is set for minutes 17 and 47 of every hour, but GitHub delivers far fewer runs: since 2026-08-27, the minute-17 entry alone has averaged about 6 a day. It calls `cron-ats`, `cron-poll`, `cron-backfill` and `cron-freshness` on www.tailrd.ca with the `x-cron-secret` header, each capped at 300 s. Its only secret is `CRON_SECRET`. By design the job holds no database credential, because it runs third-party PyPI code (`python-jobspy`). `vercel.json` defines no Vercel crons.

**The audience filter you are replacing.** The crawl keeps entry-level jobs only: `ats_scraper.py` (`_is_entry_level()` / `entry_tier()`, `HARD_SENIOR`, `STUDENT_ROLE_TITLE`), plus the `off_target` retirement in `ats_scraper.py`, `listing_freshness.py` and `cron-freshness` (switch: `CRON_ATS_RETIRE_OFF_TARGET`). `experience_level` holds only `internship` and `new_grad`, and the UI offers just those two. The places that set them: `EXPERIENCE_OPTIONS` and `normalizeExperienceLevels()` in `frontend/src/components/JobFilterBar.tsx`, `EXPERIENCE_FILTER_MAP` in `backend/services/job_filters.py`, and onboarding's `frontend/src/setup/steps/ExperienceStep.tsx` and `frontend/src/setup/answersToFilters.ts`.

**Data on `main`**

- `scraped_jobs` is 189 MB with 51,478 rows: 6,183 `active`, 1,181 `off_target`, 24,355 `removed`, 19,759 `expired`, 0 `stale`. Every `off_target` row is in CA or the US, so all of them were hidden for their level, and v2 puts them back in scope.
- The visible feed is 5,665 jobs: rows where `duplicate_of IS NULL AND listing_status IN ('active','stale') AND trim(company) <> ''`.
  - Country: CA 2,563, US 3,102.
  - Level: `internship` 2,502, `new_grad` 3,163.
  - Source: ATS boards 2,421, GitHub lists 1,475, LinkedIn 1,161, Indeed 608.
  - Category: Software Engineering 1,519, Other 1,380, Engineering and Development 1,068, Machine Learning and AI 415, Accounting and Finance 315. Legal and Compliance has only 23. There is no healthcare, government, education or trades category at all.
- Eight tables hold foreign keys to `scraped_jobs.id`: `application_records`, `user_saved_jobs`, `tailored_resumes`, `job_match_scores`, `match_traces`, `job_match_notifications`, `connection_requests` and `pending_questions`. Row ids never change.
- Storage:
  - Descriptions of `removed` and `expired` rows take about 78 MB compressed. 48 MB of that sits on 30,673 rows that have been dead for over 30 days and that no user table references.
  - Live rows' descriptions take about 19 MB.
  - Other large tables: `company_logos` 33 MB and `match_traces` 25 MB (growing about 1 MB a day).
- Extensions: `pg_trgm`, `vector`, `unaccent` and `fuzzystrmatch` are available. Only `plpgsql` is installed.

**What has already bitten this codebase (keep it true in v2)**

- A 401, 403, 429 or 999 is a bot wall, never evidence that a job is dead.
- Workday answers HTTP 200 with the same app shell for live, closed and made-up job ids, so judge Workday by its CxS JSON.
- A partial board snapshot never removes rows: missing from a partial crawl is not evidence of removal.
- The Python bundle is about 273 MB against Vercel's 250 MB standard size. PyMuPDF 2.0 alone took production down on 2026-10-24: every call returned 500. Add nothing heavy to `backend/requirements.txt` (no headless browser, pandas, numpy or ML libraries). Scraper-only dependencies go in a separate requirements file that only the Actions runner installs.
- `pytest` enters the app lifespan, so it runs every boot migration against whatever `DATABASE_URL` points at.
- Several agent sessions share the main checkout (`C:\Users\elmas\Desktop\Tailrd`), including its HEAD and its `./test.db`. Never switch branches, stash or reset there, and never run two pytest processes in one checkout.
- GitHub turns off scheduled workflows in a public repository after 60 days without a commit, without telling anyone. `scrape-jobs.yml` has a detector, and any new scheduled workflow falls under the same rule.
- The repository is public, so Actions logs and everything committed are public.
- The owner bans the em dash character everywhere: code, comments, copy, docs and commit messages. Use a colon, comma or parentheses instead.

## Mission and decision authority

You are building the job-ingestion pipeline for Tailrd, a Jobright-style job search app. Tailrd already keeps a live catalogue of about 5,700 internship and new-grad jobs, mostly in tech, and its pipeline is good at freshness. Turn it into a production-grade ingestion system that pulls from all major job sources, covers Canada and the USA, covers every career family (not just tech) and every experience level, detects and retires dead postings, and runs on a schedule without supervision.

You have full authority over the scraper. Modify it, gut it, or delete it and start over, whichever produces the better system. Keep nothing out of sentiment; reuse only what survives the Phase 0 review. The source facts in the previous section are not sentiment: whatever you rewrite must keep them true. The rest of the app (auth, unrelated API routes, styling) is out of scope, except where it must read the new job data:

- the Jobs page and onboarding filters (experience levels, career families);
- the job list and detail pages;
- which jobs reach the fit engine and match alerts.

Work autonomously until done or the time budget runs out. Do not stop to ask questions: the owner is asleep. Make the sensible call, record it in `docs/scraper-v2/DECISIONS.md` (one rule and its reason each, easy to reverse), and keep going. The only hard stops are the guardrails below.

Definition of done:

- The scheduled entrypoint, run exactly as its workflow runs it, ingests jobs from every Tier 1 and Tier 2 source into `scraper-v2-dev`, covering both countries, all career families and all experience levels.
- The app shows only live jobs, and dead jobs disappear within 72 hours.
- The Jobs page and onboarding offer Jobright's six experience levels, each backed by live jobs.
- Storage and transfer stay inside the Neon budget.
- All acceptance gates pass.
- The morning report is written.

## Decisions already made (do not reopen)

The owner made these, or they were made for the owner on 2026-10-25. If you have to bend one, record why in DECISIONS.md.

1. Every experience level is in scope, shown as Jobright's six options (see "Experience levels"). The internship and new-grad feed must not shrink except by dead jobs removed.
2. Classification is rules-based. No model calls anywhere in ingestion.
3. `scraped_jobs` is extended in place with additive columns. No parallel `jobs` table and no copy of the table: eight tables reference its ids, and a copy needs about twice its size under a 512 MB cap.
4. One sanitized text description per job (`description` plus the existing `description_sections`). No `description_html` column: it would double the largest cost in a capped database.
5. No embeddings for dedup. Use the existing deterministic matcher plus `pg_trgm`.
6. The existing LinkedIn guest and JobSpy steps stay exactly as they are, with the same terms and cadence. Don't extend them to more levels and don't turn them off. They carry 31% of today's feed, and their future is the owner's licensing call. Report their yield.
7. The curated GitHub lists (`cron-poll`) stay. They are the best internship and new-grad source there is.
8. No Vercel crons. On Hobby they run at most once a day, and a more frequent expression fails the deploy, so GitHub Actions stays the clock.
9. `role_category` keeps its 17 values, because the fit engine reads it. The 20 career families go in a new column, and the Jobs category filter moves to them, with old saved values mapped.

## Non-negotiable guardrails

**Git / GitHub**

- Work on branch `feat/scraper-v2` in a worktree: `git fetch origin`, then `git worktree add .worktrees/scraper-v2 -b feat/scraper-v2 origin/main`. Do everything there. Never check out, stash, reset or commit in the main checkout, because other sessions use it.
- Commit small and often with descriptive messages, staging by explicit path. Push the branch and open a draft PR early through the GitHub MCP, and keep its description current.
- Never force-push main. Never rewrite history on shared branches.
- Never commit secrets, `.env` files, or API keys. New keys go in a new `.env.example` (none exists yet) with a comment, and in the morning report.

**Neon**

- Create a Neon branch `scraper-v2-dev` from `main` and do ALL development and test runs there.
  - Its pooled connection string goes only in the worktree's own `.env`: copy the root `.env` and change `DATABASE_URL`.
  - Never edit the root `.env`.
  - Never run DDL on the production branch unless the production deploy policy applies and every gate passes.
- Migrations are additive only: new tables, new nullable or constant-default columns, new indexes, `CREATE EXTENSION`. Never drop or rename an existing table or column in this PR. Deprecate instead and note it in DECISIONS.md.
- Never run anything that rewrites `scraped_jobs`: no `ALTER COLUMN ... TYPE`, no `ADD COLUMN` with a volatile default or a stored generated column, no `VACUUM FULL`, no `CLUSTER`, no copy into a new table. A rewrite needs about twice the table's size. At the 512 MB cap every insert, update and delete in the app fails, including sign-ups and saved jobs.
- Preserve existing job rows: bring them into the new schema in place, in batches. Never truncate and never delete a row.
- Use the repo's migration pattern: an idempotent module in `backend/migrations/`, registered in the lifespan in `backend/main.py`. Data backfills belong in scripts, not boot migrations, because a boot migration must finish in seconds on a cold start.
- Reads cost transfer, which is capped at 5 GB a month. Aggregate in SQL, and never pull `description` or `description_sections` to your machine in bulk.

**Vercel**

- Preview deployments only, unless the production deploy policy applies. Preview URLs (`*.vercel.app`) sit behind Vercel Authentication: open them with the Vercel MCP (`get_access_to_vercel_url`, `web_fetch_vercel_url`).
- Do not change production env vars. A Preview `DATABASE_URL` scoped to the git branch `feat/scraper-v2` and pointing at `scraper-v2-dev` is fine. Never change the unscoped Preview `DATABASE_URL`, which every other preview uses.
- Hobby limits checked on 2026-10-25 (recheck with the MCP):
  - functions stop at 300 s;
  - crons run at most daily, and can fire up to an hour late;
  - monthly included usage is 4 Active CPU hours, 360 GB-hours of memory and 1M invocations;
  - going over can switch a feature off for up to 30 days.

  Heavy CPU work does not belong in a Vercel function.

**Scraping conduct** (protects the app from bans and legal trouble)

- Prefer official or documented public JSON endpoints over HTML scraping, and HTML scraping over headless browsers. Use a headless browser only where nothing else works, and isolate it, never in the Vercel bundle.
- Identify with a stable User-Agent containing the app name and a contact URL: `TailrdBot/2.0 (+https://www.tailrd.ca)`. Respect robots.txt. Per-host rate limits (default 1 req/s, lower where a site pushes back). Exponential backoff on 429 and 5xx. Cache responses within a run.
- Never log into any site, use anyone's account, bypass CAPTCHAs, bot checks or paywalls, or use residential-proxy tricks. Registering for an API key needs an account, so you can't do that either. If a source blocks us, disable the adapter, record why, and move on.
- Spend no more than 45 minutes on any single source. Timebox, then move on.
- Store only job-posting data. No personal data of applicants or recruiters beyond a public contact email on a posting.

**Scope**

- Do not refactor unrelated code, change auth, billing, or UI styling, or swap frameworks or ORMs.
- Do not change how the fit engine scores (`backend/services/fit_engine.py`, `match-engine/`). v2 may change which jobs reach it, never how it scores them.
- If something out of scope blocks you, fix the minimum and note it in DECISIONS.md.

## Phase 0: recon and the refactor-vs-rewrite decision (first 45 min)

Read before writing any code:

1. The repo:
   - `docs/ingestion-pipeline.md` in full, and the modules in "What already exists";
   - `scrape-jobs.yml` and its test `backend/tests/test_scrape_jobs_workflow.py`, `ci.yml`, `vercel.json`;
   - `backend/main.py` (boot migrations) and `backend/db/models.py` (`ScrapedJob`);
   - the ingestion tests (`test_connector_fixtures.py`, `test_freshness_lifecycle.py`, `test_platform_liveness.py`, `test_cross_source_dedup.py`, `test_entry_level_classifier.py`, `test_location_parser.py`, `test_na_location.py` and their neighbours) and `backend/tests/fixtures/`;
   - every query against `scraped_jobs` (`routers/jobs.py`, `jobs_sidebar.py`, `job_filters.py`, `fit_engine.py`, `match_notifier.py`);
   - the frontend filter files above.
2. The test baseline: before changing anything, run `python -m pytest backend/tests -q` once in the worktree.
   - Set `DATABASE_URL=sqlite:///./ci_test.db` and any `JWT_SECRET` in the environment, as CI does, so the run never touches Neon.
   - Save the failure list in PLAN.md. A handful of tests already fail on clean main, and v2 must not add to them.
3. Neon (MCP), read-only on `main`:
   - the real production schema, row counts, how many jobs are live, the age of the oldest row, existing indexes;
   - `describe_project` (transfer and compute used this period) and each branch's `logical_size`.
4. Vercel (MCP): plan, crons (none), function limits, this month's usage, env var names (never values).

Write `docs/scraper-v2/PLAN.md`: what exists, what is reusable, the architecture you will build (it should match the next section unless you document a reason), the storage plan, and the decision below.

Decision rule: rewrite the scraper if any two of these are true; otherwise refactor it into the adapter structure. Judge the code as it is, not the description in the Mission.

- It parses HTML for sources that expose a JSON endpoint.
- Source logic, normalization, and DB writes are tangled in the same functions.
- It has no tests and no fixtures.
- It cannot run partially (one failing source kills the run).
- Adding a source means copying more than about 50 lines.

Either way, the end state is the architecture in the next section.

## Target architecture

One ingestion pipeline, split by source, never by role. Career family, experience level, and location are tags computed after ingestion. Layout (Python, e.g. a new `backend/ingest/` package; adapt names to repo conventions):

```
backend/ingest/
  core/
    http.py          # httpx wrapper: UA, per-host rate limit, retries, backoff, timeout, run cache
    schema.py        # RawJob + NormalizedJob (pydantic, already a dependency)
    normalize.py     # raw -> normalized: title cleanup, location parsing, remote detection, salary parsing, html -> text
    dedupe.py        # fingerprinting + near-duplicate detection
    enrich.py        # rules: career family, experience level, employment type, skills
    liveness.py      # per-source alive/dead checks (builds on platform_liveness.py), state transitions
    queue.py         # DB-backed work queue (scrape_tasks), claim with SKIP LOCKED, leases
    run.py           # orchestrator: plan -> enqueue -> workers -> report
  adapters/
    greenhouse.py  lever.py  ashby.py  smartrecruiters.py  workday.py        (ported)
    workable.py  recruitee.py  teamtailor.py  bamboohr.py  jobvite.py  icims.py
    oracle_hcm.py  taleo.py  successfactors.py  usajobs.py  jobbank_ca.py  adzuna.py  jooble.py  ...
  data/
    taxonomy.json  closed_phrases.json  limits.json  ...
backend/tests/ingest/
  fixtures/          # saved API responses + alive/dead pages
```

Seed companies stay in `backend/data/ats_companies.json`.

Adapter interface. Every adapter implements exactly this:

- `list_jobs(source) -> AsyncIterator[RawJob]`: enumerate current postings for one company or board.
- `fetch_job(ref) -> RawJob | None`: full detail for one posting when the listing lacks it.
- `check_alive(job) -> AliveResult`, returning `alive`, `reason` (`listed` | `404` | `redirect_home` | `closed_text` | `unknown`) and `confidence`.
- Metadata: `rate_limit`, `tier`, `countries`.

Rules:

- Adapters never touch the database. They yield RawJob; the core writes.
- One adapter failing is a `source_health` row, never a crashed run.
- Every stage is idempotent: re-running a task produces the same rows.
- All configuration (seed companies, rate limits, taxonomy, closed-text phrases, storage caps) lives in data files, not code. Use JSON; PyYAML is not a dependency.

Data flow per run: plan tasks, fetch via adapters, normalize, dedupe, upsert jobs (`first_seen_at` / `last_seen_at`), enqueue enrichment for new content hashes, enrich, mark not-seen jobs for liveness, run liveness, apply state transitions, write the run summary.

## Sources to cover

**Tier 1: ATS platforms with public JSON job boards.** Implement all, with live integration tests against at least 3 real companies each. Fast, stable, structured; they should produce most of the volume.

- Already built, to port: Greenhouse, Lever, Ashby, SmartRecruiters.
- New: Workable, Recruitee, Teamtailor, BambooHR (careers list endpoint), Jobvite if a public feed exists.
- The autofill work has already found public GET endpoints for several of these:
  - Workable: `https://apply.workable.com/api/v1/widget/accounts/<account>`;
  - forms for Recruitee, BambooHR, JazzHR, Breezy, Pinpoint, Paylocity and Comeet (NOTES.md, round 8).

  URLs in `scraped_jobs` show which employers use which platform.
- Verify each endpoint's current shape with a live request before coding against memory.

**Tier 2: enterprise ATS and public-sector feeds.** This is where law, healthcare, finance, government, education, and trades jobs live; without it coverage is tech-only.

- Workday: per-tenant CxS JSON. The connector exists and 33 tenants are enabled.
- iCIMS: public job search pages.
- Oracle Cloud Recruiting: the public `hcmRestApi` requisition finder, which `platform_liveness.py` already queries. JPMorgan Chase (`jpmc.fa.oraclecloud.com`, site `CX_1001`) is one of the registry entries waiting for this connector.
- Oracle Taleo and SAP SuccessFactors: attempt them, and disable them if blocked within the 45-minute cap. Registry entries for both exist, disabled, with the reason.
- USAJOBS official API: needs `USAJOBS_API_KEY`. If the key is missing, build against fixtures and leave the adapter disabled.
- Job Bank Canada: check for an official feed or open-data export first; otherwise respectful HTML scraping of public search results. Check GC Jobs and the provincial public services for official feeds too.
- Provincial and state health authorities, large hospitals, and universities: add at least 10 as Workday, iCIMS or Oracle tenants in the seed list.
- Plan description fetching for every level. Today's budget (`WORKDAY_DETAIL_BUDGET`, 40 per run) was sized for entry-level volume, and BMO alone lists about 1,250 postings.

**Tier 3: aggregator APIs for breadth.** Adzuna (CA and US), Jooble, and any other aggregator with a documented API.

- You can't register for keys. Build each adapter against its documented response shape with recorded fixtures, and leave it disabled until its key exists.
- Put the key names in `.env.example` and the report.
- Record the aggregator as `source` and the employer's URL as `apply_url` when provided.

**Tier 4: LinkedIn, Indeed, Glassdoor.** Best effort only. Public, login-free listing pages only, rate limit of at most 1 request per 3 seconds, robots.txt respected, isolated adapter, disabled by default. If they block or require an account, leave the adapter disabled and say so in the report. Licensed data is the sustainable route and that decision is mine. The existing LinkedIn guest and JobSpy steps are grandfathered as they are (decision 6). Add no Tier 4 volume beyond them.

**Curated lists.** The GitHub internship and new-grad lists stay a source (decision 7).

**Company discovery** (so Tier 1 and 2 have real breadth)

- Grow `backend/data/ats_companies.json` from 252 enabled entries, mostly tech, to at least 600: at least 250 Canadian and 350 US, spread across the career families below, not only startups.
  - Fill in `country` on every entry, and add `sector` and `careers_url`.
  - Draw from public GitHub lists of Greenhouse / Lever / Ashby boards, Fortune 500 and largest-Canadian-employer lists, major hospitals, law firms (AmLaw 200, Canadian national firms), banks, universities, federal and provincial employers, utilities, construction and engineering firms.
- Write an `ats-detect` script: given a careers URL, detect the ATS (URL patterns plus known script tags) and emit a seed entry. Reuse the URL patterns in `chrome-extension/src/content/siteRegistry.ts`, where the extension already detects most ATS platforms by URL. Use it to fill gaps.
- Mirror seeds into a `companies` table so they can be managed without a deploy.

## Coverage targets: locations, careers and levels

Countries: Canada and the United States only. Jobs elsewhere are dropped at normalization unless `remote_scope` includes CA or US. Being outside those countries is now the only reason a job is `off_target`.

**Location normalization** (extend `location_parser.py` and `na_location.py`)

- Parse into `country` (CA / US), `region` (2-letter province or state), `city`, `remote_type` (onsite / hybrid / remote, stored in `work_type`), and `remote_scope` (which countries a remote role accepts).
- Handle the messy forms: "Toronto, ON", "Greater Toronto Area", "Remote - Canada", "US Remote", "New York, NY / Remote", "Multiple locations", French place names from Quebec employers.
- Multi-location postings become one job row with every location in `locations_json`, never one row per city.
- Every major metro must appear in a full run. CA: Toronto, Montreal, Vancouver, Ottawa-Gatineau, Calgary, Edmonton, Winnipeg, Halifax, Quebec City. US: at least the 25 largest metros.

**Career families** (fixed taxonomy stored as `career_family`; one primary, optional secondary)

1. Software Engineering
2. Data, AI & Analytics
3. IT, Security & Infrastructure
4. Product & Design
5. Engineering (civil, mechanical, electrical, chemical, industrial)
6. Legal (lawyers, paralegals, compliance, contracts)
7. Healthcare, Clinical (physicians, nurses, allied health, pharmacy)
8. Healthcare, Administration & Support
9. Finance & Accounting
10. Banking, Insurance & Investment
11. Sales & Business Development
12. Marketing & Communications
13. Operations, Supply Chain & Logistics
14. Human Resources & Recruiting
15. Education & Research
16. Government & Public Sector
17. Skilled Trades, Construction & Manufacturing
18. Customer Support & Administrative
19. Hospitality, Retail & Food Service
20. Other

`role_category` stays filled with its 17 values from the same rules, because the fit engine reads it. Keep the mapping between the two taxonomies in a data file.

**Experience levels** (Jobright's six, replacing today's two options)

| Filter option (exact label) | Stored `experience_level` values |
|---|---|
| Intern/New Grad | `internship`, `new_grad` |
| Entry Level | `entry` |
| Mid Level | `mid` |
| Senior Level | `senior` |
| Lead/Staff | `lead` |
| Director/Executive | `director`, `executive` |

- `experience_level` plays the role of the draft's `seniority`. Extend its vocabulary rather than adding a second column.
- Classify from the title, the source's own level fields and required years (`experience_years_required`). Record the boundary rules in `taxonomy.json` and DECISIONS.md: where "Manager", "Principal", "Associate" and roman-numeral levels land.
- Change these together, with their tests: `EXPERIENCE_OPTIONS`, `normalizeExperienceLevels()`, `EXPERIENCE_FILTER_MAP`, and onboarding's `ExperienceStep.tsx` and `answersToFilters.ts`.
- A filter saved under the old taxonomy (`internship`, `new_grad`, `intern_new_grad`, `entry`) must still resolve. The legacy `entry` key means internship plus new grad today, so giving it its new meaning is a deliberate change: note it.
- A user who never chose levels sees Intern/New Grad and Entry Level by default, which is what Tailrd shows today, and can widen the filter.
- Drop "not entry level" as a reason for `off_target`: remove it from `_is_entry_level`, `HARD_SENIOR` and the senior-title sweep. Senior rows are now tagged, not hidden. Let the next crawl revive the 1,181 `off_target` rows that are still listed.
- Match alerts and the fit engine's candidates follow each user's chosen levels (`user_settings.experience_levels`, which onboarding writes and nothing reads yet). With no saved choice, they get Intern/New Grad and Entry Level. Never alert a student about a Director role.

**Volume targets for the first full run** (report shortfalls honestly; never pad)

- At least 15,000 live jobs total: at least 4,000 in Canada and 8,000 in the US.
- Every family 1 to 19 with at least 200 jobs. Legal, Healthcare Clinical, Engineering, Finance & Accounting, and Skilled Trades each at least 400.
- Every experience level with at least 300 live jobs.
- Intern/New Grad at least 5,000 (5,665 visible on 2026-10-25). The student feed must not shrink except by dead jobs removed.
- If a family is short, the fix is more Tier 2 tenants in the seed list, never looser classification.
- Volume never outranks the storage budget: more jobs count only if they fit.

## Data model (Neon Postgres)

Extend `scraped_jobs` in place (decision 3) and add the new tables. The draft's `jobs` fields map onto what exists:

| Draft field | In `scraped_jobs` |
|---|---|
| id | `id`, unchanged: eight tables reference it |
| company_id | new, nullable, references `companies.id` |
| source, source_job_id | `source_platform` (with `board_key`, `source_trust`); `external_id` (`{platform}:{slug}:{id}`) |
| fingerprint | new; see Dedupe |
| content_hash | `raw_hash` |
| duplicate_of | `duplicate_of` |
| title, title_normalized | `title`, `title_norm` |
| description_text, description_html | `description` and `description_sections`; no HTML column (decision 4) |
| apply_url, canonical_url | `url` (unique, already canonical via `canonical_url()`); add a nullable `apply_url` only where an aggregator gives the employer's link |
| career_family, career_family_secondary | new |
| seniority | `experience_level`, vocabulary extended |
| employment_type | `employment_type` |
| remote_type, remote_scope[] | `work_type`; `remote_scope` new |
| skills[] | `skills` (type `json`; converting it to `jsonb` would rewrite the table, so a GIN index needs a new column or is skipped) |
| enrichment_version | new |
| locations, country[], region[], city[] | `locations_json`, `country`, `region`, `city`, `location_search` |
| salary_min, salary_max, salary_currency, salary_period | exist |
| salary_is_estimated | new |
| status (active / stale / closed) | `listing_status` (`active`, `stale`, `removed`, `expired`, `off_target`). Never `status`: that column is the user's application-workflow enum. |
| closed_reason, closed_at | `closed_reason` new; `listing_status_changed_at` |
| first_seen_at, last_seen_at, last_checked_at | `first_seen_at`, `last_seen_at`, `last_probed_at` |
| consecutive_failures, posted_at, expires_at | `consecutive_failures` new; `posted_date`; `expires_at` new |

Indexes to add: (listing_status, career_family), (listing_status, experience_level), (listing_status, country, region), (company_id), (listing_status, last_probed_at) for the liveness worker, and the fingerprint index per Dedupe. These already exist: `listing_status`, `last_seen_at`, `last_probed_at`, `board_key`, `external_id`, `title_norm`, and a unique index on `url`.

New tables (snake_case):

- **companies**: id, name, domain, country, sector, ats, ats_token (board token, tenant, or site), careers_url, enabled, created_at.
- **scrape_runs**: id, started_at, finished_at, trigger (cron / manual), tasks_total, tasks_ok, tasks_failed, jobs_seen, jobs_new, jobs_closed, notes.
- **scrape_tasks** (the queue): id, run_id, kind (list / fetch / enrich / liveness), payload jsonb, status, attempts, lease_until, last_error, created_at. Workers claim with `SELECT ... FOR UPDATE SKIP LOCKED`. Delete finished tasks after 7 days.

**source_health** already exists, with columns board_key, platform, slug, consecutive_failures, total_failures, last_error, last_success_at, last_failure_at, last_job_count and updated_at. Add what is missing: `disabled_until` and a 7-day yield average. Auto-disable after 5 consecutive failures and retry after 24 h, as today.

**Dedupe**

- Fingerprint = sha256 of (company domain | normalized title | sorted normalized locations | employment_type). Use it as a match key for cross-source twins, not a uniqueness constraint. One employer often posts the same title in one city as separate requisitions (three "Registered Nurse - ICU" openings, five "Software Engineer" seats). That is why direct rows never merge with each other today. Uniqueness per posting stays on `url` and `external_id`.
- Cross-source duplicates (an aggregator re-listing an employer posting): keep the employer-source row and point the aggregator row at it via `duplicate_of`.
  - Build on `cross_source_dedup.py`, which matches exact twins and then difflib >= 0.93 on normalized titles.
  - Where it helps, add `pg_trgm` similarity on the title plus the first 500 characters of the description.
  - No embeddings (decision 5).

## Dead-job detection and job lifecycle

Status machine: `active -> stale -> closed`, plus `closed -> active` if the same posting reappears in a listing (same `external_id` or `url`, or the same fingerprint from the same source). In `listing_status`, closed is `removed` (a hard signal or two misses) or `expired` (age).

- active: seen in the latest listing, or passed its last liveness check.
- stale: missed one listing or failed one check. Hidden from search and feeds immediately; never deleted. Today `stale` rows are still shown, though there are none right now. Change the feed rule in one place and measure the visible count before and after.
- closed: missed two consecutive checks at least 24 h apart, or a hard signal (404 / 410, explicit closed text). Keep the row: it stops a slow aggregator from re-ingesting the posting as new, and it feeds analytics.
- Hard expiry: any job not seen for 45 days is closed with reason `ttl`, no check needed. Where today's sweeps are stricter (aggregator rows expire after 21 or 30 days), keep the stricter rule.

**Liveness checks**, cheapest first; stop at the first decisive signal:

1. Listing presence (Tier 1 and 2): the board was re-listed and this job ID is absent. No per-job request needed.
2. HTTP: 404 / 410 means closed (`404`). A 301 / 302 landing on the careers home, a generic search page, or a different job means closed (`redirect_home`). Timeouts and 5xx are inconclusive: retry later, never change status on them. A 401, 403, 429 or 999 is a bot wall, never a death. The one exception is Workday's `S22` errorCode; see the pipeline doc.
3. Content: a 200 still needs inspection. Keep a per-source phrase list in a data file ("no longer accepting applications", "this position has been filled", "job not found", "this job is no longer available", "posting has expired") plus structural checks (apply button gone, title now generic). Workday and other client-rendered pages return 200 shells: for those, check the JSON endpoint, never the HTML.
4. JobPosting JSON-LD: `validThrough` in the past means closed.

`platform_liveness.py` already does checks 2 to 4 for Workday, SmartRecruiters, Greenhouse, Lever, Ashby, Oracle HCM and LinkedIn. Extend it rather than starting over.

**Cadence**

- Re-list every enabled Tier 1 and 2 board daily.
- Per-URL checks for aggregator and Tier 4 jobs every 48 h; jobs older than 21 days every 24 h.
- The job detail page already triggers an on-demand check (`POST /jobs/{id}/check-live`, once per job per browser session). Keep it working with the new states.

**Required fixtures**: at least 3 alive and 3 dead saved pages per Tier 1 and 2 source. The liveness classifier must score 100% on them.

## Storage and transfer budget (Neon free plan)

- Gate: `main` and `scraper-v2-dev` each stay at or below 450 MB logical size (`list_branches`, `logical_size`), and REPORT.md projects the 30-day steady state. The cap is 512 MB and enforced, and at the cap every write in the app fails.
- Reclaim before you grow:
  - Strip `description`, `description_sections` and `change_log` from rows that have been `removed` or `expired` for over 30 days and that no user table references (`application_records`, `user_saved_jobs`, `tailored_resumes`, `job_match_notifications`, `connection_requests`, `pending_questions`). That frees about 48 MB now, and more as rows age.
  - Work in batches of a few thousand rows, then run a plain `VACUUM`, never `VACUUM FULL`.
  - Keep the rows themselves (id, url, external_id, title, company, fingerprint, status, timestamps): they are what stops re-ingestion.
- Keep each row small:
  - Store sanitized text only.
  - Cap description length at a value that keeps 99% of real postings whole, and record it.
  - Keep no raw payloads in the database: fixtures go in the repo and run caches stay in memory.
- Cap how many postings one employer can occupy, because a big retailer or hospital network can list thousands. Also skip postings that were already old when first seen, where the source gives a date. Put both caps in a data file, so the owner can raise them after upgrading the plan, and report what they cut.
- Transfer: 5 GB per project per month, and the app already uses about 4 GB of it. Measure `data_transfer_bytes` (`describe_project`) at the start, after each full run and at the end. The build itself may spend at most 500 MB. In steady state, everything together must project under 4.5 GB a month (show the arithmetic). Writes are free; reads count, whoever makes them.
- Compute: 100 CU-hours per project per month, and `scraper-v2-dev`'s endpoint counts too. Let it suspend when idle.
- If the targets can't fit, stop growing at the budget. Say in the report what the fix costs: Neon Launch bills about $0.35 per GB-month of storage plus compute, roughly $5 a month at today's usage.

## Scheduling and running within platform limits

The platform facts are in the guardrails: Vercel functions stop at 300 s and Hobby crons are daily at best. GitHub Actions, today's clock, allows 6 h per job, and runner minutes are free in a public repository.

- Preferred: heavy ingestion (listing, detail fetches, normalization, enrichment, upserts) runs as a GitHub Actions scheduled workflow, writing to Neon. For example `.github/workflows/ingest.yml`: cron every 6 h at an off-peak minute (never :00), plus `workflow_dispatch`. The repository is public and the existing scrape job deliberately holds no database credential, so this workflow must follow these rules:
  - It is a separate workflow. Its job installs only first-party code and pinned, hashed dependencies (never `python-jobspy`), has `permissions: contents: read`, and pins actions by commit SHA.
  - It connects as a dedicated least-privilege Neon role: `SELECT`, `INSERT` and `UPDATE` on the ingestion tables, `DELETE` only on `scrape_tasks`, and no DDL. Boot migrations stay in the app. The role's connection string is an Actions secret.
  - You may create that role on `scraper-v2-dev` to test it. For production, do not create the role or the secret yourself. Put the exact SQL and the secret's name in the report.
  - While the secret is missing, the workflow exits 0 with a notice, so it never runs red.
- Production keeps ingesting after the merge. Until the owner adds that secret, the existing endpoint path (`cron-ats` and the other routes called by `scrape-jobs.yml`) is what feeds production, so it must run the v2 logic inside its 300 s boxes.
- Vercel keeps only light work: the click-time check, small liveness batches and metrics, all called with the existing `x-cron-secret` header. If you run ingestion on Vercel instead, use the same queue model: each invocation claims a batch, works until about 240 s (80% of 300), exits, and Actions calls it again. Prove it fits, including Active CPU against the 4 hours a month.
- Cut-over: the old path and the new runner must never crawl the same board in the same cycle. Switch per board or with one flag. When you change `scrape-jobs.yml`, update `test_scrape_jobs_workflow.py` in the same commit.

**Concurrency and safety**

- A Postgres advisory lock prevents two full runs from overlapping.
- Per-host concurrency 2, global 20, tunable per adapter via `rate_limit`.
- Every task has a lease; expired leases are reclaimed. Max 3 attempts, then `failed` with the error stored.
- A full run over the 600-company seed list must finish in under 3 h. Measure it and record the number.

**Observability**

- One `scrape_runs` row per run with counts.
- A `scrape-report` script (e.g. `python -m backend.ingest.report`) prints the last run's summary, per-source yield, and jobs per family, level and country. Add the same numbers to `GET /jobs/ingest-metrics` so the workflow log keeps showing them.
- Yield alert: if a source's job count drops more than 60% against its 7-day average, write a warning to the run notes and `source_health`. If `ALERT_WEBHOOK_URL` (new, optional) is set, post there too.
- Structured JSON logs, one line per task, never one per job. Never log a connection string or a key: the logs are public.

## Testing and acceptance gates

All of these must pass before the morning report says "done". The production deploy policy also requires all of them before any production deploy.

**Unit tests (no network; pytest, under `backend/tests/`)**

- normalize: at least 40 location strings covering CA, US, remote, multi-location and Quebec French place names; salary parsing (ranges, hourly vs annual, CAD vs USD); HTML to text.
- dedupe: identical, near-identical, cross-source duplicates, and true non-duplicates, including same-title separate requisitions at one employer.
- liveness: the alive/dead fixture set at 100%.
- enrich: a hand-labeled sample of at least 200 real fetched jobs, at least 5 per career family and 15 per experience level, stored in `backend/tests/fixtures/ingest/labels.json`.
  - Label each job before the classifier sees it, and commit the labels first. Log every label you change afterwards, with the reason.
  - Gates: 90% or better on primary career family, 85% or better on experience level.
- The existing suite: no failure that isn't on the Phase 0 baseline list.
- CI runs on SQLite. Postgres-only code (`SKIP LOCKED`, advisory locks, `pg_trgm`) either degrades on SQLite or is tested in a CI job with a `postgres:17` service container.

**Integration tests (network, tagged, run nightly in CI)**

- Each Tier 1 adapter lists at least 1 job for 3 real companies.
- Workday, Oracle Cloud Recruiting, iCIMS, Job Bank, and every adapter whose key exists (USAJOBS, the aggregators) return at least 1 job.
- They're deselected in the per-PR run (a pytest marker); a nightly workflow runs them.

**End-to-end on the Neon dev branch**

- Full run completes with 0 unhandled exceptions and a task failure rate under 5%.
- Volume and coverage targets met, or each shortfall documented with its cause.
- Duplicate rate in a random 500-job sample under 2%.
- A second run immediately after the first creates 0 new rows for unchanged jobs (idempotency).
- Closure simulation: remove 50 jobs from a fixture board, run liveness twice, all 50 reach `stale` then `closed`.
- The app's job list and detail pages render against the new schema on the preview, and show only live jobs. Existing API routes still return 200, and the four cron routes still return 2xx (locally or on the preview).
- Each of the six experience options returns jobs on the Jobs page, and a filter saved under the old taxonomy still resolves.
- A test user whose level is Intern/New Grad gets no Senior, Lead or Director job in a match alert.
- Storage and transfer stay inside the budget, with the numbers in the report.

**Code quality**

- Frontend: `npx tsc --noEmit` is clean in `frontend/`, and the frontend tests you touched pass (`npx vitest run`; vitest isn't in CI).
- Python: no linter is configured, so run `ruff check` on the new package only. It's a dev tool and never goes in `backend/requirements.txt`.
- CI runs unit tests on every PR.
- `match-engine/`: `make test` stays green, and `make eval-fast` is no worse than before. It is red today by owner decision D-154.
- `README.md` gains an "Ingestion" section: run locally, add a source, add a company, read the run report. Rewrite `docs/ingestion-pipeline.md` to describe v2, since it's the document the next person reads.

## Rollout to production (only under the production deploy policy)

1. All gates are green on `scraper-v2-dev` and the preview. Measure `main`'s size and this month's transfer.
2. Make a backup branch `pre-scraper-v2` from `main`, unless it would count against a limit; otherwise rely on the 6-hour restore window. Then run the reclaim on `main` in batches, measuring size as you go.
3. Merge the PR through the GitHub MCP.
   - Confirm the Vercel production deployment is READY and `get_runtime_errors` is clean.
   - Confirm the boot migrations landed: check `main`'s schema with the Neon MCP.
4. Run the first production ingestion in a bounded mode, with a cap on new rows per run. Check size and transfer after each run until the catalogue is steady.
5. If production errors after the deploy, roll back to the previous production deployment with the Vercel MCP, then open a revert PR. The additive migrations stay, and the old code ignores them.

## Execution order (timeboxes within the 12-hour budget)

1. Phase 0 recon, test baseline, PLAN.md, DECISIONS.md. (45 min)
2. Worktree, Neon dev branch, additive migrations, the data-model mapping, the storage plan, and the reclaim on the dev branch. (30 min)
3. Core: http client, schema, queue, orchestrator skeleton, run report. Prove it end to end with Greenhouse alone before writing any other adapter. (1 h 15 min)
4. Port the other existing connectors, then the new Tier 1 adapters, fixtures, integration tests. (1 h)
5. Normalizer, location parser, dedupe, unit tests. (45 min)
6. Enrichment (rules): career families and experience levels, labeled sample, accuracy test. (1 h)
7. Seed list to 600 companies plus the ats-detect script. First full ingestion; fix what breaks; check storage and transfer. (1 h)
8. Tier 2: Workday, Oracle, iCIMS, USAJOBS, Job Bank, then the Taleo and SuccessFactors attempts. Add non-tech tenants to the seeds, re-run, check family and level coverage. (1 h 30 min)
9. Liveness worker, state machine, closure-simulation test. (45 min)
10. Tier 3 aggregators. Tier 4 only if time remains and only as specified. (30 min)
11. Scheduling: Actions workflow, cut-over switch, advisory lock, alerts. (45 min)
12. App integration: the six experience levels, the career-family filter, feed visibility, alerts and the fit engine's candidates. (1 h)
13. Final full run, gates, README and pipeline doc, CI, PR description, morning report, then the rollout if the policy applies. (the remaining 1 h 15 min)

At the 6-hour mark, write a progress note into the PR description, so a session that dies still leaves a trail.

If the budget runs out mid-phase:

- finish the current commit;
- confirm `main` is untouched (unless the rollout ran) and that the branch runs;
- write the report with exactly what is done, what is not, and the next step.

## Morning report

Write `docs/scraper-v2/REPORT.md` and paste it into the PR description. Numbers over adjectives.

1. Status: done or partial (what is missing). Deploy state: PR link, preview URL, and whether anything touched production (and what).
2. Decision: refactor or rewrite, and why, in 3 lines.
3. Sources: a table of every adapter with tier, enabled, companies seeded, jobs in the last run, errors, and notes (blocked, needs key, flaky). Include the grandfathered LinkedIn and JobSpy steps and the GitHub lists.
4. Coverage:
   - jobs by country, by province or state (top 15), by career family and by experience level, each against its target;
   - the visible feed before and after (5,665 on 2026-10-25);
   - which targets were missed, and the planned fix.
5. Quality: dedupe rate, enrichment accuracy on the labeled sample, liveness fixture results, idempotency result, closure-simulation result, full-run duration, test baseline vs final.
6. Budget: logical size of `main` and `scraper-v2-dev` before and after, transfer and compute used, the monthly projections, Vercel usage.
7. Action items for me:
   - env vars and API keys to add (name plus where to get it);
   - the Neon role and Actions secret for the ingestion workflow (exact SQL and names);
   - Vercel or DNS settings to change;
   - the Neon plan, if the budget binds;
   - sources needing a licensing decision (LinkedIn, Indeed, Glassdoor);
   - backup branches to delete;
   - anything you were unsure about.
8. Known risks and what to watch in the first week: source bans, storage and transfer growth, cron overlap, GitHub's schedule drift.
9. Commits, one line each.
