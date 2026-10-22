// Recruitee question bank builder (GET only). Usage:
//   node test/e2e/tools/qbank-recruitee.mjs <company,company,...> test/e2e/results/qbank/recruitee.json [offersPerCompany=40]
// then: QBANK=test/e2e/results/qbank/recruitee.json QBANK_URL="https://qbank.recruitee.com/o/qbank/c/new" \
//   node node_modules/vitest/vitest.mjs run test/qbank.test.ts
// Companies: prod scraped_jobs urls like <company>.recruitee.com (read-only SELECT).
// Each company's public offers API lists its offers with their open questions
// (kind and options) and the location question; the form itself is never loaded.
// Output: the qbank format test/qbank.test.ts reads (Greenhouse vocabulary).
import { writeFileSync } from "node:fs";

const COMPANIES = process.argv[2].split(",");
const OUT = process.argv[3];
const PER = Number(process.argv[4] ?? 40);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = async (u) => {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(u, { headers: { "User-Agent": "Mozilla/5.0", Accept: "application/json" } });
      if (r.status === 429) { await sleep(3000); continue; }
      return r.status === 200 ? await r.json() : null;
    } catch { await sleep(1000); }
  }
  return null;
};

const TYPES = {
  boolean: "multi_value_single_select",
  single_choice: "multi_value_single_select",
  dropdown: "multi_value_single_select",
  multi_choice: "multi_value_multi_select",
  multiple_choice: "multi_value_multi_select",
  string: "input_text",
  text: "textarea",
  number: "input_number",
  date: "input_date",
  file: "input_file",
};
const seenKinds = new Map();
const bank = [];
for (const company of COMPANIES) {
  const j = await get(`https://${company}.recruitee.com/api/offers/`);
  await sleep(300);
  const offers = (j?.offers ?? []).filter((o) => o.status === "published" || !o.status);
  if (!offers.length) { console.log(company, "no offers"); continue; }
  const recent = [...offers].sort((a, b) => String(b.published_at).localeCompare(String(a.published_at))).slice(0, PER);
  for (const o of recent) {
    const questions = [];
    for (const q of [...(o.open_questions ?? [])].sort((a, b) => (a.position ?? 0) - (b.position ?? 0))) {
      if (!seenKinds.has(q.kind)) seenKinds.set(q.kind, JSON.stringify(q).slice(0, 400));
      let type = TYPES[q.kind] ?? "input_text";
      let options = q.kind === "boolean" ? ["Yes", "No"] : (q.open_question_options ?? []).map((x) => String(x.body ?? x.name ?? "").trim()).filter(Boolean);
      if ((type === "multi_value_single_select" || type === "multi_value_multi_select") && options.length === 0) type = "input_text";
      if (type !== "multi_value_single_select" && type !== "multi_value_multi_select") options = [];
      questions.push({ group: "Questions", label: String(q.body ?? "").trim(), description: "", required: !!q.required, name: `oq_${q.id}`, type, options });
    }
    // The location question an offer in several places asks ("Which location
    // are you applying for?"), its options the offer's own places.
    if (o.location_question_visible && o.locations_question && (o.locations ?? []).length > 1) {
      const places = o.locations.map((l) => [l.city, l.state, l.country].filter(Boolean).join(", ")).filter(Boolean);
      const multi = /multi/i.test(String(o.locations_question_type ?? ""));
      questions.push({ group: "Location", label: String(o.locations_question).trim(), description: "", required: !!o.locations_question_required, name: "locations", type: multi ? "multi_value_multi_select" : "multi_value_single_select", options: places });
    }
    const location = o.location || [o.city, o.state_name, o.country].filter(Boolean).join(", ");
    console.log(company, o.id, questions.length, "|", o.title, "|", location);
    if (questions.length) bank.push({ board: company, id: String(o.id), title: o.title, company: o.company_name ?? company, location, questions });
  }
}
writeFileSync(OUT, JSON.stringify(bank, null, 1));
console.log("postings", bank.length, "questions", bank.reduce((n, p) => n + p.questions.length, 0));
for (const [k, s] of seenKinds) console.log("KIND", k, s);
