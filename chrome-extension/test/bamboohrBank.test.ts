// @vitest-environment-options {"url": "https://qbank.bamboohr.com/careers/1"}
/**
 * The BambooHR question bank (round 8: prod's one active posting and up to six
 * more of each of its 6 companies', 33 postings, read blind). Labels and
 * options verbatim from each job's detail JSON (GET only). Tested
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

const YN = ["Yes", "No"];

describe("the BambooHR bank (round 8)", () => {
  it("a yes or no asked after a comma takes a yes or no in a text box, never the current title (Solace)", () => {
    const L = "This position would be 2 days in-office in the Ottawa area, is this something that will work for you?";
    const select = scanned(ALL, L, YN, "Ottawa, Ontario, Canada", "solace");
    expect(select).toEqual(each(["Yes", "No", "No", "Yes", "Yes", "Yes"]));
    expect(scanned(ALL, L, [], "Ottawa, Ontario, Canada", "solace")).toEqual(select);
  });
  it("years in an industry named are that industry's, never the career's (Picton Mahoney)", () => {
    expect(scanned(ALL, "How many years experience fo you have in the financial services industry?", [], "Toronto, Ontario, Canada", "pictonmahoney")).toEqual(each([null, null, null, null, null, null]));
  });
  it("the European Union is an EU citizen's to work in; nobody else's is known (Solace)", () => {
    const L = "Are you currently eligible to work in the European Union for any employer?";
    const S = "Are you currently eligible to work in the European Union for any employer without sponsorship?";
    // "German citizen (EU)" gives no status under the allow-list (D-61).
    expect(scanned(ALL, L, [], "", "solace")).toEqual(each([null, null, null, null, null, null]));
    expect(scanned(ALL, S, YN, "", "solace")).toEqual(each([null, null, null, null, null, null]));
    // A German citizen, so stated, may work anywhere in the Union.
    const german = { ...persona("BERLIN_STAFF"), workAuthorization: "German citizen" };
    const one = (label: string, options: string[]): string | null => {
      const control = options.length ? `<select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select>` : `<input type="text" id="q0" name="q0">`;
      document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">${esc(label)}</label>${control}</div></form>`;
      setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "solace" });
      return scanPage(german, true).fields.find((x) => x.label.startsWith(label.slice(0, 40)))?.proposedValue ?? null;
    };
    expect(one(L, [])).toBe("Yes");
    expect(one(S, YN)).toBe("Yes");
    setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  });
  it("having managed onsite execution is experience, never an in-person requirement (Profound Medical)", () => {
    const L = "Have you managed end-to-end event logistics including vendors, booth planning, contracts, travel, and onsite execution?";
    expect(scanned(ALL, L, YN, "Mississauga, Ontario, Canada", "profoundmedical")).toEqual(each([null, null, null, null, null, null]));
  });
  it("a Bachelor of Technology is the Science one of a level's kinds (Profound Medical)", () => {
    const options = ["GED or Equivalent", "High School", "Some College", "College - Associates", "College - Bachelor of Arts", "College - Bachelor of Fine Arts", "College - Bachelor of Science", "College - Master of Arts", "College - Master of Fine Arts", "College - Master of Science", "College - Master of Business Administration", "College - Doctorate", "Medical Doctor", "Other"];
    expect(scanned(ALL, "Highest Education Obtained", options, "", "profoundmedical")).toEqual(
      each(["Some College", "College - Master of Science", "High School", "College - Master of Science", null, "College - Bachelor of Science"])
    );
  });
});
