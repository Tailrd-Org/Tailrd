// Breezy question bank builder (GET only). Usage:
//   node test/e2e/tools/qbank-breezy.mjs <company[:id+id],...> test/e2e/results/qbank/breezy.json [jobsPerCompany=6]
// then: QBANK=test/e2e/results/qbank/breezy.json QBANK_URL="https://qbank.breezy.hr/p/000000000000-qbank/apply" \
//   node node_modules/vitest/vitest.mjs run test/qbank.test.ts
// Companies: prod scraped_jobs urls like <company>.breezy.hr/p/<id>-<slug>
// (read-only SELECT). A company's public jobs JSON (/json) names its jobs, and
// each apply page carries its form in <script id="portal-data">: the
// standard fields the posting asks (position.application_form) and its
// questionnaire. Work history and education rows are left out (rows, not
// questions). Nothing is sent.
// Output: the qbank format test/qbank.test.ts reads (Greenhouse vocabulary).
import { writeFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const SPECS = process.argv[2].split(",");
const OUT = process.argv[3];
const PER = Number(process.argv[4] ?? 6);
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = async (u, json = false) => {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(u, { headers: { "User-Agent": UA, Accept: json ? "application/json" : "text/html" } });
      if (r.status === 429) { await sleep(3000); continue; }
      if (r.status !== 200) return null;
      return json ? await r.json() : await r.text();
    } catch { await sleep(1000); }
  }
  return null;
};
const clean = (s) => String(s ?? "").replace(/\s+/g, " ").trim();

// The standard fields a posting may ask, as Breezy labels them (live cases:
// "Full Name", "Email Address", "Phone Number", the address box).
const STANDARD = [
  ["name", "Full Name", "input_text"],
  ["email_address", "Email Address", "input_text"],
  ["phone_number", "Phone Number", "input_text"],
  ["address", "Address", "input_text"],
  ["headline", "Headline", "input_text"],
  ["summary", "Summary", "textarea"],
  ["salary", "Desired Salary", "input_text"],
  ["resume", "Resume", "input_file"],
  ["cover_letter", "Cover Letter", "textarea"],
];
const TYPES = {
  multiplechoice: "multi_value_single_select",
  dropdown: "multi_value_single_select",
  checkboxes: "multi_value_multi_select",
  paragraph: "textarea",
  text: "input_text",
  date: "input_date",
  number: "input_number",
  file: "input_file",
};

const bank = [];
const kinds = new Map();
const read = new Set();
for (const spec of SPECS) {
  const [company, idList] = spec.split(":");
  const base = `https://${company}.breezy.hr`;
  let ids = idList ? idList.split("+") : [];
  if (!idList) {
    const jobs = await get(`${base}/json`, true);
    await sleep(300);
    if (!Array.isArray(jobs)) { console.log(company, "no jobs json"); continue; }
    ids = jobs.map((j) => j.friendly_id).filter(Boolean).slice(0, PER);
  }
  for (const id of ids) {
    if (read.has(`${company}/${id}`)) continue;
    read.add(`${company}/${id}`);
    const html = await get(`${base}/p/${id}/apply`);
    await sleep(300);
    if (!html) { console.log(company, "gone", id); continue; }
    const doc = new JSDOM(html).window.document;
    const script = [...doc.querySelectorAll("script")].find((s) => s.id === "portal-data" || /"questionnaire_in_experience"|"application_form"/.test(s.textContent));
    let data;
    try { data = JSON.parse(script?.textContent ?? "null")?.data; } catch { data = null; }
    // The older template (AngularJS) keeps the position in data-position and
    // the questionnaire's sections, the same shape, in another attribute.
    if (!data?.position) {
      let position = null;
      let sections = [];
      for (const el of doc.querySelectorAll("*")) {
        for (const a of el.attributes) {
          if (!/^[[{]/.test(a.value.trim()) || a.value.length < 20) continue;
          let v;
          try { v = JSON.parse(a.value); } catch { continue; }
          if (a.name === "data-position" && v?.application_form) position = v;
          else if (Array.isArray(v) && v.some((s) => Array.isArray(s?.questions))) sections = v;
        }
      }
      if (position) data = { position, questions: sections, company: { name: company } };
    }
    if (!data?.position) { console.log(company, "no form", id); continue; }
    const p = data.position;
    const form = p.application_form ?? {};
    const questions = [];
    for (const [key, label, type] of STANDARD) {
      const mode = form[key];
      if (!mode || mode === "hidden") continue;
      questions.push({ group: "Application", label, description: "", required: mode === "required", name: key, type, options: [] });
    }
    for (const group of data.questions ?? []) {
      for (const q of group.questions ?? []) {
        const tid = q.type?.id ?? "text";
        kinds.set(tid, (kinds.get(tid) ?? 0) + 1);
        let type = TYPES[tid] ?? "input_text";
        const options = (q.options ?? []).map((o) => clean(o.text ?? o)).filter(Boolean);
        if ((type === "multi_value_single_select" || type === "multi_value_multi_select") && options.length === 0) type = "input_text";
        // Its description is HTML ("<p>e.g., VISA Sponsorship…</p>"): its text.
        questions.push({ group: "Questions", label: clean(q.text), description: clean(String(q.body ?? "").replace(/<[^>]+>/g, " ")), required: !!q.required, name: String(q._id ?? q.text), type, options: type.startsWith("multi_value") ? options : [] });
      }
    }
    const loc = p.location ?? {};
    const location = [loc.city, loc.state?.id ?? loc.state?.name, loc.country?.name].filter(Boolean).join(", ") || (loc.name ?? "");
    console.log(company, id.slice(0, 12), questions.length, "|", clean(p.name), "|", location);
    bank.push({ board: company, id, title: clean(p.name), company: clean(p.company?.name ?? data.companyForClient?.name ?? data.company?.name ?? company), location, questions });
  }
}
writeFileSync(OUT, JSON.stringify(bank, null, 1));
console.log("postings", bank.length, "questions", bank.reduce((n, p) => n + p.questions.length, 0));
for (const [k, n] of kinds) console.log("KIND", k, n);
