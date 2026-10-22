// @vitest-environment-options {"url": "https://recruiting.paylocity.com/Recruiting/Jobs/Apply/1"}
/**
 * The Paylocity question bank (round 8: prod's 4 live postings and the other
 * jobs of their companies, 19 postings, read blind). Labels and options
 * verbatim from each apply page's own data (GET only). Tested through the
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

/** One question on a page placed in `location` for `company`: each persona's
 *  proposed answer (null when blank). */
function scanned(who: string[], label: string, options: string[], location: string, company = "Acme", multi = false): Record<string, string | null> {
  const control = options.length
    ? `<select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select>`
    : `<input type="text" id="q0" name="q0">`;
  // A screener question allowing several answers: a box per option.
  const row = multi
    ? `<fieldset class="field" id="q0"><legend>${esc(label)}</legend>${options.map((o, j) => `<label><input type="checkbox" name="q0[]" value="${j}"> ${esc(o)}</label>`).join("")}</fieldset>`
    : `<div class="field"><label for="q0">${esc(label)}</label>${control}</div>`;
  const out: Record<string, string | null> = {};
  for (const w of who) {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">${esc(location)}</div><form id="application-form">${row}<div class="field"><label for="em">Email Address</label><input type="text" id="em" name="cEmail"></div></form>`;
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company });
    const squash = (s: string): string => s.replace(/\s+/g, " ").trim();
    const f = scanPage(persona(w), true).fields.find((x) => squash(x.label).startsWith(squash(label).slice(0, 40)));
    out[w] = f?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}

describe("the Paylocity bank (round 8)", () => {
  it("residing in one of the states listed by their codes is the home state's (Lightology)", () => {
    const L = "Are you residing in any of the following states: AL, CO, CT, GA, IL, IN, MD, MI, NJ, NY, OH, TN, TX, FL, and WY?";
    expect(scanned(ALL, L, YN, "Chicago, IL", "Lightology LLC")).toEqual(each(["No", "No", "Yes", "No", "No", "No"]));
  });
  it("salary ranges written in thousands ('$40-45k', '$55k+') hold the stated salary (Choice Solutions)", () => {
    const options = ["$40-45k", "$45-50k", "$50-55k", "$55k+"];
    expect(scanned(ALL, "What are your salary expectations for a role like this?", options, "Overland Park, KS", "Choice Solutions LLC")).toEqual(each([null, "$55k+", "$55k+", "$55k+", null, null]));
    // As the page asks it, a box per range: "$185,000" is one figure, never
    // "$185" and "000".
    expect(scanned(ALL, "What are your salary expectations for a role like this?", options, "Overland Park, KS", "Choice Solutions LLC", true)).toEqual(each([null, "$55k+", "$55k+", "$55k+", null, null]));
  });
  it("a final-year student's academic status is the senior year; a certificate holder's, the completed certification program (ATB)", () => {
    const options = ["Undergraduate – Freshman", "Undergraduate – Sophomore", "Undergraduate – Junior", "Undergraduate – Senior", "Graduate (Master’s)", "Graduate (Doctorate)", "Technical, Trade, Vocational, or Certification Program (In Progress)", "Technical, Trade, Vocational, or Certification Program (Completed)"];
    expect(scanned(ALL, "What is your current academic status?", options, "St. Louis, MO", "ATB Technologies")).toEqual(
      each([options[3], null, options[7], null, null, null])
    );
  });
  it("a graduation date asked by month and year gives nothing from a year alone (decision 73; ATB)", () => {
    expect(scanned(ALL, "What is your expected graduation date? (Month/Year)", [], "St. Louis, MO", "ATB Technologies")).toEqual(each(["April 2027", null, null, "May 2025", null, "June 2026"]));
  });
  it("years with languages are a skill's, never capped by the career (Lightology)", () => {
    const options = ["Less than 1 year", "1-2 years", "3-4 years", "Over 4 years"];
    expect(scanned(ALL, "How many years of experience do you have with backend web development languages such as PHP, JavaScript?", options, "Chicago, IL", "Lightology LLC")).toEqual(each([null, null, null, null, null, null]));
    // A role's years stay capped by the career (decision 106).
    expect(scanned(ALL, "How many years of experience do you have working as a web developer?", options, "Chicago, IL", "Lightology LLC")).toEqual(each([null, null, null, null, null, "Less than 1 year"]));
  });
});
