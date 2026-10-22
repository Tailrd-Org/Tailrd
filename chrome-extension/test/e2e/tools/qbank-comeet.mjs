// Comeet question bank builder (GET only). Usage:
//   node test/e2e/tools/qbank-comeet.mjs <company/companyUid[:posUid+posUid],...> test/e2e/results/qbank/comeet.json [positionsPerCompany=6]
// then: QBANK=test/e2e/results/qbank/comeet.json QBANK_URL="https://www.comeet.com/jobs/qbank/00.000/qbank/00.000" \
//   node node_modules/vitest/vitest.mjs run test/qbank.test.ts
// Companies: prod scraped_jobs urls like www.comeet.com/jobs/<company>/<companyUid>/<slug>/<posUid>
// (read-only SELECT). A company's page (/jobs/<company>/<companyUid>) lists
// its positions in COMPANY_POSITIONS_DATA, and each position's hosted page
// is served with its screening questionnaires in QUESTIONNAIRES_DATA (each
// question's type, title and options). Nothing is sent. Output: the qbank
// format test/qbank.test.ts reads (Greenhouse vocabulary).
import { writeFileSync } from "node:fs";

const SPECS = process.argv[2].split(",");
const OUT = process.argv[3];
const PER = Number(process.argv[4] ?? 6);
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = async (u) => {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(u, { headers: { "User-Agent": UA, Accept: "text/html" } });
      if (r.status === 429) { await sleep(3000); continue; }
      return r.status === 200 ? await r.text() : null;
    } catch { await sleep(1000); }
  }
  return null;
};
/** A page variable assigned on its own line: `NAME = <json>;`. */
const pageVar = (html, name) => {
  const m = new RegExp(`^\\s*${name} = (.*);\\s*$`, "m").exec(html ?? "");
  if (!m) return null;
  try { return JSON.parse(m[1]); } catch { return null; }
};
const clean = (s) => String(s ?? "").replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();

const bank = [];
const kinds = new Map();
for (const spec of SPECS) {
  const [path, idList] = spec.split(":");
  const base = `https://www.comeet.com/jobs/${path}`;
  let urls = [];
  const list = pageVar(await get(base), "COMPANY_POSITIONS_DATA") ?? [];
  await sleep(300);
  const byUid = new Map(list.map((p) => [p.uid, p]));
  if (idList) urls = idList.split("+").map((uid) => byUid.get(uid)?.url_comeet_hosted_page).filter(Boolean);
  else urls = list.filter((p) => !p.is_internal).slice(0, PER).map((p) => p.url_comeet_hosted_page).filter(Boolean);
  for (const url of urls) {
    const html = await get(url);
    await sleep(300);
    const pos = pageVar(html, "POSITION_DATA");
    const qns = pageVar(html, "QUESTIONNAIRES_DATA");
    if (!pos) { console.log(path, "gone", url); continue; }
    const questions = [];
    for (const qn of Array.isArray(qns) ? qns : []) {
      for (const q of qn.questions ?? []) {
        kinds.set(q.type, (kinds.get(q.type) ?? 0) + 1);
        const options = (q.options ?? []).map((o) => clean(o.title)).filter(Boolean);
        questions.push({ group: clean(qn.name) || "Questionnaire", label: clean(q.title), description: clean(q.explanation), required: !!q.is_required, name: `q.${q.uid}`, type: options.length ? "multi_value_single_select" : "input_text", options });
      }
    }
    const loc = pos.location ?? {};
    const location = [loc.city, loc.state, loc.country].map(clean).filter(Boolean).join(", ");
    console.log(path, pos.uid, questions.length, "|", clean(pos.name), "|", location);
    bank.push({ board: path.split("/")[0], id: pos.uid, title: clean(pos.name), company: clean(pos.company_name) || path.split("/")[0], location, questions });
  }
}
writeFileSync(OUT, JSON.stringify(bank, null, 1));
console.log("postings", bank.length, "questions", bank.reduce((n, p) => n + p.questions.length, 0));
for (const [k, n] of kinds) console.log("KIND", k, n);
