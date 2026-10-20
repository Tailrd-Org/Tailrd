// @vitest-environment-options {"url": "https://jobs.smartrecruiters.com/oneclick-ui/company/qbank/publication/00000000-0000-0000-0000-000000000000"}
/**
 * The SmartRecruiters question bank (round 7, 2026-10-20): 41 postings of 24
 * SmartRecruiters companies in prod scraped_jobs, each posting's screening
 * questions read from its one-click page (GET only) and answered for six
 * personas; and round 7's live Greenhouse boards. Each test is an answer the
 * bank or a live page got wrong, or a blank a stated fact answers; labels and
 * options verbatim.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import { detectJobPlace } from "../src/content/jobLocation";
import * as P from "./e2e/profiles.mjs";
import type { UserApplicationProfile } from "../src/shared/types";
import { stubLayout } from "./helpers/layout";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());
beforeEach(() => {
  document.body.innerHTML = "";
});

const ALL = ["COMPLETE_CANADIAN", "US_H1B_SENIOR", "BOOTCAMP_CAREER_GAP", "US_OPT_ANALYST", "BERLIN_STAFF", "INDIA_NEW_GRAD"] as const;
type Who = (typeof ALL)[number];
const persona = (who: Who) => (P as Record<string, unknown>)[who] as UserApplicationProfile;
const esc = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const YN = ["Yes", "No"];

interface Q {
  label: string;
  options?: string[];
  type?: "select" | "text" | "textarea" | "checkbox";
  description?: string;
}

/** A posting's questions as the bank renders them (one form, its place
 *  outside it); each persona's proposed answer to the question `ask`. */
function answers(questions: Q[], ask: string, location = "San Francisco, CA, United States", company = "Acme"): Record<Who, string | null> {
  const rows = questions.map((q, i) => {
    const id = `q${i}`;
    const type = q.type ?? (q.options?.length ? "select" : "text");
    const desc = q.description ? `<div id="${id}-d" class="description">${esc(q.description)}</div>` : "";
    const aria = q.description ? ` aria-describedby="${id}-d"` : "";
    if (type === "checkbox") return `<div class="field">${desc}<input type="checkbox" id="${id}" name="sr_${i}" value="1"${aria}><label for="${id}">${esc(q.label)}</label></div>`;
    const control =
      type === "select"
        ? `<select id="${id}" name="sr_${i}"${aria}><option value="">Select...</option>${(q.options ?? []).map((o, j) => `<option value="${j}">${esc(o)}</option>`).join("")}</select>`
        : type === "textarea"
          ? `<textarea id="${id}" name="sr_${i}"${aria}></textarea>`
          : `<input type="text" id="${id}" name="sr_${i}"${aria}>`;
    return `<div class="field"><label for="${id}">${esc(q.label)}</label>${desc}${control}</div>`;
  });
  const out = {} as Record<Who, string | null>;
  for (const who of ALL) {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">${esc(location)}</div><form id="application-form">${rows.join("")}</form>`;
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company });
    const { fields } = scanPage(persona(who), true);
    const holder = document.getElementById(`q${questions.findIndex((q) => q.label === ask)}`)?.closest(".field");
    const f = fields.find((x) => document.querySelector(`[data-ap-field="${x.id}"]`)?.closest(".field") === holder);
    out[who] = f?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}

/** One question alone. */
const one = (q: Q, location?: string, company?: string): Record<Who, string | null> => answers([q], q.label, location, company);
const every = (v: string | null): Record<Who, string | null> => Object.fromEntries(ALL.map((w) => [w, v])) as Record<Who, string | null>;

describe("work-right rules claim only work-right questions", () => {
  it("holding OPT is an F-1 student's (Resultant)", () => {
    // Read as "are you authorized?": an H-1B holder and a US citizen said Yes.
    expect(one({ label: "Do you currently hold Optional Practical Training (OPT) work authorization?", options: YN }, "Indianapolis, IN, United States")).toEqual({
      COMPLETE_CANADIAN: "No",
      US_H1B_SENIOR: "No",
      BOOTCAMP_CAREER_GAP: "No",
      US_OPT_ANALYST: "Yes",
      BERLIN_STAFF: "No",
      INDIA_NEW_GRAD: "No",
    });
  });

  it("an agreement restricting the right to work for the employer is a non-compete (Western Digital)", () => {
    // "…restricts your right to work for WD…" read as the right to work: Yes
    // for everyone authorized in the US.
    const label =
      "Are you currently party to any employment, consulting or personal services agreement that (i) restricts your right to terminate such agreements or (ii) restricts your right to work for WD immediately upon hire?";
    expect(one({ label, options: YN }, "Rochester, MN, United States", "Western Digital")).toEqual(every("No"));
  });

  it("eligible to work without required sponsorship, now or in the future (M3USA)", () => {
    // "without required sponsorship" was no sponsorship question: the H-1B
    // and OPT holders said Yes.
    expect(one({ label: "Are you eligible to work at M3USA without required sponsorship now or in the future?", options: YN }, "Fort Washington, PA, United States", "M3USA")).toEqual({
      COMPLETE_CANADIAN: "No",
      US_H1B_SENIOR: "No",
      BOOTCAMP_CAREER_GAP: "Yes",
      US_OPT_ANALYST: "No",
      BERLIN_STAFF: "No",
      INDIA_NEW_GRAD: "No",
    });
  });

  it("obligations that would keep the applicant from joining are a non-compete too (Axon, live)", () => {
    const label = "Do you have any contractual obligations, agreements, relationships, or commitments to another person or entity that would impact, impede or interfere with your ability to join Axon?";
    expect(one({ label, options: ["Yes", "No", "Maybe / Would like to discuss"] }, "Boston, Massachusetts, United States", "Axon")).toEqual(every("No"));
  });
});

describe("a former employer is asked about, not a hypothetical one", () => {
  it("Wellmark's certification certifies (its 'if employed by Wellmark' is no question)", () => {
    // Its last sentence runs on past "Wellmark, Inc." into "I have read…", so
    // "if employed by Wellmark" read as "have you been employed by
    // Wellmark?": No, for everyone, to a certification.
    const label =
      "VERIFICATION TO BE COMPLETED ELECTRONICALLY AFTER COMPLETION OF INITIAL APPLICATION I certify answers given herein are true and complete and I understand this information may be verified as part of Wellmark's standard background check process. False or misleading information provided in the application process may result in the rejection of the application. I hereby understand and acknowledge that, unless otherwise defined by applicable law, any employment relationship with Wellmark, Inc. is an at will nature, which means that the Employee may resign at any time and the Employer may discharge Employee at any time and with or without notice. In the event of employment, I understand that false or misleading information given in my application, interview(s), or any supplemental information in the employment process may result in discharge. I understand also, if employed by Wellmark, I am required to abide by its Code of Conduct and all other rules, regulations, and policies of Wellmark, Inc. I have read and understand the above statements and certify that the answers given herein are true and complete";
    expect(one({ label, options: YN }, "Des Moines, IA, United States", "Wellmark, Inc.")).toEqual(every("Yes"));
  });

  it("an internship paid by the employer is a job there (LLNL)", () => {
    // Read as "have you had an internship?": Yes for three applicants who
    // never worked at LLNL.
    expect(one({ label: "Have you had a job or internship paid by LLNL?", options: YN }, "Livermore, CA, United States", "Lawrence Livermore National Laboratory")).toEqual(every("No"));
  });
});
