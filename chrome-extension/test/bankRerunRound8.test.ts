// @vitest-environment-options {"url": "https://job-boards.greenhouse.io/qbank/jobs/1"}
/**
 * What the last re-run of every bank (round 8, after the smaller ATSs'
 * fixes) caught: answers right before those fixes and wrong after them.
 * Labels and options verbatim from each bank (GET only). Tested through the
 * scan path.
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

const persona = (who: string) => (P as Record<string, unknown>)[who] as UserApplicationProfile;
const esc = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const ALL = ["COMPLETE_CANADIAN", "US_H1B_SENIOR", "BOOTCAMP_CAREER_GAP", "US_OPT_ANALYST", "BERLIN_STAFF", "INDIA_NEW_GRAD"];
const YN = ["Yes", "No"];
const each = (values: (string | null)[]): Record<string, string | null> => Object.fromEntries(ALL.map((w, i) => [w, values[i]]));

type Shape = "select" | "text" | "textarea" | "boxes";

/** One question on a page placed in `location`: each persona's proposed
 *  answer (null when blank). */
function scanned(label: string, options: string[], location: string, shape: Shape = options.length ? "select" : "text"): Record<string, string | null> {
  const row =
    shape === "boxes"
      ? `<fieldset class="field" id="q0"><legend>${esc(label)}</legend>${options.map((o, j) => `<label><input type="checkbox" name="q0[]" value="${j}"> ${esc(o)}</label>`).join("")}</fieldset>`
      : shape === "select"
        ? `<div class="field"><label for="q0">${esc(label)}</label><select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select></div>`
        : shape === "textarea"
          ? `<div class="field"><label for="q0">${esc(label)}</label><textarea id="q0" name="q0"></textarea></div>`
          : `<div class="field"><label for="q0">${esc(label)}</label><input type="text" id="q0" name="q0"></div>`;
  const out: Record<string, string | null> = {};
  for (const w of ALL) {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">${esc(location)}</div><form id="application-form">${row}<div class="field"><label for="em">Email Address</label><input type="text" id="em" name="cEmail"></div></form>`;
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company: "Acme" });
    const squash = (s: string): string => s.replace(/\s+/g, " ").trim();
    const f = scanPage(persona(w), true).fields.find((x) => squash(x.label).startsWith(squash(label).slice(0, 40)));
    out[w] = f?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}

