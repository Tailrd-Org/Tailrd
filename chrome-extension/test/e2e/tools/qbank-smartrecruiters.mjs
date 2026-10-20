// SmartRecruiters question bank builder (GET only). Usage:
//   node test/e2e/tools/qbank-smartrecruiters.mjs <Company[:id+id],Company,...> test/e2e/results/qbank/smartrecruiters.json [postingsPerCompany=3]
// then: QBANK=test/e2e/results/qbank/smartrecruiters.json QBANK_URL="https://jobs.smartrecruiters.com/oneclick-ui/company/qbank/publication/00000000-0000-0000-0000-000000000000" \
//   node node_modules/vitest/vitest.mjs run test/qbank.test.ts
// Companies: prod scraped_jobs urls like jobs.smartrecruiters.com/<Company>/<id> (read-only SELECT).
// The public Posting API (GET) names each posting and its place; the
// screening questions are in the one-click page itself, the
// `window.__OC_CONTEXT__` its app renders them from
// (`screeningConfiguration.questions`). That page answers a plain GET with a
// bot-check page, so each is opened in a fresh headful browser, every request
// but GET aborted, and the context is read once the page has set it. Nothing
// is typed, clicked or posted. Output: the qbank format (Greenhouse vocabulary),
// and beside it <out>.raw.json, each posting's screening configuration as
// read, so the bank can be rebuilt without a browser:
//   node test/e2e/tools/qbank-smartrecruiters.mjs --reconvert test/e2e/results/qbank/smartrecruiters.json
import { readFileSync, writeFileSync } from "node:fs";

const RECONVERT = process.argv[2] === "--reconvert";
const SPECS = RECONVERT ? [] : process.argv[2].split(",").map((s) => s.trim()).filter(Boolean);
const OUT = process.argv[3];
const RAW = OUT.replace(/\.json$/, ".raw.json");
const PER = Number(process.argv[4] ?? 3);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const HEADERS = { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36" };

async function getJson(url) {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(url, { headers: HEADERS });
      if (r.status === 429) { await sleep(4000); continue; }
      return r.status === 200 ? await r.json() : null;
    } catch { await sleep(1000); }
  }
  return null;
}

/** Markdown as the page shows it: a link's text, no emphasis marks. */
const clean = (s) =>
  String(s ?? "")
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\*\*|__/g, "")
    .replace(/(^|\s)\*([^*]+)\*(?=\s|$|[.,;:?!])/g, "$1$2")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();

/** One posting's one-click context, read in its own browser. */
async function oneClickContext(company, uuid) {
  const url = `https://jobs.smartrecruiters.com/oneclick-ui/company/${company}/publication/${uuid}?dcr_ci=${company}`;
  const { chromium } = await import("playwright");
  const browser = await chromium.launch({ headless: false });
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    await page.route("**/*", (route) => {
      const m = route.request().method();
      return m === "GET" || m === "HEAD" || m === "OPTIONS" ? route.continue() : route.abort();
    });
    await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
    for (let i = 0; i < 30; i++) {
      const ctx = await page.evaluate(() => (window.__OC_CONTEXT__ ? JSON.stringify(window.__OC_CONTEXT__) : null)).catch(() => null);
      if (ctx) return JSON.parse(ctx);
      await sleep(500);
    }
    return null;
  } catch (e) {
    console.log(company, uuid, "page", e.message.split("\n")[0]);
    return null;
  } finally {
    await browser.close().catch(() => {});
  }
}

const seenTypes = new Map();

/**
 * The bank's questions from the context's screening questions. An "info"
 * block is text, no question: it rides along as the next question's
 * description (the Hispanic definition above "Are you Hispanic or Latino?").
 * An "eeo" question holds several selects (Gender, Race/Ethnicity): one each.
 * A "checkbox" with one value ("Confirmed") is one box under its label; a
 * "files" question is an upload, left out.
 */
