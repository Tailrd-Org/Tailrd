// BambooHR question bank builder (GET only). Usage:
//   node test/e2e/tools/qbank-bamboohr.mjs <company[:id+id],...> test/e2e/results/qbank/bamboohr.json [jobsPerCompany=6]
// then: QBANK=test/e2e/results/qbank/bamboohr.json QBANK_URL="https://qbank.bamboohr.com/careers/1" \
//   node node_modules/vitest/vitest.mjs run test/qbank.test.ts
// Companies: prod scraped_jobs urls like <company>.bamboohr.com/careers/<id>
// (read-only SELECT). A company's job list (/careers/list) names its jobs,
// and each job's detail JSON (/careers/<id>/detail) is its form: the standard
// fields the posting asks (formFields), its custom questions (each one's
// kind, options and "Other") and the EEO lists it shows. Contact and address
// fields and the uploads are left out. Nothing is sent. Output: the qbank
// format test/qbank.test.ts reads (Greenhouse vocabulary).
import { writeFileSync } from "node:fs";

const SPECS = process.argv[2].split(",");
const OUT = process.argv[3];
const PER = Number(process.argv[4] ?? 6);
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const getJson = async (u) => {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(u, { headers: { "User-Agent": UA, Accept: "application/json" } });
      if (r.status === 429) { await sleep(3000); continue; }
      if (r.status !== 200) return null;
      return await r.json();
    } catch { await sleep(1000); }
  }
  return null;
};
const clean = (s) => String(s ?? "").replace(/<[^>]+>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
const LEFT_OUT = new Set(["firstName", "lastName", "email", "phone", "streetAddress", "city", "state", "zip", "countryId", "resumeFileId", "coverLetterFileId", "linkedinUrl", "websiteUrl", "customQuestions"]);
const EEO = { genderId: "Gender", ethnicityId: "Ethnicity", veteranStatusId: "Veteran Status", disabilityId: "Disability" };
const optionText = (o) => clean(typeof o === "string" ? o : o?.text ?? o?.label ?? o?.name ?? o?.value);

function custom(q) {
  const label = clean(q.question);
  const base = { group: "Questions", label, description: "", required: !!q.isRequired, name: `customQuestions.${q.id}` };
  const options = (q.options ?? []).map(optionText).filter(Boolean);
  if (q.hasOther === "yes" && !options.some((o) => /^other\b/i.test(o))) options.push("Other");
  if (q.type === "yes_no") return { ...base, type: "multi_value_single_select", options: ["Yes", "No"] };
  if (q.type === "long") return { ...base, type: "textarea", options: [] };
  if (/checkbox|multi/.test(q.type ?? "") && options.length) return { ...base, type: "multi_value_multi_select", options };
  if (options.length) return { ...base, type: "multi_value_single_select", options };
  return { ...base, type: "input_text", options: [] };
}

const bank = [];
const kinds = new Map();
for (const spec of SPECS) {
  const [company, idList] = spec.split(":");
  const base = `https://${company}.bamboohr.com/careers`;
  let ids = idList ? idList.split("+") : [];
  if (!idList) {
    const list = await getJson(`${base}/list`);
    await sleep(300);
    ids = (list?.result ?? []).map((j) => String(j.id)).slice(0, PER);
  }
  for (const id of ids) {
    const d = await getJson(`${base}/${id}/detail`);
    await sleep(300);
    const r = d?.result;
    if (!r?.formFields) { console.log(company, "gone", id); continue; }
    const questions = [];
    for (const [key, f] of Object.entries(r.formFields)) {
      if (LEFT_OUT.has(key) || EEO[key] || !f || typeof f !== "object" || Array.isArray(f)) continue;
      const options = (f.options ?? []).map(optionText).filter(Boolean);
      questions.push({ group: "Application", label: clean(f.label) || key, description: "", required: !!f.isRequired, name: key, type: options.length ? "multi_value_single_select" : "input_text", options });
    }
    for (const q of r.formFields.customQuestions ?? []) {
      kinds.set(q.type, (kinds.get(q.type) ?? 0) + 1);
      questions.push(custom(q));
    }
    for (const [key, label] of Object.entries(EEO)) {
      const f = r.formFields[key];
      const opts = (Array.isArray(f) ? f : f?.options ?? []).map(optionText).filter(Boolean);
      if (opts.length) questions.push({ group: "EEO", label: clean(f?.label) || label, description: "", required: false, name: key, type: "multi_value_single_select", options: opts });
    }
    const j = r.jobOpening ?? {};
    const loc = j.location ?? {};
    const location = [loc.city, loc.state, loc.addressCountry].map(clean).filter(Boolean).join(", ");
    console.log(company, id, questions.length, "|", clean(j.jobOpeningName), "|", location);
    bank.push({ board: company, id, title: clean(j.jobOpeningName), company, location, questions });
  }
}
writeFileSync(OUT, JSON.stringify(bank, null, 1));
console.log("postings", bank.length, "questions", bank.reduce((n, p) => n + p.questions.length, 0));
for (const [k, n] of kinds) console.log("KIND", k, n);
