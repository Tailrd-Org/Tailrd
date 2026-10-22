// @vitest-environment-options {"url": "https://qbank.breezy.hr/p/000000000000-qbank/apply"}
/**
 * The Breezy question bank (round 8: prod's 8 active postings and up to six
 * more of each of its 7 companies', 43 postings, read blind). Labels and
 * options verbatim from each apply page's own data (GET only). Tested
 * through the scan path.
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
function scanned(who: string[], label: string, options: string[], location: string, company = "Acme"): Record<string, string | null> {
  const control = options.length
    ? `<select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select>`
    : `<input type="text" id="q0" name="q0">`;
  const out: Record<string, string | null> = {};
  for (const w of who) {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">${esc(location)}</div><form id="application-form"><div class="field"><label for="q0">${esc(label)}</label>${control}</div><div class="field"><label for="em">Email Address</label><input type="text" id="em" name="cEmail"></div></form>`;
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company });
    const squash = (s: string): string => s.replace(/\s+/g, " ").trim();
    const f = scanPage(persona(w), true).fields.find((x) => squash(x.label).startsWith(squash(label).slice(0, 40)));
    out[w] = f?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}

describe("the Breezy bank (round 8)", () => {
  it("the school one is enrolled at is the one in progress, never a graduate's old one (Gastronomous)", () => {
    expect(scanned(ALL, "What school are you enrolled at?", [], "Oakville, ON, Canada")).toEqual(each(["University of Waterloo", null, null, null, null, null]));
  });
  it("willing to undergo a security clearance process is the posting's requirement, accepted by applying (decision 49; VetsEZ)", () => {
    expect(scanned(ALL, "Are you willing to undergo a security clearance process?", YN, "Remote, United States")).toEqual(each(["Yes", "Yes", "Yes", "Yes", "Yes", "Yes"]));
  });
  it("of two No options, the one that says the applicant will not relocate (Zendar)", () => {
    const options = ["YES, and I would relocate to work at Zendar.", "YES, and I already live locally.", "NO, I am looking for a remote-only job.", "NO, I am not able to relocate for this role."];
    const yes = options[0];
    const no = options[3];
    expect(scanned(ALL, "I acknowledge this posting is for an on-site position.", options, "Berkeley, CA", "Zendar")).toEqual(each([yes, no, no, yes, yes, yes]));
  });
  it("a language besides English spoken fluently: the listed one, else Other; none at that level is No (JND)", () => {
    const options = ["No", "Spanish", "Korean", "Chinese", "French", "Japanese", "German", "Portuguese", "Hindi", "Arabic", "Other"];
    expect(scanned(ALL, "Do you have the ability to read, write, speak, and communicate fluently in any language other than English?", options, "Plano, TX", "JND")).toEqual(
      each(["French", "Other", "No", "Korean", "German", "Other"])
    );
  });
  it("why the applicant is a good fit is no skills list (Zendar)", () => {
    const L = "What makes you a good fit for this position? What unique skill or contribution do you think you would bring to the company? [We value authentic answers written by humans. An application with an AI generated generic answer will be disqualified.]";
    expect(scanned(ALL, L, [], "Berkeley, CA", "Zendar")).toEqual(each([null, null, null, null, null, null]));
  });
  it("details of employment restrictions are the applicant's, even described with a visa's words (JND)", () => {
    const L = "(Optional) Please provide details if there are any conditions or restrictions for employment lasting more than 2 years.";
    expect(scanned(ALL, L, [], "DFW, TX, United States", "JND")).toEqual(each([null, null, null, null, null, null]));
    // One posting describes it "e.g., VISA Sponsorship or Transfer Needed,
    // EAD Expiration Date, etc.": read as sponsorship, it got the profile's
    // own sponsorship answer ("No" for a Canadian on a US job).
    const status = ["Yes (Permanently)", "Yes (Can work more than 2 years)", "Yes (Can work less than 2 years)", "No, I require VISA sponsorship before the employment."];
    for (const w of ALL) {
      document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">Plano, TX</div><form id="application-form"><div class="field"><label for="q6">Are you authorized to work for any employer in the US?</label><select id="q6" name="q6"><option value="">Select...</option>${status.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select></div><div class="field"><label for="q7">${esc(L)}</label><div id="q7-d" class="description">e.g., VISA Sponsorship or Transfer Needed, EAD Expiration Date, etc.</div><input type="text" id="q7" name="q7" aria-describedby="q7-d"></div></form>`;
      setResolveContext({ jobCountry: "US", jobCity: "Plano", jobPlaces: null, company: "JND" });
      expect(scanPage(persona(w), true).fields.find((x) => x.label.startsWith("(Optional) Please provide"))?.proposedValue ?? null, w).toBeNull();
    }
  });
  it("a STEM major from the degrees held; none for someone with no college major (JND)", () => {
    expect(scanned(ALL, "Did you major in STEM (Science, Technology, Engineering, Math)?", YN, "Plano, TX", "JND")).toEqual(each(["Yes", "Yes", null, "Yes", "Yes", "Yes"]));
  });
  it("a city and state outside North America is the stated state, not the city alone (JND, VetsEZ)", () => {
    expect(scanned(["INDIA_NEW_GRAD", "US_OPT_ANALYST", "BERLIN_STAFF"], "What is your current location? (City, State)", [], "Plano, TX", "JND")).toEqual({ INDIA_NEW_GRAD: "Bengaluru, Karnataka", US_OPT_ANALYST: "Boston, MA", BERLIN_STAFF: "Berlin" });
  });
});
