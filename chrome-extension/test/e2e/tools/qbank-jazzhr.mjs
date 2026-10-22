// JazzHR question bank builder (GET only). Usage:
//   node test/e2e/tools/qbank-jazzhr.mjs <company[:id+id],...> test/e2e/results/qbank/jazzhr.json [jobsPerCompany=4]
// then: QBANK=test/e2e/results/qbank/jazzhr.json QBANK_URL="https://qbank.applytojob.com/apply/QBANK0001/qbank" \
//   node node_modules/vitest/vitest.mjs run test/qbank.test.ts
// Companies: prod scraped_jobs urls like <company>.applytojob.com/apply/<id>/...
// (read-only SELECT). A company's job list (/apply) names its open jobs, and
// each job's apply page is served with its whole form: the standard fields
// and its questionnaire (selects, text, checkbox groups). Nothing is sent.
// Output: the qbank format test/qbank.test.ts reads (Greenhouse vocabulary).
import { writeFileSync } from "node:fs";
import { JSDOM } from "jsdom";

const SPECS = process.argv[2].split(",");
const OUT = process.argv[3];
const PER = Number(process.argv[4] ?? 4);
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
const clean = (s) => String(s ?? "").replace(/\s+/g, " ").trim();
/** A label without its required marker, or the control it wraps (the EEO
 *  selects sit inside their labels). */
const labelText = (el) => {
  if (!el) return "";
  const c = el.cloneNode(true);
  c.querySelectorAll("i.asterisk, .asterisk, select, option, input, textarea").forEach((x) => x.remove());
  return clean(c.textContent).replace(/\s*\*$/, "");
};
const isRequired = (el) => Boolean(el && (el.querySelector("i.asterisk, .asterisk") || /\*\s*$/.test(clean(el.textContent))));
const PLACEHOLDER_OPTION = /^(--.*--|select\b.*|choose\b.*|please select.*)$/i;
// The paste-a-résumé box and the parser's hidden XML: not questions.
const SKIP_NAMES = /^resumator-(resumetext|xml)-value$/;

function questionsOf(doc) {
  const form = doc.querySelector("form#form_submit_new_resume") ?? doc.querySelector("form[action*='/apply/']");
  if (!form) return null;
  const out = [];
  const doneGroups = new Set();
  for (const el of form.querySelectorAll("input, select, textarea")) {
    if (["hidden", "submit", "button", "reset", "image"].includes(el.type)) continue;
    if (SKIP_NAMES.test(el.name)) continue;
    const box = el.closest(".form-group");
    const questionnaire = /^resumator-(questionnaire|checkbox|radio)/.test(el.name) || /resumator-questionnaire/.test(el.className);
    const group = questionnaire ? "Questions" : /eeo/i.test(el.name) ? "EEO" : "Application";
    // A checkbox or radio group answers one question: its label is the group's.
    // Questionnaire boxes share a .form-group (each box has a name of its own);
    // the EEO radios share a name under a .resumator-label heading.
    if (el.type === "checkbox" || el.type === "radio") {
      const wrapper = el.closest(".resumator-field-wrapper");
      const scope = el.type === "radio" ? (wrapper ?? box ?? form) : box;
      const members = !scope ? [] : el.type === "radio"
        ? [...scope.querySelectorAll("input[type=radio]")].filter((i) => i.name === el.name)
        : [...scope.querySelectorAll("input[type=checkbox]")];
      if (members.length > 1) {
        const key = el.type === "radio" ? `radio:${el.name}` : scope;
        if (doneGroups.has(key)) continue;
        doneGroups.add(key);
        const head = el.type === "radio" && wrapper ? wrapper.querySelector(".resumator-label") : scope.querySelector("label.control-label");
        const options = members.map((i) => labelText(i.closest("label") ?? form.querySelector(`label[for="${i.id}"]`))).filter(Boolean);
        let label = labelText(head);
        if (!label) {
          // No heading of its own: the text just before the group (the EEO
          // disability radios sit under "Please check one of the boxes below:").
          let prev = (scope.closest(".resumator-input") ?? scope).previousElementSibling;
          while (prev && !clean(prev.textContent)) prev = prev.previousElementSibling;
          label = clean(prev?.textContent) || options[0] || "";
        }
        out.push({ group, label, description: "", required: isRequired(head), name: el.name.replace(/-\d+$/, ""), type: el.type === "checkbox" ? "multi_value_multi_select" : "multi_value_single_select", options });
        continue;
      }
    }
    const lab = (el.id && form.querySelector(`label[for="${el.id}"]`)) || el.closest("label");
    let label = labelText(lab) || clean(el.getAttribute("placeholder")) || clean(el.getAttribute("aria-label"));
    if (!label) label = el.name.replace(/^resumator-|-value$/g, "");
    const required = isRequired(lab) || el.required;
    let type = "input_text";
    let options = [];
    if (el.tagName === "SELECT") {
      options = [...el.options].map((o) => clean(o.textContent)).filter((o) => o && !PLACEHOLDER_OPTION.test(o));
      type = el.multiple ? "multi_value_multi_select" : "multi_value_single_select";
    } else if (el.tagName === "TEXTAREA") type = "textarea";
    else if (el.type === "file") type = "input_file";
    else if (el.type === "number") type = "input_number";
    else if (el.type === "date") type = "input_date";
    else if (el.type === "checkbox") type = "input_checkbox";
    out.push({ group, label, description: "", required, name: el.name, type, options });
  }
  return out;
}

const bank = [];
const kinds = new Map();
const read = new Set();
for (const spec of SPECS) {
  const [company, idList] = spec.split(":");
  const base = `https://${company}.applytojob.com`;
  let jobs = [];
  if (idList) jobs = idList.split("+").map((id) => `${base}/apply/${id}`);
  else {
    const list = await get(`${base}/apply`);
    await sleep(300);
    if (!list) { console.log(company, "no job list"); continue; }
    const d = new JSDOM(list, { url: `${base}/apply` }).window.document;
    const seen = new Set();
    for (const a of d.querySelectorAll("a[href*='/apply/']")) {
      const m = /\/apply\/([A-Za-z0-9]{6,})(\/|$)/.exec(a.href);
      if (m && !seen.has(m[1])) { seen.add(m[1]); jobs.push(a.href); }
    }
    jobs = jobs.slice(0, PER);
  }
  for (const url of jobs) {
    const jobId = /\/apply\/([A-Za-z0-9]+)/.exec(url)?.[1] ?? url;
    if (read.has(`${company}/${jobId}`)) continue;
    read.add(`${company}/${jobId}`);
    const html = await get(url);
    await sleep(300);
    if (!html) { console.log(company, "gone", url); continue; }
    const doc = new JSDOM(html).window.document;
    const questions = questionsOf(doc);
    if (!questions) { console.log(company, "no form", url); continue; }
    for (const q of questions) kinds.set(q.type, (kinds.get(q.type) ?? 0) + 1);
    const titleTag = clean(doc.title).replace(/\s*-\s*Career Page$/i, "");
    const [title, ...rest] = titleTag.split(" - ");
    const location = clean(doc.querySelector("[title='Location']")?.textContent);
    console.log(company, jobId, questions.length, "|", title, "|", location);
    bank.push({ board: company, id: jobId, title: clean(title), company: clean(rest.join(" - ")) || company, location, questions });
  }
}
writeFileSync(OUT, JSON.stringify(bank, null, 1));
console.log("postings", bank.length, "questions", bank.reduce((n, p) => n + p.questions.length, 0));
for (const [k, n] of kinds) console.log("KIND", k, n);
