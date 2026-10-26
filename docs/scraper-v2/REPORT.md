# Scraper v2: morning report

Branch `feat/scraper-v2` (pushed). Built 2026-10-25 00:47 to 2026-10-26 10:00 EDT; the 12-hour budget was cut into three windows by the account's usage limits, so the last pieces were not finished. This file is also the PR description.

## 1. Status

**Partial.** The pipeline, the taxonomy, the app changes, 11 adapters and the 2,364-entry registry are built, merged on the branch, tested, and proven on the Neon branch `scraper-v2-dev` (16,088 live jobs after one run). Not done: the Task 4 adapter fix, the scheduling/CI work (Task 8, on its own branch), the docs rewrite, liveness fixtures, the final whole-branch review.

**Deploy state.**
- **Production:** not touched. No merge, no migration on `main`, no Vercel change, no secret. The deploy policy needs every gate green, and several are not (section 5).
- **PR:** not opened. The GitHub MCP token is read-only for Tailrd-Org (403). Open it in one click at https://github.com/Tailrd-Org/Tailrd/pull/new/feat/scraper-v2 and paste this file as the description.
- **Preview:** not checked.
- **Dev database:** Neon `scraper-v2-dev` (`br-summer-sunset-b7t9a1u0`), a copy of production from 2026-10-25 04:47 UTC, now holding the v2 schema and data.

## 2. Decision: refactor

One of the spec's five rewrite criteria held: cron-ats mixed orchestration, normalization and writes. The connectors already used JSON APIs, had tests and fixtures, and survived per-board failures. So the write path moved into `backend/ingest/core/pipeline.py`, which cron-ats and the new runner share, and `ATSJob` / `BoardSnapshot` / reconciliation stayed.

## 3. Sources

