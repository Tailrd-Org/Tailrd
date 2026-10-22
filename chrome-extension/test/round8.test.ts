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

describe("local to the DMV (Significance on Lever, live, round 8)", () => {
  const Q = "Are you local to the DMV to accomodate a hybrid work schedule?";
  it("the DMV is Washington, DC and its Maryland and Virginia suburbs", () => {
    // US_VETERAN lives in Austin, TX: sent to the AI live.
    expect(scanned(["US_VETERAN", "COMPLETE_CANADIAN"], Q, YN, "Hybrid - Washington, DC")).toEqual({ US_VETERAN: "No", COMPLETE_CANADIAN: "No" });
  });
  it("DC itself is local; a Maryland or Virginia town may be far from it", () => {
    const at = (location: string, addressCity: string, addressState: string): string | null => {
      const p = { ...persona("US_VETERAN"), location, addressCity, addressState, addressStreet: "", postalCode: "" } as UserApplicationProfile;
      document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">${esc(Q)}</label><select id="q0" name="q0"><option value="">Select...</option><option value="0">Yes</option><option value="1">No</option></select></div></form>`;
      setResolveContext({ jobCountry: "US", jobCity: "Washington", jobPlaces: null, company: "Acme" });
      const v = scanPage(p, true).fields.find((x) => x.label.startsWith("Are you local"))?.proposedValue ?? null;
      setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
      return v;
    };
    expect(at("Washington, DC", "Washington", "DC")).toBe("Yes");
    expect(at("Virginia Beach, VA", "Virginia Beach", "VA")).toBeNull();
  });
});

describe("a select's question a paragraph long (Stand Together on Lever, live, round 8)", () => {
  it("is the select's label, not the card's heading above it", () => {
    const Q = "Koch Internship Program (KIP) participants attend programming hosted online by Stand Together Fellowships on Thursdays from 1-4 pm ET, attend an on-site summit in Arlington, VA, and work with one of Stand Together Fellowships’ non-profit partner organizations (national think tanks or state policy organizations). Are you available to attend all online and on-site KIP programming and work with a partner organization either part time or full time for the duration of the program as required for participation?";
    // Lever's markup, verbatim but for the card id.
    document.body.innerHTML = `<form id="application-form"><div class="section page-centered application-form" data-qa="additional-cards"><h4 data-qa="card-name">ST Fellowships - Koch Internship Program</h4><ul><li class="application-question custom-question"><div><div class="application-label full-width dropdown"><div class="text">${Q}<span class="required">✱</span></div></div><div class="application-field full-width required-field"><div class="application-dropdown"><select name="cards[2e05ab8f][field1]" required=""><option value="">Select...</option><option value="Yes">Yes</option><option value="No">No</option></select></div></div></div></li></ul></div></form>`;
    const f = scanPage(persona("US_OPT_ANALYST"), true).fields[0];
    expect(f.label.startsWith("Koch Internship Program (KIP) participants attend")).toBe(true);
  });
});

describe("the Lever bank's re-run (round 8)", () => {
  it("a location list of countries takes the applicant's own (decision 93 at a country's scale; Kepler)", () => {
    // Answered by country before decision 93, blank after it.
    const got = scanned(["COMPLETE_CANADIAN", "US_VETERAN", "BERLIN_STAFF"], "Which location are you applying for?", ["Canada", "United States"], "Toronto, ON");
    expect(got).toEqual({ COMPLETE_CANADIAN: "Canada", US_VETERAN: "United States", BERLIN_STAFF: null });
  });
  it("accepting an offer named in a notice is no question about accepting it (Woven by Toyota)", () => {
    const Q = "Woven by Toyota conducts a thorough pre-employment background check as a condition of employment. This check may include verification of your education, employment history, criminal history, and any other relevant information. If you accept an offer of employment, you consent to this background check and acknowledge that any employment offer is contingent upon satisfactory results.";
    // A checkbox list of one, as Lever renders it (the bank's multi-select).
    document.body.innerHTML = `<form id="application-form"><fieldset><legend>${esc(Q)}</legend><label><input type="checkbox" name="cards[w][field0]" value="I acknowledge">I acknowledge</label></fieldset></form>`;
    setResolveContext({ jobCountry: "US", jobCity: null, jobPlaces: null, company: "Woven by Toyota" });
    const field = scanPage(persona("COMPLETE_CANADIAN"), true).fields[0];
    setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
    expect(field?.proposedValue ?? null).toMatch(/^(yes|I acknowledge)$/);
  });
});

