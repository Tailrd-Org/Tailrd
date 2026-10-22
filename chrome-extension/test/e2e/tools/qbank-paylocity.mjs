// Paylocity question bank builder (GET only). Usage:
//   node test/e2e/tools/qbank-paylocity.mjs <jobId,...> test/e2e/results/qbank/paylocity.json [jobsPerCompany=6]
// then: QBANK=test/e2e/results/qbank/paylocity.json QBANK_URL="https://recruiting.paylocity.com/Recruiting/Jobs/Apply/1" \
//   node node_modules/vitest/vitest.mjs run test/qbank.test.ts
// Seeds: prod scraped_jobs urls like recruiting.paylocity.com/Recruiting/Jobs/Details/<jobId>
// (read-only SELECT). Each apply page (/Recruiting/Jobs/Apply/<jobId>) is
// served with its form as data, window.pageData: the screener's questions
// (each one's kind and answers in a small XML document), the application's
// sections and their included fields, and the EEO lists. Its company's job
// list (/Recruiting/Jobs/All/<publicLinkKey>, pageData.Jobs) names more jobs.
// The bank keeps the screener and the standard fields that are questions;
// contact, address, work history, education and reference rows are left out.
// Nothing is sent. Output: the qbank format test/qbank.test.ts reads
// (Greenhouse vocabulary).
import { writeFileSync } from "node:fs";

const SEEDS = process.argv[2].split(",");
const OUT = process.argv[3];
const PER = Number(process.argv[4] ?? 6);
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";
const BASE = "https://recruiting.paylocity.com/Recruiting/Jobs";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = async (u) => {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(u, { headers: { "User-Agent": UA, Accept: "text/html" }, redirect: "follow" });
      if (r.status === 429) { await sleep(3000); continue; }
      if (r.status !== 200 || /JobNotFound/i.test(r.url)) return null;
      return await r.text();
    } catch { await sleep(1000); }
  }
  return null;
};
const pageData = (html) => {
  const at = html?.indexOf("window.pageData");
  if (!html || at < 0) return null;
  const start = html.indexOf("{", at);
  // The object runs to the script's end; trim back to the last brace that parses.
  const stop = html.indexOf("</script>", start);
  for (let end = html.lastIndexOf("}", stop); end > start; end = html.lastIndexOf("}", end - 1)) {
    try { return JSON.parse(html.slice(start, end + 1)); } catch { /* shorter */ }
  }
  return null;
};
const clean = (s) => String(s ?? "").replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/&#39;|&rsquo;/g, "'").replace(/&quot;/g, '"').replace(/\s+/g, " ").trim();
const xmlTags = (xml, tag) => [...String(xml ?? "").matchAll(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`, "g"))].map((m) => m[1]);

/** A screener question: "multi" lists its answers (one, or several when it
 *  allows multiple responses); "text" is a line, or a paragraph. */
function screenerQuestion(q) {
  const kind = xmlTags(q.data, "questionType")[0] ?? "text";
  const label = clean(q.title);
  const base = { group: "Screener", label, description: "", required: !!q.isRequired, name: `screener.${q.screenerQuestionId}` };
  if (kind === "multi") {
    const options = xmlTags(q.data, "answers").map((a) => clean(xmlTags(a, "title")[0])).filter(Boolean);
    return { ...base, type: q.allowsMultipleResponses ? "multi_value_multi_select" : "multi_value_single_select", options };
  }
  const paragraph = xmlTags(q.data, "isParagraph")[0] === "true";
  return { ...base, type: paragraph ? "textarea" : "input_text", options: [] };
}

// The standard fields that are questions, by their config names, and the
// words the live page labels them with (rounds 3 and 4 pins).
const STANDARD = {
  smsOptedIn: { options: ["Yes", "No"] },
  appliedBefore: { options: ["Yes", "No"] },
  dateAvailable: { type: "input_text" },
  desiredSalary: { type: "input_text" },
  howDidYouHear: { values: true },
  referredBy: { type: "input_text" },
  workedHereBefore: { label: "Have you worked with us before?", options: ["Yes", "No"] },
  authorizedToWork: { options: ["Yes", "No"] },
  priorConviction: { label: "Have you ever been convicted of a felony or misdemeanor?", options: ["Yes", "No"] },
};

function standardQuestions(data) {
  const out = [];
  for (const s of data.customJobApplication?.sections ?? []) {
    if (!s.isIncluded) continue;
    for (const f of s.fields ?? []) {
      const spec = STANDARD[f.name];
      if (!spec || !f.isIncluded) continue;
      const label = spec.label ?? clean(f.displayName);
      const options = spec.values ? (f.values ?? []).map((v) => clean(v.label ?? v.value)).filter(Boolean) : spec.options ?? [];
      const type = options.length ? "multi_value_single_select" : spec.type ?? "input_text";
      out.push({ group: s.headingText ?? s.name, label, description: "", required: !!f.isRequired, name: `${s.name}.${f.name}`, type, options });
    }
  }
  if (data.shouldIncludeEeoSection || data.shouldIncludeEeoQuestions) {
    const list = (k) => (data[k] ?? []).map((o) => clean(o.text)).filter(Boolean);
    for (const [label, k] of [["Gender", "genderOptions"], ["Race/Ethnicity", "raceOptions"], ["Veteran Status", "militaryServiceOptions"], ["Disability Status", "disabilityOptions"]]) {
      const options = list(k);
      if (options.length) out.push({ group: "EEO", label, description: "", required: false, name: `eeo.${k}`, type: "multi_value_single_select", options });
    }
  }
  return out;
}

const bank = [];
const read = new Set();
const lists = new Set();
const queue = [...SEEDS];
while (queue.length) {
  const id = queue.shift();
  if (read.has(id)) continue;
  read.add(id);
  const data = pageData(await get(`${BASE}/Apply/${id}`));
  await sleep(300);
  if (!data) { console.log("gone", id); continue; }
  const key = data.publicLinkKey;
  if (key && !lists.has(key)) {
    lists.add(key);
    const list = pageData(await get(`${BASE}/All/${key}`));
    await sleep(300);
    const more = (list?.Jobs ?? []).filter((j) => !j.IsInternal).map((j) => String(j.JobId)).filter((j) => !read.has(j));
    queue.push(...more.slice(0, PER));
  }
  const questions = [...standardQuestions(data), ...(data.application?.job?.screener?.questions ?? []).sort((a, b) => a.order - b.order).map(screenerQuestion)];
  const company = clean(data.moduleName).replace(/\s*-\s*Careers Page$/i, "");
  const location = clean(data.locationName);
  console.log(id, questions.length, "|", clean(data.jobTitle), "|", company, "|", location);
  bank.push({ board: company, id, title: clean(data.jobTitle), company, location, questions });
}
writeFileSync(OUT, JSON.stringify(bank, null, 1));
console.log("postings", bank.length, "questions", bank.reduce((n, p) => n + p.questions.length, 0));