describe("the last bank re-run (round 8)", () => {
  it("a major being pursued now is a student's: No for a graduate (Appian)", () => {
    const L = "Are you currently pursuing a Major in one of the following disciplines: Computer Science, Data Science, Information Systems, Economics, Systems Engineering, Mathematics or a related STEM field";
    expect(scanned(L, YN, "McLean, Virginia")).toEqual(each(["Yes", "No", "No", "No", "No", "No"]));
  });
  it("a PhD program's field is a doctorate's, never a master's (Pinterest)", () => {
    const L = "In 1–2 sentences, please describe your PhD program, including your research focus, specialization, or primary area of study.";
    expect(scanned(L, [], "San Francisco, CA, US", "textarea")).toEqual(each([null, null, null, null, null, null]));
  });
  it("a race option's own description names no second race ('…West Indian descent', Instacart)", () => {
    const options = [
      "White / Caucasian (A person having origins in any of the original peoples of Europe, the Middle East, or North Africa)",
      "Hispanic or Latinx (A person of Cuban, Mexican, Puerto Rican, South or Central American or other Spanish culture or origin regardless of race)",
      "Black or African-American (A person of African, Caribbean, West Indian descent)",
      "Asian or Asian-American (A person having origins in any of the original peoples of Asia and the Indian subcontinent including all 48 Asian countries - ie. Cambodia, China, India, Japan, Korea, Malaysia, Pakistan, the Philippine Islands, Thailand, Vietnam)",
      "Native Hawaiian or other Pacific Islander (A person having origins in any of the original peoples of Hawaii, Guam, Samoa, or other Pacific Islands)",
      "Indigenous Peoples (A person having origins in any of the original peoples including Aboriginal People, American Indian, Alaska Native, First Nations, Native American, or Alaska Native, Indigenous peoples from non-US countries)",
      "A race, ethnicity, or origin not shown",
      "I don't wish to answer",
    ];
    expect(scanned("What is your race and/or ethnicity? Please check all that apply.", options, "United States - Remote", "boxes")).toEqual(
      each([options[0], options[3], options[2], options[3], options[0], options[7]])
    );
  });
  it("a salary written with thousands dots ('€120.000') is 120,000 in a list of 'k' ranges (NISC)", () => {
    const options = ["less than 25k", "26-30k", "31-35k", "36-40k", "41-45k", "46-50k", "51-55k", "56-60k", "61-65k", "66-70k", "71-75k", "76-80k", "81-85k", "86-90k", "91-95k", "96-100k", "101-110k", "111-120k", "121-130K", "131-140K", "141-150K", "151-160K", "161-170K", "171K+"];
    // A list whose currency nothing names, on a posting whose place is not
    // read: each figure by its number.
    expect(scanned("Target Compensation", options, "Any NISC Location")).toEqual(each(["91-95k", "171K+", "81-85k", "91-95k", "111-120k", null]));
    // In the US: dollars, so no Canadian dollars or euros.
    expect(scanned("Target Compensation", options, "Lake St. Louis, MO")).toEqual(each([null, "171K+", "81-85k", "91-95k", null, null]));
  });
  it("a graduation semester asked is the month and year; a year alone gives nothing (decision 73; RF-SMART, Terranova, SDL)", () => {
    expect(scanned("What is your intended semester and year of graduation?", [], "Atlanta, GA")).toEqual(each(["April 2027", null, null, "May 2025", null, "June 2026"]));
    expect(scanned("Graduation Semester", [], "Seattle, WA")).toEqual(each(["April 2027", null, null, "May 2025", null, "June 2026"]));
  });
  it("a whole address abroad carries the stated state the region table lacks, a city-state once (Riot, FiscalNote)", () => {
    const a = scanned("What is your current full address?", [], "Los Angeles, CA");
    expect(a.INDIA_NEW_GRAD).toBe("14, 5th Cross, Indiranagar, Bengaluru, Karnataka 560038, India");
    expect(a.BERLIN_STAFF).toBe("Torstraße 109, Berlin, 10119, Germany");
    expect(a.US_H1B_SENIOR).toBe("1200 Westlake Ave N, Unit 805, Seattle, WA 98109");
  });
  it("a long-text box takes no street unless its label asks for an address ('Jane Street' in its description, Jane Street)", () => {
    const out: Record<string, string | null> = {};
    for (const w of ALL) {
      document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">Extra</label><div id="q0-d" class="description">Why you’re interested in Jane Street How you heard about us If you have a job now, why you’re looking for a new one</div><textarea id="q0" name="question_37020268002" aria-describedby="q0-d"></textarea></div></form>`;
      setResolveContext({ jobCountry: "US", jobCity: "New York", jobPlaces: null, company: "Jane Street" });
      out[w] = scanPage(persona(w), true).fields.find((x) => x.label.startsWith("Extra"))?.proposedValue ?? null;
    }
    setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
    expect(out).toEqual(each([null, null, null, null, null, null]));
  });
  it("an either-or after the question is no yes or no: how they heard (Credence)", () => {
    const L = "How did you hear about us? Was it a job board or an employee? Do tell....";
    expect(scanned(L, [], "McLean, Virginia, United States")).toEqual(each(["LinkedIn", "Company website", "Social media", "Job board", "Referral", "LinkedIn"]));
  });
});

/** What the final live regression of round 8 caught, from the same fixes:
 *  options verbatim from each posting's Greenhouse API (GET). */
describe("the final live regression (round 8)", () => {
  it("a level of education being pursued is a student's: blank for a graduate (Tenstorrent)", () => {
    // "Highest Degree Earned/Pursuing" (Axtria) counts a degree in progress;
    // this asks only for one, and graduates got their finished degree.
    const options = ["High School", "Associate's Degree", "Bachelor's Degree", "Master's Degree", "Master of Business Administration (M.B.A.)", "Juris Doctor (J.D.)", "Doctor of Philosophy (Ph.D.)", "Other"];
    expect(scanned("Current level of education pursuing", options, "Toronto, Ontario, Canada")).toEqual(each(["Bachelor's Degree", null, null, null, null, null]));
  });
  it("relocation support asked is the applicant's request, though its options say where they live (Scopely; the day session's decision 2)", () => {
    const options = [
      "Yes – I would be relocating for this role. (Choose this option if you do not currently reside in the required city or country and would need relocation support.)",
      "No – I am already based in the required location. (Choose this option if you already live in the city or country where the role is based.)",
      "No - I am not willing to relocate.",
    ];
    // Refusing to move asks for nothing: the stated No stays.
    expect(scanned("Will you require relocation support?", options, "US - San Francisco, California, United States")).toEqual(
      each([null, options[2], options[2], null, null, null])
    );
  });
});