/**
 * Palantir on Lever (round 8's regression, live): "Give us three numbers that
 * describe you - and tell us why. … We're not looking for your predicted
 * grades or your age - …" (a textarea in a card) was written "22", the
 * applicant's age: "your age" anywhere was the age question.
 */
describe("an age the question says it is not asking for", () => {
  const THREE_NUMBERS =
    "Give us three numbers that describe you - and tell us why. They can be anything: a stat from your life, a score, a streak, a count, a ranking, a time. The number itself matters less than what it reveals about you. We're not looking for your predicted grades or your age - we want the numbers you choose to give us insight into who you are.";
  it("is no age question, in a card's textarea or a text box", () => {
    document.body.innerHTML = `<form id="application-form"><ul><li class="application-question custom-question"><div class="application-label full-width"><div class="text">${esc(THREE_NUMBERS)}</div></div><div class="application-field full-width"><textarea class="card-field-input" name="cards[262ed6b7-e6fc-4130-81ab-749cc54c56d8][field1]"></textarea></div></li></ul></form>`;
    const f = scanPage(persona("COMPLETE_CANADIAN"), true).fields.find((x) => x.label.startsWith("Give us three numbers"));
    expect(f).toBeDefined();
    expect(f?.proposedValue ?? null).toBeNull();
    expect(scanned(["COMPLETE_CANADIAN"], THREE_NUMBERS, [], "Denver, CO, United States")).toEqual({ COMPLETE_CANADIAN: null });
  });
  it("an age asked is still the age", () => {
    expect(scanned(["COMPLETE_CANADIAN"], "What is your age?", [], "Denver, CO, United States")).toEqual({ COMPLETE_CANADIAN: "22" });
  });
});

describe("a signature asked as the name entered below a statement (Datalab on Lever)", () => {
  // The card's question is the statement; "SIGNATURE" is only the card's
  // heading, which the scan read as the label before round 8.
  const STATEMENT =
    "By entering your name below: I, the undersigned, certify that the statements made by me and information in all documents provided by me herewith are complete and true to the best of my knowledge and belief. I understand that any willful misstatement may lead to disqualification, or to dismissal if employed. I understand any offer of employment is conditional upon my passing a credit, criminal background and drug check.";
  it("takes the applicant's full name", () => {
    document.body.innerHTML = `<form id="application-form"><div class="application-page"><h4>SIGNATURE</h4><ul><li class="application-question custom-question"><div><div class="application-label full-width text"><div class="text">${esc(STATEMENT)}<span class="required">✱</span></div></div><div class="application-field full-width required-field"><input required="required" class="card-field-input" type="text" placeholder="Type your response" value="" name="cards[23ac2dc9-e2da-4790-9e24-c1bd95f90717][field0]"></div></div></li></ul></div></form>`;
    const f = scanPage(persona("COMPLETE_CANADIAN"), true).fields.find((x) => x.label.startsWith("By entering your name"));
    expect(f?.category).toBe("fullName");
    expect(f?.proposedValue).toBe("Maya Tremblay");
  });
});

describe("a job board the list does not name, among named ones (Horizon on Greenhouse)", () => {
  // Verbatim (Greenhouse job board API, 2026-10-22; "AfrianAmerican" sic).
  const HORIZON = ["Disabled-jobsearch.com", "Asian-jobsearch.com", "LGBTQ-jobsearch.com", "Veterans-jobsearch.com", "Seniors-jobsearch.com", "NativeAmerican-jobsearch.com", "Neurodivergent-jobsearch.com", "Diversity-jobsearch.com", "Hispanic-jobsearch.com", "Black-jobsearch.com", "AfrianAmerican-jobsearch.com", "Other Jobboards", "Social Media", "Horizon Website"];
  it("LinkedIn is one of the other job boards", () => {
    expect(scanned(["INDIA_NEW_GRAD"], "How did you hear about us?*", HORIZON, "Toronto, ON, Canada")).toEqual({ INDIA_NEW_GRAD: "Other Jobboards" });
  });
});
