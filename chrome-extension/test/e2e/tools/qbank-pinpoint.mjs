// Pinpoint question bank builder (GET only). Usage:
//   node test/e2e/tools/qbank-pinpoint.mjs <company[:uuid+uuid],...> test/e2e/results/qbank/pinpoint.json [postingsPerCompany=6]
// then: QBANK=test/e2e/results/qbank/pinpoint.json QBANK_URL="https://qbank.pinpointhq.com/en/postings/00000000-0000-0000-0000-000000000000/applications/new" \
//   node node_modules/vitest/vitest.mjs run test/qbank.test.ts
// Companies: prod scraped_jobs urls like <company>.pinpointhq.com/en/postings/<uuid>
// (read-only SELECT). A company's public postings JSON names its postings and
// places, and each posting's application page (/applications/new) is served
// with its form: native standard fields, and components configured in
// <script type="application/json">: the phone, its custom questions
// (questionDetails) and its equality-monitoring lists. Nothing is sent.
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
const clean = (s) => String(s ?? "").replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const TYPES = {
  short_text: "input_text",
  long_text: "textarea",
  multiple_choice: "multi_value_single_select",
  checkboxes: "multi_value_multi_select",
  number: "input_number",
  date: "input_date",
  file: "input_file",
  yes_no: "multi_value_single_select",
  boolean: "multi_value_single_select",
};
const COUNTRIES = { UK: "United Kingdom", GB: "United Kingdom", US: "United States", CA: "Canada", IN: "India", MX: "Mexico", DE: "Germany", IE: "Ireland", FR: "France", NL: "Netherlands", ES: "Spain", AU: "Australia" };

const bank = [];
const kinds = new Map();
const read = new Set();
for (const spec of SPECS) {
  const [company, idList] = spec.split(":");
  const base = `https://${company}.pinpointhq.com`;
  const postings = await get(`${base}/postings.json`, true);
  await sleep(300);
  const list = Array.isArray(postings) ? postings : postings?.data ?? [];
  const uuidOf = (p) => /\/postings\/([0-9a-f-]{36})/.exec(String(p.url ?? p.path ?? ""))?.[1];
  const byUuid = new Map(list.map((p) => [uuidOf(p), p]));
  const ids = idList ? idList.split("+") : list.map(uuidOf).filter(Boolean).slice(0, PER);
  for (const id of ids) {
    if (read.has(`${company}/${id}`)) continue;
    read.add(`${company}/${id}`);
    const html = await get(`${base}/en/postings/${id}/applications/new`);
    await sleep(300);
    if (!html) { console.log(company, "gone", id); continue; }
    const doc = new JSDOM(html).window.document;
    const form = doc.querySelector("form[action*='/applications']");
    if (!form) { console.log(company, "no form", id); continue; }
    const questions = [];
    // Native standard fields, as the page labels them.
    for (const el of form.querySelectorAll("input, select, textarea")) {
      if (["hidden", "submit", "button", "radio"].includes(el.type)) continue;
      if (!/^application(_form)?\[/.test(el.name)) continue;
      const lab = (el.id && form.querySelector(`label[for="${el.id}"]`)) || el.closest("label");
      const label = clean(lab?.textContent).replace(/\s*\*$/, "");
      if (!label) continue;
      const type = el.tagName === "TEXTAREA" ? "textarea" : el.type === "checkbox" ? "input_checkbox" : el.type === "file" ? "input_file" : "input_text";
      questions.push({ group: "Application", label, description: "", required: el.required || /\(required\)|\*/i.test(lab?.textContent ?? ""), name: el.name, type, options: [] });
    }
    // Components: the phone, files, custom questions, equality monitoring.
    for (const s of doc.querySelectorAll('script[type="application/json"]')) {
      let j;
      try { j = JSON.parse(s.textContent); } catch { continue; }
      const name = String(j.name ?? "");
      if (/\[phone\]$/.test(name)) {
        questions.push({ group: "Application", label: "Phone", description: "", required: !!j.required, name, type: "input_text", options: [] });
      } else if (/\[(cv|cover_letter)\]$/.test(name)) {
        questions.push({ group: "Application", label: clean(j.labelText) || "Attach", description: "", required: false, name, type: "input_file", options: [] });
      } else if (/answers_attributes/.test(name) && j.questionDetails) {
        const qd = j.questionDetails;
        const qt = qd.questionType ?? "short_text";
        kinds.set(qt, (kinds.get(qt) ?? 0) + 1);
        let type = TYPES[qt] ?? "input_text";
        if (qt === "multiple_choice" && (j.maxNumAnswerOptions ?? 1) > 1) type = "multi_value_multi_select";
        let options = (j.options ?? []).map((o) => clean(o.label ?? o.textEn ?? o.value ?? o)).filter(Boolean);
        if ((qt === "yes_no" || qt === "boolean") && options.length === 0) options = ["Yes", "No"];
        if (type.startsWith("multi_value") && options.length === 0) type = "input_text";
        questions.push({ group: "Questions", label: clean(qd.title), description: clean(qd.additionalInformation), required: !!(qd.required ?? j.required), name, type, options: type.startsWith("multi_value") ? options : [] });
      } else if (/equality_monitoring/.test(name)) {
        const topic = String(j.id ?? "").replace(/^application_form_equality_monitoring_/, "").replace(/_/g, " ");
        const options = (j.options ?? []).map((o) => clean(o.label)).filter(Boolean);
        if (topic && options.length) questions.push({ group: "EEO", label: topic, description: "", required: false, name: String(j.id), type: "multi_value_single_select", options });
      }
    }
    // The place as the posting names it: city, province and the country its
    // name ends on ("London, UK"; "India"; "Mexico - Monterrey").
    const meta = byUuid.get(id) ?? {};
    const loc = meta.location ?? {};
    const name = clean(loc.name);
    const code = /,\s*([A-Z]{2})$/.exec(name)?.[1];
    const country = code ? COUNTRIES[code] : /^(India|Mexico|Canada|United States|United Kingdom)\b/.exec(name)?.[1];
    const city = clean(loc.city) || clean(/ - (.+)$/.exec(name)?.[1]);
    const province = clean(loc.province);
    const location = typeof loc === "string" ? loc : [city, province && province !== city ? province : "", country].filter(Boolean).join(", ") || name;
    const title = clean(meta.title) || clean(doc.title).replace(/\s*[-|].*$/, "");
    const companyName = clean(doc.querySelector('meta[property="og:site_name"]')?.getAttribute("content")) || company;
    console.log(company, id.slice(0, 8), questions.length, "|", title, "|", location);
    bank.push({ board: company, id, title, company: companyName, location, questions });
  }
}
writeFileSync(OUT, JSON.stringify(bank, null, 1));
console.log("postings", bank.length, "questions", bank.reduce((n, p) => n + p.questions.length, 0));
for (const [k, n] of kinds) console.log("KIND", k, n);
