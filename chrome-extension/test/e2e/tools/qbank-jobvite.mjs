// Jobvite question bank builder (GET only). Usage:
//   node test/e2e/tools/qbank-jobvite.mjs <company[:id+id],company,...> test/e2e/results/qbank/jobvite.json [jobsPerCompany=8]
// then: QBANK=test/e2e/results/qbank/jobvite.json QBANK_URL="https://jobs.jobvite.com/qbank/job/oQBANK01/apply" \
//   node node_modules/vitest/vitest.mjs run test/qbank.test.ts
// Companies: prod scraped_jobs urls like jobs.jobvite.com/<company>/job/<id> (read-only SELECT).
// A company's postings, titles and places come from its job list page
// (/<company>/jobs). Each form is the apply page as served: Jobvite writes the
// whole form into it (angular.module('preloadedData').constant('preloadedData',
// { applyFields: [...], preScreeningFormFields: {...} })), so nothing is posted
// and no XHR is needed. Output: the qbank format test/qbank.test.ts reads
// (Greenhouse vocabulary).
import { writeFileSync } from "node:fs";

const SPECS = process.argv[2].split(",");
const OUT = process.argv[3];
const PER = Number(process.argv[4] ?? 8);
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const getText = async (u) => {
  for (let i = 0; i < 3; i++) {
    try {
      const r = await fetch(u, { headers: { "User-Agent": UA, Accept: "text/html" } });
      if (r.status === 429) { await sleep(3000); continue; }
      return r.status === 200 ? await r.text() : null;
    } catch { await sleep(1000); }
  }
  return null;
};
const clean = (s) => String(s ?? "").replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").replace(/\s+,/g, ",").trim();

