# Chrome Web Store Submission: Tailrd

Everything below is ready to paste into the CWS developer dashboard
(https://chrome.google.com/webstore/devconsole).

## Release 0.6.0 (prepared 2026-10-22)

The Store still serves 0.4.0 (listing checked 2026-10-22: "Version 0.4.0",
"Updated July 15, 2026", 3 users). Neither 0.5.0 nor 0.5.1 was uploaded, and
0.6.0 supersedes both: it carries autofill rounds 4 to 8 and 0.5.1's
null-safe tailor card. **Never upload `tailrd-extension-0.5.0.zip`** (it
predates that fix); no 0.5.1 zip was ever built.

**What to upload:** `chrome-extension/tailrd-extension-0.6.0.zip`
(git-ignored), built from main at the commit that moved the version to 0.6.0
(`git log -1 -- chrome-extension/manifest.json`). Dashboard > the Tailrd item >
Package > Upload new package, then Submit for review. To rebuild it: from
`chrome-extension/`, `node build.mjs && python scripts/make-store-zip.py`.

**What changes in the dashboard**
- Version 0.4.0 to 0.6.0.
- Name: "Tailrd: Job Application Assistant" (0.4.0's title carries an em dash
  and says "Autofill"). The listing title follows the manifest.
- Permissions, host permissions, content scripts, CSP and background:
  identical to 0.4.0 (compared field by field on 2026-10-22), so existing
  users get no permission prompt and the update installs silently.
- Data use: unchanged. Diagnostic capture is on for one account only, the
  owner's admin account (prod `user_settings`, checked 2026-10-22), so the
  "never field values" disclosure stays true for every Store user.
- Listing description: the two lines the 0.5.0 section flags are still live
  (checked 2026-10-22). The description below has both edits applied.
- Images: replace all six with `store-previews/` (the live ones are July's,
  from before the new logo). Screenshot 1 and the marquee tile were redrawn
  on 2026-10-22 to the 0.6.0 panel: its button reads "Fill from my profile"
  (no field-count caption), it has a "Saved sign-ins" row and "Attach", and
  no "Open Dashboard" link. Screenshot 1 no longer claims "every ATS" nor
  shows other companies' logos, and the marquee names no platforms (the
  0.4.0 rejection was for a list of platform names).

**What's new since 0.4.0** (the dashboard has no release-notes field for an
extension; this is for your records or an announcement):

```
Tailrd 0.6.0
- One Autofill click carries a multi-page application to its review page.
  Each filled page turns by itself after a 2-second countdown; Pause, or
  clicking into the page, holds it. It never clicks Submit.
- Screening questions are answered on your device from your profile, and
  far more of them correctly: work authorization by country, sponsorship
  now and later, relocation and in-office questions, start dates, education
  (degree in progress or finished, majors, graduation month and year),
  languages, years of experience, how you heard about the job.
- Applications reach their screening steps on more sites, your résumé
  attaches where the form asks for it, and education rows, multi-select
  lists and searchable dropdowns fill more reliably.
- Answers are only ever chosen from the options a form offers; questions
  your profile cannot answer stay blank, and the panel asks you about them.
- The tailoring card never shows a raw score where none was computed.
- Demographic answers still never leave your device.
```

**Description (long), updated: paste in place of the live text**

```
Tailrd helps you fill job applications, and you stay in control of every answer.

It never submits an application by itself. The final Submit is always yours.
It never makes up facts about you. Answers come from your profile; standard questions (acknowledging a policy, consent boxes) get the usual answer, and anything else is left blank for you to complete.

Applying online means retyping the same details into a new form for every role. Tailrd keeps that information in one profile and puts it into the form in front of you, so you can spend your time on the parts of the application that actually need you.

WHAT IT DOES
• Recognizes an application form when you open one, on company career sites and on the hiring platforms they run on, and offers to fill it.
• Fills the fields it can answer from your Tailrd profile: contact details, work history, education, links, work authorization, and screening questions you've already answered in your profile.
• Works through applications that span several pages: one click fills each page and moves to the next, stopping at the review page. If the site asks you to create an account first, Tailrd fills that step with the email and password you saved for it.
• Answers dropdowns and multiple-choice questions only with options the form actually offers. If your profile does not answer a question, Tailrd leaves it blank for you to complete.
• Attaches the résumé from your account, suggests résumé edits for the job, and offers talking points and ideas for your cover letter.

WHAT IT NEVER DOES
• Demographic (EEO) answers never leave your device: they are stored locally and filled locally.

You need a free Tailrd account (www.tailrd.ca) to sync your profile.
```

