# Tailrd

A job aggregation and smart-apply platform that scrapes job listings from 9 jobright-ai GitHub repositories, classifies them by country/work type/role category, and provides a filterable dashboard with AI-powered resume matching.

## Features

- Aggregates jobs from 9 jobright-ai GitHub repos (8 New Grad + 1 Internship)
- Classifies jobs by country (US/Canada), work type (Remote/Hybrid/On Site), role category (17 categories)
- Daily automated polling via Vercel Cron Jobs
- Rich filtering UI (country, work type, category, experience level)
- AI-powered match scoring (Gemini/Ollama)
- Resume upload and analysis
- Job detail view with match breakdown

## Tech Stack

- **Frontend**: React + Vite + TypeScript
- **Backend**: FastAPI + SQLAlchemy
- **Database**: PostgreSQL (Neon)
- **Hosting**: Vercel (serverless)
- **AI**: Google Gemini / Ollama (local)

## Architecture

- Backend services: CountryFilter, WorkTypeClassifier, MarkdownParser, AggregatorService
- 9 GitHub repos polled daily for new job listings
- Property-based testing with Hypothesis (Python) and fast-check (TypeScript)

## Getting Started

1. Clone the repo
2. Set up environment variables (see `.env.example`)
3. Install dependencies:
   ```bash
   pip install -r backend/requirements.txt
   cd frontend && npm install
   ```
4. Run locally:
   ```bash
   uvicorn backend.main:app --reload
   cd frontend && npm run dev
   ```
5. Seed sources:
   ```
   POST /github-sources/seed
   ```
6. Poll jobs:
   ```
   GET /github-sources/cron-poll
   ```

## Deployment (Vercel)

- Push to main or run `vercel --prod`
- Set `DATABASE_URL` env var to your Neon PostgreSQL URL
- Optionally set `GITHUB_TOKEN` for higher API rate limits
- Daily cron job at `/github-sources/cron-poll` keeps jobs updated

## Ingestion

Jobs come from employers' own boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workday and the adapters in `backend/ingest/adapters/`), the curated GitHub lists, and the LinkedIn/Indeed scrapers. The v2 runner, `python -m backend.ingest.run`, crawls every enabled board in `backend/data/ats_companies.json` and keeps the catalogue fresh. [`docs/ingestion-pipeline.md`](docs/ingestion-pipeline.md) explains the whole pipeline; this section is the short version.

