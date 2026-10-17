// @vitest-environment-options {"url": "https://job-boards.greenhouse.io/qbank/jobs/1"}
/**
 * QUESTION BANK (opt-in, never part of the normal run): real application
 * questions, read from a posting's public data, rendered as a plain form and
 * scanned by the extension's own scanner for each persona, so hundreds of
 * real questions can be reviewed in seconds instead of a live page each.
 *
 *   QBANK=test/e2e/results/qbank/greenhouse.json node node_modules/vitest/vitest.mjs run test/qbank.test.ts
 *
 * QBANK_URL puts the page on another ATS host (a Workable bank runs on
 * apply.workable.com, so the scanner picks that site's adapter, not Greenhouse's).
 *
 * Writes <bank>-out.json beside the bank: per posting and persona, every
 * question with the category the scanner gave it, its proposed answer, and
 * whether the device kept it from the AI. Read it with
 * test/e2e/tools/qbank-review.cjs. It reviews the DECISION, not the widget:
 * native selects here, where a live page may load its options on open.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { readFileSync, writeFileSync } from "node:fs";
import { stubLayout } from "./helpers/layout";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import { detectJobPlace } from "../src/content/jobLocation";
import * as PERSONAS from "./e2e/profiles.mjs";
import type { UserApplicationProfile } from "../src/shared/types";

interface BankQuestion {
  group: string;
  label: string;
  description?: string;
  required: boolean;
  name: string;
  type: string;
  options: string[];
}
interface BankPosting {
  board: string;
  id: string;
  title: string;
  company: string;
  location: string;
  questions: BankQuestion[];
}

const BANK = process.env.QBANK;
const WHO = (process.env.QBANK_PERSONAS ?? "COMPLETE_CANADIAN,US_H1B_SENIOR,BOOTCAMP_CAREER_GAP,US_OPT_ANALYST,BERLIN_STAFF,INDIA_NEW_GRAD").split(",");

const esc = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** One posting as a plain form: label + control per question, its place outside the form. */
function render(p: BankPosting): string {
  const rows = p.questions.map((q, i) => {
    const id = `q${i}`;
    const label = `${esc(q.label)}${q.required ? "*" : ""}`;
    const desc = q.description ? `<div id="${id}-d" class="description">${esc(q.description)}</div>` : "";
    const aria = q.description ? ` aria-describedby="${id}-d"` : "";
    if (q.type === "input_hidden") return "";
    if (q.type === "input_file") return `<div class="field"><label for="${id}">${label}</label><input type="file" id="${id}" name="${esc(q.name)}"></div>`;
    if (q.type === "input_number") return `<div class="field"><label for="${id}">${label}</label>${desc}<input type="number" id="${id}" name="${esc(q.name)}"${aria}></div>`;
    if (q.type === "input_date") return `<div class="field"><label for="${id}">${label}</label>${desc}<input type="date" id="${id}" name="${esc(q.name)}"${aria}></div>`;
    if (q.type === "textarea") return `<div class="field"><label for="${id}">${label}</label>${desc}<textarea id="${id}" name="${esc(q.name)}"${aria}></textarea></div>`;
    // A lone box under the text it accepts (Jobvite's "I Accept").
    if (q.type === "input_checkbox") return `<div class="field">${desc}<input type="checkbox" id="${id}" name="${esc(q.name)}" value="1"${aria}><label for="${id}">${label}</label></div>`;
    if (q.type === "multi_value_multi_select" && q.options.length) {
      const boxes = q.options.map((o, j) => `<label><input type="checkbox" name="${esc(q.name)}[]" value="${j}"> ${esc(o)}</label>`).join("");
      return `<fieldset class="field" id="${id}"><legend>${label}</legend>${desc}${boxes}</fieldset>`;
    }
    if (q.options.length) {
      const opts = [`<option value="">Select...</option>`, ...q.options.map((o, j) => `<option value="${j}">${esc(o)}</option>`)].join("");
      return `<div class="field"><label for="${id}">${label}</label>${desc}<select id="${id}" name="${esc(q.name)}"${aria}>${opts}</select></div>`;
    }
    return `<div class="field"><label for="${id}">${label}</label>${desc}<input type="text" id="${id}" name="${esc(q.name)}"${aria}></div>`;
  });
  return `<h1 class="job__title">${esc(p.title)}</h1><div class="job__location">${esc(p.location)}</div><form id="application-form">${rows.join("\n")}</form>`;
}

describe.skipIf(!BANK)("question bank", () => {
  let restore: () => void;
  beforeAll(() => {
    restore = stubLayout();
    const url = process.env.QBANK_URL;
    if (url) (globalThis as unknown as { jsdom: { reconfigure(o: { url: string }): void } }).jsdom.reconfigure({ url });
  });
  afterAll(() => restore());

  it("scans every posting for every persona", { timeout: 900_000 }, () => {
    const bank = JSON.parse(readFileSync(BANK!, "utf8")) as BankPosting[];
    const out: unknown[] = [];
    for (const p of bank) {
      for (const who of WHO) {
        const profile = (PERSONAS as Record<string, unknown>)[who] as UserApplicationProfile;
        document.body.innerHTML = render(p);
        const place = detectJobPlace(document);
        setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company: p.company });
        const { fields } = scanPage(profile, true);
        const byEl = new Map<Element, (typeof fields)[number]>();
        for (const f of fields) {
          const el = document.querySelector(`[data-ap-field="${f.id}"]`);
          if (el) byEl.set(el.closest(".field") ?? el, f);
        }
        const answers = p.questions.map((q, i) => {
          const holder = document.getElementById(`q${i}`)?.closest(".field") ?? document.querySelectorAll("form > .field")[i] ?? null;
          const f = holder ? byEl.get(holder) : undefined;
          return {
            group: q.group,
            label: q.label,
            options: q.options,
            type: q.type,
            category: f?.category ?? null,
            proposed: f?.proposedValue ?? null,
            abstained: Boolean(f?.deviceAbstained),
            scanned: Boolean(f),
          };
        });
        out.push({ board: p.board, id: p.id, title: p.title, location: p.location, place, persona: who, answers });
      }
    }
    writeFileSync(BANK!.replace(/\.json$/, "-out.json"), JSON.stringify(out, null, 1));
    expect(out.length).toBe(bank.length * WHO.length);
  });
});