**Before submitting:** the résumé edits and cover-letter ideas call OpenAI,
whose billing has been down since 2026-08-12. A reviewer who tries them sees
them fail; the test instructions below keep to the autofill, which needs no
AI. Fix the billing first, or expect a question from review.

**Checked on this build** (2026-10-22): type check clean; unit tests 3,183
(176 files); `node test/scan-smoke.mjs` passed; build; the zip's manifest has
no `key` and no `localhost`, description 129 characters; permissions as
0.4.0. CI's other jobs on the same tree: backend pytest and the frontend's
tsc and vite build.

## Release 0.5.0 (prepared 2026-10-05, NOT uploaded; superseded by 0.6.0)

The Store still serves 0.4.0, the July build (listing checked 2026-10-05:
"Version 0.4.0", "Updated July 15, 2026", 3 users). Every real fill in prod
telemetry since then ran on it: the 2026-09-28 and 2026-10-03 reports carry no
`extension_version`, which only builds older than 2026-08-12 omit. None of the
August to October autofill work has reached a Store user yet.

**What to upload:** `chrome-extension/tailrd-extension-0.5.0.zip` (231 KB,
git-ignored, built from commit 31a4371 on `night/autofill-round4`). Dashboard > the Tailrd item > Package > Upload new package, then
Submit for review. To rebuild it: from `chrome-extension/`,
`node build.mjs && python scripts/make-store-zip.py`.

