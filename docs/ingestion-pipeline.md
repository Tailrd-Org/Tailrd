# Job Ingestion Pipeline

How Tailrd sources, structures, deduplicates, and (most importantly) **keeps
fresh** its job catalogue. The design goal is to beat aggregator competitors
(Jobright et al.) on the axes they are weakest: ghost/expired listings,
low-trust reposted data, and unexplained matching. Volume is not the goal; a
smaller catalogue where every listing is real and current is.

This page describes scraper v2 as built on the branch `feat/scraper-v2`
(2026-10-25 and 26). v2 widened what the pipeline stores and moved the heavy
crawling out of Vercel. It kept the freshness rules, each of which cost an
incident to learn. What changed:

- **Every experience level is stored and tagged**, not filtered out at crawl
  time: 8 stored values, offered as Jobright's 6 filter options. Every job also
  gets one of 20 career families. Both come from rules (no model calls) in
  `backend/ingest/taxonomy.py` and `backend/ingest/data/taxonomy.json`.
- **`off_target` means outside the US and Canada** for crawled rows, nothing
  else. **`stale` is hidden** from the feed.
- **One write path.** `backend/ingest/core/pipeline.py` ingests a board crawl
  for the `cron-ats` endpoint and for the new runner
  (`python -m backend.ingest.run`). The runner has a work queue
  (`scrape_tasks`), a run log (`scrape_runs`) and a report
  (`python -m backend.ingest.report`).
- **New platforms are adapters**: one module each under
  `backend/ingest/adapters/`, 13 so far. The five older connectors stay in
  `ats_scraper.py`.
- **The registry grew** from 278 entries to 2,364 (1,094 enabled), every one
  with `hq_country` and `sector`.
- **Storage caps** in `backend/ingest/data/limits.json` keep ingestion inside
  Neon's free 512 MB plan, and two scripts reclaim and backfill rows.
- **GitHub Actions is still the clock.** A new daily workflow, `ingest.yml`,
  runs the runner. `scrape-jobs.yml` keeps the light work.

Everything in the earlier design stands unless a section says otherwise: the
403 rule, platform-aware liveness, partial snapshots never removing rows, the
two timestamps, ghost scoring, the GitHub-list rules. The build's own decisions
are numbered in `docs/scraper-v2/DECISIONS.md` (cited below as DECISIONS n),
with its spec and plan beside it.

| If you want to | Read |
|---|---|
| Run a crawl or read its report | "The runner, queue and report", and the README's Ingestion section |
| Add a platform | "Adding a source" |
| Add an employer | "The registry" and "Adding a company" |
| Know why a job is or is not in the feed | "The listing lifecycle" |
| Change what counts as Entry or Mid, or a career family | "Experience levels and career families" |
| Know the Neon limits and how ingestion stays inside them | "Storage and transfer" |
| Know what runs when | "Schedules" |
| See what is unfinished | "Known limits" |

## Architecture at a glance

```
GitHub Actions                                Vercel serverless (FastAPI)
scrape-jobs.yml, minutes 17 and 47 of every hour
┌──────────────────────────────┐              ┌──────────────────────────────────┐
│ 60-day inactivity check      │              │                                  │
│ scripts/scrape_jobspy.py     │──POST───────▶│ /jobs/ingest-batch  (low trust)  │
│ scripts/scrape_linkedin.py   │──POST───────▶│                                  │
│                              │              │ /github-sources/cron-ats         │
│ curl cron-ats ───────────────┼─────────────▶│   least-recently-crawled shard,  │
│                              │              │   through ingest/core/pipeline   │
│ curl cron-poll ──────────────┼─────────────▶│   GitHub lists    (medium trust) │
│ curl cron-backfill ──────────┼─────────────▶│   descriptions/locations/logos   │
│ curl cron-freshness ─────────┼─────────────▶│   expiry + platform liveness     │
│ curl ingest-metrics (log) ───┼─────────────▶│   pipeline health snapshot       │
│ fail the run on any non-2xx  │              │                                  │
└──────────────────────────────┘              │ GET  /jobs/logo/{sha}.png|svg    │
                                              │ POST /jobs/{id}/check-live       │
ingest.yml, once a day at 06:23 UTC           │   (logos, click-time liveness)   │
┌──────────────────────────────┐              └────────────────┬─────────────────┘
│ python -m backend.ingest.run │                               │
│   every enabled registry     │                               │
│   board, through             │                               │
│   ingest/core/pipeline,      │                               │
│   then the lifecycle sweeps  │                               │
└──────────────┬───────────────┘                               │
               │  INGEST_DATABASE_URL                          │
               ▼                                               ▼
  Neon Postgres  (scraped_jobs, source_health, company_logos,
                  companies, scrape_runs, scrape_tasks)

Browser (job feed) ───────────────────────────────────────────▶ Vercel
```

Key modules:

| Path | Role |
|---|---|
| `backend/ingest/core/pipeline.py` | The write path every board crawl shares (`ingest_snapshot`), the concurrent crawl (`crawl_boards`), the storage caps (`Limits`), the fingerprint, the breaker helpers |
| `backend/ingest/run.py` | The runner: `python -m backend.ingest.run` |
| `backend/ingest/core/queue.py` | The `scrape_tasks` work queue |
| `backend/ingest/report.py` | `python -m backend.ingest.report` |
| `backend/ingest/core/http.py` | The one HTTP helper of the v2 sources: user agent, per-host pacing, retries, robots.txt |
| `backend/ingest/adapters/` | One module per new platform; `__init__.py` is the adapter registry |
| `backend/ingest/taxonomy.py` + `backend/ingest/data/taxonomy.json` | Experience levels and career families, as rules |
| `backend/ingest/data/limits.json` | Storage caps |
| `backend/services/ats_scraper.py` | The five older connectors (Greenhouse, Lever, Ashby, SmartRecruiters, Workday), `ATSJob`, `BoardSnapshot`, and `ATSScraper.scrape_board()`, which sends any other platform to its adapter |
| `backend/data/ats_companies.json` + `company_registry.py` | Which boards to crawl; sharding (`pick_shard`); `load_board_config` |
| `backend/services/structured_extraction.py` | Deterministic salary/visa/skills/employment extraction, content hashing |
| `backend/services/listing_freshness.py` | Lifecycle: `title_fields`, reconcile, stale/terminal/aggregator sweeps, verify sweeps, `record_liveness`, ghost scoring |
| `backend/services/platform_liveness.py` | Is this posting still open? Asked through each platform's own API (`check_listing`, `check_listings`), or its adapter's `check_alive` |
| `backend/services/source_health.py` | Per-board circuit breaker + dead-letter view |
| `backend/services/cross_source_dedup.py` | Cross-source twin collapsing (exact + conservative fuzzy), `canonical_url` |
| `backend/services/job_filters.py` | `EXPERIENCE_FILTER_MAP`, the default levels, career-family helpers |
| `backend/services/logo_cache.py` | Self-hosted logo store (`company_logos`), propagation by company, `/jobs/logo` serving |
| `backend/services/logo_harvester.py` + `logo_image.py` | Logo source cascade; image validation and normalization |
| `backend/routers/github_sources.py` (`cron-ats`, `cron-poll`) | The endpoint crawl of one shard, GitHub-list polling |
| `backend/routers/jobs.py` (`ingest-batch`, `cron-backfill`, `cron-freshness`, `ingest-metrics`) | Aggregator ingest, repair, lifecycle cron, metrics |
| `backend/migrations/add_ingestion_freshness.py`, `add_listing_probe_columns.py`, `add_company_logos.py`, `add_scraper_v2.py` | Schema migrations (idempotent DDL, run at app startup and by the runner) |
| `backend/scripts/cleanup_feed.py`, `reclaim_dead_descriptions.py`, `backfill_v2_tags.py` | One-time and maintenance scripts (see "Scripts") |
| `scripts/ats_detect.py` | Finds the ATS behind a careers URL and prints a registry entry |
| `.github/workflows/scrape-jobs.yml`, `ingest.yml`, `ingest-network-tests.yml`, `ci.yml` | The schedules and the tests (see "Schedules") |
| `requirements-ingest.txt` | The runner's pinned, hashed dependencies |

## Sources and trust

`scraped_jobs.source_trust` says how far a row can be trusted to be the
canonical copy:

| `source_trust` | Sources | Notes |
|---|---|---|
| `high` | Employers' board APIs: the five older connectors and every adapter | The employer's own copy: canonical, direct apply URL |
| `medium` | Curated GitHub job lists (cron-poll) | Direct links, human-curated, but not the employer's feed |
| `low` | LinkedIn guest API, JobSpy (Indeed/LinkedIn) | Gap-filler only; never becomes canonical when a direct twin exists |

Adapters also carry a `TIER` constant (1 to 4). It says what kind of source
they read and has nothing to do with trust: **1** public JSON or RSS boards,
**2** enterprise ATS and public-sector sources, **3** aggregator APIs, **4**
LinkedIn, Indeed and Glassdoor (best effort; no adapter exists, because the
existing LinkedIn and JobSpy steps are grandfathered and stay as they are).

Board fetches use the public JSON (or RSS) feeds that platforms publish for job
boards to consume. Where a platform has none, the adapter reads its public
pages politely (iCIMS, SuccessFactors' Recruiting Marketing sites, Job Bank
Canada). Nothing drives a headless browser. The five older connectors space
requests to one API host by `ATS_PER_HOST_INTERVAL` (default 0.35 s, one lock
per host); adapters go through `backend/ingest/core/http.py` (see "Conduct"). A
board row's `url` points at the original posting: we never present another
platform's listing as ours. An aggregator adapter's row is the exception by
nature: its `url` is the ad on the aggregator, and `scraped_jobs.apply_url`
holds the employer's link when the source gives one.

### Platforms

The five older connectors live in `ats_scraper.py`: Greenhouse, Lever and Ashby
(public board JSON APIs), SmartRecruiters and Workday (paged; see below). Every
other platform is an adapter:

| Platform (`PLATFORM`) | Tier | What it reads | Description | `check_alive` | Registry `config` |
|---|---|---|---|---|---|
| `workable` | 1 | Widget API `apply.workable.com/api/v1/widget/accounts/<account>?details=true`, one request per board, no paging | In the list | Yes (job API: 200 alive, 404 dead) | none |
| `recruitee` | 1 | `<sub>.recruitee.com/api/offers/`, one request | In the list, with salary | Yes (the built-in route answers first) | none |
| `teamtailor` | 1 | RSS `<host>/jobs.rss?per_page=100&offset=N`, walked page by page (30 pages at most) | In the feed | No (the page check reads its honest 404) | `host`, when not `<slug>.teamtailor.com` |
| `bamboohr` | 1 | `<tenant>.bamboohr.com/careers/list` | `fetch_job`, one request per new posting | Yes (the built-in route answers first) | none |
| `oracle_hcm` | 2 | `hcmRestApi` requisition finder `recruitingCEJobRequisitions`, 200 per page, newest first | `fetch_job` | Yes (an empty details answer is dead) | `host`, `site` |
| `icims` | 2 | `https://<host>/jobs/search?pr=N&in_iframe=1`, HTML cards | `fetch_job` (JSON-LD) | Yes (410/404 dead, JSON-LD alive) | `host` |
| `jibe` | 2 | `https://<host>/api/jobs?limit=100&page=N`, JSON | In the list | No | `host` |
| `taleo` | 2 | Enterprise career sections with a JSON search (`POST /careersection/rest/jobboard/searchjobs`) | `fetch_job` | No, on purpose | `host`, `section`, optional `portal` |
| `successfactors` | 2 | The employer's own Recruiting Marketing (Jobs2Web) site, `/search/` HTML | `fetch_job` | No, on purpose | `host` |
| `jobbank_ca` | 2 | Job Bank Canada search pages, newest first | `fetch_job` (RDFa), which also gives the employer's `apply_url` | Yes (410/404 or past its end date is dead) | `params`, `max_pages` |
| `usajobs` | 2 | `data.usajobs.gov/api/search` | In the list | No | `params`, `max_pages`, `page_size`, `fields` |
| `adzuna` | 3 | Adzuna search API, Canada and US | The API's snippet | No | `country`, `params`, `max_pages`, `page_size` |
| `jooble` | 3 | Jooble REST API | The API's snippet | No | `keywords`, `location`, `host`, `max_pages`, `page_size` |

`usajobs` needs `USAJOBS_API_KEY` and `USAJOBS_USER_AGENT`, `adzuna` needs
`ADZUNA_APP_ID` and `ADZUNA_APP_KEY`, and `jooble` needs `JOOBLE_API_KEY`
(`.env.example` lists them). Until they are set the adapter is disabled:
`adapter_enabled()` is false, `load_companies()` leaves its boards out, and no
request is made. These three were built to the documented response shapes with
hand-made fixtures and have never run against a live server.

Jobvite has no adapter: its career sites publish no JSON, XML or RSS feed, only
HTML pages, and the legacy XML feed answers 403 and needs a company id only
customers get. `ats_detect` still recognizes a Jobvite board.

What each adapter learned the hard way:

- **Workable.** An unknown account is a 404, but an account that exists with no
  postings answers 200 with `jobs: []`, so a 200 with zero jobs does not prove
  the employer uses Workable. Every account shares one host, so `RATE_LIMIT` is
  1.5 s.
- **Recruitee.** Offers can live on the employer's own domain (Time Doctor).
  Such rows keep that URL, so `adapter_for_url` never routes them here and
  liveness falls back to the page check.
- **Teamtailor.** The feed needs the tenant's real host: `owlco.teamtailor.com`
  is a 404 and `owlco.na.teamtailor.com` works. The feed shows only the first
  100 jobs unless paged, hence the walk. Places are a city and a country, with
  no region.
- **BambooHR.** The list has no description or date. A tenant without a careers
  page redirects to bamboohr.com instead of answering 404, which the adapter
  treats as a failure. A remote posting carries no place at all.
- **Oracle Cloud Recruiting.** A wrong `siteNumber` does not fail: the API
  answers 200 with the tenant-wide total and no `requisitionList`, and the
  adapter raises. The list carries only a plain-text prefix of up to 1,000
  characters, which would pass for the whole text, so descriptions come from
  `fetch_job`. JPMorgan Chase is host `jpmc.fa.oraclecloud.com`, site
  `CX_1001`.
