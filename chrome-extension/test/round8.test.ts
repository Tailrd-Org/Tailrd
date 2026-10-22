// @vitest-environment-options {"url": "https://jobs.lever.co/qbank/00000000-0000-0000-0000-000000000000/apply"}
/**
 * Round 8 (2026-10-21): the calls round 7 left open, and what round 8's live
 * pages and banks found. Labels and options verbatim from each page (GET only).
 * Tested through the scan path: a category can answer a field before any
 * question rule reads it.
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
const YN = ["Yes", "No"];

/** One question on a page placed in `location`: the scanner's proposed answer
 *  for each persona named (null when blank). */
function scanned(who: string[], label: string, options: string[], location: string): Record<string, string | null> {
  const control = options.length
    ? `<select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select>`
    : `<input type="text" id="q0" name="q0">`;
  const out: Record<string, string | null> = {};
  for (const w of who) {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">${esc(location)}</div><form id="application-form"><div class="field"><label for="q0">${esc(label)}</label>${control}</div><div class="field"><label for="em">Email</label><input type="text" id="em" name="email"></div></form>`;
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company: "Acme" });
    const f = scanPage(persona(w), true).fields.find((x) => x.label.startsWith(label.slice(0, 40)));
    out[w] = f?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}

describe("which of the posting's locations the applicant is applying for (decision 93; Azul on Lever, round 7)", () => {
  const Q = "Which location are you applying for?";
  it("the applicant's own city when the posting lists it", () => {
    const got = scanned(["US_GREENCARD_STUDENT", "US_H1B_SENIOR"], Q, ["San Jose, CA", "Seattle, WA", "United States - Remote"], "San Jose, CA");
    expect(got).toEqual({ US_GREENCARD_STUDENT: "San Jose, CA", US_H1B_SENIOR: "Seattle, WA" });
  });
  it("else the remote option of the applicant's own country: a role they can do where they live", () => {
    // Azul: a Boston-area student among Sunnyvale and "United States - Remote".
    const got = scanned(["US_F1_STUDENT", "US_OPT_ANALYST", "US_VETERAN"], Q, ["Sunnyvale, CA", "United States - Remote"], "Sunnyvale, CA");
    expect(got).toEqual({ US_F1_STUDENT: "United States - Remote", US_OPT_ANALYST: "United States - Remote", US_VETERAN: "United States - Remote" });
  });
  it("the remote option among several offices of the applicant's country", () => {
    const got = scanned(["US_OPT_ANALYST", "BOOTCAMP_CAREER_GAP"], Q, ["New York, NY, United States", "Austin, TX, United States", "Remote - United States"], "New York, NY");
    expect(got).toEqual({ US_OPT_ANALYST: "Remote - United States", BOOTCAMP_CAREER_GAP: "Remote - United States" });
  });
  it("never another country's remote option, nor an office abroad: nothing the applicant stated picks one", () => {
    // A Canadian among Sunnyvale and "United States - Remote"; a German among
    // two US offices. Which office elsewhere is the applicant's call.
    const got = scanned(["COMPLETE_CANADIAN", "SPARSE_CANADIAN", "BERLIN_STAFF"], Q, ["Sunnyvale, CA", "United States - Remote"], "Sunnyvale, CA");
    expect(got).toEqual({ COMPLETE_CANADIAN: null, SPARSE_CANADIAN: null, BERLIN_STAFF: null });
    const two = scanned(["US_OPT_ANALYST", "BERLIN_STAFF"], Q, ["San Francisco, CA", "Toronto, ON"], "San Francisco, CA");
    expect(two).toEqual({ US_OPT_ANALYST: null, BERLIN_STAFF: null });
  });
});

describe("a family member's government employment is the applicant's to answer (decision 94; ServiceNow on SmartRecruiters)", () => {
  const D =
    "D. Within the past 5 years, has a member of your Immediate Family (spouse or child ONLY) ever been an employee of a federal, state, local, municipal, provincial or quasi-governmental entity in the U.S. or any other country?";
  it("blank for everyone, never the relatives default's No", () => {
    const got = scanned(["US_F1_STUDENT", "US_H1B_SENIOR", "UK_SENIOR", "COMPLETE_CANADIAN"], D, YN, "Santa Clara, CA");
    expect(got).toEqual({ US_F1_STUDENT: null, US_H1B_SENIOR: null, UK_SENIOR: null, COMPLETE_CANADIAN: null });
  });
  it("the applicant's own government employment, asked beside it, still answers from the history", () => {
    const C =
      "C. Within the past 5 years, have you ever been an employee of a state, local, or municipal government entity in the U.S., or any governmental or quasi-governmental entity in another country?";
    const got = scanned(["US_F1_STUDENT", "UK_SENIOR"], C, YN, "Santa Clara, CA");
    expect(got).toEqual({ US_F1_STUDENT: "No", UK_SENIOR: "No" });
  });
  it("relatives at the company itself keep the relatives default", () => {
    const got = scanned(["US_F1_STUDENT"], "Do you have any relatives that are currently employed by ServiceNow?", YN, "Santa Clara, CA");
    expect(got).toEqual({ US_F1_STUDENT: "No" });
  });
});