function toQuestions(screening) {
  const out = [];
  let info = "";
  for (const q of screening?.questions ?? []) {
    const label = clean(q.label);
    const type = String(q.type ?? "");
    if (!seenTypes.has(type)) seenTypes.set(type, JSON.stringify(q).slice(0, 300));
    const fields = q.fields ?? q.questionsFields ?? [];
    if (type === "files") continue;
    if (type === "info" || fields.length === 0) {
      info = label;
      continue;
    }
    if (type === "checkbox" && fields.length === 1 && (fields[0].values ?? fields[0].questionsFieldValues ?? []).length <= 1) {
      out.push({ group: "screening", label, description: info, required: Boolean(q.required), name: `sr_${q.id}_value`, type: "input_checkbox", options: [] });
      info = "";
      continue;
    }
    const group = q.diversity ? "diversity" : "screening";
    for (const f of fields) {
      const options = (f.values ?? f.questionsFieldValues ?? []).map((v) => clean(v.label)).filter(Boolean);
      const own = clean(f.label);
      const named = fields.length > 1 && own && !/^value$/i.test(own) ? own : label;
      const ft = String(f.type ?? type);
      let t = "input_text";
      if (options.length > 0) t = f.multipleChoice ? "multi_value_multi_select" : "multi_value_single_select";
      else if (/textarea/i.test(ft)) t = "textarea";
      else if (/^(number|numeric|integer)$/i.test(ft)) t = "input_number";
      else if (/date/i.test(ft)) t = "input_date";
      else if (/checkbox/i.test(ft)) t = "input_checkbox";
      else if (/file|upload|attach/i.test(ft)) t = "input_file";
      out.push({
        group,
        label: named,
        description: info,
        required: Boolean(q.required || f.required),
        name: `sr_${q.id}_${f.name ?? f.id ?? "value"}`,
        type: t,
        options: t.startsWith("multi_value") ? options : [],
      });
    }
    info = "";
  }
  return out;
}

/** "Plainfield, IN, United States": the posting's place as its API names it. */
function placeOf(loc) {
  if (!loc) return "";
  if (loc.fullLocation) return clean(loc.fullLocation);
  return [loc.city, loc.region, loc.country?.toUpperCase?.()].filter(Boolean).join(", ");
}

const raw = RECONVERT ? JSON.parse(readFileSync(RAW, "utf8")) : [];
for (const spec of SPECS) {
  const [company, only] = spec.split(":");
  let ids = only ? only.split("+") : [];
  if (!ids.length) {
    const list = await getJson(`https://api.smartrecruiters.com/v1/companies/${company}/postings?limit=${Math.max(PER * 4, 20)}`);
    ids = (list?.content ?? []).map((p) => p.id);
  }
  let taken = 0;
  for (const id of ids) {
    if (taken >= PER) break;
    const posting = await getJson(`https://api.smartrecruiters.com/v1/companies/${company}/postings/${id}`);
    if (!posting?.uuid) {
      console.log(company, id, "no posting");
      continue;
    }
    const ctx = await oneClickContext(company, posting.uuid);
    if (!ctx) {
      console.log(company, id, "no context (bot check?)");
      continue;
    }
    taken++;
    raw.push({ board: company, id, uuid: posting.uuid, title: clean(posting.name), company: clean(posting.company?.name ?? company), location: placeOf(posting.location), screening: ctx.screeningConfiguration ?? null });
    await sleep(1500);
  }
}
if (!RECONVERT) writeFileSync(RAW, JSON.stringify(raw, null, 1));
const bank = [];
for (const r of raw) {
  const questions = toQuestions(r.screening);
  console.log(r.board, r.id, questions.length, "|", r.title.slice(0, 60), "|", r.location);
  if (questions.length) bank.push({ board: r.board, id: r.id, title: r.title, company: r.company, location: r.location, questions });
}
writeFileSync(OUT, JSON.stringify(bank, null, 1));
console.log("postings", bank.length, "questions", bank.reduce((n, p) => n + p.questions.length, 0));
for (const [t, s] of seenTypes) console.log("TYPE", t, s);
