# Scraper v2: report

Branch `feat/scraper-v2`, built on 2026-10-25 and 2026-10-26 from the spec in `docs/scraper-v2/SPEC.md`. The account's usage limits split the 12-hour budget into several sittings. This file is also the PR description.

## 1. Status

**Done, and ready to merge.** Everything in the spec is built and checked on `scraper-v2-dev`, and every gate passes (section 5). Production is untouched and waits for your go (section 7). One run fell short: dev run #6 was stopped after 574 boards when the computer ran low on memory (section 3), so run #5 is the last full run reported here.

What the branch adds:
- one ingestion pipeline (`backend/ingest/core/pipeline.py`) shared by `cron-ats` and a new runner (`python -m backend.ingest.run`) with a task queue;
- six experience levels and twenty career families, classified by rules with no LLM;
- 13 new adapters and a registry of 2,364 employers, 1,313 of them enabled;
- dead-job detection, storage caps, a daily runner workflow and CI;
- Jobs page filters for every level and family, the README "Ingestion" section and a rewritten `docs/ingestion-pipeline.md`.

A final whole-branch review found 1 Critical and 3 Important issues. All four are fixed and re-reviewed, as are 10 of its 13 should-fix items. The re-review's one new finding (the orphan sweep ignored the switch) is fixed and pinned by tests. Dev run #5 then showed a scheduling fault, fixed in `ed2472c` (section 3). Section 8 lists what is left.

**Deploy state.**
- **Production: not touched.** No merge, no migration on `main`, no Vercel setting, no secret. The build only read `main` to measure it.
- **PR: not opened.** The GitHub MCP token is read-only for Tailrd-Org. Open it at https://github.com/Tailrd-Org/Tailrd/pull/new/feat/scraper-v2 and paste this file as the description.
- **Preview:** https://tailrd-git-feat-scraper-v2-tailrd-team.vercel.app, checked on the build of `ed2472c` (READY). It runs on the Development database, a copy of production from 2026-10-24, with the boot migrations applied: `/health` is healthy, `/app` loads, and `/jobs/stats` counts 5,713 student jobs. There `/jobs/levels` offers only Intern/New Grad and no row has a career family yet, because the old rows are relabeled by rollout step 3's tag backfill. Production looks the same between the merge and that step, except that rows `cron-ats` adds after the merge already carry a family and can be Entry Level.
- **Merging is safe on the free plan.** The switch `CRON_ATS_V2_CATALOGUE` is off by default. While it is off, `cron-ats` crawls the same 252 student boards with the same entry-level filter as today, so production keeps its boards and its student levels; new rows also get a career family, and a board that `cron-ats`'s insert time box cuts short goes first in its next run (DECISIONS 23). One board key follows a move: HubSpot's Greenhouse board, which answers 404 under its old slug. Every-level ingestion starts only when you switch it on (section 7).
- **Dev database:** the Neon branch `scraper-v2-dev` (`br-summer-sunset-b7t9a1u0`), a copy of production from 2026-10-25, running v2.

## 2. Decision: refactor

One of the spec's five rewrite criteria held: `cron-ats` mixed orchestration, normalization and writes. The connectors already used JSON APIs, had tests and fixtures, and survived per-board failures. So the write path moved into the shared pipeline, and `ATSJob`, `BoardSnapshot` and reconciliation stayed.

## 3. Sources

The registry (`backend/data/ats_companies.json`) lists 2,364 employers, **1,313 enabled**: 424 based in Canada and 889 in the US (`hq_country`). The 1,051 disabled entries each carry a `disabled_reason`:
- 492 verified US private-sector boards held for the storage budget (the reason starts with `held:`). Enable them after the plan upgrade.
- 337 on 31 platforms with no connector yet: NEOGOV 59, custom career sites 53, Dayforce 39, Avature 23, ADP 21 and others.
- 180 on Taleo, SuccessFactors and Jobvite (see the table).
- 42 others: boards listing no postings on 2026-10-25, 6 Workday boards held for a product decision, and boards that moved or cannot be read.