/** The JSON literal (array or object) that follows `key:` in the preloaded object. */
function literalAfter(body, key) {
  const k = body.indexOf(`${key}:`);
  if (k < 0) return null;
  let i = k + key.length + 1;
  while (i < body.length && /\s/.test(body[i])) i++;
  if (body[i] !== "[" && body[i] !== "{") return null;
  let depth = 0;
  let inStr = false;
  for (let j = i; j < body.length; j++) {
    const c = body[j];
    if (inStr) {
      if (c === "\\") j++;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') inStr = true;
    else if (c === "[" || c === "{") depth++;
    else if (c === "]" || c === "}") {
      depth--;
      if (depth === 0) {
        try {
          return JSON.parse(body.slice(i, j + 1));
        } catch {
          return null;
        }
      }
    }
  }
  return null;
}

/** A string literal value after `key:` ("jobTitle: 'Algorithm Engineer'"). */
function stringAfter(body, key) {
  const m = new RegExp(`${key}\\s*:\\s*(["'])((?:\\\\.|(?!\\1).)*)\\1`).exec(body);
  return m ? m[2].replace(/\\(.)/g, "$1") : "";
}

/**
 * Pre-screening questions: each is a "SubsectionTitle" element that holds
 * the question, then the element with the input ("Selection:" and its
 * values). The input takes the title's text.
 */
function preScreening(form) {
  const out = [];
  let title = "";
  for (const section of form?.section ?? []) {
    for (const el of section.element ?? []) {
      const fields = el.fields ?? [];
      if (!fields.some((f) => f.fieldType)) {
        if (/title|label|paragraph|text/i.test(el.kind ?? "")) title = clean(el.name ?? fields[0]?.name ?? "") || title;
        continue;
      }
      for (const f of fields) {
        if (!f.fieldType) continue;
        const own = clean(f.name ?? el.name ?? "");
        const generic = !own || /^(selection|answer|response|your answer|please select|choose one|select one|select)\s*:?$/i.test(own);
        // The text above it rides along: an "IAgreeCheckbox" ("I Accept")
        // accepts the paragraph before it.
        out.push({ ...f, name: generic ? title : own, required: f.required || el.required, context: title });
      }
    }
  }
  return out;
}

const seenTypes = new Map();
function toQuestion(f, group) {
  if (!f || typeof f !== "object") return null;
  const label = clean(f.name ?? f.label ?? "");
  const ft = String(f.fieldType ?? "");
  // No field type: a divider, a heading or a block of text ("Why are you being
  // asked to complete this form?" above the disability form), not a question.
  if (!ft || !label || /^(divider|heading)$/i.test(label)) return null;
  if (!seenTypes.has(ft)) seenTypes.set(ft, JSON.stringify(f).slice(0, 300));
  const options = (f.values ?? []).map((v) => clean(typeof v === "string" ? v : v?.value ?? v?.key ?? "")).filter(Boolean);
  let type = "input_text";
  // One box ("I Accept", its only value "1"; Ross Video's "IAgreeCheckbox"):
  // a lone checkbox under the paragraph it accepts, that paragraph its
  // description. It was a one-option select of "1".
  if (/check ?box/i.test(ft) && options.length <= 1) {
    return {
      group,
      label,
      description: clean(f.instructions ?? "") || clean(f.context ?? ""),
      required: !!f.required,
      name: `jv_${group === "preScreening" ? "ps_" : ""}${f.fieldId ?? label.slice(0, 20)}`,
      type: "input_checkbox",
      options: [],
    };
  }
  if (/drop ?down|select|radio|check ?box|multiple|yes ?no|boolean/i.test(ft) && options.length > 0) {
    type = f.multiSelect || /check ?box/i.test(ft) && options.length > 1 ? "multi_value_multi_select" : "multi_value_single_select";
  } else if (/^text$|text ?area|long|paragraph|essay/i.test(ft)) type = "textarea";
  else if (/date/i.test(ft)) type = "input_date";
  else if (/file|upload|attach|resume/i.test(ft)) type = "input_file";
  return {
    group,
    label,
    description: clean(f.instructions ?? ""),
    required: !!f.required,
    name: `jv_${group === "preScreening" ? "ps_" : ""}${f.fieldId ?? label.slice(0, 20)}`,
    type,
    options: type.startsWith("multi_value") ? options : [],
  };
}

const bank = [];
for (const spec of SPECS) {
  const [company, only] = spec.split(":");
  const list = await getText(`https://jobs.jobvite.com/${company}/jobs`);
  await sleep(400);
  if (!list) { console.log(company, "no job list"); continue; }
  // Each row: the posting's link and title, then its place ("2 Locations" is no place).
  const rows = new Map();
  for (const m of list.matchAll(new RegExp(`<a href="/${company}/job/([A-Za-z0-9]+)"[^>]*>([\\s\\S]*?)</a>\\s*</td>\\s*<td class="jv-job-list-location">([\\s\\S]*?)</td>`, "g"))) {
    const place = clean(m[3]);
    rows.set(m[1], { title: clean(m[2]), location: /^\d+ locations?$/i.test(place) ? "" : place });
  }
  let ids = [...rows.keys()];
  if (only) ids = ids.filter((id) => only.split("+").includes(id));
  for (const id of ids.slice(0, PER)) {
    const html = await getText(`https://jobs.jobvite.com/${company}/job/${id}/apply`);
    await sleep(400);
    if (!html) { console.log(company, id, "no apply page"); continue; }
    const start = html.indexOf("constant('preloadedData'");
    if (start < 0) { console.log(company, id, "no preloadedData"); continue; }
    const body = html.slice(start, html.indexOf("</script>", start));
    const questions = [];
    for (const f of literalAfter(body, "applyFields") ?? []) {
      const q = toQuestion(f, "applyFields");
      if (q && q.type !== "input_file") questions.push(q);
    }
    for (const f of preScreening(literalAfter(body, "preScreeningFormFields"))) {
      const q = toQuestion(f, "preScreening");
      if (q && q.type !== "input_file") questions.push(q);
    }
    const row = rows.get(id);
    const title = row?.title || stringAfter(body, "jobTitle") || id;
    console.log(company, id, questions.length, "|", title.slice(0, 60), "|", row?.location ?? "");
    if (questions.length) bank.push({ board: company, id, title, company: stringAfter(body, "careersiteName") || company, location: row?.location ?? "", questions });
  }
}
writeFileSync(OUT, JSON.stringify(bank, null, 1));
console.log("postings", bank.length, "questions", bank.reduce((n, p) => n + p.questions.length, 0));
for (const [t, s] of seenTypes) console.log("TYPE", t, s);