**What changes in the dashboard**
- Version 0.4.0 to 0.5.0.
- Name: "Tailrd: Job Application Assistant" (0.4.0 was "Job Application
  Autofill" with an em dash; renamed for the responsible-AI positioning). The
  listing title follows the manifest.
- Permissions and host permissions: identical to 0.4.0, so existing users see
  no new permission prompt and the update installs silently.
- Data use: unchanged. Diagnostic capture (see below) is on for one account
  only, the owner's (prod `user_settings`, checked 2026-10-05), so the
  "never field values" disclosure stays true for every Store user.

**Release notes (short)**

```
Tailrd 0.5.0
- One Autofill click now carries a multi-page application (Workday and
  others) to its review page. Each filled page turns by itself after a
  2-second countdown; Pause, or clicking into the page, holds it. It never
  clicks Submit.
- Many more screening questions are answered on your device from your
  profile, without waiting for AI: work authorization by country, start
  dates, relocation and in-office questions, education details, how you
  heard about the job.
- New profile answers: authorized to work in the US / in Canada, how you
  heard about jobs, expected graduation, GPA.
- Dropdowns, dates, search lists and repeated education/work rows fill
  more reliably; answers are only ever chosen from the options a form offers.
- Company career sites fill more fully: older dropdown styles are now
  recognized, and the job's location is read from the page, so questions
  about working in that country are answered.
- More forms are reached and filled: Apply buttons in French and guest
  "apply without an account" paths, phone numbers under their own country
  code, and dropdowns on Dayforce and other sites built the same way.
- Screening questions are read more carefully: age, citizenship,
  sponsorship now and later, and long policy statements you are asked to
  acknowledge.
- Questions your profile cannot answer stay blank, and the panel asks you
  about them in place.
- Demographic answers still never leave your device.
```

**Listing description: two lines are now out of date.** Suggested edits
(your call; both describe 0.5.0 behavior):

1. "Works through applications that span several pages. You click once per
   page; if the site asks you to create an account first, Tailrd fills that
   step too." becomes:
   "Works through applications that span several pages: one click fills each
   page and moves to the next, stopping at the review page. If the site asks
   you to create an account first, Tailrd fills that step with the email and
   password you saved for it."
2. "It never invents an answer. Every answer comes from your profile, or the
   field is left empty." Since October the extension also answers standard
   questions a profile does not settle (accepting the posting's stated terms,
   application consent boxes, the decline option of a demographic question
   you left unanswered). A reviewer could read the old line as a false claim.
   Suggested:
   "It never makes up facts about you. Answers come from your profile;
   standard questions (accepting the posting's terms, consent boxes) get the
   usual answer, and anything else is left blank for you to complete."

**Checked on this build** (2026-10-05, commit 31a4371, branch `night/autofill-round4`):
type check clean; unit tests 1841/1841; `node test/scan-smoke.mjs` passed;
zip holds 8 files (manifest, three scripts, four icons), no `key`, no
`localhost`, description 129 chars.

## The package

- Zip: `chrome-extension/tailrd-extension-<version>.zip`, built by
  `node build.mjs && python scripts/make-store-zip.py` (run from
  `chrome-extension/`). The script strips the dev-only `key` field (the
  dashboard rejects manifests that carry it) and enforces the 132-char
  description limit, so a bad zip fails locally instead of at upload.
- MV3, no remote code (everything is bundled by esbuild; no eval, no CDN
  scripts). `dist/` keeps the `key`, so the locally-loaded unpacked extension
  keeps its pinned dev ID.

### Extension IDs

The Store assigned its own permanent id at the first upload (it ignores dev
keys): `dadbhjlflnljgailcpgehdainjdmjeej`. The dev / unpacked id is
`apgogjfdpleeajnngkfkfekbddcpodkl`. Anything that trusts or addresses the
extension must carry both:

1. **`EXTENSION_ALLOWED_IDS` (Vercel):** both ids, comma-separated.
   Store installs connect today: prod `extension_auth_codes` holds completed
   handshakes from the Store id on 2026-07-14, 08-07, 09-14 and 09-28 (each
   with a successful `extension_token` event), and `security_events` has no
   `redirect_uri_rejected` since 2026-06-27. The variable itself could not be
   read from here (the Vercel connector gets a 403 for this team), so its
   exact value is unconfirmed; the handshakes show the Store id is accepted.
2. **Web-app to extension bridge** (`frontend/src/lib/extensionBridge.ts`):
   both ids are baked into the `EXTENSION_IDS` default (`VITE_EXTENSION_IDS`
   still overrides).

## Before testers can use it (Vercel env, REQUIRED)

Verified live on prod (2026-07-09): a fresh registration still gets
`email_verified: false` and `GET /api/extension/sync` returns **403**. Until
these two vars are set, every beta tester is dead on arrival:

| Env var | Value | Effect |
|---|---|---|
| `REQUIRE_EMAIL_VERIFICATION` | `false` | Unblocks unverified testers everywhere (web + extension). Reversible post-beta. |
| `EXTENSION_ALLOWED_IDS` | `apgogjfdpleeajnngkfkfekbddcpodkl,dadbhjlflnljgailcpgehdainjdmjeej` | Extension PKCE handshake fails closed in prod without it. |

Set both for **Production**, then redeploy (env changes need a redeploy to apply).

## Listing: basic fields

- **Name:** Tailrd: Job Application Assistant
- **Summary (132 chars max):**
  `Fills job application forms from the profile in your Tailrd account. You always review every answer and submit the form yourself.`
  (Must match `manifest.json`'s `description`. Keep it free of applicant-tracking-system
  brand names: a brand list here reads as keyword stuffing to review.)
- **Category:** Productivity > Workflow & Planning
- **Language:** English
- **Privacy policy URL:** `https://www.tailrd.ca/privacy` (live, returns 200)
- **Screenshots:** `store-previews/tailrd-1-autofill.png … tailrd-4-dashboard.png`
  (4 × 1280×800, already the right size). All four regenerate with
  `node scripts/gen-store-screenshots.mjs` from `chrome-extension/`.
- **Small promo tile (440×280):** `store-previews/tailrd-promo-small-440x280.png`
- **Marquee promo tile (1400×560):** `store-previews/tailrd-promo-marquee-1400x560.png`
  (both 24-bit PNG, no alpha; regenerate with `node scripts/gen-promo-tiles.mjs`)

- **Description (long), as live since July** (see the 0.5.0 section above for
  the two suggested edits):

```
Tailrd helps you fill job applications, and you stay in control of every answer.

It never submits an application by itself. The final Submit is always yours.
It never invents an answer. Every answer comes from your profile, or the field is left empty.

Applying online means retyping the same details into a new form for every role. Tailrd keeps that information in one profile and puts it into the form in front of you, so you can spend your time on the parts of the application that actually need you.

WHAT IT DOES
• Recognizes an application form when you open one, on company career sites and on the hiring platforms they run on, and offers to fill it.
• Fills the fields it can answer from your Tailrd profile: contact details, work history, education, links, work authorization, and screening questions you've already answered in your profile.
• Works through applications that span several pages. You click once per page; if the site asks you to create an account first, Tailrd fills that step too.
• Answers dropdowns and multiple-choice questions only with options the form actually offers. If your profile does not answer a question, Tailrd leaves it blank for you to complete.
• Attaches the résumé from your account, suggests résumé edits for the job, and offers talking points and ideas for your cover letter.

WHAT IT NEVER DOES
• Demographic (EEO) answers never leave your device: they are stored locally and filled locally.

You need a free Tailrd account (www.tailrd.ca) to sync your profile.
```

(The live text opens with an em dash after "for you"; the copy above replaces that opener.)

> **Do not reintroduce a list of applicant-tracking-system names here.** The v0.4.0
> draft was rejected under "Spam and Placement in the Store" (ref: Yellow Argon,
> 2026-07-11) for exactly that: the line naming ten ATS vendors was judged
> "excessive keywords in the item's description." Describe what the extension does;
> let the screenshots show where it runs.

## Privacy tab: permission justifications (paste per field)

- **Single purpose:** Tailrd fills job-application forms from the user's own
  Tailrd profile and tracks the applications they choose to submit.
- **Host permissions (`http://*/*`, `https://*/*`, content script `<all_urls>` / all frames):**
  Job applications are hosted on tens of thousands of company-specific ATS
  domains (`boards.greenhouse.io`, `*.myworkdayjobs.com`, `jobs.lever.co`,
  company career sites, and embedded cross-origin iframes inside them). A fixed
  domain list cannot cover them; the content script must run where the
  application form actually renders, including inside iframes. The script only
  activates its UI when it recognizes an application form; captcha provider
  frames are explicitly excluded in the manifest.
- **storage:** Caches the user's profile for offline fills; stores device-local
  answers (including optional demographic answers that deliberately never leave
  the device) and per-site account-creation credentials the user saves.
- **scripting / activeTab:** Injects a small page-context helper
  (`mainWorld.js`) needed to drive framework-controlled widgets (React selects,
  Workday dropdowns) that ignore synthetic DOM events.
- **identity:** Signs the user into their Tailrd account with an OAuth-style
  PKCE handshake via `chrome.identity.launchWebAuthFlow` (no Google account
  data is accessed).
- **alarms:** Periodic profile re-sync and auth-token refresh while the browser
  is open.
- **Remote code:** None. All code ships in the package.

## Data-use disclosures (check these boxes)

Collected and transmitted to the developer's service (www.tailrd.ca), tied to
the user's account:
- Personally identifiable information (name, email, phone, address: the
  profile the user asks us to fill forms with)
- Professional information: work history, education, skills, résumé content
- Authentication information (account email; tokens)
- User activity: which fields/sites autofill succeeded or failed on
  (field labels and outcomes only, **never field values**)

NOT collected: browsing history, financial info, health info, location,
personal communications, keystrokes.

### Diagnostic capture (off by default; read before ticking the boxes above)

There is one opt-in mode that changes the "never field values" line, so it must
be understood before this form is filled in.

`user_settings.diagnostic_capture` is **FALSE for every account** unless a
Tailrd operator sets it. While it is off, the extension asks the server before
capturing anything and therefore assembles and transmits **no answers and no
form markup at all**, so the disclosure above is exactly right.

While it is ON for an account, that account additionally sends, for its own
fills: the answer written into each field, the options the widget offered, and a
sanitised snapshot of the employer's form markup. It exists so a form that
failed can be rebuilt as a test (see
[autofill-capture-workflow.md](./autofill-capture-workflow.md)).

**If any account with this flag on belongs to a Web Store user, the listing must
disclose that field values are collected.** As shipped it is a maintainer
debugging tool used on the developer's own account, which is why the boxes above
describe the default. Two protections hold even with it on:

- **Demographic (EEO) answers are still never transmitted.** They are replaced
  with a `<demographic>` marker, and a demographic field's markup is captured as
  structure only, because a filled dropdown renders the choice as ordinary text.
  This keeps the "never leave your device" claim below literally true.
- Passwords and values shaped like a national ID or payment card are replaced
  with a type marker.

Certify: data is not sold; not used for unrelated purposes; not used for
creditworthiness.

## Pre-flight checklist

- [x] Unit tests green; typecheck clean (0.5.0: 1841/1841, 2026-10-05)
- [x] e2e multi-page flow probe green (0.4.0: user-gated page turns; 0.5.0:
      one click to the review page, `npm run test:workday-flow` 73/73 on
      2026-10-05; the terminal Submit is never clicked)
- [x] dist contains no `localhost` / dev URLs; API base is `https://www.tailrd.ca`
- [x] `externally_connectable` limited to tailrd.ca (localhost removed)
- [x] Icons 16/32/48/128 present; screenshots 1280×800
- [x] Privacy policy live at `/privacy` (200 on 2026-10-05)
- [x] Vercel: `REQUIRE_EMAIL_VERIFICATION=false` (verified live 2026-07-09: register gives email_verified true)
- [x] Upload zip, paste listing + justifications, submit for review (0.4.0)
- [x] **APPROVED + LIVE 2026-07-14 (0.4.0).** Store-assigned id: `dadbhjlflnljgailcpgehdainjdmjeej`
      (the Store strips the manifest `key`, so this is NOT the dev id
      `apgogjfdpleeajnngkfkfekbddcpodkl`; both must be trusted everywhere).
      Listing: https://chromewebstore.google.com/detail/tailrd-%E2%80%94-job-application/dadbhjlflnljgailcpgehdainjdmjeej
- [x] Frontend: both ids baked into `extensionBridge.ts` `EXTENSION_IDS`, and the real
      listing URL into `extensionStore.ts` `CHROME_STORE_URL`. No env var needed.
- [x] Store installs can connect: completed Store-id handshakes in prod on
      2026-07-14, 08-07, 09-14 and 09-28 (see "Extension IDs"). This item was
      marked BLOCKING in July; the prod handshakes show it was resolved.
- [ ] 0.6.0: upload `tailrd-extension-0.6.0.zip`, paste the updated
      description, submit for review. (0.5.0 is superseded: never upload it.)

## Test instructions (paste into the dashboard's "Test instructions" form)

A dedicated reviewer account exists on prod, pre-loaded with a full profile
and a résumé file (created 2026-07-10; password also below):

```
TEST ACCOUNT
Email: cws.reviewer@tailrd.ca
Password: Tailrd!Review2026
(Pre-loaded with a complete profile and resume. No email verification needed.)

SETUP (once)
1. Install the extension.
2. Click the Tailrd icon in the Chrome toolbar to open the side panel.
3. Click "Connect your Tailrd account": a tailrd.ca sign-in window opens.
   Sign in with the credentials above. The panel syncs the profile automatically.

CORE FUNCTIONALITY (no real application is submitted)
4. Go to https://www.tailrd.ca/demo-apply (a demo job-application form).
5. Open the panel (toolbar icon) and click "Fill from my profile".
6. Expected: the form fills from the signed-in profile within a few seconds
   (name, email, phone, address, work authorization…), and the resume file
   attaches where an upload field exists. Questions the profile cannot answer
   truthfully are left blank BY DESIGN: the extension never guesses.
7. The extension never submits an application by itself; the final Submit is
   always left to the user.

OPTIONAL: real ATS detection
Open any public Greenhouse or Lever job posting; the panel detects the
application form the same way. Please avoid pressing a real employer's final
Submit button; running Fill from my profile itself is safe.

NOTES
- AI answers are grounded in the account profile; ungroundable fields stay empty.
- Demographic (EEO) answers are stored on-device only and never transmitted.
```

**Prerequisite:** the store-assigned extension ID must be in
`EXTENSION_ALLOWED_IDS` (see "Extension IDs" above) or the reviewer's Connect
step fails closed.

## After review approval

1. Install from the store, sign in, run one real application end to end.
2. Flip `REQUIRE_EMAIL_VERIFICATION` back to `true` once email delivery
   (Resend) is configured post-beta.