- **iCIMS.** Fields are matched by their label text. Country-coded places are
  spelled out (`CA-ON-Ottawa` becomes `Ottawa, ON, Canada`) because a bare `CA`
  reads as California to the location parser. A portal that sends
  `/jobs/search` to the employer's own site raises and names the host it went
  to.
- **Jibe.** Every Jibe `robots.txt` asks for `Crawl-delay: 5`, so `RATE_LIMIT`
  is 5.0, and a longer delay in a site's robots.txt is honoured.
- **Taleo.** Only Enterprise career sections with a JSON search are covered.
  The search needs the `X-Requested-With`, `tz` and `tzname` headers (without
  `tz` it answers 500), and the adapter asks for uncompressed replies
  (`Accept-Encoding: identity`) because one host's compressed replies cannot be
  read. Taleo Business Edition (`*.tbe.taleo.net`) and the older form-post
  layout raise. The reported count over-reports (Bayshore reports 574 and lists
  445).
- **SuccessFactors.** SAP's own career hosts (`careerNN.sapsf.com`,
  `careerNN.successfactors.com`) answer every crawler with `Disallow: /`, so
  the adapter refuses them and reads the employer's Recruiting Marketing site.
  A registry entry needs that host in `config.host`.
- **Taleo and SuccessFactors claim no posting URLs and have no `check_alive`,
  on purpose.** An adapter that owns a URL replaces the generic page check,
  which already knows things the adapters did not (Taleo's "Career Section
  Unavailable" maintenance page reads as `unknown`; a Recruiting Marketing
  gone-posting redirect to `/errorpage` reads as `dead`), and a wrong `dead`
  closes live postings.
- **Job Bank Canada.** The official open data is a monthly CSV of last month's
  postings with no employer, URL or description, and the Atom feed returns only
  the newest 100, so the adapter reads the public search pages (`sort=D`, 25 a
  page; `robots.txt` asks for `Crawl-delay: 5`, so `RATE_LIMIT` is 5.0). A
  board is one search and the registry's `params` are the site's own filters. A
  posting page carries the JobPosting as RDFa, not JSON-LD, and a removed
  posting answers 410. A listing is `complete` only when the pages ran out
  before the cap, the postings seen cover the count the site reported, and at
  least one was listed.
- **USAJOBS.** The API requires the email the key was issued to as the
  `User-Agent`, which contradicts ours. It is the one place that does not send
  `TailrdBot/2.0`: an `httpx.Auth` sets the three headers after the request is
  built.
- **Adzuna and Jooble.** The key is in the URL (Adzuna's query string, Jooble's
  path) and an `httpx.HTTPStatusError` message contains the full URL, so both
  adapters catch it and re-raise without it. Never set the root logger to INFO
  in the runner: httpx would log every full request URL, keys included.
  Jooble's free key allows 500 requests in total, for the key's life, and
  serves one country site (`jooble.org` is US only). Adzuna's consumer site
  blocks our user agent, so its liveness stays `unknown`.

## The registry

`backend/data/ats_companies.json` (`{"companies": [...]}`) is the single list
of boards. Adding coverage is a JSON edit with no code change.
`company_registry.load_companies()` returns the boards the crawler may fetch:
enabled entries on one of the five older platforms (a Workday entry also needs
its `workday_url_template`) or on a platform whose adapter is enabled.