Merging the v2 code changes no volumes. Two switches start the spending, and both belong after the Neon plan has room (the free plan's 5 GB transfer cap and 512 MB size cap are why; see "Storage and transfer" in the pipeline doc):

- The `INGEST_DATABASE_URL` secret lets the daily workflow `ingest.yml` run the runner. Without it the workflow ends green and does nothing.
- The Vercel variable `CRON_ATS_V2_CATALOGUE=1` makes `cron-ats` store every experience level and crawl the adapters' boards. Unset (the default), `cron-ats` keeps the student catalogue, internships and new grads on the five older platforms, as before v2, and the Jobs page offers only the experience levels that have jobs.

Turn them on in this order: the secret, a green first run, `INGEST_V2_RUNNER=1`, then `CRON_ATS_V2_CATALOGUE=1`. A changed Vercel variable needs a redeploy.

### Run it locally

Put a database in the repo's `.env` (it is git-ignored). Use a Neon dev branch, never production: the runner writes, and `--dry-run` still applies the schema.

```
DATABASE_URL=postgresql://user:password@ep-example-pooler.us-east-1.aws.neon.tech/neondb
DATABASE_URL_DIRECT=postgresql://user:password@ep-example.us-east-1.aws.neon.tech/neondb
```

`DATABASE_URL_DIRECT` (the same database on the host without `-pooler`) is only for the run lock, because the pooler does not keep session locks. Without it the runner locks on `DATABASE_URL`. The keyed adapters (USAJOBS, Adzuna, Jooble) stay off until their variables from `.env.example` are set.

```bash
# What would change? Crawls the boards and writes nothing (but the idempotent schema step).
python -m backend.ingest.run --dry-run --boards greenhouse:geotab,lever:wattpad

# Crawl those two boards for real, or one platform, or its 20 least recently crawled boards.
python -m backend.ingest.run --boards greenhouse:geotab,lever:wattpad
python -m backend.ingest.run --platforms workable --limit 20

# Every enabled board: hours, and it stops starting boards after --budget-minutes.
python -m backend.ingest.run
```

| Flag | Default | Meaning |
|---|---|---|
| `--boards a:b,c` | all | Boards to crawl, `platform:slug` or a bare slug, comma separated |
| `--platforms p,q` | all | Only these platforms |
| `--limit N` | none | At most N boards, least recently crawled first |
| `--concurrency N` | 20 | Boards crawled at once |
| `--per-host N` | 2 | Boards crawled at once on one API host |
| `--budget-minutes N` | 170 | No board starts and no row is inserted after this |
| `--dry-run` | off | Log per board what is `known`, `new` and would be removed; write nothing |
| `--no-schema` | off | Skip `create_all` and the boot migrations, for a database role with no DDL privilege (`ingest.yml` uses it) |
| `--trigger T` | `manual` | Label stored in `scrape_runs.trigger` (the workflow passes `cron`) |

It prints one JSON line per board and a final summary, and exits 0 when the run completed, was skipped (another run holds the lock) or was a dry run, and 1 on failure.

### Add a source

A source is one module in `backend/ingest/adapters/`, named after the platform. Any module there that defines `PLATFORM` and `list_jobs` is picked up, and nothing else registers it (a leading underscore marks a helper). `workable.py` is a short, complete example.

```python
from backend.ingest.core import http
from backend.services.ats_scraper import ATSJob
from backend.services.platform_liveness import ALIVE, DEAD, UNKNOWN, LivenessResult

PLATFORM = "acme"                   # the ats_platform value in ats_companies.json
TIER = 1                            # 1 public boards, 2 enterprise and public sector, 3 aggregators
COUNTRIES = ("CA", "US")
RATE_LIMIT = 1.0                    # seconds between requests to one host
REQUIRED_ENV: tuple[str, ...] = ()  # the adapter stays off while any of these is unset
HOST_PATTERNS = (r"^https?://jobs\.acme\.com/[^/?#]+/j/\w+",)   # posting URLs it owns


async def list_jobs(client, slug, company_name, *, config, deadline=None):
    """Every posting on the board, all levels and countries: (jobs, complete, total)."""
    # api.acme.com is invented: the shape is what counts.
    response = await http.request(client, "GET", f"https://api.acme.com/boards/{slug}",
                                  rate_limit=RATE_LIMIT)
    response.raise_for_status()
    data = response.json()
    jobs = [ATSJob(title=p["title"], company=company_name, location=p["location"], url=p["url"],
                   external_id=f"{PLATFORM}:{slug}:{p['id']}") for p in data["postings"]]
    return jobs, len(jobs) == data["total"], data["total"]


async def fetch_job(client, slug, ref, *, config):      # optional: text the list lacks
    return {"description": "...", "employment_type": "..."}


async def check_alive(client, url):                     # optional: is one posting still open?
    return LivenessResult(ALIVE, "api_200", True)
```

Rules that matter:

- Send every request through `http.request`: it sets the `TailrdBot/2.0` user agent, paces each host and retries 429 and 5xx. Check `http.robots_allowed` before reading HTML. Never log in and never get past a bot wall.
- `list_jobs` returns `ATSJob`s unfiltered. `external_id` is `"{PLATFORM}:{slug}:{source id}"` and `url` is the public posting URL without tracking parameters. Keep `external_id` the same for the same posting: the refresh matches a listing by `url` and, when that is not stored, by `external_id` on the same board, so a posting whose URL changed (a retitle in the URL slug) keeps its row.
- `complete` is `True` only when you are sure the list is the whole board, because a complete list removes every stored row it does not contain. No reported total, a page cap, a spent `deadline` or an unreadable entry means `False`.
- A broken board (HTTP error, unexpected payload) raises. Never return an empty list for it.
- A transport error in `check_alive` is `UNKNOWN`, never `DEAD`.

Fixtures and tests:

1. Save a real, trimmed response under `backend/tests/fixtures/ingest/`.
2. Add tests in `backend/tests/ingest/`, modelled on `test_adapters_tier1.py`: `served()` builds an `httpx.MockTransport` client and `no_pacing` skips the rate-limit waits. Cover the parsed fields, `complete` both ways, a broken payload, and `check_alive` alive, dead and unknown.
3. Add one `@pytest.mark.network` test that lists at least one job for three real boards. It is skipped unless `RUN_NETWORK_TESTS=1`; the nightly workflow runs it.

```bash
export DATABASE_URL=sqlite:///./ci_test.db JWT_SECRET=x   # pytest runs the app's boot migrations: never aim it at Neon
python -m pytest backend/tests/ingest/test_adapters_tier1.py -q
RUN_NETWORK_TESTS=1 python -m pytest backend/tests -m network -q -rs
```

On PowerShell, set the two variables with `$env:DATABASE_URL = "sqlite:///./ci_test.db"; $env:JWT_SECRET = "x"`. An adapter that imports a new third-party package must add it to `requirements-ingest.txt` (hashed; its header says how to regenerate it). Leave `backend/requirements.txt` alone: the Vercel bundle is near its size limit.

### Add a company

Add an entry to the `companies` array in `backend/data/ats_companies.json`. No code change: the next run picks it up and mirrors it into the `companies` table.

```json
{
  "company_name": "Acme Health",
  "ats_platform": "workable",
  "board_slug": "acme-health",
  "enabled": true,
  "sector": "healthcare",
  "hq_country": "CA",
  "country": "CA",
  "careers_url": "https://apply.workable.com/acme-health/"
}
```

| Field | Meaning |
|---|---|
| `ats_platform`, `board_slug` | The platform and the board's token, tenant or subdomain. A built-in (`greenhouse`, `lever`, `ashby`, `smartrecruiters`, `workday`) or an adapter's `PLATFORM` |
| `hq_country` | `CA` or `US`: where the employer is based and mostly hires. Every entry has one |
| `country` | Set only when every posting on the board is in that one country (a hospital, university, school board, government, regional utility). It switches the location filter off for the board. Leave it off for a multinational: RBC's London jobs would be filed under Canada |
| `sector` | One of `agriculture_resources`, `education`, `energy_utilities`, `engineering_construction`, `finance_banking`, `government`, `healthcare`, `hospitality_food`, `insurance`, `job_board`, `legal`, `manufacturing`, `nonprofit`, `pharma_biotech`, `professional_services`, `real_estate`, `retail_consumer`, `technology`, `telecom_media`, `transportation_logistics` |
| `careers_url` | The employer's careers page |
| `config` | An object the adapter reads, for example `{"host": "...", "site": "..."}` for Oracle. Omit it when the adapter needs none. Workday entries carry `workday_url_template` instead |
| `enabled`, `disabled_reason` | `false` parks a board, and the reason says why |

The Oracle, iCIMS and Jibe boards (221 of the 1,315 enabled boards) were held until their adapters' completeness checks were fixed, and are enabled now. Boards parked for the storage budget start their `disabled_reason` with `held:`; leave those off.

To find the platform behind a careers page, run the detector. It prints a registry entry on stdout (JSON) and its evidence on stderr, and exits 0 when it found an ATS, 1 when the page shows none and 2 when the page could not be read:

```bash
python scripts/ats_detect.py https://www.acme.com/careers
python scripts/ats_detect.py https://jobs.lever.co/acme --no-fetch
python scripts/ats_detect.py https://www.acme.com/careers --html saved_page.html --name "Acme"
```

It reads the URL first and fetches the page once (`robots.txt` respected, no login, no headless browser) only when the URL names no board. The entry it prints has a guessed `company_name` and no `hq_country` or `sector`: add them. A platform without an adapter comes out `enabled: false`. Then check the board before enabling it:

```bash
python -m backend.ingest.run --dry-run --boards workable:acme-health
```

### Read the run report

```bash
python -m backend.ingest.report           # text
python -m backend.ingest.report --json    # the same numbers as JSON
```

It prints the last run (boards ok, failed and deferred, jobs seen, new and closed, what the storage caps held back, notes), the yield per source (live jobs, new in 24 hours and in 7 days), live jobs by career family, experience level, country and the top 15 regions, and the database size. "Live" means what the feed shows.

Each run is one row in `scrape_runs`, and a failing board is a row in `source_health`:

```sql
SELECT id, trigger, status, started_at, finished_at, tasks_ok, tasks_failed,
       jobs_seen, jobs_new, jobs_closed, notes
FROM scrape_runs ORDER BY id DESC LIMIT 5;

SELECT board_key, consecutive_failures, last_error, jobs_7d_avg, disabled_until
FROM source_health WHERE consecutive_failures > 0
ORDER BY consecutive_failures DESC LIMIT 20;
```

`status` is `running`, `completed`, `failed` (the error is in `notes`) or `abandoned` (a crashed run, marked by the next one), and `stats` holds the whole summary as JSON. A board that fails five runs in a row is skipped for 24 hours. A board that lists more than 60% below its 7-day average gets a yield warning in `notes` (and at `ALERT_WEBHOOK_URL`, if set).