Boards enabled in the registry on the branch, and what the dev run (#3, 2026-10-25) did with them:

| Adapter | Tier | Enabled boards | Run #3: listed / new rows | Notes |
|---|---|---|---|---|
| workday | 2 | 682 | 181,077 / 1,851 | 23 myworkdaysite.com boards back in service (Task 11) |
| greenhouse | 1 | 149 | 20,668 / 3,627 | |
| ashby | 1 | 75 | 2,481 / 1,358 | |
| bamboohr | 1 | 72 | 796 / 391 | list has no description; detail fetched per new job |
| lever | 1 | 45 | 4,858 / 717 | |
| smartrecruiters | 1 | 44 | 11,557 / 874 | |
| workable | 1 | 12 | 453 / 82 | |
| recruitee | 1 | 3 | 17 / 8 | |
| teamtailor | 1 | 0 | n/a | built and tested; no board found yet |
| jobbank_ca | 2 | 6 | 825 / 0 | rows held back by the run's insert deadline, not by errors |
| oracle_hcm | 2 | 0 (151 listed, held) | not run | held: Task 4 review found a false "complete" path |
| icims | 2 | 0 (98 held) | not run | held: same review |
| jibe (iCIMS Jibe) | 2 | 0 (67 held) | not run | held: same review; honours Crawl-delay 5 s |
| taleo | 2 | 0 (53 disabled) | not run | JSON-search flavour only |
| successfactors | 2 | 0 (121 disabled) | not run | needs each employer's Recruiting Marketing host |
| usajobs | 2 | 3 | not run | needs `USAJOBS_API_KEY`, `USAJOBS_USER_AGENT` |
| adzuna | 3 | 2 | not run | needs `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` |
| jooble | 3 | 1 | not run | needs `JOOBLE_API_KEY` (free key: 500 requests total) |
| jobvite | 1 | dropped | n/a | no public feed (HTML only, legacy XML feed 403) |
| GitHub lists (cron-poll) | curated | 50 sources | 1,514 live | unchanged; rows now carry a career family |
| LinkedIn guest + JobSpy | 4 (grandfathered) | unchanged | 1,749 live | unchanged per spec decision 6; licensing is your call |

Registry: 2,364 entries, 1,094 enabled (Canada 376, US 718 by `hq_country`). Another 431 verified US boards are held for the storage budget, and the 221 Oracle/iCIMS/Jibe boards for the Task 4 fix. Run #3: 1,046 boards ok, 0 failed, 0 deferred; 222,732 postings seen.

## 4. Coverage (dev branch after run #3)

**Live jobs: 16,088** (target 15,000): Canada 7,269 (target 4,000), US 8,819 (target 8,000). Before v2, the feed had 5,665.

**By career family** (target 200 each; 400 for Legal, Healthcare Clinical, Engineering, Finance & Accounting, Skilled Trades):

| Family | Live | | Family | Live |
|---|---|---|---|---|
| software_engineering | 2,733 | | hospitality_retail_food | 518 |
| other | 2,545 | | product_design | 507 |
| data_ai_analytics | 1,258 | | banking_insurance_investment | 503 |
| sales_bizdev | 1,227 | | finance_accounting | 493 |
| engineering | 1,068 | | education_research | 398 |
| operations_supply_chain | 1,039 | | **legal** | **310 (short of 400)** |
| healthcare_clinical | 658 | | hr_recruiting | 275 |
| it_security_infrastructure | 621 | | government_public_sector | 209 |
| marketing_communications | 545 | | **healthcare_admin_support** | **92 (short of 200)** |
| customer_support_admin | 544 | | skilled_trades | 542 |

**By experience level** (target 300 each): entry 3,170; mid 2,997; internship 2,789; lead 2,454; senior 2,115; new_grad 1,955; director 589; executive 19 (Director/Executive together 608). Intern/New Grad is 4,744, short of the spec's 5,000. With Entry Level, the old "New Grad / Entry Level" audience is 7,914 (DECISIONS.md 4).

**Top regions:** ON 2,746; US-CA 2,053; NY 817; QC 729; BC 614; AB 497; TX 436; IL 333; WA 295; VA 267; MA 230; CO 219; FL 173. 2,382 Canadian and 2,294 US rows have no parsed province or state.

**Misses and the fix for each:**
- Legal and Healthcare Admin & Support are short. Most of their boards are the held Oracle/iCIMS/Jibe and SuccessFactors employers (hospitals, law firms), and many more were never reached before the run's insert deadline. Fix the Task 4 adapters, re-enable those boards, and run again; no looser classification.
- "Other" holds 2,545 rows. That is worth a look before launch.

## 5. Quality and gates

| Gate | Result |
|---|---|
| Full run on dev: 0 unhandled exceptions, failure rate under 5% | Pass: 0 failed of 1,065 boards |
| Volume and coverage | Mostly pass: 16,088 live, both countries, every level; Legal, Healthcare Admin and Intern/New Grad alone short (section 4) |
| Taxonomy accuracy on the labeled sample (550 real postings, labels committed before code) | Pass: family 95.8%, level 95.8%. In-sample. A reviewer's 177 fresh titles scored about 93% family and 95% level |
| Closure simulation | Pass (unit test in `test_pipeline.py`): 50 dropped postings are hidden and closed with `closed_reason` `delisted` on the first reconcile |
| Idempotency | Partial: a second live run on 5 boards added 0 duplicates; no second full run was made |
| Duplicate rate under 2% in a random 500-job sample | Not measured |
| Liveness fixtures, 3 alive and 3 dead per Tier 1/2 source | Not done |
| Adapter `complete` flags | Tier 1 and public adapters fixed after review; Oracle/Jibe/iCIMS/Taleo/SuccessFactors fix unfinished, so their boards are held |
| App renders against the new schema, only live jobs, routes 200 | Backend tests pass; the preview was not checked by hand |
| Existing suite | Integration set (1,338 tests) green; full-suite result in section 8 |
| CI runs unit tests on every PR; Postgres job; nightly network tests | Not merged: Task 8 is on `sv2/t8-sched`, unreviewed |
| README "Ingestion" and pipeline doc rewrite | Not done (the docs agent hit the usage limit) |
| Storage: dev branch at or under 450 MB | Pass: 327.6 MB logical |
| Transfer: the build at most 500 MB | Pass: 66.6 MB on the dev branch |

Full-run duration: run #3 took 7,936 s and stopped inserting at its deadline after 8,908 new rows. From this laptop each insert costs about 0.5 to 0.8 s of round trips to Neon. In GitHub Actions (same region as Neon) it would be far faster.

## 6. Budget

- **Storage.** `main` 305.5 MB, `scraper-v2-dev` 327.6 MB logical (cap 512). `limits.json` holds every employer to 60 live rows and stops inserting at 400 MB of `pg_database_size` (about 436 MB logical). The reclaim cleared 22,557 long-dead descriptions (48 MB made reusable).
- **Transfer.** Production alone used 257 MB in its first 45 hours on the new account, about 4.1 GB a month against the 5 GB cap. The dev branch used 66.6 MB for the whole build. A daily full v2 run should cost tens of MB a run (run #3 read 22,602 stored rows back), roughly 1 GB a month. **Together that is at or over the free cap, which takes the site down until the 1st.** See action item 1.
- **Compute.** Dev 2,813 compute-seconds; production 4,737 since 10-24.

## 7. Action items for you

1. **Neon plan, before any v2 schedule touches production.** Launch is about $5 a month at today's size. Otherwise keep v2's full crawl to every second or third day.
2. **Open the PR** at the link above (the MCP token cannot), and paste this report.
3. **API keys** (optional; adapters stay off without them):
   - `USAJOBS_API_KEY` + `USAJOBS_USER_AGENT` (your email): https://developer.usajobs.gov/apirequest/
   - `ADZUNA_APP_ID` + `ADZUNA_APP_KEY`: https://developer.adzuna.com/signup
   - `JOOBLE_API_KEY`: https://jooble.org/api/about

   They go in Vercel production env vars, and in the runner's environment once it is scheduled.
4. **Scheduling secret** (only after Task 8 is merged): a least-privilege Neon role for the ingest workflow and the Actions secret `INGEST_DATABASE_URL`. Task 8's report on `sv2/t8-sched` has the exact SQL.
5. **Licensing call on LinkedIn/Indeed** (JobSpy and the guest API). Unchanged tonight; they carry about 1,750 live jobs.
6. **Delete** the Neon branch `scraper-v2-dev` once the PR is merged or abandoned.

## 8. What is left, in order

1. Finish the Task 4 fix: honest `complete` flags in `oracle_hcm`, `jibe`, `icims`, `taleo`, `successfactors`. A partial edit sits uncommitted in `.worktrees/sv2-t4-tier2`. Then re-enable the 221 held boards.
2. Review and merge Task 8 (`sv2/t8-sched`, 6 commits: `ingest.yml`, nightly network tests, the CI Postgres job, the `INGEST_V2_RUNNER` cut-over).
3. Write the README "Ingestion" section and rewrite `docs/ingestion-pipeline.md`.
4. Add liveness fixtures (3 alive, 3 dead per Tier 1/2 source), measure the duplicate rate, run a second full run for idempotency.
5. Do the final whole-branch review, then roll out per the spec's "Rollout to production": reclaim and backfill on `main`, then a bounded first run.

Full backend suite on the final head (`fe9e343`): FULLSUITE_RESULT.

## 9. Risks for the first week after launch

- **Neon transfer** (section 6) is the one that can take the site down.
- **Workday rate limits:** 682 tenants are crawled from one IP. Run #3 had no blocks, but nothing guards against a ban beyond per-host pacing.
- **Classification of "other"** (2,545 live rows) and the in-sample accuracy numbers. Spot-check the feed by family.
- **Cron overlap:** cron-ats and the v2 runner share one pipeline but not one lock until Task 8's cut-over flag is on.

## Decisions made overnight

Spec decisions are in SPEC.md; the build's own are DECISIONS.md 1 to 16. The rulings that cost the most if wrong:
- The weak entry tier (junior, analyst, associate, level I) is Entry Level, not New Grad.
- The Oracle/iCIMS/Jibe boards are held until their fix lands.
- Storage caps of 60 live rows per employer and a 400 MB stop.
- The tag backfill never lifts a stored student row above Entry Level.
- Three tasks' per-task reviews (1, 6, 11) were folded into the final review, which has not run yet.

## Commits
- 6a97bf8d docs(scraper-v2): spec, plan and first decisions for the v2 ingestion build
- cf45ea28 feat(ingest): scraper v2 foundation: schema, adapter registry, polite HTTP, taxonomy API
- 61844f53 docs(scraper-v2): legacy job-function map, hq_country vs country
- aaef2dc2 test(ingest): hand-labeled sample of 550 real postings for the taxonomy gates
- 9e113cdf feat(ingest): Job Bank Canada adapter, read from the public search and posting pages
- fd669c09 refactor(ingest): move cron-ats's per-board write path into ingest/core/pipeline.py
- 032385c9 feat(ingest): Workable adapter, one widget request per board with descriptions
- 2ebd8864 data(registry): 1,197 enabled boards across every sector, hq_country on every entry
- 3cd24dd3 feat(ingest): USAJOBS adapter, built to the published API and disabled until a key exists
- 1dcec990 feat(ingest): Recruitee adapter, offers API with descriptions and salary
- 4fb30d79 fix(ingest): Job Bank pay that the salary parser would misread is left out
- a78498ef feat(jobs): six experience levels, career_family filter, stale hidden, level-aware alerts
- 48879ae0 feat(ingest): Teamtailor adapter, per-tenant RSS feed with descriptions
- 3fb2fd21 feat(ingest): Adzuna adapter (Canada and US), built to the published OpenAPI and disabled until keys exist
- e3cd6ac6 feat(ingest): Oracle Cloud Recruiting adapter (oracle_hcm)
- 2906bd54 feat(ingest): BambooHR adapter, careers list with lazy detail fetch
- 45d4048b feat(ingest): Jooble adapter, built to the published REST API and disabled until a key exists
- f248f901 fix(ingest): Recruitee liveness reads robots.txt before the one page it fetches
- ff988ddf docs(ingest): Recruitee adapter docstring gives the measured offer sizes
- bd7efec9 data(registry): Job Bank Canada boards, and USAJOBS/Adzuna/Jooble boards inert until keyed
- 7aa63f5c feat(jobs-ui): Jobright's six experience levels and the 20 career families
- ae7776ad feat(onboarding): six experience levels and the career families
- 985bcf2f feat(ingest): iCIMS career portal adapter (icims)
- f4379bfe feat(ingest): every level, v2 tags, storage caps and closed reasons in the shared pipeline
- 1130b303 feat(ingest): v2 runner, work queue and ingest report
- 34d5e075 feat(scripts): reclaim the text of postings closed over 30 days ago
- 3d3a2aab feat(ingest): experience levels and career families from rules
- cc9c8c87 fix(ingest): a listing is complete only with a positive reported total (review I1, M1, M2)
- bbe42439 feat(ingest): Taleo career section adapter (taleo)
- 87723ec9 fix(ingest): Teamtailor pages its RSS feed instead of reading only the first 100 jobs
- 4ddbd834 fix(ingest): check_alive answers unknown when the connection fails
- f3839e60 feat(ingest): SuccessFactors adapter (successfactors) over Recruiting Marketing sites
- d5908739 perf(ingest): a new row is not read back after its insert
- 41ba3c90 fix(ingest): an adapter imported before the scraper is no longer dropped from the registry
- f9489850 docs(scraper-v2): Task 11, the follow-ups the task reviews routed out
- e56fb3d6 chore(ingest): storage caps sized for 1,200 employers on the 512 MB plan
- 85c8384c feat(ingest): Jibe career site adapter (jibe)
- f775f6eb fix(ingest): taxonomy review round 1: bounded years regex, school grades, academic ranks
- 25cfc528 test(freshness): the weak entry tier is Entry Level since v2
- a4f394fc test(taxonomy): the whitespace-run timing check allows a busy CI runner
- 2e52ddda fix(ingest): Taleo and SuccessFactors claim no posting URLs
- 863bc754 fix(ingest): iCIMS check_alive reads a closed notice the way the generic check does
- 744b17ca data(registry): JPMorgan and Goldman on Oracle Cloud Recruiting, 67 Jibe boards, 14 more Tier 2 boards
- 8a627e9b chore(gitignore): backend/ingest/data is tracked like backend/data
- 79d9ce59 fix(scraper): a dean is a senior post, never the entry-level Associate
- f4bfaef5 feat(ingest): GitHub-list and ingest-batch rows get the v2 family tags
- b4344c61 feat(workday): myworkdaysite.com boards crawl with their own posting URLs
- 93d4c735 fix(setup): the experience question fits six levels
- 12f70147 feat(scripts): backfill the v2 tags on rows that predate scraper v2
- b985734e feat(scripts): ats_detect finds the ATS behind a careers URL
- af4bf5fc test(workday): a stored myworkdaysite URL maps back to its CxS detail
- ff485145 fix(jobs): stale direct rows keep hiding their mirrors; alerts skip unlevelled jobs
- 338280e1 fix(scripts): ats_detect never offers a vendor's own name as the employer's slug
- 5f0814da fix(scripts): the backfill keeps a stored student label a title cannot overturn
- fe9e3433 data(registry): hold Oracle, Jibe and iCIMS boards until the Task 4 review fix lands
