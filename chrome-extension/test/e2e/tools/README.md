# e2e review tools

Small scripts for the live-page loop: run real postings through the packaged
extension (`test/e2e/run.mjs`), read every write, fix what is wrong, pin what
is right, and check nothing else moved. Run them from `chrome-extension/`.
Results land in `test/e2e/results/` (git-ignored, kept on this machine).

| script | what it does |
| --- | --- |
| `review.cjs <run.json> [id]` | Every field the extension touched, as the extension saw it: category, label, outcome, value, options. Start here. |
| `fails.cjs <run.json> [id]` | Full keys of every failing check and unexpected write, for fixing or pinning. |
| `rundiff.cjs <old.json> <new.json> [id]` | Per page, the fields whose value changed between two runs. |
| `rescore.mjs <run.json> [id]` | Re-score a saved run against the CURRENT pins, no browser. Use after editing pins. |
| `pins-from-run.cjs <run.json> ... > pins.json` | Draft pins from a reviewed run: changed values, plus questions that must stay blank. Review before writing. |
| `write-pins.cjs <cases.mjs> <pins.json> [notes.json]` | Write drafted pins into a case file (cases with no `expect:` yet). |
| `regress.mjs [--prefix regress] [--only id] [--size 13]` | The whole pinned suite in batches (about 2.5 h headful), each SmartRecruiters case in a browser of its own (its bot check lets one page through per fresh browser), then the failing cases. |
| `freeze-probe.mjs <case id>` | A page that never finishes: streams the page's inputs and the extension's log, then pauses the frozen page in the debugger and prints the stack (`FREEZE_MS`, `PROFILE`, `LOCK`). |
| `qbank-workable.mjs <accounts> <out.json>` | A Workable question bank from its public GET form endpoint, for `test/qbank.test.ts` (with `QBANK_URL` on a Workable host). |
| `qbank-ashby.mjs <board,board,...> <out.json> [jobsPerBoard]` | An Ashby question bank: the public posting API (GET) lists a board's jobs, each form comes from the page's own `ApiJobPosting` GraphQL query (never a mutation). Run with `QBANK_URL` on jobs.ashbyhq.com. |
| `qbank-lever.mjs <board[:jobs],board,...> <out.json> [jobsPerBoard]` | A Lever question bank: the public postings API (GET) lists a board's jobs, and each posting's own `/apply` page is read as served (standard fields, custom cards, the EEO survey). Run with `QBANK_URL` on jobs.lever.co. |
| `qbank-jobvite.mjs <company[:id+id],...> <out.json> [jobsPerCompany]` | A Jobvite question bank: a company's job list page names its postings, and each apply page carries its whole form (`preloadedData`), read as served. Run with `QBANK_URL` on jobs.jobvite.com. |
| `qbank-smartrecruiters.mjs <Company[:id+id],...> <out.json> [postingsPerCompany]` | A SmartRecruiters question bank: the public Posting API (GET) names each posting, and its screening questions come from the one-click page's `window.__OC_CONTEXT__`, read in a fresh headful browser with every non-GET request aborted (a plain GET meets a bot check). Writes `<out>.raw.json` beside it. Run with `QBANK_URL` on jobs.smartrecruiters.com. |
| `qbank-recruitee.mjs <company,...> <out.json> [offersPerCompany]` | A Recruitee question bank from a company's public offers API (GET): each offer's open questions with their kinds and options, and its location question where it lists several places. Run with `QBANK_URL` on a recruitee.com host. |
| `qbank-breezy.mjs <company[:id+id],...> <out.json> [jobsPerCompany]` | A Breezy question bank (GET): a company's jobs JSON (`/json`) names its jobs, and each apply page carries its form as data: the newer template in `<script id="portal-data">`, the older (AngularJS) one in its `data-position` attribute and a questionnaire attribute. The standard fields the posting asks and its questionnaire; work history and education rows are left out. Run with `QBANK_URL` on a breezy.hr host. |
| `qbank-comeet.mjs <company/companyUid[:posUid+posUid],...> <out.json> [positionsPerCompany]` | A Comeet question bank (GET): a company's page (`/jobs/<company>/<companyUid>`) lists its positions in `COMPANY_POSITIONS_DATA`, and each position's hosted page carries its screening questionnaires in `QUESTIONNAIRES_DATA` (each question's type, title and options). Run with `QBANK_URL` on www.comeet.com. |
| `qbank-bamboohr.mjs <company[:id+id],...> <out.json> [jobsPerCompany]` | A BambooHR question bank (GET): a company's job list (`/careers/list`) names its jobs, and each job's detail JSON (`/careers/<id>/detail`) is its form: the standard fields the posting asks, its custom questions (kind, options, "Other") and the EEO lists it shows. Contact, address and uploads are left out. Run with `QBANK_URL` on a bamboohr.com host. |
| `qbank-paylocity.mjs <jobId,...> <out.json> [jobsPerCompany]` | A Paylocity question bank (GET): each apply page (`/Recruiting/Jobs/Apply/<jobId>`) is served with its form as `window.pageData`: the screener's questions (each one's kind and answers in a small XML document), the application's included standard fields and the EEO lists; its company's job list (`/Recruiting/Jobs/All/<publicLinkKey>`) names more jobs. Contact, address and history rows are left out. Run with `QBANK_URL` on recruiting.paylocity.com. |
| `qbank-pinpoint.mjs <company[:uuid+uuid],...> <out.json> [postingsPerCompany]` | A Pinpoint question bank (GET): a company's postings JSON (`/postings.json`) names its postings and places, and each application page (`/applications/new`) is served with its form: the native standard fields, and components configured in `<script type="application/json">` (the phone, custom questions with their kinds and options, the equality-monitoring lists). Run with `QBANK_URL` on a pinpointhq.com host. |
| `qbank-jazzhr.mjs <company[:id+id],...> <out.json> [jobsPerCompany]` | A JazzHR question bank (GET): a company's job list (`/apply`) names its jobs, and each apply page is served with its whole form: the standard fields, the questionnaire (selects, text, checkbox groups) and the EEO block, read as served. Run with `QBANK_URL` on an applytojob.com host. |
| `qbank-review.cjs <bank-out.json>` | One line per distinct bank question with every persona's answer. |
| `qbank-diff.cjs <old-out.json> <new-out.json> [label regex]` | Every bank answer that changed between two runs, grouped by question. Copy `<bank>-out.json` aside before a re-run, then read every line. |

`run.mjs --filter` takes substrings of case ids, ATS names, or `=<id>` for one
exact case.

A case with `nextPages: N` goes on past page 1: after each fill the harness
presses the panel's Next page gate, as an applicant reading each page would,
up to N times, and keeps what every page held (`nextPages` in the saved run;
`review.cjs` shows each page under its own heading). Nothing more is risked:
the flow never clicks Submit and every non-GET request stays blocked, so a
Next that saves to the server leaves the page where it was. Pins still read
page 1.