| Adapter | Tier | Seeded / enabled | Run #5: boards ok / retried | Listed / inserted | Notes |
|---|---|---|---|---|---|
| workday | 2 | 968 / 682 | 670 / 12 | 228,478 / 1,908 | existing connector; 279 boards held for storage; the 12 retries were 5xx answers, timeouts and one maintenance page |
| greenhouse | 1 | 231 / 149 | 148 / 1 | 26,566 / 958 | existing; HubSpot's board moved (fixed) |
| oracle_hcm | 2 | 151 / 107 | 107 / 1 | 135,306 / 1,387 | new; Stanford turned off (its site is on its own domain) |
| jibe | 2 | 67 / 67 | 66 / 1 | 72,860 / 85 | new; The Cheesecake Factory's site moved (fixed) |
| icims | 2 | 98 / 45 | 45 / 1 | 24,782 / 993 | new; Carle Health turned off (robots.txt) |
| smartrecruiters | 1 | 56 / 44 | 43 / 1 | 15,964 / 404 | existing |
| lever | 1 | 63 / 45 | 45 / 0 | 5,808 / 83 | existing |
| ashby | 1 | 102 / 75 | 74 / 1 | 3,024 / 12 | existing |
| bamboohr | 1 | 80 / 72 | 70 / 2 | 862 / 0 | new; crawled after run #5's insert stop (52 inserted in run #6) |
| workable | 1 | 16 / 12 | 12 / 0 | 389 / 0 | new; same (3 inserted in run #6) |
| jobbank_ca | 2 | 6 / 6 | 6 / 0 | 1,001 / 0 | new; crawled after the insert stop |
| recruitee | 1 | 3 / 3 | 3 / 0 | 17 / 0 | new |
| usajobs, adzuna, jooble | 2, 3, 3 | 3 / 3, 2 / 2, 1 / 1 | not run | n/a | need API keys (section 7) |
| teamtailor | 1 | 0 / 0 | n/a | n/a | built and tested; no Canadian or US board found yet |
| taleo | 2 | 53 / 0 | n/a | n/a | built; cannot tell a removed posting from an open one yet (section 8) |
| successfactors | 2 | 121 / 0 | n/a | n/a | built; needs each employer's own career-site host |
| jobvite | 1 | 6 / 0 | n/a | n/a | dropped: no public feed |
| GitHub lists | curated | 50 sources | n/a | 1,503 live | unchanged; rows now carry a career family |
| LinkedIn guest + JobSpy | 4 | unchanged | n/a | 1,658 live (LinkedIn 1,106, Indeed 552) | unchanged (spec decision 6); the licensing call is yours |

**Full-run duration.** Dev run #5, the last full run, crawled all 1,309 boards then enabled (from `16e9c90`, 2026-10-26) in 2 hours 33 minutes against a 120-minute budget: 1,289 boards ok, 20 retried, 0 failed. It listed 515,057 postings, found 426,945 in Canada and the US, inserted 5,830, refreshed 12,494 in place, removed 179 and revived 56. The caps held back 54,281 postings at the 60-per-employer cap and 9,570 dated more than 90 days ago. Inserting stops at the budget while refreshing and reconciling go on, so the 655 boards that finished after the 120-minute mark inserted nothing. The scheduled runner gets 170 minutes.

That run also showed a scheduling fault, fixed in `ed2472c` (DECISIONS 23). The boards crawled after the insert stop were stamped like the rest, so the next run queued them last again: of the 574 boards run #6 reached, 560 came from run #5's first half. Now a board that loses new postings to the stop keeps its old stamp and goes first in the next run.

Of run #5's 20 retries, 16 were transient: 7 timeouts or dropped connections, 8 server errors (5xx) and 1 Workday maintenance page. The other 4 were registry faults, fixed in `27f50a8`:
- HubSpot's Greenhouse board moved from `hubspot` (404) to `hubspotjobs`, and the frozen student catalogue follows it.
- The Cheesecake Factory's career site moved to www.cakecareers.com (the same Jibe API).
- Stanford's Oracle site is on its own domain, which the Oracle adapter refuses: disabled.
- Carle Health's robots.txt disallows every crawler: disabled.

**Run #6 was cut short.** It started from `27f50a8` on 2026-10-26 at 20:39 UTC, over all 1,307 enabled boards. After 80 minutes and 574 boards, Claude Code stopped it because the computer was low on memory, and it was not restarted. Those 574 boards: 571 ok, 3 timeouts, 0 registry faults; 104,762 postings listed and 3,567 inserted. HubSpot's new board listed 66 postings and inserted the 14 in Canada and the US, and The Cheesecake Factory's new host listed 2,944. The next run marked it `abandoned`, as the runner does with a run that died.

## 4. Coverage (dev branch after run #6)

Counted on `scraper-v2-dev` on 2026-10-26 after run #6. Visible means what the Jobs page can show: open and not hidden as a cross-source twin (5,943 twins are hidden).

| Measure | Target | Dev |
|---|---|---|
| Visible jobs | 15,000 | **25,053** |
| Canada | 4,000 | 9,903 |
| United States | 8,000 | 15,150 |
| Default feed (Intern/New Grad plus Entry Level) | 5,000 | 11,119 |
| Employers with a visible job | | 1,957 |

**The visible feed before and after.** Production showed 5,665 jobs on 2026-10-25, all at the student levels. On dev, the default view (Intern/New Grad plus Entry Level) shows 11,119 and all six levels show 25,053. Production keeps its student feed after the merge until you switch on every level.

**By experience level** (target 300 each): Intern/New Grad 6,382 (4,055 internships and 2,327 new grad), Entry Level 4,737, Mid Level 5,687, Senior Level 3,351, Lead/Staff 3,861, Director/Executive 1,035.

**By career family** (target 200 each, 400 for the five marked *): Software Engineering 3,064; Healthcare, Clinical* 2,281; Engineering* 1,908; Sales & Business Development 1,667; Operations, Supply Chain & Logistics 1,600; Data, AI & Analytics 1,588; Skilled Trades, Construction & Manufacturing* 1,235; Education & Research 1,051; Hospitality, Retail & Food Service 867; Customer Support & Administrative 795; IT, Security & Infrastructure 790; Marketing & Communications 779; Banking, Insurance & Investment 774; Finance & Accounting* 711; Product & Design 690; Legal* 465; Government & Public Sector 438; Human Resources & Recruiting 384; Healthcare, Administration & Support 371. Other: 3,564. No family yet: 31 rows from before the tag backfill.

**By source:** employer boards 21,892, GitHub lists 1,503, LinkedIn 1,106, Indeed 552.

**By province or state** (top 15 across both countries; the spec sets no target): Ontario 3,476; California 2,842; New York 1,195; Texas 870; Quebec 856; Alberta 827; British Columbia 816; Illinois 549; Washington 481; Virginia 391; Massachusetts 391; Colorado 374; Florida 342; Ohio 327; Pennsylvania 257. The other provinces: Saskatchewan 202, Nova Scotia 96, New Brunswick 76, Manitoba 54, Newfoundland and Labrador 22, Prince Edward Island 3, and the territories 22. 7,332 jobs have no province or state (section 8).

**Major metros** (spec: every one must appear): all 34 do. Counted on the core city and its main suburbs:
- Canada: Toronto 2,414; Montreal 899; Vancouver 667; Ottawa-Gatineau 499; Calgary 440; Edmonton 294; Halifax 60; Quebec City 57; Winnipeg 45.
- US: San Francisco-Oakland 1,323; New York 624; Chicago 393; Los Angeles 390; Boston 324; Seattle 306; Dallas-Fort Worth 277; Washington 234; Denver 219; Atlanta 204; Phoenix 161; Houston 144; Philadelphia 124; San Diego 111; Tampa-St. Petersburg 104; Minneapolis-St. Paul 82; Miami 77; Detroit 65; Charlotte 46; Baltimore 45; Portland 45; St. Louis 38; Orlando 34; San Antonio 16; Riverside-San Bernardino 10.

**Targets missed:** none of the spec's. The thin spots are Riverside-San Bernardino, San Antonio and Winnipeg, and the jobs without a province or state. The planned fix for the metros is more Tier 2 tenants there (hospitals, school districts, cities), not looser matching.

**After this count,** the local `cron-ats` check (section 5) ran with the switch off, as production runs it. It retired 1,871 of the runner's above-entry rows on the 67 student boards it crawled, and `cron-freshness`'s orphan sweep retired 85 more. That is decision 21's switch-off behavior on a branch that holds every level; production stores none of those rows. The runner's next crawl of those boards brings them back. Dev showed 23,233 visible jobs afterwards.

## 5. Quality and gates

| Gate | Result |
|---|---|
| Volume (spec: 15,000 live, 4,000 in Canada, 8,000 in the US) | Pass: 25,053 visible on dev, 9,903 in Canada and 15,150 in the US (section 4) |
| Families (spec: 200 each, 400 for five) and levels (300 each) | Pass: the smallest family is Healthcare, Administration & Support at 371, and the five that need 400 run from Legal 465 to Healthcare, Clinical 2,281. The smallest level is Director/Executive at 1,035 |
| Metros (spec: all 9 Canadian and the 25 largest US metros) | Pass: all 34 have visible jobs. The fewest are Riverside-San Bernardino (10) and San Antonio (16) |
| Storage (spec gate: dev under 450 MB) | Pass: 344.5 MB logical after six runs |
| Full run | Run #5: 1,309 boards in 2 hours 33 minutes, 0 failed (section 3) |
| Ingest role | Pass: the documented grants ran the runner with `--no-schema`, inserted 144 rows and ran the sweeps with 0 permission errors (section 7, item 5) |
| Jobs page (Playwright, local build on dev) | Pass: `/jobs/levels` offers all six options. Each option alone loads 50 cards of only its levels; the default loads Intern/New Grad plus Entry Level; a filter saved under the old taxonomy (`new_grad`) loads as those two; a job's detail view opens. 0 hidden rows, 0 page errors |
| API and cron routes (local, on dev) | Pass: the job routes answer 200 for every level, a legacy level, two families, a legacy category, a city, the stats, the rail, the city list and one job. The four cron routes answer 200 with the secret, and `cron-ats` answers 403 without it. The email and OpenAI keys were blank and the alert budget 0, so no email went out. `cron-backfill` took 311 s from this computer, whose round trips to Neon are far slower than Vercel's; the branch changes only a comment in that route |
| Enrichment accuracy (spec: family 90%, level 85%) | Pass: on the 550 hand-labeled real postings (labels committed before the rules), family 95.8% and level 95.8%. That is in-sample. A reviewer's 177 fresh titles scored 93.2% family and 95.5% level; the 83 hardest of them scored 88.0% family |
| Duplicate rate (spec: under 2%) | Pass: of 500 random visible rows on dev, 1 has a twin from another source (0.2%) and 0 are the same posting stored twice. 44 more share a title, employer and city with another row of the same board: separate requisitions (DECISIONS 9). Across all visible rows, one pair shares a board and an id, and they are two different COSTI postings (section 8) |
| Idempotency | Pass: 30 completed boards re-run 23 hours later refreshed 704 known jobs in place and inserted only the 14 genuinely new ones (0 re-inserts) |
| Closure simulation | Pass (`backend/tests/ingest/test_closure.py`): a board drops 50 of its 120 postings. All 50 close at the next reconcile with `closed_reason` `delisted` (DECISIONS 5: a complete listing closes at once, with no stale step), two liveness passes leave them closed, and one relisted posting comes back |
| Mass-removal guard | A complete listing that would close more than half of a board's open rows (and more than 20) is held, not applied |
| Adapter completeness | Every adapter calls a listing complete only on positive evidence. Three task reviews found the opposite; all fixed and re-reviewed |
| Liveness fixtures (spec: 100%) | 125 of 128 cases pass (95 recorded exchanges, 15 sources). The 3 misses are Taleo's removed postings, which answer with the same page as open ones; no Taleo board is enabled |
| Existing suite | Full backend suite on the final code (`ed2472c`), with CI's settings: **5,274 passed, 0 failed**, 33 skipped, 3 expected failures (the Taleo cases), in 35 minutes. Baseline on `main`: 4,088 passed |
| Frontend | `npx tsc --noEmit` clean; the level and onboarding tests pass (59 of 59) |
| Python lint | `ruff check backend/ingest`: all checks passed (ruff 0.7.4) |
| `match-engine/` | No file changed, and it imports nothing from `backend/`, so `make test` and `make eval-fast` are as on `main` |
| CI, nightly network tests, Postgres job | Merged (Task 8): secret masking verified, dependencies pinned with hashes |
| Docs | README "Ingestion" section; `docs/ingestion-pipeline.md` rewritten for v2 |

## 6. Budget

Neon readings on 2026-10-26 at about 22:00 UTC. The project's usage period began on 2026-10-24 at 16:06 UTC, when it moved to the Tailrd account.

| | Before the build | Now |
|---|---|---|
| `main` logical size | 301.8 MB (2026-10-25, 04:52 UTC) | 309.0 MB. That is production's own growth: the build wrote nothing to `main` |
| `scraper-v2-dev` logical size | 301.8 MB (a copy of `main`, 2026-10-25) | 344.5 MB, with 25,053 visible jobs. The free plan's cap is 512 MB and the spec's gate 450 |
| `main` transfer | | 361.4 MB in 2.2 days, about 163 MB a day |
| `scraper-v2-dev` transfer | | 119.5 MB for the whole build: six runs, the test suites and the checks |
| Compute | | 3.4 of the month's 100 CU-hours: `main` 1.8, `scraper-v2-dev` 1.6, Development 0.1 |

Monthly projections:
- **Production as it is.** 163 MB a day comes to 4.9 GB in a 30-day month and 5.05 GB in a 31-day month, against the free plan's 5 GB. November ends just under the cap and December just over, with or without v2 (section 7, item 1).
- **The daily runner** adds about 1.1 GB a month: run #5 transferred about 39 MB, and run #6 transferred 13.9 MB for 574 boards, about 32 MB for all of them. Production plus the runner is about 6 GB a month, which needs the Launch plan.
- **Storage.** With the switch on, `main` grows toward the dev branch's size: here every level came to about 43 MB more than the copy of production. The 400 MB insert stop bounds it until you raise it.
- **Compute.** Production uses about 0.8 CU-hours a day, 24 a month. The dev runs stayed at the 0.25 CU minimum, so a 2.5-hour daily run adds about 0.6 CU-hours a day, 19 a month: about 43 of the 100.
- **Vercel.** No new function, route or cron. With the switch off, `cron-ats` does the work it does today. With the switch on and `INGEST_V2_RUNNER=1`, `cron-ats` skips the boards the runner crawled, so Vercel's share of the crawl shrinks. The runner runs on GitHub Actions, which is free for a public repository.

## 7. Action items for you

1. **Upgrade Neon to Launch.** This matters even without v2: production alone transfers about 163 MB a day, 4.9 GB in a 30-day month and just over 5 GB in a 31-day month, against the free plan's 5 GB (section 6). When the cap is reached the database stops and the site goes down until the 1st. Launch bills about $0.35 per GB-month of storage plus compute, roughly $5 a month at today's usage.
2. **Open the PR** at the link in section 1.
3. **Roll out** (when you say go):
   1. Run the `add_scraper_v2` migration once by hand on `main`, away from the :17 and :47 cron runs: its index builds briefly block writes to `scraped_jobs`.
   2. Merge. Vercel deploys. Check runtime errors, and check that `/jobs/levels` answers.
   3. On `main`, run the dry run and then `--apply` of `python -m backend.scripts.reclaim_dead_descriptions` (frees about 48 MB) and `python -m backend.scripts.backfill_v2_tags` (career families on the existing rows, and Entry Level for the junior and associate titles stored as new grad; until it runs, the Jobs page offers Intern/New Grad alone).
4. **After the upgrade, switch on every level.**
   1. Set `CRON_ATS_V2_CATALOGUE=1` on Vercel and redeploy.
   2. Raise `max_live_per_company` and `stop_inserting_at_mb` in `backend/ingest/data/limits.json`, and enable the boards held for storage.
   3. For the daily runner, create the role below on `main` and add its connection string as the Actions secret `INGEST_DATABASE_URL`. Run `ingest.yml` once by hand, and after a green run set `INGEST_V2_RUNNER=1` on Vercel (it makes `cron-ats` leave the runner's boards alone). Switch on step 1 first or together with the secret (section 8).
5. **The ingest role** (from `docs/ingestion-pipeline.md`, "ingest.yml: daily, heavy"). Tested on `scraper-v2-dev` on 2026-10-26: a throwaway role with exactly these grants ran the runner with `--no-schema` on 3 boards, inserted 144 rows, ran the sweeps and hit 0 permission errors, and was dropped afterwards. Run this as the database owner on `main`, after the v2 deploy:

   ```sql
   SELECT pg_get_serial_sequence(t, 'id')
   FROM unnest(ARRAY['companies','scrape_runs','scrape_tasks','scraped_jobs','source_health']) AS t;

   CREATE ROLE ingest LOGIN PASSWORD '<a long random password>' NOSUPERUSER NOCREATEDB NOCREATEROLE;
   GRANT USAGE ON SCHEMA public TO ingest;
   GRANT SELECT ON companies, company_logos, scrape_runs, scrape_tasks, scraped_jobs, source_health TO ingest;
   GRANT INSERT, UPDATE ON companies, scrape_runs, scrape_tasks, scraped_jobs, source_health TO ingest;
   GRANT DELETE ON scrape_tasks TO ingest;
   GRANT USAGE ON SEQUENCE companies_id_seq, scrape_runs_id_seq, scrape_tasks_id_seq,
                           scraped_jobs_id_seq, source_health_id_seq TO ingest;
   ```

   The secret is `postgresql://ingest:<password>@<neon host>/<database>?sslmode=require`. If you ever drop the role and create it again under the same name, Neon's pooled host can keep serving sessions of the dropped role for a while, and they fail with "permission denied" (seen in the test). Use a new name or the direct host for the first run after that.
6. **Optional API keys** (the adapters stay off without them; add them on Vercel and as Actions secrets):
   - `USAJOBS_API_KEY` and `USAJOBS_USER_AGENT` (your email): https://developer.usajobs.gov/apirequest/
   - `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`: https://developer.adzuna.com/signup
   - `JOOBLE_API_KEY`: https://jooble.org/api/about
7. **Licensing:** LinkedIn, Indeed and Glassdoor stay as they were; whether to keep them is your call.
8. **Branches to delete:** the Neon branch `scraper-v2-dev` when you no longer need it, and `pre-scraper-v2` if the rollout makes one.
9. **Calls I was unsure about** (section 8): the "Other" family's size, frontline roles in Entry Level, and whether alerts should follow the Jobs page filter.

## 8. Known gaps

- **Taleo liveness.** A removed Taleo posting looks like an open one to the page check. No Taleo board is enabled; enabling them needs a Taleo API check first.
- **Places without a province or state.** 7,332 visible jobs on dev (29%) have none: 1,538 are remote, 764 name only the country, and the rest name a place the parser did not resolve, such as "Montreal" alone or "San Mateo, CA United States". They show in the feed and under their country, but anything that filters by province or state misses them. About 3,000 could be placed from other rows of the same city. Separately, 109 rows (0.4%) pair a province with the US or a state with Canada. Planned fix: fill the province or state from the city when its other rows agree, and read a state code that comes before "United States".
- **One Workday tenant's ids.** COSTI's Workday paths all end in `_-1`, so its postings share the id `-1` (2 live rows on dev). The crawler can then take a new COSTI posting for a retitle of an old one and move the old row to it instead of adding a row. Fix: treat an id of one or two characters as missing. The rule predates v2; COSTI is a new board.
- **"Other" family.** 3,564 of the 25,053 visible jobs on dev (14.2%) are Other, more than any single family, and 31 have no family yet. Spot-check the feed by family before switching on every level.
- **Final-review items left for a product decision:**
  - S11: match alerts follow the onboarding level only, not the Jobs page filter.
  - S12: Entry Level includes frontline roles (cashier, driver) from the new retail and municipal boards. Check the mix after switching on.
  - S13: some helper code is repeated in three adapters.
- **The runner and the switch.** With the runner's secret set while the switch is off, `cron-ats` (on the student boards it crawls) and `cron-freshness`'s orphan sweep retire the runner's above-entry rows for their level, and the runner's next crawl brings them back, every day. The local check showed the size of it: 1,871 rows on one `cron-ats` shard (section 4). Switching on first avoids it (section 7, item 4).

## 9. Risks for the first week after switching on

- **Neon transfer and storage** (section 6). Watch `data_transfer_bytes` and `logical_size` daily for the first week.
- **Source bans.** 682 Workday tenants are crawled from one address. None refused the crawler in runs #5 and #6: run #5's 12 Workday retries were server errors, dropped connections and one maintenance page, and no 403 or 429 came back. Every request carries the TailrdBot user agent, with at most 2 crawls at once per host. Watch `source_health` for a tenant that starts failing; the breaker pauses it after repeated failures.
- **Run length.** Run #5 needed 153 minutes for 1,309 boards; the workflow's budget is 170. Enabling the 492 held boards would push a run past it. Boards past the budget still refresh, and DECISIONS 23 puts them first the next day, so the cut rotates instead of landing on the same boards every day. When you enable them, raise `--concurrency` in `ingest.yml`, or `--budget-minutes` together with the job's `timeout-minutes` (200 now).
- **Cron overlap.** `cron-ats` and the runner must not crawl the same board in one cycle. `INGEST_V2_RUNNER=1` makes `cron-ats` skip any board the runner crawled in the last 20 hours, and the runner's advisory lock stops two runs at once.
- **GitHub's schedule drift.** Scheduled workflows start late or are dropped when GitHub is busy. A dropped day costs nothing lasting: `cron-ats` takes a board back 20 hours after the runner last crawled it.
- **Classification:** the "Other" family and the frontline Entry Level roles (section 8).

## Decisions made

DECISIONS.md 1 to 23 (the spec's own decisions are in SPEC.md). The ones that cost the most if wrong:
- The weak entry tier (junior, analyst, associate, level I) is Entry Level, not New Grad (4).
- Removals from a complete listing apply at once, with a hold when a listing would close most of a board (5).
- Storage caps of 60 live rows per employer with student postings exempt, and a 400 MB insert stop (11).
- The tag backfill never lifts a stored student row above Entry Level (13).
- `CRON_ATS_V2_CATALOGUE` keeps production as it was until you switch it on.

## Commits

<details><summary>105 commits since main, merges left out (this report's own commit aside)</summary>

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
- 46a8f6f9 feat(ingest): INGEST_V2_RUNNER flag lets cron-ats stand aside for the v2 runner
- a4f394fc test(taxonomy): the whitespace-run timing check allows a busy CI runner
- dcef4c9d build(ingest): hashed requirements for the daily ingest workflow
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
- c6e9da1e ci: network marker in pyproject, a postgres:17 job for the Postgres-only ingest tests
- ca8b820c ci(ingest): daily ingest workflow and nightly network tests
- 187cd3ee ci(ingest): say why a role with no DDL stops at the schema step; pin the kill switch
- 5f0814da fix(scripts): the backfill keeps a stored student label a title cannot overturn
- 9aa46c66 ci(ingest): tidy the ingest.yml comments; the CI pg8000 pin follows requirements.txt
- fe9e3433 data(registry): hold Oracle, Jibe and iCIMS boards until the Task 4 review fix lands
- d1e8d1fe docs(scraper-v2): morning report and the overnight decisions
- 5f3cfac2 fix(ingest): Oracle complete needs a trusted count, as the review's F1 and F6 ask
- a1e877d3 fix(ingest): Jibe complete needs a trusted totalCount, as the review's F2 and F9 ask
- d1d7e956 fix(ingest): Taleo complete needs positive evidence the pages were read, per the review's F3
- a300f287 fix(ingest): the scheduled runner skips the schema step (--no-schema)
- 0a06ebcc fix(ingest): SuccessFactors no longer reads a page it cannot parse as an empty site, per the review's F4
- d0c98b31 fix(ingest): iCIMS complete needs the pager as evidence, and a closed notice beats JSON-LD, per the review's F5, F7 and F8
- 3760cb9c docs(ingest): README Ingestion section and the scraper v2 pipeline document
- c74c7d56 test(ingest): pin --no-schema in the runner and in the ingest workflow
- 60104793 fix(ingest): a Taleo walk ends only at the last page its count implies
- 735ef243 data(registry): re-enable the 221 Oracle, iCIMS and Jibe boards
- 0f2f2c09 docs(scraper-v2): Task 12, liveness fixtures
- f3cd5264 build(ingest): resolve requirements-ingest.txt with a two-week cooldown
- aca91244 fix(ingest): a malformed INGEST_DATABASE_URL cannot print its password into the public log
- 88ea3f0f test(ingest): liveness fixtures, 95 real exchanges for 15 sources replayed through check_listing
- 4a0601b7 fix(liveness): 'this position is no longer posted' reads as closed
- a20e2206 fix(cron-ats): CRON_ATS_V2_CATALOGUE, off by default, keeps the catalogue cron-ats had before scraper v2
- f9b91a09 fix(ingest): student postings go in first and are never held back by max_live_per_company
- 923ad3fc fix(dedup): a student-feed copy is never the twin of a row above entry level
- 651c0c18 feat(jobs): list the experience options that have live jobs, and count the viewer's levels in the stats and the rail
- e2e12fce feat(frontend): offer only the experience levels that have live jobs, and pass the viewer's levels to the stats and the rail
- 1555aa5c docs(scraper-v2): describe the every-level switch, the 1,315 enabled boards and the external_id fallback
- d42bf842 docs(scraper-v2): DECISIONS 14 corrected and entries 17 to 22; .env.example gains the three missing variables
- a335b3c4 fix(freshness): hold a complete listing that would close most of a board
- 20c8612d fix(frontend): keep the saved levels whose options are hidden when another filter changes
- ff62c2a6 docs: say what INGEST_V2_RUNNER does where the README gives the switch order
- 2e2aacfd fix(security): store only http(s) posting and apply URLs, on the configured host where there is one
- 7d3cef2c fix(crawl): key an adapter board on a host of its own by that host
- 8e91939f fix(freshness): an orphan row the sweep restores or checks gets its title's level
- 6479055f fix(fit): stale is not closed for trace pruning and the calibration references
- df2bae2f fix(dedup): source trust follows the adapter's tier, and Tier 3 aggregator rows are an inferior source
- 428d0350 fix(freshness): a known posting whose URL changed is found by its external_id and moves
- 02dcbe48 fix(http): every request honours the site's robots.txt Crawl-delay, up to 10 s
- 30bd1cb6 fix(migration): scraped_jobs.fingerprint keeps its column but gets no index
- bdc9bd53 style(ingest): tidy the final-review fixes' docstrings, and build the listed-URL set only when needed
- 55ed38d3 fix(freshness): the orphan sweep follows CRON_ATS_V2_CATALOGUE
- c6cefb5c docs: describe the code as the final re-review found it
- 6d83be1d docs: a disabled catalogue board drops out of cron-ats, and tighter wording for the mass-removal hold
- 42748122 docs: say that the runner's orphan sweep and cron-freshness disagree while the switch is off
- 16e9c90b fix(freshness): with the v2 catalogue off, the orphan sweep relabels nothing
- 27f50a83 data(registry): HubSpot and The Cheesecake Factory follow their moved boards; Stanford and Carle Health are off
- 33f851b8 docs(scraper-v2): decision 12 holds 492 US boards for storage, not 431
- 615c02cc docs: 1,313 boards are enabled after the registry fixes (107 Oracle, 45 iCIMS, 67 Jibe)
- ed2472c5 fix(ingest): a board the insert stop cut keeps its last crawl time, so the next run takes it first

</details>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