| Field | Meaning |
|---|---|
| `company_name`, `ats_platform`, `board_slug`, `enabled` | Required. The slug is the board's token, tenant or subdomain. `ats_platform` is one of the five older platforms or an adapter's `PLATFORM` |
| `hq_country` | `CA` or `US`: where the employer is based and mostly hires. Every entry has one |
| `country` | Set only on a board where **every posting** is in that one country. It switches the location filter off for the board and says which country a bare city belongs to (BDO Canada's "London" is London, Ontario) |
| `sector` | One of 20: `agriculture_resources`, `education`, `energy_utilities`, `engineering_construction`, `finance_banking`, `government`, `healthcare`, `hospitality_food`, `insurance`, `job_board`, `legal`, `manufacturing`, `nonprofit`, `pharma_biotech`, `professional_services`, `real_estate`, `retail_consumer`, `technology`, `telecom_media`, `transportation_logistics` |
| `careers_url` | The employer's careers page |
| `config` | An object the platform's adapter reads; `company_registry.load_board_config(platform, slug)` returns a copy, `{}` when there is none |
| `workday_url_template` | Workday only: the tenant's CxS endpoint base (see "Workday specifics") |
| `disabled_reason`, `notes`, `company_logo_url`, `open_postings_at_verify` | Why a board is off, free notes, the curated logo, and how many postings it had when it was verified |

**`hq_country` is not `country`**, on purpose (DECISIONS 10). `country` is an
instruction to the crawler: every posting on this board is in that country, so
skip the location filter. Put on a multinational (RBC, Pfizer), it would file
their London and Zurich jobs under Canada or the US. So `country` is set only
on single-country boards (hospitals, universities, school boards, governments,
regional utilities, Job Bank searches), and `hq_country`, which every entry
has, only says where the employer is based. `load_board_countries()` reads
`country` and never `hq_country`.

Each runner run mirrors the registry into the `companies` table: one row per
`ats` and `ats_token`, carrying the name, domain (from the curated logo URL),
country (`hq_country`, else the board's `country`), sector, `careers_url`,
`config` and `enabled`. Only changed rows are written, and a board that left
the registry is disabled, never deleted, because `scraped_jobs.company_id`
points at it.

A verified board that is not crawled has `enabled: false` and a
`disabled_reason`; one parked on purpose starts it with `held:`. Hundreds of
verified US boards are held for the storage budget (DECISIONS 12; 492 on
2026-10-26; the free plan cannot hold ~1,200 employers' postings, see "Storage
and transfer"). The Oracle, iCIMS and Jibe boards are held until their
adapters' completeness checks are fixed. The rest stay disabled with their
reason: platforms with no connector (NEOGOV, Dayforce, ADP, Avature and
others), and Taleo, SuccessFactors and some iCIMS boards the adapters cannot
read yet. Count what is enabled with:

```bash
python - <<'EOF'
import collections, json
boards = json.load(open("backend/data/ats_companies.json", encoding="utf-8"))["companies"]
print(collections.Counter(b["ats_platform"] for b in boards if b.get("enabled", True)))
EOF
```

`scripts/ats_detect.py <careers url>` finds the ATS behind a careers page and
prints an entry for this file (see "Adding a company").

## Crawling the boards

### Crawl orchestration (cron-ats)

`POST /github-sources/cron-ats` crawls one shard of the registry per call and
hands every snapshot to the shared pipeline.

- **Shard choice:** the registry is split into `CRON_ATS_SHARDS` shards
  (default: sized so a run crawls ~150 boards; a board's shard comes from a
  crc32 of its slug). Each run crawls the shard whose most recent
  `source_health.last_success_at` is OLDEST (`pick_shard`); a never-crawled
  shard goes first. The old `hour % shard_count` starved a shard whenever
  GitHub kept skipping the same hours (shard 1 once went 38.5 h without a
  crawl).
- **Concurrency:** up to `CRON_ATS_CONCURRENCY` (default 6) boards crawl at
  once, never two on the same API host. The shared-host chains launch first
  (Greenhouse, Ashby and Lever, then SmartRecruiters), and Workday tenants,
  each a host of its own, fill the slots the chains leave. All boards of one
  adapter platform count as one host (`board_host()`), so they crawl one at a
  time here.
- **List budget:** paging past a board's newest-first head stops once
  `CRON_ATS_LIST_BUDGET_SECONDS` (default 150 s, from the start of the run) is
  spent. Such a board finishes partial for this run. No board starts after it
  either; those wait for the shard's next run and are counted in
  `boards_deferred`.
- **Insert budget:** the run stores no new rows after
  `CRON_ATS_INSERT_BUDGET_SECONDS` (default 220 s) and goes on refreshing and
  reconciling, so it ends inside Vercel's 300 s. The rows it left surface as
  new on the next pass.
- **Every level:** the crawl keeps every experience level and only US and
  Canadian postings
  (`ATSScraper(filter_entry_level=False, filter_north_america=True)`). Level is
  a tag, not a filter.
- **Cut-over:** with `INGEST_V2_RUNNER` on, boards the daily runner crawled in
  the last 20 hours are left to it (see "Schedules").

### Workday specifics

Workday has no global board API; each tenant exposes a CxS JSON endpoint.
A registry entry is scrapeable only when it carries the endpoint base:

```json
{
  "company_name": "BMO",
  "ats_platform": "workday",
  "board_slug": "bmo",
  "workday_url_template": "https://bmo.wd3.myworkdayjobs.com/wday/cxs/bmo/external",
  "enabled": true
}
```

To find a tenant's base: open the company's careers site, watch the network
tab for a POST to `/wday/cxs/{tenant}/{site}/jobs`, and copy everything up to
`/jobs`. `load_companies()` skips a workday entry without a template, so it
would read as covered and never be crawled: an enabled workday entry must
carry one (`test_every_enabled_workday_board_has_a_template`). A company that
is not on a public Workday site is filed under its real system (`custom`,
`avature`, `eightfold`, `successfactors`, `oracle`, `taleo`) and disabled
with the reason. When one tenant hosts several sites, each site takes its
own slug and none takes the bare tenant (BlackBerry and QNX on `bb`, PwC,
RBC, Morgan Stanley). (All 16 tenants that had rows but no template, Magna,
Parsons, Hitachi, Lilly and others, got one in 2026-09. On 2026-09-29 the 28
enabled entries that had never been crawled were sorted out: 9 sites
enabled, 5 held with a template for a product decision or a missing
feature, 17 filed under their real system, and a duplicate ServiceNow
entry removed.)

The connector pages the WHOLE list, 20 postings per POST, up to
`WORKDAY_MAX_PAGES` (default **100**; prod must not pin it to the old 8).
The first 8 pages are always fetched so new jobs are found; later pages stop
at the run's list budget or at Workday's 2,000-posting listing ceiling. A
snapshot counts as **complete** only when nothing stopped it early, the
reported total is under 2,000, and the distinct URLs cover the total; a list
that shifts mid-crawl reads as partial, never as a takedown. Descriptions
are fetched per NEW job only, within the detail budgets (see "The shared
pipeline").

Some tenants are on `wdN.myworkdaysite.com` instead of `myworkdayjobs.com`
(HCA, UPenn, the University of Washington, Sutter and others: 23 boards). Their
public posting URLs are `https://{host}/recruiting/{tenant}/{site}/job/...`,
and `workday_public_base()` builds that base for the host (every other host
uses `https://{host}/{site}`). A bare 200 proves nothing there, because a
made-up path under the same site also answers 200 with a shell. The evidence
for the URL shape is the CxS detail's own `jobPostingInfo.externalUrl`, which
matched the built URL on 22 of the 23 boards (Franciscan Health differs only in
the case of the site name, and both spellings answer). Boards are keyed by CxS
host, so the 11 on `wd5.myworkdaysite.com` crawl one at a time, which is slow
for the biggest (1,000 postings or more: 40 to 90 list pages each).

SmartRecruiters works the same way: up to `SMARTRECRUITERS_MAX_PAGES`
(default 20) pages of 100. Apply URLs are
`https://jobs.smartrecruiters.com/{company}/{id}`; the old
`careers.smartrecruiters.com` links 302 to the employer's careers home for
live and closed postings alike, and legacy rows are migrated to the new URL
when the board lists them.

**Partial snapshots confirm, complete ones reconcile.** A complete snapshot
goes through `reconcile_board` (listed rows confirmed or revived, vanished
rows `removed`). A partial one still bumps `last_seen_at` for every stored
row of that board it DID list (`pipeline.confirm_listed`, positive evidence
is safe) and only skips the removal half: absence from a partial crawl is
not
evidence of removal. Before this, rows past page 8 of BMO (1,012 postings)
or CIBC (513) were never confirmed and never removed.

## The shared pipeline

`ingest_snapshot(db, client, snapshot, ctx)` in
`backend/ingest/core/pipeline.py` takes one `BoardSnapshot` (what
`ATSScraper.scrape_board()` returns: the filtered jobs, every live URL, and
`complete`) and brings the catalogue in line with it. The `cron-ats` endpoint
and the runner both call it, so a posting is stored, refreshed, reconciled and
retired by the same rules whichever of them crawled it. `ctx` (from
`new_context()`) carries the run's state: the logo and country lookups, the
caps, the detail budgets and the counters. In order:

1. **Adopt legacy rows.** Rows stored under an old key or URL move onto the key
   and URL this crawl uses (`adopt_site_rows` for shared Workday tenants,
   `migrate_smartrecruiters_urls`), so they are matched, not duplicated.
2. **Refresh known listings** (`refresh_known_listings`, column-only reads):
   bump `last_seen_at`, detect edits (title, location, salary, description
   hash), re-derive the fields an edit moves, and relabel a crawler row whose
   `experience_level` no longer agrees with the taxonomy. The listings not
   stored yet are the new ones.
3. **Reconcile or confirm,** before any insert, so the rows the listing dropped
   have closed (and freed their place under the per-employer cap) when the
   employer's live rows are counted. A complete listing goes through
   `reconcile_board`; a partial one only through `confirm_listed` (see "Partial
   snapshots confirm, complete ones reconcile").
4. **Select the new rows,** newest first, within the caps that need no request
   (below).
5. **Fetch descriptions** the list payload lacks, one detail request each
   (SmartRecruiters, Workday, and adapters that define `fetch_job`), within the
   detail budgets. A row the budget cannot cover is not inserted this run and
   surfaces as new on the board's next crawl. A row whose request failed is
   stored without a description.
6. **Insert** each row and commit it on its own. A new direct row hides the
   LinkedIn and Indeed copies of itself that arrived first
   (`mark_inferior_twins`).
7. **Record the board** in `source_health` (`record_board_success`), including
   the yield check (see "Per-board health and the circuit breaker").

A new row gets: `title_norm`; `role_category` (the 17 old values, which the fit
engine reads); `experience_level`, `experience_years_required`,
`career_family`, `career_family_secondary` and `enrichment_version` (all from
`listing_freshness.title_fields`); `country`, `region`, `city`,
`locations_json`, `location_search` and `work_type`; a logo (the logo store,
else the curated registry logo, else one derived from the company domain); the
structured extraction (salary, employment type, visa, skills, `raw_hash`);
`source_trust` `high`, `board_key` and `external_id`; a `fingerprint`;
`company_id` when the `companies` table has the board; `apply_url` when the
source gives one; and the description, cut at `description_max_chars`.

`external_id` is `{platform}:{slug}:{the source's own posting id}`, so a
re-crawl updates in place even if the apply URL changes shape. An adapter
builds the whole string itself. The five older connectors give the bare id and
`stored_external_id` adds the board key, so no row ever carries
`workable:financeit:workable:financeit:ABC123`.

A posting missing from a **complete** listing is removed at once:
`listing_status` `removed` with `closed_reason` `delisted`, and revived if the
board lists it again. The spec's two-step stale-then-closed exists to survive a
flaky listing; revive-on-reappearance already does that, and it is tested
(DECISIONS 5).

### Storage caps and detail budgets

`backend/ingest/data/limits.json` holds the caps. `load_limits()` reads it: a
key that is absent, 0 or null is no cap, and an unreadable file raises, so a
broken data file cannot quietly switch the storage guard off. Why the caps
exist is in "Storage and transfer".

| Key | Now | Effect |
|---|---|---|
| `max_live_per_company` | 60 | Live rows (active or stale) one employer may hold, counted by the company name on the listing. A board whose employer is at the cap inserts no more until rows close |
| `skip_posted_older_than_days` | 90 | A new posting the source dates older than this at first sight is not stored |
| `description_max_chars` | 12000 | The stored description is cut here; hashes and extraction read the whole text. 12,000 keeps every stored description whole (p99 10,073, max 11,817 on the dev branch) |
| `max_new_rows_per_run` | 15000 | New rows one run may insert |
| `stop_inserting_at_mb` | 400 | `pg_database_size` above this stops inserting for the rest of the run (read before the first insert and every 500 inserts); refreshing and reconciling go on |

What a run held back is in its `caps` counters (`too_old`, `company_cap`,
`run_cap`, `storage`, `deadline`, `detail_budget`, `descriptions_cut`) and
`inserting_stopped` (`run_cap`, `storage` or `deadline`). They appear in the
`cron-ats` response and in `scrape_runs.stats`.

Detail requests have budgets of their own. `cron-ats` allows 40 a run
(`WORKDAY_DETAIL_BUDGET`) and 10 a board (`WORKDAY_DETAIL_PER_BOARD`), shared
by SmartRecruiters, Workday and the adapters with `fetch_job`. The runner
allows 200 a board (`DETAIL_PER_BOARD` in `run.py`) and no run-wide limit.
Every cap is checked before any detail request, so a capped employer costs no
requests.

## The runner, queue and report

### The runner

`python -m backend.ingest.run` crawls the registry's boards through the shared
pipeline. `ingest.yml` runs it daily, and you run it by hand (the README's
Ingestion section has the commands). One run, in order:

1. **Lock.** A session advisory lock (`pg_try_advisory_lock`, key
   7311027530300) on `DATABASE_URL_DIRECT` when it is set, else `DATABASE_URL`.
   The pooled host's PgBouncer does not keep session locks, so the lock wants
   the direct host. A second run logs `skipped` and exits 0: two runs never
   overlap.
2. **Schema.** `create_all` (as `main.create_tables` does it), then every boot
   migration that `backend/main.py`'s lifespan calls, read from its source with
   `ast` so the runner never imports the web app. All idempotent, and silent
   once applied. `--no-schema` skips this step, for a database role with no DDL
   privilege: several boot migrations send `CREATE INDEX IF NOT EXISTS` on
   every start, which Postgres refuses to a role that does not own the table
   even when the index exists. The database must then already carry the schema,
   which the app applies when it deploys; `ingest.yml` passes the flag.
3. **Housekeeping.** Finished tasks older than 7 days are deleted, and a run a
   crash left `running` is marked `abandoned`.
4. **Plan.** One `scrape_runs` row, the registry mirrored into `companies`, and
   one `list` task per enabled board that is not in breaker cooldown, least
   recently crawled first.
5. **Crawl.** Rounds of claim and crawl: `--concurrency` boards at once, at
   most `--per-host` on one API host. Each snapshot goes through
   `ingest_snapshot`. A failed board goes back to the queue and is tried again
   after the others, up to 3 attempts, then recorded once in `source_health` (a
   board that fails all 3 attempts counts as one failure for the breaker).
   Nothing starts, and nothing is inserted, after `--budget-minutes`.
6. **Sweeps.** The lifecycle sweeps `cron-freshness` runs, in its order
   (`run_sweeps`). That function is a copy of the sequence in
   `routers/jobs.py`, kept in step by hand because the web router is not
   imported: change one, change the other.
7. **Summary.** Counts and notes go to `scrape_runs` and the last log line.
   Yield warnings are also POSTed to `ALERT_WEBHOOK_URL` when it is set.

| Flag | Default | Meaning |
|---|---|---|
| `--boards` | all | Comma-separated boards to crawl, `platform:slug` or a bare slug |
| `--platforms` | all | Comma-separated platforms to crawl |
| `--limit` | none | Crawl at most this many boards, least recently crawled first |
| `--concurrency` | 20 | Boards crawled at once |
| `--per-host` | 2 | Boards crawled at once on one API host |
| `--budget-minutes` | 170 | No board starts and no row is inserted after this |
| `--dry-run` | off | Crawl the planned boards and log per board what would change (`known`, `new`, `would_remove`); write nothing but the schema step |
| `--no-schema` | off | Skip `create_all` and the boot migrations (see step 2); for a database role with no DDL privilege |
| `--trigger` | `manual` | Who started the run, stored in `scrape_runs.trigger` (the workflow passes `cron` for its schedule) |

The exit code is 0 for a completed run, a skipped one and a dry run, and 1 for
a failure. The log is JSON lines on stdout: one `task` line per board attempt
(`board`, `status` `ok`, `retry`, `failed` or `deferred`, then `listed`,
`found`, `known`, `new`, `inserted`, `removed`, `revived`, `complete`,
`rows_read` and `ingest_seconds`), never one per job, bracketed by
`run_started` and `run_finished` (or `run_failed`, `skipped`, `dry_run_*`).
`rows_read` counts the rows the database sent back, the measure of Neon
transfer. The log is public in GitHub Actions, so nothing secret is ever
written: `pipeline.safe_error` strips URL query strings and masks every
adapter's `REQUIRED_ENV` values, the connection strings and their passwords,
and the webhook URL.

Measured on the dev branch (run #3, 2026-10-25, from a laptop in Canada): 1,046
boards ok, none failed, 222,732 postings seen, 8,908 new rows, 7,936 s, and it
stopped inserting at its deadline. From the laptop each insert costs 0.5 to 0.8
s of round trips to Neon (us-east-1); from Actions the round trips are far
shorter.

### The queue (`scrape_tasks`)

`backend/ingest/core/queue.py`. A task is one unit of work for one run. `kind`
is `list`, `fetch`, `enrich` or `liveness`, and only `list` is used (one per
board, payload `platform`, `slug`, `company`): classification runs inline at
insert, so there are no `enrich` tasks (DECISIONS 6). Statuses are `pending`,
`running`, `done` and `failed`.

| Function | Does |
|---|---|
| `enqueue(db, run_id, kind, payloads)` | One `pending` task per payload |
| `claim(db, run_id, limit, lease_seconds)` | Leases up to `limit` tasks, oldest first: `running` until `lease_until`. `FOR UPDATE SKIP LOCKED` on Postgres, a plain select on SQLite. A lapsed lease (its worker died) is taken back. Every claim is an attempt |
| `finish(task, ok, error)` | `done` on success. On failure, `failed` once it has had 3 attempts (`MAX_ATTEMPTS`), else back to `pending` |
| `fail_unfinished(db, run_id, error)` | Fails what a dead or out-of-time run left |
| `delete_finished(db)` | Deletes finished tasks older than 7 days (`RETAIN_DAYS`); the runner calls it at the start of a run |
| `counts(db, run_id)` | `{status: tasks}` |

A list task's lease is an hour (`LIST_LEASE_SECONDS`), longer than any one
board's crawl.

### Runs and the report

Each run is one row in `scrape_runs`: `trigger` (`cron` or `manual`), `status`
(`running`, `completed`, `failed` with the error in `notes`, or `abandoned`),
`started_at`, `finished_at`, `tasks_total`, `tasks_ok`, `tasks_failed`,
`jobs_seen`, `jobs_new`, `jobs_closed` (removals plus what the sweeps closed),
`notes` (yield warnings, what stopped inserting, boards deferred) and `stats`,
the whole summary as JSON (counts per platform, `caps`, the sweeps,
`database_mb`, `rows_read`).

`python -m backend.ingest.report` (`--json` for the raw numbers) prints the
last run, the yield per source (live jobs, new in 24 hours, new in 7 days, for
each ATS platform, LinkedIn, Indeed and the GitHub lists), live jobs by career
family, experience level, country and the top 15 regions, and the database and
`scraped_jobs` sizes. "Live" means what the feed shows (see "The listing
lifecycle"). Every number is an aggregate computed in SQL; no job row leaves
the database. `GET /jobs/ingest-metrics` is the older snapshot the
`scrape-jobs.yml` log carries; it does not hold the report's numbers.

### cron-ats and the runner

| | `cron-ats` | The runner |
|---|---|---|
| Runs as | A Vercel function, 300 s | A GitHub Actions job, hours |
| Boards per call | One shard (~150 boards), the least recently crawled | Every enabled board, least recently crawled first |
| Time boxes | List 150 s, insert 220 s | `--budget-minutes` (170) |
| Concurrency | 6 boards, one per API host | `--concurrency` 20, `--per-host` 2 |
| Detail requests | 40 a run, 10 a board | 200 a board |
| Also does | Nothing else | Registry mirror, lifecycle sweeps, run log |
| Failures land in | `source_health` | `source_health`, `scrape_tasks`, `scrape_runs` |

Both write `source_health.last_success_at`, which is how the cut-over keeps
them from crawling one board twice in a cycle (see "Schedules").

## Experience levels and career families

Both are **tags computed from the posting after it is fetched**, never filters
on what gets fetched: the crawl keeps every level and the feed's filters
choose. They are rules, not a model (spec decision 2, and ingestion has been
model-free since the July cost incident). `backend/ingest/taxonomy.py` applies
the keyword and boundary rules in `backend/ingest/data/taxonomy.json` in a
fixed order, at about 450 microseconds a posting. Importing the module loads
nothing heavy; the rules file is compiled on first use.

### Experience levels

`scraped_jobs.experience_level` holds one of eight values, most junior first:
`internship`, `new_grad`, `entry`, `mid`, `senior`, `lead`, `director`,
`executive` (`taxonomy.LEVELS`). The UI offers Jobright's six options, in this
order, and `EXPERIENCE_FILTER_MAP` (`backend/services/job_filters.py`) expands
each one into the stored values:

| UI option (value, label) | Stored values |
|---|---|
| `intern_new_grad`, "Intern/New Grad" | `internship`, `new_grad` |
| `entry`, "Entry Level" | `entry` |
| `mid`, "Mid Level" | `mid` |
| `senior`, "Senior Level" | `senior` |
| `lead`, "Lead/Staff" | `lead` |
| `director`, "Director/Executive" | `director`, `executive` |

The map also resolves the keys the two-option UI used to save: `internship` is
`internship`, `new_grad` is `new_grad` plus `entry` (the old "New Grad / Entry
Level" option), and `executive` is `executive`. The old `entry` key meant
internship plus new grad; it now means Entry Level only, on purpose. A person
with no saved choice, an empty saved list or nothing recognized gets Intern/New
Grad plus Entry Level (`DEFAULT_EXPERIENCE_FILTER`), so the level filter is
never empty. `frontend/src/lib/jobFilterOptions.ts` holds the frontend side
(`EXPERIENCE_OPTIONS`, `normalizeExperienceLevels`), which the filter bar and
onboarding both read. `GET /jobs` takes `experience_level` as a comma-separated
list of the option values.

Match alerts follow the same levels. The sweep scores only jobs whose
`experience_level` is in the user's saved levels
(`user_settings.experience_levels`, expanded by the same map), so a student is
never alerted about a Director role, and a job with a blank level is never
scored for alerts.

`classify_experience(title, department, employment_type, years_required)`
decides in this order, and the first match wins:

1. **The strong entry tier** (`ats_scraper.entry_tier()` says `strong`: intern,
   co-op, new grad and the like) keeps `experience_level_for(...)`, so a title
   the old crawler read as an internship or new-grad track keeps that label.
2. **Named tracks** the tier does not name: a summer associate or analyst, a
   teaching assistant for a numbered course, an MBA, leadership or analyst
   development program.
3. **The weak tier** (junior, entry-level, level I, associate, analyst, 0-2
   years) is `entry`. That is why those jobs moved from `new_grad` to `entry`
   (DECISIONS 4), except where step 4's rank words apply ("Associate Dean" is a
   dean).
4. **Boundary rules** from `taxonomy.json`, after dropping a boss phrase
   ("Executive Assistant to the CEO" reads as "Executive Assistant") and taking
   the lower band ("Senior/Staff" is senior). Where the words land:

   | Level | Words |
   |---|---|
   | `executive` | Chief officer or a C-acronym, president (not vice president), EVP/SVP, managing partner, provost, general counsel, a school superintendent |
   | `director` | Director, head of, VP/AVP, dean, a deputy, assistant or associate chief or general counsel, a plain VP beside a chief title, a school principal |
   | `lead` | Staff (not "Member of Technical Staff"), lead, principal, architect (not a customer-facing solutions architect), manager of people or of a function, supervisor, superintendent, foreman, chief of staff |
   | `senior` | Senior, sr, III, IV, level 3 and up, L4 and up, SME |
   | `mid` | II, level 2, mid-level, intermediate, experienced, associate manager, counsel |
   | `entry` | BDR/SDR, assistants, clerks, tellers, receptionists, staff accountants and auditors, coordinators (not clinical), frontline hourly roles |

   Individual-contributor manager titles (product, project, program, account,
   relationship, case, community, customer success, partner, portfolio,
   contracts, events, and the marketing-channel managers) take their own level
   words and are `mid` without one: "Senior Product Manager" is senior and
   "Associate Project Manager" is entry. A bank's VP rank on an individual role
   is dropped ("Senior Penetration Tester, Vice President" is senior). Academic
   and public ranks have their own rules (professors, deans, provosts,
   superintendents, principals, wardens, judges), and a school grade is not a
   seniority ("Grade 3 Teacher" is mid).
5. **`years_required`** from the description, when the title names no level:
   0-1 years is entry, 2-4 mid, 5-7 senior, 8 and up lead.
6. **`mid`** otherwise.

`years_required(text)` returns the lower bound of the first experience
requirement a description states ("3+ years", "minimum of 5 years", "2-4 years
of experience", number words, the French "ans"). The figure must sit within 60
characters of the word experience, a figure above 20 is a company's own age
("25+ years of experience"), and ages, dates and "years in business" never
count. It is stored in `experience_years_required`.

### Career families

`scraped_jobs.career_family` holds one of 20 slugs (`taxonomy.FAMILIES`), and
`career_family_secondary` the runner-up. Rows and filters carry slugs, not
labels, because five labels contain commas and every list filter on `GET /jobs`
is comma-separated (DECISIONS 3).

| Slug | Label | Slug | Label |
|---|---|---|---|
| `software_engineering` | Software Engineering | `operations_supply_chain` | Operations, Supply Chain & Logistics |
| `data_ai_analytics` | Data, AI & Analytics | `hr_recruiting` | Human Resources & Recruiting |
| `it_security_infrastructure` | IT, Security & Infrastructure | `education_research` | Education & Research |
| `product_design` | Product & Design | `government_public_sector` | Government & Public Sector |
| `engineering` | Engineering | `skilled_trades` | Skilled Trades, Construction & Manufacturing |
| `legal` | Legal | `customer_support_admin` | Customer Support & Administrative |
| `healthcare_clinical` | Healthcare, Clinical | `hospitality_retail_food` | Hospitality, Retail & Food Service |
| `healthcare_admin_support` | Healthcare, Administration & Support | `other` | Other |
| `finance_accounting` | Finance & Accounting | | |
| `banking_insurance_investment` | Banking, Insurance & Investment | | |
| `sales_bizdev` | Sales & Business Development | | |
| `marketing_communications` | Marketing & Communications | | |

`classify_family(title, department, description)` returns (primary, secondary).
`taxonomy.json` holds an ordered list of rules, and specific phrases outrank
generic words ("product marketing" before "product", "solutions engineer"
before "engineer", "internal audit" before "compliance"). They are read in four
tiers: title rules, department rules, weak title words (engineer, analyst,
operations), and description rules over the first 600 characters, which use
sector words only (hospital, bank, university), never tech boilerplate. The
first match is the primary, and the next different family the title or
department names is the secondary. `other` comes back when nothing matches, and
for jobs with no family (custodian, security guard, asset protection).

Where the labeling rubric put the hard cases:

- Customer success goes to customer support, not sales. Presales solutions
  engineers, architects and consultants go to sales; post-sale implementation,
  deployment and professional services go to customer support.
- Program and project management (technical program management too) goes to
  operations, and so does general management (CEO, COO, president), because the
  legacy map files Management and Executive there.
- Government affairs, public affairs and public policy go to government whoever
  the employer is. Food service is hospitality wherever it is. Housekeeping,
  environmental services and clerks at a hospital are healthcare
  administration. Payroll is finance, business analysts are data, SRE, DevOps
  and infrastructure engineers are IT, embedded and firmware are software,
  journalism and technical writing are marketing and communications.
- A CTO's family comes from the department (IT means IT, Engineering means
  software), because the title alone cannot tell.
- Government & Public Sector is mostly function-based (policy, inspection, a
  government-named title or description). A city's accountant is finance. The
  registry's `sector` is not passed to `classify_family`; if the product wants
  every government employer's jobs in that family, that is where to add it.

`role_category` keeps its 17 values, because the fit engine reads it.
`taxonomy.LEGACY_CATEGORY_TO_FAMILIES` maps each one to families so a saved
job-function filter migrates (Software Engineering to `software_engineering`;
Engineering and Development to `engineering` and `it_security_infrastructure`;
Accounting and Finance to `finance_accounting` and
`banking_insurance_investment`; Creatives and Design to `product_design` and
`marketing_communications`; and so on). The frontend holds an identical copy,
and `backend/tests/test_taxonomy_frontend_agreement.py` fails if the two
differ. `GET /jobs` takes `career_family` (comma-separated slugs) and still
takes `role_category` for old clients. A row with no family yet answers a
family filter through its `role_category` and the legacy map, so a family
filter never hides rows that predate v2. `GET /jobs/stats` returns
`by_career_family` and `by_experience_level` over the visible rows.

### Where the tags are set

| Path | `experience_level` | `career_family` |
|---|---|---|
| Board crawl (`cron-ats`, the runner) | `classify_experience` at insert, and relabeled on any crawl where it disagrees | `classify_family` at insert, and again when an edited title is detected |
| GitHub lists (`AggregatorService`) | Unchanged: the list's own (`new_grad`, or `internship` by repo or title) | `classify_family(title)` |
| LinkedIn and Indeed (`POST /jobs/ingest-batch`) | Unchanged: the level the scraper sent | `classify_family(title)` |
| Rows stored before v2 | `backfill_v2_tags`, ATS rows only | `backfill_v2_tags`, from the title alone |

The student lists keep their own level logic on purpose (spec decision 6): the
GitHub lists are student lists, and the LinkedIn and Indeed searches use
entry-level terms only. A few writers still insert untagged rows (see "Known
limits").

`ENRICHMENT_VERSION` (1) is stored beside the tags, so rows tagged by older
rules can be found. Bump it when a rules change should re-tag stored rows. A
crawl relabels `experience_level` on its own, but a family is only re-derived
when a title edit is detected, and `backfill_v2_tags` selects
`enrichment_version IS NULL`, so a re-tag after a bump needs its own selection.

Accuracy is measured on a hand-labeled sample of 550 real postings
(`backend/tests/fixtures/ingest/labels.json`), labeled before the classifier
existed and committed first, with the functions called the way the pipeline
calls them. The primary family scores 95.8% and the level 95.8% (the gates are
90% and 85%). Those are in-sample figures, because the rules were tuned on the
mismatches; a reviewer's 177 fresh titles scored about 93% for family and 95%
for level. `backend/tests/ingest/test_taxonomy.py` holds the accuracy test and
a guard that every title the older entry-level tests pin keeps its label.

## The listing lifecycle (freshness)

Every row has a `listing_status` separate from the user's workflow `status`:

```
          board lists it / platform API says open         board stopped listing it
   ┌────────┐ ─────────────────────────▶ last_seen_at bumped   ┌─────────┐
   │ active │ ──────────────────────────────────────────────▶ │ removed │
   └────────┘ ◀── revived (board lists it again, or the        └─────────┘
        │           platform's own API says open)                   ▲
        │ not re-confirmed for 72h                                 │ platform API / honest
        ▼                                                          │ page says dead
   ┌────────┐ ─────────────────────────────────────────────────────┘
   │ stale  │ ──── 21 days without positive evidence ───▶ ┌─────────┐
   └────────┘      (also: aggregator age, unreconcilable) │ expired │
     hidden from the feed                                 └─────────┘
```

Feed visibility: `duplicate_of IS NULL`, a `listing_status` that is NULL or not
in `HIDDEN_LISTING_STATUSES` (`removed`, `expired`, `off_target`, `stale`), and
a non-blank company. Rows are **never deleted** (saved jobs and applications
reference them); `removed`/`expired` rows stay in a user's Liked list with a
"No longer accepting applications" badge and a disabled Apply.

`stale` is hidden since v2, as the spec asks: a row nobody has vouched for in
72 hours leaves the feed, and comes back when a crawl or a check vouches for it
again. There were no stale rows on 2026-10-25, so nothing disappeared when it
changed. Everything that reads `HIDDEN_LISTING_STATUSES` follows it: the feed,
the match alerts (`match_notifier._alertable()`), the report's "live" counts,
the fit engine's trace pruning. Two places deliberately do not. Cross-source
dedup counts `stale` as open, so a stale direct row keeps hiding its LinkedIn
and Indeed twins instead of freeing them to become permanent duplicates when it
recovers. The closing-soon card still lists a user's own saved stale jobs,
which it exists to warn about.

`closed_reason` says why a row closed. It is stamped whenever a row is removed
or expires, and cleared when it comes back: `delisted` (a complete board
listing no longer has it, or an Ashby org's board omits it), `404` (the posting
answers not-found), `redirect_home` (it redirects to a careers home or an error
page), `closed_text` (the page or the platform's API says it is closed or
inactive), `ttl` (it aged out). `listing_freshness.closed_reason_for()` maps a
liveness verdict's reason onto these, and an adapter's `check_alive` may answer
in these terms directly. GitHub-list closures do not stamp it yet.

`off_target` hides a row that is still open but outside the feed's scope. Since
v2 every experience level is in scope, so for crawled (ATS) rows the one reason
left is **location**: the listing is outside the US and Canada
(`ATSScraper.rejection()` says `location`, and `RETIRABLE_REJECTIONS` is
`{"location"}`). A listing with no location either way (`unplaced`) proves only
that the posting is open; it never retires a row. Board crawls set `off_target`
on the ATS rows they list whose own listing fails that test (a London, UK
posting stored before the filter knew better), and a listing that passes again
brings the row back. A cron-freshness sweep,
`retire_unreconcilable_off_target`, does the same for ATS rows on never-crawled
`unknown` boards, which no crawl will judge. The one level rule left belongs to
the grandfathered scrapers: `retire_senior_aggregator_rows` sets LinkedIn and
Indeed rows with a plainly senior title (`ats_scraper.HARD_SENIOR`) off_target,
because those searches are scoped to entry level and the spec leaves them as
they are. Both sweeps re-read what they hid on every run and give back a row
their rule no longer rejects. An off_target row is never called dead, and it
becomes `removed` once its board stops listing it.
`CRON_ATS_RETIRE_OFF_TARGET=0` turns all of this off and puts the rows back
(crawled rows on their board's next crawl, the sweeps' rows on the next sweep).
Like any Vercel env var, the change needs a redeploy to take effect; the runner
reads the repository variable of the same name (see "Schedules").

### Two timestamps: `last_seen_at` vs `last_probed_at`

- `last_seen_at` is **positive evidence only**: a board crawl listed the row,
  or the platform's own API said the posting is open. The sweeps key on it.
- `last_probed_at` is stamped by EVERY liveness check, whatever the answer.
  The verify sweeps rotate on it (least-recently-probed first).

The old verifier stamped `last_seen_at` on every probe, so a bot wall or an
SPA shell passed for a confirmation and kept dead rows alive for weeks.

### Platform-aware liveness (`platform_liveness.py`)

A plain GET cannot tell a live posting from a dead one on most of the
catalogue: Workday serves the same ~6.5 KB app shell (HTTP 200) for live,
closed and made-up job ids, and Ashby/Oracle HCM pages are SPAs. So each
known ATS is asked through its own public API:

| Platform | Check | Dead when |
|---|---|---|
| Workday (`myworkdayjobs.com`, `myworkdaysite.com`) | CxS job endpoint `/wday/cxs/{tenant}/{site}/job/...` | JSON `errorCode` S21 / S22, or 404/410 |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{co}/postings/{id}` | 404, or `active: false` |
| Greenhouse boards | `boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}` | 404 |
| Any `gh_jid` URL (custom career domains) | `boards.greenhouse.io/embed/job_app?token={id}` | 404 |
| Lever (incl. EU) | `api.lever.co/v0/postings/{company}/{id}` | 404 |
| Ashby | membership in the org's posting-api board (fetched once per org per run) | not listed (an empty board is `unknown`) |
| Oracle HCM | `recruitingCEJobRequisitionDetails` finder by id | `items: []` |
| BambooHR | the posting's `/careers/{id}/detail` endpoint | 404 with `{"type": "not_found"}` |
| Recruitee | the public offers API, by slug | 404 there **and** the offer page is 404/410 too (an edited title gets a new slug and the old one redirects) |
| LinkedIn guest page | closed banner / `expired_jd_redirect` | banner, trk token, or 404 |
| A URL an enabled adapter owns (its `HOST_PATTERNS`) | the adapter's `check_alive`, when it has one (Workable, iCIMS and Job Bank Canada reach it today) | whatever its API or page says |
| Anything else | the page itself | 404/410, redirect to an error page, an off-site redirect to a careers home, or a dead phrase in the VISIBLE text |

The built-in routes come first; an adapter's `check_alive` is asked just before
the generic page check. It receives the liveness client, whose default headers
are a browser's, so it must still send through `http.request`, which sets our
user agent on every request. A `check_alive` that raises, or returns anything
but a `LivenessResult`, reads as `unknown`. A transport error (timeout,
connection reset) is `unknown`, never `dead`.

Three verdicts: `dead`; `alive`, which is `authoritative` only when the
platform's own API or board said so (the only answer allowed to revive a
row); `unknown` for everything inconclusive.

**The 403 rule and its one exception.** A page answering 401, 403, 429 or 999
is a bot wall and never evidence of death. The single exception is Workday's
CxS JSON `errorCode: "S22"` (posting unpublished) returned with a 403: an
API-level signal a bot wall never produces, checked 85/85 against a board
search by requisition id. Indeed is never requested at all (Cloudflare walls
every probe).

Politeness: at most 2 requests in flight per host, 8 overall; a host that
fails 3 times in a row is skipped for the rest of the run; 25 s cap per URL;
bodies are size-capped. `check_listings(client, urls, concurrency=8,
deadline=, cache=)` never raises: URLs not started by the deadline are simply
absent from the result and wait for the next run.

`listing_freshness.record_liveness(db, row_id, listing_status, result)`
applies one verdict with the sweep rules: always stamp `last_probed_at`;
dead: `removed`; authoritative alive: bump `last_seen_at` (and revive a
stale/removed row to `active`); anything else changes nothing more. A dead
verdict also stamps `closed_reason`.

### The sweeps (cron-freshness, in order)

The runner runs the same sweeps, in the same order, after its crawl
(`run_sweeps`).

1. **Legacy board_key adoption:** direct rows with no `board_key` get one
   derived from their URL (`greenhouse:acme`), or `unknown`.
2. **Stale sweep:** direct rows not re-confirmed in 72 h go `stale` (hidden
   from the feed).
3. **Aggregator expiry:** LinkedIn/Indeed rows (by source OR by URL host: the
   retired external scraper stored LinkedIn cards as `source_platform='ats'`)
   expire after 21 days; GitHub-list and other non-ATS rows after 30. Age runs
   from the EARLIEST of posted date, first seen and scraped, so a future-dated
   row cannot escape. Rows on a real board are never touched.
4. **Off-target retires** (no network, bounded, column-only): crawler rows on
   boards no crawl reconciles whose own location is outside the US and Canada
   (`retire_unreconcilable_off_target`), and LinkedIn/Indeed rows with a
   plainly senior title (`retire_senior_aggregator_rows`). See `off_target`
   above.
5. **Verification, one shared 150 s box** (each phase stops starting checks at
   60/80/100% of it; unused time flows on):
   - stale backlog, 600 rows per run;
   - active direct rows no crawl has confirmed for 48 h, 150 per run (rows past
     a partial crawl, boards we don't crawl);
   - visible GitHub-list and LinkedIn rows, 200 per run.

   All three work least-recently-probed first, skip rows probed in the last 20
   h, hidden duplicates, blank-company rows and Indeed, and commit every 100
   rows.
6. **Terminal expiry,** after the checks so this run's probes count (it only
   ends a row a check has reached since its last positive evidence):
   - a `stale` row with no positive evidence for 21 days: `expired` (it had
     every board crawl and ~6 verifier runs a day to prove otherwise);
   - a direct row on a board no crawl reconciles (`board_key` `''` or
     `unknown`): `expired` 30 days after its last positive evidence.
7. **Ghost-risk scoring** (below).

Removal of jobs that vanished from their board happens earlier, inside the
crawl's reconciliation. GitHub-list rows also leave when their list marks them
closed (a lock or strikethrough, matched on company + title) or drops the URL
from the README, checked on every full re-parse of a source (skipped if it
would remove more than half of the source's rows).

### Click-time check (`POST /jobs/{id}/check-live`)

When a user opens a job, the frontend asks once per job per browser session.
The endpoint runs `check_listing` on that row and applies the verdict with
`record_liveness`, so a posting that died since the last sweep is caught the
moment someone looks at it. The response carries `{listing_status,
verdict}`; a `dead` verdict (or a removed/expired status) switches the panel
to the closed state and drops the card from the feed. The frontend ignores
errors and stops asking for the session if the route is missing (405, or a
404 whose detail is exactly `Not Found`), so an unknown job id must answer
404 with a different detail (e.g. `Job not found.`).

## Storage and transfer

Tailrd runs on Neon's free plan, and two of its limits shaped the whole
ingestion design:

- **512 MB of logical size per branch, enforced.** At the cap every write in
  the app fails: inserts, updates, deletes, sign-ups, saved jobs. On 2026-10-26
  `main` held 305.5 MB, and the dev branch 327.6 MB after run #3. The gate for
  any run or deploy is 450 MB on both. Neon's logical size runs about 36 MB
  above `pg_database_size`, which is why the runner stops inserting at 400 MB
  of the latter.
- **5 GB of data transfer a month per project.** Reads count, whoever makes
  them, and running out suspends the database until the 1st, which takes the
  whole site down. Production alone ran at about 4.1 GB a month when the v2
  build started (257 MB in its first 45 hours on the new account).

Compute is capped too (100 CU-hours a month per project, dev branches
included), so let a dev branch suspend when it is idle.

How ingestion stays inside the limits:

- **Caps.** `backend/ingest/data/limits.json` (see "The shared pipeline"): 60
  live rows an employer, nothing already older than 90 days at first sight,
  descriptions cut at 12,000 characters, inserts stopped at 400 MB. About 1,100
  employers share the 512 MB, so 60 spreads the room instead of letting a few
  giants (Walmart, HCA and Dollar General each list thousands of postings) fill
  it. The values are sized for the free plan (DECISIONS 11); raise them after
  upgrading it.
- **Reclaim.** Descriptions of long-dead postings are the largest avoidable
  cost (below).
- **Reads are columns, not rows.** The refresh selects the few columns it
  compares (about 600 bytes per listed known row), the sweeps are column
  queries, and a whole-row read of the catalogue is exactly the egress mistake
  that melted the budget once already. The runner logs the rows each task read
  (`rows_read`).
- **One full re-list a day, not one an hour** (DECISIONS 8). With every level
  stored, a board's rows grow about tenfold and the refresh reads them back, so
  hourly re-crawls would cost gigabytes of transfer a month. A daily v2 run
  reads tens of MB (run #3 read 22,602 stored rows back), roughly 1 GB a month.
- **Additive schema only.** Never rewrite `scraped_jobs`: no
  `ALTER COLUMN ... TYPE`, no volatile default, no `VACUUM FULL`, no copy into
  a new table. A rewrite needs about twice the table's size, which the cap does
  not leave.

The sums are the owner's call (measured 2026-10-25): production's own transfer
plus a daily v2 run reaches or passes the 5 GB cap. `ingest.yml` does nothing
until the `INGEST_DATABASE_URL` secret exists, so adding the secret is the
moment that spending starts. The choices are the Neon Launch plan (about $5 a
month at today's size) or a slower schedule (change the cron expression in
`ingest.yml` to every second or third day). Check the current numbers before
adding the secret.

### The v2 schema

`backend/migrations/add_scraper_v2.py` is registered last in the lifespan in
`backend/main.py` and also runs from the runner. It inspects first and sends no
DDL once everything exists, takes an advisory lock (key 7311027530201) and a
`lock_timeout` of five seconds, and adds only nullable columns and indexes:

- on `scraped_jobs`: `career_family`, `career_family_secondary`,
  `remote_scope`, `enrichment_version`, `fingerprint`, `company_id`,
  `closed_reason`, `consecutive_failures`, `salary_is_estimated`, `apply_url`,
  `expires_at`;
- on `source_health`: `disabled_until`, `jobs_7d_avg`;
- six indexes on `scraped_jobs`: `ix_scraped_jobs_status_family`,
  `ix_scraped_jobs_status_level`, `ix_scraped_jobs_status_geo`,
  `ix_scraped_jobs_company_id`, `ix_scraped_jobs_status_probed`,
  `ix_scraped_jobs_fingerprint` (not unique: uniqueness stays on `url` and
  `external_id`);
- the tables `companies`, `scrape_runs` and `scrape_tasks`, which `create_all`
  builds.

Nothing fills `remote_scope`, `salary_is_estimated`, `expires_at` or
`scraped_jobs.consecutive_failures` yet: `ATSJob` has no field to carry them.
The first run on a database builds the six indexes, each holding a SHARE lock
(writes wait, reads continue) while it builds. A cold start that waits longer
than the `lock_timeout` fails and the next one retries, so before the first
production deploy run the migration once by hand, against that database, and
time it (with `DATABASE_URL` set to that database):

```bash
python -c "from backend.migrations.add_scraper_v2 import run_migration; run_migration()"
```

Cold starts then find everything in place.

### Reclaiming dead text

`python -m backend.scripts.reclaim_dead_descriptions` (a dry run by default)
clears `description`, `description_sections` and `change_log` on rows that have
been `removed` or `expired` for more than `--days` (30) and that no user table
references (`application_records`, `user_saved_jobs`, `tailored_resumes`,
`job_match_notifications`, `connection_requests`, `pending_questions`). The row
stays: its url, external_id, title, company, fingerprint, status and timestamps
are what stop a slow aggregator from re-ingesting the posting as new. `--apply`
clears in batches of `--batch` (2000), one transaction each, then runs a plain
`VACUUM (ANALYZE) scraped_jobs` (`--no-vacuum` skips it). A plain VACUUM makes
the space reusable by new rows without shrinking the files, so the sizes
printed afterwards stay about where they were and stop growing. On the dev
branch the first run cleared 22,557 rows (48 MB made reusable). No workflow
runs it; run it when the size gate gets close.

## Company logos

Logos are **self-hosted**: one validated, squared image per employer in the
`company_logos` table, keyed by a normalized company name (`company_key`:
markdown, `(Ashby)`-style tags, punctuation, accents and legal suffixes
folded away). Rows point at `/jobs/logo/<sha1>.png` (or `.svg`), served by
`GET /jobs/logo/{sha}.png|svg` with no auth, an immutable year-long cache
header, nosniff, and a sandboxing CSP for SVG. A re-harvest that changes the
image changes the URL, so the cache can never serve a stale logo.

- **Harvester cascade** (`harvest_company_logo`), first hit wins: an existing
  real logo URL on any row; the LinkedIn guest job page; LinkedIn guest
  search; the ATS board's own logo (Ashby, Workday, Lever, Greenhouse,
  SmartRecruiters, BambooHR, Workable); homepage icons on VERIFIED domains
  only (DNS, no unrelated redirect, not parked, company token match); Wikidata
  P154; Google s2 at 256 px. Every candidate goes through
  `logo_image.normalize_logo`: decoded, at least 64 px, not a known
  placeholder, visible on white, aspect ratio within 4:1, trimmed, padded to a
  square, 128x128 PNG. SVGs are sanitized (no script, no external refs).
- **Propagation:** a stored logo is written to every row of that company,
  visible or hidden, so one LinkedIn row's logo covers the 1,000 Workday rows
  of the same employer.
- **Misses** back off 14 days per attempt, capped at 90, and never demote a
  stored logo.
- **Where it runs:** cron-backfill Phase 3 first re-points rows of already
  stored companies (no network), then harvests companies with visible rows
  and no stored logo, busiest first: 6 at once, 45 s per company, at most
  150 companies and 150 s per run.
- **Frontend chain:** stored logo; else Google s2 (`sz=256`) only for a real
  domain (never a name guess); else a letter avatar from the cleaned name.
  Anything under 40 px or wider than 2.2:1 counts as a miss. unavatar is gone
  (25 requests per day per IP, tiny favicons).

## Ghost-job scoring

`ghost_risk_score` (0-100) + `ghost_risk_factors` are **surfaced, not
silently filtered**: the product decides hide vs badge. Factors:

| Factor | Points |
|---|---|
| Open > 45 days (`> 90` days) | +25 (+40) |
| Evergreen description ("always accepting applications", "talent pool", ...) | +25 |
| Repost pattern (same employer+title previously removed) | +20 |
| Company has ≥5 active listings and >50% open >45 days | +15 |

Scoring is incremental: new rows are scored once (the only pass that reads
descriptions; the evergreen flag is cached in the factors JSON), and aging
rows are re-scored column-only as their age factors move, least recently
scored first. "Open" counts from the source's `posted_date` when it is
plausible (after 2000 and before we first saw the row), else from our first
sighting: Lever lists postings from 2016-2021 that we first saw in 2026.

## Structured extraction

`structured_extraction.py` is **deliberately regex/taxonomy based, no model
calls**. The ingest crons touch thousands of listings per hour; per-listing
LLM calls are how the OpenAI bill melted once already. Extracted at ingest
(board rows) or when the backfill lands a description (LinkedIn and Indeed
rows):

- `salary_min/max/currency/period`: from source-structured pay fields
  (Greenhouse `pay_input_ranges`, Lever `salaryRange`, Ashby
  `compensationTierSummary`) or description text; magnitude sanity checks
  reject years/metrics masquerading as pay
- `employment_type`: source commitment field wins, then title, then text
- `visa_sponsorship`: `yes`/`no` only on explicit statements (negative
  patterns checked first); silence stays `unknown`
- `skills`: curated ~150-term taxonomy, word-boundary matched, capped at 20;
  ambiguous single tokens (`r`, `go`, `ui`) only count in titles
- `raw_hash`: whitespace-insensitive content fingerprint

## Change detection (bait-and-switch)

Re-crawls diff stored rows against fresh board data: title/location/salary
changes and description-hash changes append to a capped `change_log` and bump
`edit_count`. A posting whose salary statement disappears gets one
`salary_removed` entry (and its stored pay is cleared): an edit-frequency/
trust signal the UI can surface. Whitespace padding is not an edit, the
description hash is taken over the sanitized text exactly as at insert, and a
crawler row's company follows its registry name without a change entry. A
Workday "N Locations" count never overwrites a stored place name.

## Deduplication

1. **URL identity:** `scraped_jobs.url` is UNIQUE; `canonical_url()` strips
   `utm_*` parameters and the lists' own `ref=` attribution (functional params
   like `gh_jid` survive). Rows stored before that existed (`?utm_source=vansh`
   copies) are collapsed onto the clean URL by `scripts/cleanup_feed.py` phase
   a.
2. **Stable external id:** board rows carry
   `external_id = {platform}:{slug}:{source's own id}`, so re-crawls update in
   place even if the apply URL changes shape.
3. **Cross-source twins:** exact match on normalized employer + normalized
   title + city containment; the highest-trust copy wins, losers get
   `duplicate_of` (soft-hidden, never deleted). Direct rows never merge with
   each other: identical titles on one board are distinct requisitions. A
   hidden twin's real logo is inherited by a winner whose logo is generated.
4. **Fuzzy fallback:** aggregator rows whose *normalized* title is
   near-identical (SequenceMatcher ≥ 0.93, small length gap) to a direct row's
   may be absorbed. Deliberately not embeddings: deterministic, free, and a
   wrong merge hides a real job. A qualifier word ("... Infrastructure") blocks
   the merge by design.
5. **Fingerprint:** every new board row also gets a `fingerprint`, the sha256
   of the company domain (else the folded name), the normalized title, the
   sorted normalized places and the employment type. It is a match key for one
   posting across sources, never a unique index, because one employer posts
   identical titles as separate requisitions (DECISIONS 9). Uniqueness stays on
   `url` and `external_id`.

## GitHub-list sources (cron-poll)

The parser (`markdown_parser.py`) reads pipe tables, several tables per
README and HTML `<table>` READMEs; takes the OUTER link of a badge-wrapped
link (never the shields.io image); strips markdown/HTML emphasis and legend
emoji from company and title cells (`**Tesla**` rendered a `*` avatar); and
gives a year-less date ("Nov 30") the most recent such day not more than 2
days in the future (the current year made 2025 postings into Oct-Dec 2026
ones that topped the date-sorted feed). Closed rows (lock, strikethrough) are
never inserted. Renamed repos are followed (the source's owner/name update in
place); only 404/410/451 park a source in `error`, other failures keep it
`active`, and retryable errors are retried after a 12-hour cooldown
(`sources_due`).

Cadence (`sources_due`, `_due_at`): a list whose file committed in the last
7 days is due hourly, a quieter one daily. A list with nothing to ingest
(every row links to a list vendor such as jobright.ai or zapply.jobs, or the
README has no job table) and no visible rows is `parked`: re-checked weekly,
and only with the slots productive lists leave free. cron-poll polls up to
`CRON_POLL_MAX_SOURCES` (12) due sources, most overdue first. The stored
commit is stamped with `PARSE_REVISION` (`<sha>@r1`): bump it when the parse
or ingest rules change and every list is re-parsed once, since a README is
otherwise only re-read when its commit changes. A list due only for the
revision, with nothing in the feed, waits behind every other due list (a
never-polled one still goes first): after the r1 bump the ~33 vendor lists
held the productive lists back for 12-15 h. An interval other than hourly
or daily set through `PUT /github-sources/{id}` sticks.

One source per file: speedyapply keeps new-grad and international roles in
`NEW_GRAD_USA.md`, `INTERN_INTL.md` and `NEW_GRAD_INTL.md` beside its README.
A `file_path` in `REPOS` makes a source at `<repo>/blob/HEAD/<file>` whose
commit check only counts commits touching that file; a `countries` allowlist
(the `*_INTL.md` files read for `CA` only) drops everything else, before the
new-URL liveness probe spends its budget on it. An admin-set poll interval
must be 5 to 10,080 minutes.

List rows are dropped at ingest, and hidden on every run
(`hide_list_copies_of_board_rows`), when the board crawl carries the same
posting under another URL spelling: a precise `posting_identity` matches a
visible board row whatever the title or employer spelling ("... - College
Grad" beside "... (College Grad)"); any other URL, and a `removed` board row,
still needs the same employer and title. Identities are the Greenhouse job
id, Workday tenant + requisition across
locale, site alias, `-1` repost and `/apply` variants, Lever/Ashby UUID; a
`-N` goes only when a requisition of 4+ digits, not a bare year, is left).
Only a visible board row, or one its board `removed`, stands in for a list
copy, a visible one before a removed sibling; an `expired` or `off_target`
one does not (the crawler's level and location filters are not the list's;
`off_target` still stands in for LinkedIn/Indeed twins). A hidden copy comes
back (`release_list_copies_of_lapsed_board_rows`) once its board row stops
standing in. `canonical_url` drops `utm_*` and `ref=` and keeps every other
query segment byte for byte. Legend marks leave titles (`🛂`/`🇺🇸` set
`visa_sponsorship='no'`, which cron-backfill keeps when a description
lands), and an escaped `\|` stays inside its cell.

One posting on two lists, or twice on one (speedyapply lists one Workday
requisition under several site aliases, and repeats negarprh postings
under another path case, title or employer name), is one card: a precise
`posting_identity` (Workday, Greenhouse, Lever, Ashby) matches whatever
the title or employer spelling, any other URL needs the same employer and
title (`list_row_key`). A list row whose posting another list row in the
feed carries is not stored, and cron-poll hides the rest
(`hide_repeated_list_rows`: under the board row a repeat is already hidden
behind, else the oldest list row); `release_repeated_list_rows` gives a
repeat back when that row leaves the feed. List rows with a plainly senior
title (`ats_scraper.HARD_SENIOR`: "Engineer I -II -III", "Level 4") are not
stored, unless the title also names the student role itself
(`ats_scraper.STUDENT_ROLE_TITLE`: "Office of General Counsel Intern",
"Software Engineer Intern - Sr. Design"). Naming the program is not enough:
"Head of Internships", "Senior Manager, Early Careers" and "Principal
Engineer (New Grad Mentor)" stay out.

cron-poll time boxes, from the start of the request (the workflow's curl and
Vercel both stop at 300 s): no new source after 120 s, a source still
polling at 200 s is cut off, no description fetch after 170 s, and the
match-alert sweep is cut off at 240 s. A cut only lands at an await, so
synchronous work runs past it: a source's insert loop (~20 s for a 375-row
file) and the rest of that poll (past 170 s no description fetch is left to
cut it at), the column-only list dedup passes, and, once the sweep has stopped
scoring (`llm_unavailable`, or its scoring budget spent), every remaining
user's queries and email sends (the Resend SDK's 30 s timeout each). At
today's scale (5 eligible users) the worst run ends around 225-265 s. The
sweep runs whether or not any list was due. The response carries per-source
`seconds` and phase `timings`.

## Per-board health and the circuit breaker

Every board outcome lands in `source_health`. Five consecutive failures open
the breaker: the board is skipped for 24 h, then retried. `disabled_until` is
stamped when the breaker opens and honoured by the cooldown check, so setting
it to a future time also parks a board by hand. The runner counts a board that
fails all 3 of its attempts in a run as one failure.

v2 adds a yield check. Each complete listing moves the board's `jobs_7d_avg`,
an average weighted by the time since the last crawl (weight 1 - e^(-days/7)),
so it spans about a week however often the board is crawled. A complete listing
more than 60% below that average, once the average is at least 5, is a **yield
warning**. It goes to `source_health.last_error` (until the next success), to
the run's `notes` and log, and, when `ALERT_WEBHOOK_URL` is set, to that URL as
a Slack-style `{"text": ...}` POST. A partial listing says nothing about yield.

`GET /jobs/ingest-metrics` (cron-secret) is the dead-letter view (failing
boards with their last error) plus the day-one metrics: listings ingested/24 h
and 7 d, removed/24 h, dedup rate, % ghost-flagged, median active listing age,
active-by-trust. The `scrape-jobs.yml` workflow logs it every run. It does not
hold the v2 report's numbers; `python -m backend.ingest.report` does.

## Schedules

GitHub Actions is the clock. Vercel crons are off the table: on the Hobby plan
they run at most once a day, and a more frequent expression fails the deploy.
Three workflows are scheduled:

| Workflow | When | Does |
|---|---|---|
| `scrape-jobs.yml` | Minutes 17 and 47 of every hour, plus manual | The light work: the LinkedIn and JobSpy scrapers, then the `cron-ats`, `cron-poll`, `cron-backfill` and `cron-freshness` endpoints on Vercel, then a metrics log |
| `ingest.yml` | Once a day at 06:23 UTC (02:23 in Toronto and New York), plus manual | The heavy crawl: `python -m backend.ingest.run` over every enabled registry board, writing straight to Neon |
| `ingest-network-tests.yml` | Nightly at 03:41 UTC, plus manual | The tests marked `network`, against live boards, on SQLite |

`ci.yml` runs on every push to `main` and every pull request.

### scrape-jobs.yml: hourly, light work

`.github/workflows/scrape-jobs.yml`, cron `17 * * * *` and `47 * * * *`.
GitHub's scheduler is best-effort: from 2026-08-27 the single minute-17 entry
delivered ~5.7 runs a day (23 before), so a second entry doubles the chances
and every budget is sized for irregular firing (least-recently-crawled
shard, least-recently-probed verification). A `concurrency` group
(`cancel-in-progress: false`) keeps two deliveries from ever running at once.

1. **Inactivity check:** reads the newest commit on the default branch and
   fails from day 50 of GitHub's 60-day inactivity clock (see "The 60-day
   inactivity rule" below). Read-only, never re-enables anything.
2. JobSpy + LinkedIn scripts: `/jobs/ingest-batch` (low trust). These stay
   `continue-on-error`: blocked scrapers are routine.
3. `/github-sources/cron-ats`: the least-recently-crawled **shard** of the
   registry; ingests, confirms and reconciles (see "The cut-over between
   cron-ats and the runner").
4. `/github-sources/cron-poll`: GitHub lists.
5. `/jobs/cron-backfill`: descriptions, locations, logos (Phase 3), twin
   absorption, extraction-on-description-arrival, plus two bounded DB-only
   repairs: `repair_country` (rows whose stored country contradicts their
   location) and `legacy_smartrecruiters` (rows still on
   `careers.smartrecruiters.com/<Company>/<id>`, which now redirects to the
   employer's homepage, move to the `jobs.smartrecruiters.com` posting URL, or
   hide behind the board row that already holds it; a list or LinkedIn/Indeed
   copy holding it leaves the legacy row where it is).
6. `/jobs/cron-freshness`: board_key adoption, stale/aggregator/terminal
   sweeps, platform liveness verification, ghost scoring.
7. `/jobs/ingest-metrics`: logged snapshot, runs even after a failure.
8. A final step fails the run if the inactivity check or any endpoint step
   failed.

Every endpoint call prints its HTTP status, time and response body and fails
its step on anything but 2xx; the steps are `continue-on-error` so the rest of
the run still happens, and the final step turns the run red. (Before this,
every curl ended in `|| true` and a 401 or 500 showed green.) `--max-time` is
300 s on the four crons, Vercel's function limit: cron-ats pages for at most
150 s plus processing, cron-backfill plans its whole pass into
`BACKFILL_BUDGET_S` (240 s: up to 75 s of descriptions, then a logo harvest
of what is left, at most 150 s; a fetch the budget cuts off costs the row no
attempt), cron-freshness runs its sweeps plus a 150 s verification
box. A curl timeout at 300 s means the function itself hit Vercel's ceiling.
The job has a 40-minute timeout so a hung run can't hold the concurrency
group.

The job token is `contents: read` and nothing more. Its only users are
`actions/checkout` (with `persist-credentials: false`, so the token is not
left in `.git/config`) and the inactivity check (two REST reads). The
scrapers and the endpoint steps authenticate to Tailrd with `CRON_SECRET`,
never with the GitHub token. Because the scripts run with `CRON_SECRET` in
their env, `python-jobspy` and `httpx` are pinned to exact versions (the
ones the job resolved in 2026-09); bump them deliberately. Their transitive
dependencies still float within python-jobspy's own ranges; a hashed
requirements file (`pip install --require-hashes`) is the next step if that
ever matters.

### ingest.yml: daily, heavy

`.github/workflows/ingest.yml` runs the v2 runner once a day (cron
`23 6 * * *`) and on `workflow_dispatch`. A job there gets hours and, in a
public repository, free minutes, where a Vercel function gets 300 s. It runs
`python -m backend.ingest.run --no-schema --trigger <cron|manual> --budget-minutes 170`
(the trigger is `cron` for the schedule and `manual` otherwise), with a
200-minute job timeout, 30 minutes above the budget: anything longer is hung.
It has a concurrency group of its own (`ingest`, `cancel-in-progress: false`),
and the runner's advisory lock stops a run started some other way. A day on
which GitHub drops the fire costs nothing lasting: with `INGEST_V2_RUNNER` on,
`cron-ats` resumes crawling a board 20 hours after the runner last did.

It holds a database credential in a public repository, so it is narrower than
`scrape-jobs.yml` on purpose:

- It is a **separate workflow**, so `scrape-jobs.yml`, which installs
  `python-jobspy` from PyPI, never sees the credential.
- The job token is `contents: read`, and actions are pinned by commit SHA.
- It installs `requirements-ingest.txt` and nothing else, with
  `pip install --require-hashes --only-binary=:all:`, so a new PyPI release
  cannot reach it unreviewed and no package's build code runs. Never add
  `python-jobspy` to that file. The file lists the runner's import closure
  only, and `backend/tests/test_ingest_workflows.py` fails when the runner
  starts importing a package that is not in it. The header of the file says how
  to regenerate it with `uv pip compile --generate-hashes`.
- The credential reaches only the one step that runs the ingest.

**The secret.** `INGEST_DATABASE_URL` is the connection string the runner uses
as `DATABASE_URL`; the workflow derives `DATABASE_URL_DIRECT` from it for the
run lock by dropping `-pooler` from the host. **While the secret is empty the
workflow ends green with a notice and does nothing**: every step after the
check is skipped, so it never runs red for want of setup. The 60-day check
still runs. These optional secrets are passed to the runner, and empty means
the adapter or the webhook stays off: `USAJOBS_API_KEY`, `USAJOBS_USER_AGENT`,
`ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `JOOBLE_API_KEY`, `ALERT_WEBHOOK_URL`. The
repository variable `CRON_ATS_RETIRE_OFF_TARGET` is the `off_target` kill
switch for the runner (the Vercel variable of that name covers `cron-ats`
only); empty means on.

**The role.** The credential is the connection string of a dedicated Neon role
that can SELECT, INSERT and UPDATE the ingestion tables, DELETE only from
`scrape_tasks`, and run no DDL. That is why the workflow starts the runner with
`--no-schema`: by default the runner creates and migrates the schema at start,
and several boot migrations send `CREATE INDEX IF NOT EXISTS` on every start,
which Postgres refuses to a role that does not own the table even when the
index exists ("must be owner of table cover_letters"). The app applies the
schema when it deploys, so create the role after the v2 code is deployed (the
app's first cold start creates `companies`, `scrape_runs` and `scrape_tasks`).
Run this as the database owner on the production branch. Check the five
sequence names first; the query returns `public.<table>_id_seq` for each:

```sql
SELECT pg_get_serial_sequence(t, 'id')
FROM unnest(ARRAY['companies','scrape_runs','scrape_tasks','scraped_jobs','source_health']) AS t;
```

```sql
CREATE ROLE ingest LOGIN PASSWORD '<a long random password>' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT USAGE ON SCHEMA public TO ingest;
GRANT SELECT ON companies, company_logos, scrape_runs, scrape_tasks, scraped_jobs, source_health TO ingest;
GRANT INSERT, UPDATE ON companies, scrape_runs, scrape_tasks, scraped_jobs, source_health TO ingest;
GRANT DELETE ON scrape_tasks TO ingest;
GRANT USAGE ON SEQUENCE companies_id_seq, scrape_runs_id_seq, scrape_tasks_id_seq,
                        scraped_jobs_id_seq, source_health_id_seq TO ingest;
```

The secret is then
`postgresql://ingest:<password>@<neon host>/<database>?sslmode=require`; a
pooled host is fine. The grants were derived by logging every statement of a
runner run against a local `postgres:17`, then running the runner as this role
with the schema step skipped and forcing every write: no `permission denied`
appeared. `company_logos` is read only (the logo harvest runs in
`cron-backfill` on Vercel), and nothing deletes from `scraped_jobs` (the runner
closes rows, it never deletes them). The role has been tested on a local
container and never on Neon, and a table the runner starts to use later needs
its own grant.

Two cautions. Repository secrets are readable by any workflow that a
collaborator with write access runs from a branch (`workflow_dispatch` works
from any branch), so if the repository ever has more writers than the owner,
store the secret as an environment secret restricted to `main` and add
`environment:` to the job. And `workflow_dispatch` has no inputs, so the first
real run is a full crawl (hours, up to 15,000 inserts): try `--dry-run` or
`--limit` by hand first, or lower `max_new_rows_per_run`.

### Tests in CI

- **`ci.yml`, `backend` job:**
  `python -m pytest backend/tests -q -m "not network"` on SQLite, Python 3.13.
  The tests that call live boards are deselected, because a pull request must
  not depend on a board being up.
- **`ci.yml`, `backend-postgres` job:** a throwaway `postgres:17` service
  container and `INGEST_TEST_POSTGRES_URL`, running the ingest tests only a
  real Postgres can exercise (`FOR UPDATE SKIP LOCKED` claims, the run's
  advisory lock, JSON null versus SQL NULL, VACUUM):
  `backend/tests/ingest/test_queue.py`, `test_reclaim.py` and `test_run.py`, on
  Python 3.12. A test file that starts reading `INGEST_TEST_POSTGRES_URL` must
  be added to that job's list
  (`test_ci_runs_every_test_that_waits_for_the_postgres_url` fails until it
  is), and the job fails if a Postgres-only test was skipped.
- **`ingest-network-tests.yml`:** nightly at 03:41 UTC, ahead of the day's
  `ingest.yml` fire (06:23) so the two never page the same boards at once. It
  runs `python -m pytest backend/tests -m network -q -rs` with
  `RUN_NETWORK_TESTS=1`, on SQLite and with no database credential. The
  optional adapter keys are its only secrets; a test whose key is missing
  skips, and `-rs` says why. A board that changed its API, or an adapter that
  stopped listing jobs, turns the scheduled run red within a day.

### The cut-over between cron-ats and the runner

Both crawl the same registry, and they must not crawl one board twice in a
cycle. The switch is the Vercel environment variable `INGEST_V2_RUNNER`:

- **Off (the default):** `cron-ats` crawls its shard as it always has.
- **On** (`1`, `true`, `yes` or `on`): `cron-ats` leaves every board whose
  `source_health.last_success_at` is under 20 hours old
  (`V2_RUNNER_FRESH_HOURS`) to the runner and counts it in `boards_skipped_v2`,
  a key its response carries only while the flag is on. A response that skipped
  its whole shard is the cut-over working, not a fault. A board the runner
  missed, or a day it did not run at all, is picked up by `cron-ats` once 20
  hours have passed, so `cron-ats` is the backstop.

Both write `last_success_at`, which is what makes the stamp work. Turn the flag
on after `ingest.yml` has completed a run; before that it only slows `cron-ats`
to about one crawl of a board every 20 hours. A changed Vercel variable reaches
the functions with the next deployment, and no workflow sets it. Twenty hours
against a daily run leaves a window of about four hours before each 06:23 fire
in which `cron-ats` may crawl a board the runner is about to crawl again; a
longer `V2_RUNNER_FRESH_HOURS` would shrink it at the cost of a slower fallback
when the runner is down.

### The 60-day inactivity rule

GitHub disables scheduled workflows in a public repository after 60 days
without repository activity (in practice, without a commit), and it does so
silently: the runs just stop. That is how the external scraper's "Hourly Job
Scraper" died on 2026-08-26, 60 days after its last commit. This repository
is public, so the same clock runs on every scheduled workflow here
(`scrape-jobs.yml`, `ingest.yml`, `ingest-network-tests.yml`), and every push
to the default branch resets it.

- **The detector.** The workflow's first step reads the newest commit on the
  default branch (`gh api`, `contents: read`) and fails once it is 50 days
  old; the final step then turns the run red. A failed scheduled run emails
  the user who last changed the workflow's `schedule` (or whoever last
  re-enabled the workflow), provided their Actions notifications are on.
  That leaves about 10 days, and the error names the day the schedule turns
  off. The response is either to push any commit to the default branch
  (another 60 days) or to make the durable fix below. The check uses the
  committer date, which is at or before the push, so it can fire early but
  never late. `ingest.yml` carries the identical step (a test holds the two
  the same) and runs it even while that workflow waits for its secret;
  `ingest-network-tests.yml` has none, so it relies on those two.
- **No automated keepalive.** An earlier draft re-enabled the workflow on
  every run (`PUT .../actions/workflows/scrape-jobs.yml/enable`). It was
  removed: nobody has confirmed that re-enabling resets the timer
  (efrecon/gh-action-keepalive found that toggling does not), GitHub has
  blocked the best-known keepalive action's repository on ToS grounds, and
  the call needed `actions: write` on a job that runs third-party PyPI code.
  Don't replace it with automated dummy commits either.
- **A disabled workflow can't alert about itself.** The detector only speaks
  while the workflow still runs. For an alarm that survives the workflow
  being off, point an external uptime monitor at `GET /jobs/ingest-metrics`
  (header `x-cron-secret`) and alert when `ingested_24h` is 0.
- **Already disabled?** Actions tab, "Scrape Jobs" (or "Ingest"), "Enable
  workflow", then push a commit.

The durable fix is to stop depending on GitHub's scheduler:

1. **An external cron calling the endpoints.** A scheduler such as
   cron-job.org POSTs `/github-sources/cron-ats`, `/github-sources/cron-poll`,
   `/jobs/cron-backfill` and `/jobs/cron-freshness` with the `x-cron-secret`
   header, one at a time. Its request timeout has to cover the up to 300 s
   each can take. This doesn't run the JobSpy and LinkedIn scripts or the v2
   runner (they need a Python runner), so on its own it drops the low-trust
   LinkedIn and Indeed rows and the daily full crawl.
2. **An external cron dispatching this workflow.** A scheduler calls
   `POST /repos/{owner}/{repo}/actions/workflows/scrape-jobs.yml/dispatches`
   with `{"ref": "main"}` and a fine-grained token limited to this
   repository with Actions: write (the workflow already has
   `workflow_dispatch`). Every step keeps running, scrapers included. Once
   the dispatches arrive, delete the `schedule:` block: the 60-day rule is
   about scheduled workflows, so a dispatch-only workflow has no clock to
   run out. Do the same for `ingest.yml`.

### Environment variables (all optional)

On Vercel (the endpoints):

| Variable | Default | Effect |
|---|---|---|
| `CRON_ATS_SHARDS` | sized to ~150 boards/run | Number of registry shards |
| `CRON_ATS_LIST_BUDGET_SECONDS` | 150 | Wall-clock budget for paging big boards past their head pages |
| `CRON_ATS_INSERT_BUDGET_SECONDS` | 220 | Wall-clock budget for inserting new rows; refreshing and reconciling go on after it |
| `CRON_ATS_CONCURRENCY` | 6 | Boards crawled at once (never two per API host) |
| `WORKDAY_MAX_PAGES` | 100 | Workday list pages (20 postings each) per board; do not pin to 8 |
| `SMARTRECRUITERS_MAX_PAGES` | 20 | SmartRecruiters list pages (100 postings each) per board |
| `ATS_PER_HOST_INTERVAL` | 0.35 | Seconds between requests to one API host (the five older connectors) |
| `CRON_ATS_RETIRE_OFF_TARGET` | on | `0`/`false`/`no`/`off` stops retiring rows to `off_target` and restores them (see the lifecycle section); needs a redeploy |
| `INGEST_V2_RUNNER` | off | The cut-over: `cron-ats` leaves boards the runner crawled in the last 20 hours to it |
| `USAJOBS_API_KEY`, `USAJOBS_USER_AGENT`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `JOOBLE_API_KEY` | unset | Switch the keyed adapters on; `.env.example` lists them |

For the runner (a `.env` locally, secrets in `ingest.yml`):

| Variable | Default | Effect |
|---|---|---|
| `DATABASE_URL` | local SQLite file | The database the run writes to |
| `DATABASE_URL_DIRECT` | `DATABASE_URL` | The non-pooled host, for the run's session lock |
| `ALERT_WEBHOOK_URL` | unset | Receives the run's yield warnings |
| the adapter keys above | unset | As on Vercel |

## Scripts

The database scripts take `DATABASE_URL` from the environment (set it
explicitly for the run, so a stray `.env` cannot aim a write at the wrong
database), read column-only and are idempotent. Try them on a dev branch first.

- **`backend/scripts/cleanup_feed.py`** brings the catalogue, in one pass, to
  the state the lifecycle code converges to over days of cron runs:
  - phase a: GitHub-list data fixes (year-less dates stamped with the wrong
    year go back a year, markdown stripped from company/title through the
    parser's own clean-up, `utm_*` URL twins get `duplicate_of` on the utm
    copy);
  - phase b: the cron-freshness sweeps, called from `listing_freshness`;
  - phase c: every still-visible row checked with `check_listings` and the
    verdicts applied with `record_liveness`;
  - phase d: report of what stays visible with verdict `unknown`, by host.

  It is a **dry run by default**: no writes and no migrations, enforced by a
  statement guard (only SELECT/SHOW get through) and by the database itself
  (`SET TRANSACTION READ ONLY` on Postgres, `PRAGMA query_only` on SQLite).
  Phase c still makes its outbound HTTP checks so the report shows real
  verdicts. Typical use:

  ```
  # representative read-only projection on prod
  DATABASE_URL=... python backend/scripts/cleanup_feed.py --limit 300 --sample
  # the real thing (runs the idempotent last_probed_at migration first)
  DATABASE_URL=... python backend/scripts/cleanup_feed.py --apply
  # only Workday rows, or only some phases
  DATABASE_URL=... python backend/scripts/cleanup_feed.py --apply --phase c,d --host-filter myworkdayjobs.com
  # resume an interrupted apply without re-checking what it already did
  DATABASE_URL=... python backend/scripts/cleanup_feed.py --apply --phase c,d --recheck-hours 6
  ```

  Options: `--phase`, `--limit`, `--sample`/`--seed`, `--host-filter`,
  `--concurrency` (8), `--deadline-minutes` (90), `--recheck-hours`,
  `--no-migrate`. Deploy the wave-1/2 code first: the script uses the same
  `listing_freshness` / `platform_liveness` the crons use.
- `backend/scripts/dedup_jobs.py`: whole-catalogue cross-source dedup sweep.
- `backend/scripts/harvest_logos.py`: legacy exhaustive favicon harvest
  (superseded by cron-backfill Phase 3 and the logo store).
- `backend/scripts/backfill_descriptions.py`, `backfill_locations.py`:
  description and location repair.
- **`backend/scripts/reclaim_dead_descriptions.py`**
  (`python -m backend.scripts.reclaim_dead_descriptions [--apply] [--days 30] [--batch 2000] [--no-vacuum]`)
  clears the text of postings closed long ago. See "Reclaiming dead text".
- **`backend/scripts/backfill_v2_tags.py`**
  (`python -m backend.scripts.backfill_v2_tags [--apply] [--batch 1000]`, a dry
  run by default) tags the rows that predate v2. It takes rows whose
  `listing_status` is active, stale or off_target and whose
  `enrichment_version` is NULL, and gives each `career_family`,
  `career_family_secondary` and `enrichment_version` from its title. ATS rows
  (`source_platform = 'ats'`) also get `experience_level` recomputed with
  `classify_experience`, with one limit: a row stored as `internship` or
  `new_grad` moves at most to `entry`. Those rows passed the old entry-level
  filter, perhaps on a department or a commitment this script cannot see, so a
  title that says mid or above is no proof that they left the student feed.
  They keep their label (the run prints how many), and the next crawl, which
  has the department, derives the level (DECISIONS 13). GitHub-list and
  LinkedIn/Indeed rows keep their level, and closed rows are left alone. It
  reads a few short columns and never the description, writes one UPDATE per
  set of rows that share the same new tags, and repeats
  `enrichment_version IS NULL` in the UPDATE so a crawl that tagged a row
  meanwhile keeps its fresher tags. It prints the target `database: host/name`
  before touching anything, then the counts per family and per level. Run it
  again any time: the second run sees only untagged rows.
- **`scripts/ats_detect.py`**
  (`python scripts/ats_detect.py <careers url> [--html FILE] [--name NAME] [--no-fetch]`)
  finds the ATS behind a careers page and prints a registry entry. See "Adding
  a company".

## Adding a source

A new platform is an adapter: one module in `backend/ingest/adapters/`. The
five older connectors stay in `ats_scraper.py`; do not add to them. The
README's Ingestion section has the short recipe. This is the contract and the
reasons behind it.

1. **Find the public endpoint.** Prefer the platform's JSON or RSS feed over
   HTML, and HTML over anything else. Check the endpoint's current shape with a
   live request before coding against memory, and read the host's `robots.txt`.
   Spend no more than about 45 minutes on a source. One that blocks us (a bot
   wall, `Disallow: /`, a login) is disabled and its reason recorded, never
   worked around.
2. **Write the module.** The registry in `backend/ingest/adapters/__init__.py`
   loads every module in the package whose name does not start with an
   underscore and that defines a non-empty `PLATFORM` and a callable
   `list_jobs`. Nothing else registers it. A module that fails to import is
   logged and skipped and never breaks anything else. A leading underscore
   marks a shared helper.

   | Name | Meaning |
   |---|---|
   | `PLATFORM` | The registry's `ats_platform` value |
   | `TIER` | 1 to 4 (see "Sources and trust") |
   | `COUNTRIES` | Tuple of the countries it covers |
   | `RATE_LIMIT` | Seconds between requests to one host: 1.0 at least, more where the site asks |
   | `REQUIRED_ENV` | Tuple of env var names; the adapter is disabled while any is unset or blank |
   | `HOST_PATTERNS` | Tuple of regexes (strings or compiled) for the posting URLs it owns, matched against the whole URL and the bare host |
   | `async list_jobs(client, slug, company_name, *, config, deadline=None)` | Returns `(jobs, complete, total)`: every listing of the board, unfiltered (all levels, all countries), whether the list is the whole board, and the total the source reported |
   | `async fetch_job(client, slug, ref, *, config)` | Optional. Returns `{"description": str, "employment_type": str}`, plus `apply_url` when the page gives the employer's link. The pipeline calls it for new rows whose list had no description, with `ref` set to `ATSJob.detail_ref` (or the URL) |
   | `async check_alive(client, url)` | Optional. Returns a `LivenessResult` from `backend.services.platform_liveness` |

   `ATSScraper.scrape_board()` sends a platform that is not one of the five
   older ones to its adapter, passing `config` from
   `company_registry.load_board_config()` and the crawl's `deadline` (a
   `time.monotonic()` instant). A platform with no adapter, or a disabled one,
   gets the usual empty incomplete snapshot and `list_jobs` is never called.
3. **Follow the rules.**
   - **HTTP.** Send every request through
     `backend.ingest.core.http.request(client, method, url, rate_limit=RATE_LIMIT, ...)`,
     never `client.get`. It sets the `TailrdBot/2.0` user agent on every
     request (also on a client that did not come from `make_client`), paces
     each host for the whole process (shared by every client and adapter),
     retries 429, 5xx and timeouts with jittered exponential backoff (a
     `Retry-After` over 60 s fails the request at once), and returns every
     other status for you to judge. For HTML, ask `http.robots_allowed` first.
   - **Rows.** `list_jobs` returns `ATSJob`s (`backend.services.ats_scraper`),
     unfiltered: the scraper applies its location filter afterwards, and
     `BoardSnapshot.all_urls` keeps the URL of every listing, filtered out or
     not, so reconciliation never mistakes "filtered out" for "taken down".
     Fill `salary_text` and `employment_type` when the source structures them.
     `external_id` is `"{PLATFORM}:{slug}:{source id}"`, built whole. `url` is
     the public posting URL without tracking parameters. A posting open in
     several places stays one job: every distinct place goes in `location`,
     primary first, joined with `"; "` (the location parser splits on `;`).
     `posted_date` is timezone-aware UTC, and `description` is plain text
     through `clean_html`.
   - **`complete`.** True only when you are sure the list is the whole board,
     because a complete listing removes every stored row it does not contain.
     It is False when the source gave no total, the list is shorter than the
     reported total, a page cap or the `deadline` stopped the walk, an entry
     could not be read, or an id repeated across pages (a list that shifts
     while you page). Never report a search that found nothing as complete.
   - **Failure.** A broken board (an HTTP error, a payload of the wrong shape,
     a redirect to the vendor's home page) raises, so `source_health` records a
     failure. Never return an empty list for it.
   - **Secrets.** A key that sits in a URL must not reach a log or an exception
     message (`httpx.HTTPStatusError` carries the full URL): catch it and
     re-raise without the URL. `pipeline.safe_error` masks every `REQUIRED_ENV`
     value as a second line of defence.
   - **`check_alive`.** Answer `alive`, `dead` or `unknown`, and mark
     `authoritative` only an answer from the platform itself. A transport
     error, a bot wall (401, 403, 429, 999) or anything unclear is `unknown`,
     never `dead`. A wrong `dead` closes live postings.
   - **Dependencies.** Keep `backend/requirements.txt` as it is: the Vercel
     bundle is near its size limit (PyMuPDF 2.0 alone took production down on
     2026-10-24). A package the adapter needs goes in `requirements-ingest.txt`
     with hashes, and the CI test that checks the runner's imports will say if
     one is missing.
   - **Do not ask the registry at import time.** Adapters import from
     `backend.services.ats_scraper`, which loads the registry as it finishes
     importing. An adapter that calls `adapter_enabled()` while it is itself
     half imported would run discovery without itself.
4. **Fixtures and tests.** Save a real response, trimmed (whole postings
   dropped, long bodies cut at a closing tag, fields no adapter reads emptied),
   under `backend/tests/fixtures/ingest/`. A real title that contains an em
   dash is written into the fixture as a JSON unicode escape, because the
   repository bans the character in files. Put the tests in
   `backend/tests/ingest/`, modelled on `test_adapters_tier1.py`: its
   `served()` builds an `httpx.MockTransport` client, and its `no_pacing`
   fixture replaces `http._sleep` and resets `http._next_slot` and
   `http._robots`, so a test never waits out a `RATE_LIMIT`. Cover the parsed
   fields, `complete` both ways, a broken payload, `check_alive` alive, dead
   and unknown, and that a job the scraper's filters reject still appears in
   `all_urls`. Add one `@pytest.mark.network` test that lists at least one job
   for three real boards.
5. **Liveness.** If the platform's posting pages cannot tell live from dead (a
   single-page app, a redirect to the home page), give the adapter a
   `check_alive` over its API and a `HOST_PATTERNS` that matches only posting
   URLs, not board pages. An adapter that claims a URL replaces the generic
   page check for it, so claim only what you can judge better.
6. **Registry.** Add boards (see "Adding a company"). Board keys, freshness,
   health tracking and dedup come for free: they key off `BoardSnapshot` and
   the shared pipeline.

## Adding a company

Add an entry to `backend/data/ats_companies.json` (the fields are in "The
registry"). No code changes.

1. **Find the board.** `python scripts/ats_detect.py <careers url>` prints a
   registry entry on stdout (JSON) and its evidence on stderr; the exit code is
   0 when it found an ATS, 1 when the page shows none, 2 when the page could
   not be read. It reads the URL first: a board or posting link of Greenhouse,
   Lever, Ashby, SmartRecruiters, Workday (both hosts), Workable, Recruitee,
   Teamtailor, BambooHR, Jobvite, iCIMS or Oracle names its board. Only when
   the URL is the employer's own site does it fetch the page, once (TailrdBot
   user agent, `robots.txt` respected, one request a second, no login, no
   headless browser), and read its links, iframes and script sources for the
   same board links and for the vendors' script markers. A page you saved by
   hand can be read with `--html FILE`, and `--no-fetch` reads the URL only.
2. **Complete the entry.** The printed entry has a guessed `company_name` and
   no `hq_country` or `sector`: add both, `country` only for a single-country
   board, `careers_url`, and `config` where the platform needs it. A platform
   with no connector in the checkout, or a Workday address without its site
   name, comes out `enabled: false` with a `disabled_reason`; it turns enabled
   by itself once an adapter lands, since "supported" is computed from
   `SUPPORTED_PLATFORMS` and `adapter_platforms()`. `ats_detect` cannot read a
   bot-walled or script-rendered page.
3. **Check the board before enabling it.**
   `python -m backend.ingest.run --dry-run --boards <platform>:<slug>` logs
   what the board lists and what would be new, and writes nothing. Then crawl
   it for real against a dev branch and look at the rows.
4. **Workday.** The entry needs `workday_url_template`, and a company that is
   not on a public Workday site is filed under its real system and disabled
   with the reason (see "Workday specifics"). The registry tests run in CI, for
   instance that every enabled Workday board has a template.

## Tests

The v2 tests live in `backend/tests/ingest/` and their fixtures in
`backend/tests/fixtures/ingest/`.

| File | Covers |
|---|---|
| `test_adapters_tier1.py`, `test_adapters_tier2.py`, `test_adapters_public.py` | The adapters against saved payloads, plus the `network` tests |
| `test_adapter_registry.py`, `test_adapter_routing.py`, `test_adapter_import_order.py` | Discovery, `REQUIRED_ENV`, `HOST_PATTERNS`, and the routing in `scrape_board` and the liveness hook |
| `test_http.py` | The user agent, retries, `Retry-After`, per-host pacing, `robots.txt` |
| `test_pipeline.py`, `test_closure.py` | The shared write path: idempotency (a second ingest of the same snapshot adds 0 rows), caps, and the closure simulation (50 postings dropped from a complete fixture board are hidden and closed with `closed_reason` `delisted` by the first reconcile) |
| `test_queue.py`, `test_run.py`, `test_report.py` | The queue (claim, lease expiry, attempts), a runner end to end on SQLite with a fake adapter, the report |
| `test_taxonomy.py`, `test_taxonomy_api.py` | The accuracy gates over `labels.json`, per-rule tests, the guard on the older entry-level labels |
| `test_reclaim.py`, `test_backfill_v2_tags.py`, `test_ats_detect.py` | The scripts |
| `test_scraper_v2_migration.py` | The migration runs twice, and the second run sends no DDL |

How to run them:

- **On SQLite, always.** `pytest` enters the app's lifespan, so it runs every
  boot migration against whatever `DATABASE_URL` points at. Set
  `DATABASE_URL=sqlite:///./ci_test.db` and any `JWT_SECRET`, as CI does, and
  never aim a test at Neon. The SQLite files are relative to the working
  directory, so run from a scratch directory (with `PYTHONPATH` set to the
  repo) when another pytest may be running.
- **Network tests** carry `@pytest.mark.network` (registered in
  `pyproject.toml`) and are skipped unless `RUN_NETWORK_TESTS=1`;
  `backend/tests/ingest/conftest.py` does the skipping, so a bare local run
  never calls a live board. Run them with
  `python -m pytest backend/tests -m network -q -rs`.
- **Postgres-only tests** (`FOR UPDATE SKIP LOCKED`, the advisory lock, JSON
  null handling, VACUUM) skip unless `INGEST_TEST_POSTGRES_URL` names a scratch
  Postgres. CI gives them one; locally, a throwaway `postgres:17` container
  will do.
- **Slow setup.** The autouse `setup_db` fixture in `backend/tests/conftest.py`
  rebuilds the schema for every test (0.4 to 2.5 s each). A module that needs
  no database overrides it, as `test_taxonomy.py` does, and runs in under a
  second.

## Conduct (ToS hygiene)

- Every v2 request identifies as `TailrdBot/2.0 (+https://www.tailrd.ca)`
  (`http.USER_AGENT`), paces per host (one request a second by default, more
  where a site asks), backs off on 429 and 5xx, and respects `robots.txt` (read
  once per host per process; only a 200 is parsed). The HTML adapters stop with
  `PermissionError` when it bars the page they read. USAJOBS is the one
  exception to the user agent, because its API requires its registered email.
- Board fetches use public JSON or RSS feeds intended for consumption, or a
  platform's public pages read politely. No login-walled scraping, no bypassing
  a bot check, no headless browser. A source that blocks us is disabled and the
  reason recorded.
- Liveness checks use the same public APIs (or the public posting page), at
  most 2 requests in flight per host, and never judge a bot wall.
- LinkedIn data comes only from the guest API/JobSpy at low trust, is never
  reposted elsewhere, and always links back to the original source. Indeed is
  never probed. Whether to license that data is the owner's decision.
- `robots.txt`-sensitive HTML fetching happens only in the description backfill
  for direct company pages, with a bounded attempt count, and in the adapters
  above.
- Store job-posting data only: nothing about applicants or recruiters beyond a
  public contact email on a posting.
- The repository is public, so Actions logs and everything committed are
  public. No key, connection string or URL query string reaches a log, and no
  secret reaches a commit (`.env` is git-ignored; `.env.example` holds names
  only).

## Known limits

Unfinished or deliberately not done, as of 2026-10-26:

- **Held boards.** The Oracle, iCIMS and Jibe boards are `enabled: false` until
  their adapters' completeness checks are fixed (DECISIONS 14). Taleo and
  SuccessFactors share the review item and have no enabled boards.
- **Neon budget.** Production plus a daily run reaches the 5 GB transfer cap of
  the free plan (see "Storage and transfer"). Production has not been touched
  by v2: no merge, no migration on `main`, no ingest role or secret. The ingest
  role was only tried on a local Postgres (see "ingest.yml: daily, heavy").
  Bound the first production run by lowering `max_new_rows_per_run` in
  `limits.json`.
- **The keyed adapters** (USAJOBS, Adzuna, Jooble) have never run against a
  live server, so their first real call may differ from the documentation
  (field types, page-size ceilings, error formats). Jooble's free key is 500
  requests in total and serves one country site, which one `JOOBLE_API_KEY`
  variable cannot express for both countries. The pipeline stores every adapter
  row as `source_platform = 'ats'` with `source_trust = 'high'`, aggregators
  included: decide their trust before switching them on.
- **Coverage gaps.** Jobvite has no public feed. Taleo covers only career
  sections with a JSON search (not Business Edition or the older layout).
  SuccessFactors needs each employer's Recruiting Marketing host, and 121
  registry boards wait for it. iCIMS portals that send their search to the
  employer's own site are not covered.
- **A list that shifts while it is paged** (a posting removed between two page
  requests) is invisible to the paged adapters, unlike Oracle's changing count.
  The page after it shifts up, one live posting is skipped, and a complete
  listing could close it until the next crawl revives it (DECISIONS 5).
- **`Crawl-delay`.** `core/http.py` does not read it. Jibe and Job Bank honour
  theirs through `RATE_LIMIT`, and any other iCIMS portal or site might ask for
  more than one request a second.
- **Fields nothing fills:** `remote_scope`, `salary_is_estimated`, `expires_at`
  and `scraped_jobs.consecutive_failures`. `ATSJob` has no field for them, so
  Job Bank's "advertised until" and USAJOBS' close dates are lost.
- **Untagged writers.** `POST /github-sources/scrape-linkedin`, the legacy
  `github_scraper.py` and the admin `POST /jobs/create` still insert rows with
  no career family. `backfill_v2_tags` tags what they leave behind.
- **Family accuracy.** Rows that predate v2 get their family from the title
  alone (92.5% against 96.5% with department and description). `other` is the
  second-largest family (2,545 of 16,088 live rows on the dev branch after run
  #3), so spot-check the feed by family before launch. On the same run Legal
  (310 live) and Healthcare Administration (92) were below their volume
  targets, mostly because their boards are the held ones.
- **`description_extractor` does not know `myworkdaysite.com`,** so its
  URL-based fallback cannot read those rows. The crawl's own detail fetch is
  fine.
- **Legacy twins.** On the dev branch 115 pairs of (`board_key`, `external_id`)
  hold two rows, and 114 of them predate v2 (old and new Greenhouse board URL
  twins, Workday locale twins; 42 of those rows are visible). v2 touched one
  pair. A random sample of 500 visible rows held one cross-source twin (0.2%)
  and no posting stored twice; 42 of the 500 shared title, employer and city on
  one board, which are separate requisitions (DECISIONS 9).
- **Idempotency** was checked on 30 fully listed boards re-run about 23 hours
  after run #3: 704 known rows refreshed in place, 14 genuinely new rows
  inserted, 21 removed, none re-inserted. A second full run and liveness
  fixtures for the new adapters (3 alive and 3 dead pages per source) are not
  done.
- **Duplicated sweep sequence.** `run_sweeps` in `backend/ingest/run.py` copies
  `cron_freshness`; they are kept in step by hand.
