// @vitest-environment-options {"url": "https://qbank.pinpointhq.com/en/postings/00000000-0000-0000-0000-000000000000/applications/new"}
/**
 * The Pinpoint question bank (round 8: prod's 8 active postings and up to six
 * more of each of its 4 companies', 31 postings, read blind). Labels and
 * options verbatim from each application page's own data (GET only). Tested
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

const FE = "Franklin Electric";

describe("the Pinpoint bank (round 8)", () => {
  it("the employer's own site where one works is no home address (Franklin Electric)", () => {
    expect(scanned(ALL, "What Franklin Electric location do you currently work from?", [], "Fort Wayne, Indiana", FE)).toEqual(each([null, null, null, null, null, null]));
  });
  it("a current salary is never the desired one (Franklin Electric)", () => {
    expect(scanned(ALL, "What is your current Annual Salary (CTC)?", [], "Vadodara, India", FE)).toEqual(each([null, null, null, null, null, null]));
  });
  it("currently studying is being enrolled now (Franklin Electric)", () => {
    expect(scanned(ALL, "Are you currently studying?", [], "Monterrey, Mexico", FE)).toEqual(each(["Yes", "No", "No", "No", "No", "No"]));
  });
  it("ever been interviewed by the company is the prior-application default, No (Franklin Electric)", () => {
    expect(scanned(ALL, "Have you ever been interviewed by Pluga Franklin Electric earlier?", [], "Vadodara, India", FE)).toEqual(each(["No", "No", "No", "No", "No", "No"]));
  });
  it("a bond agreement with the present employer is the non-compete default, No (Franklin Electric)", () => {
    expect(scanned(ALL, "Do you have contract/bond agreement with the present employer?", [], "Vadodara, India", FE)).toEqual(each(["No", "No", "No", "No", "No", "No"]));
  });
  it("'No Response' is a list's decline (Franklin Electric)", () => {
    expect(scanned(ALL, "Gender", ["Male", "Female", "Non-Binary", "No Response"], "Fort Wayne, Indiana", FE)).toEqual(each(["Female", "Female", "Male", "Male", "Male", "No Response"]));
  });
  it("'Gay or Lesbian' is Homosexual, never Lesbian for a man (White Swan)", () => {
    expect(scanned(ALL, "Sexual Orientation", ["Heterosexual", "Bisexual", "Homosexual", "Lesbian"], "London, United Kingdom", "White Swan Data")).toEqual(
      each(["Heterosexual", "Heterosexual", null, "Homosexual", "Bisexual", null])
    );
  });
  it("one race is never a mixed option, and a race the list splits by origin is left to the applicant (White Swan)", () => {
    const options = ["White British", "White Irish", "Black British", "Black African", "Black Caribbean", "Black And White Caribbean", "Black And White African", "Asian And White", "Chinese", "Bangladeshi", "Indian", "Pakistani", "Other Ethnicity"];
    expect(scanned(ALL, "Ethnicity", options, "London, United Kingdom", "White Swan Data")).toEqual(each([null, null, null, null, null, null]));
  });
  it("a protected veteran who keeps the category to themself is no decline (White Swan, Flatirons)", () => {
    const options = ["Active Wartime Or Campaign Badge Veteran", "Armed Forces Service Medal Veteran", "Disabled Veteran", "I Am A Protected Veteran, But I Choose Not To Self-Identify The Classifications To Which I Belong", "I Am Not A Protected Veteran", "I Am Not A Veteran", "I Choose Not To Identify", "Recently Separated Veteran"];
    const never = "I Am Not A Veteran";
    expect(scanned(ALL, "Veteran Status", options, "London, United Kingdom", "White Swan Data")).toEqual(each([never, never, never, never, never, "I Choose Not To Identify"]));
    // Two declines: either says it; the first listed.
    expect(scanned(["INDIA_NEW_GRAD"], "Veteran Status", [...options, "Prefer Not To Say"], "Chennai, Tamil Nadu, India", "Flatirons Solutions")).toEqual({ INDIA_NEW_GRAD: "I Choose Not To Identify" });
  });
});
