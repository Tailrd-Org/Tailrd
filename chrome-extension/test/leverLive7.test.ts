// @vitest-environment-options {"url": "https://jobs.lever.co/qbank/00000000-0000-0000-0000-000000000000/apply"}
/**
 * Round 7's live Lever boards (2026-10-20): 13 postings of boards in prod
 * scraped_jobs that no bank or case held, run blind. Each test is a field the
 * run left blank that a stated fact answers, or one it read wrong; labels and
 * options verbatim from each posting's /apply page (GET only).
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
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
function scanned(who: string[], label: string, options: string[], location: string, type: "select" | "checks" | "text" = options.length ? "select" : "text"): Record<string, string | null> {
  const control =
    type === "select"
      ? `<select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select>`
      : type === "checks"
        ? options.map((o, i) => `<label><input type="checkbox" name="q0[]" value="${i}"> ${esc(o)}</label>`).join("")
        : `<input type="text" id="q0" name="q0">`;
  const field = type === "checks" ? `<fieldset class="field" id="q0"><legend>${esc(label)}</legend>${control}</fieldset>` : `<div class="field"><label for="q0">${esc(label)}</label>${control}</div>`;
  const out: Record<string, string | null> = {};
  for (const w of who) {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">${esc(location)}</div><form id="application-form">${field}<div class="field"><label for="em">Email</label><input type="text" id="em" name="email"></div></form>`;
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company: "Acme" });
    const f = scanPage(persona(w), true).fields.find((x) => x.label.startsWith(label.slice(0, 40)));
    out[w] = f?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}

describe("a status question read whole (CFS Energy)", () => {
  it("'…as defined by 8 U.S.C. 1324b(a)(3)…' does not end the question", () => {
    // Blank for everyone: the statement was split after "8 U.S.C." and only
    // "1324b(a)(3) (e.g., a refugee or asylee)?" was read.
    const label =
      "The following question is asked for the purpose of ensuring compliance with U.S. Export Control Laws. Are you one of the following: a U.S. citizen, a U.S. national, a lawful permanent resident of the U.S. (e.g. green card holder), or a protected individual as defined by 8 U.S.C. 1324b(a)(3) (e.g., a refugee or asylee)?*";
    const got = scanned(["COMPLETE_CANADIAN", "BOOTCAMP_CAREER_GAP"], label, YN, "Devens, MA");
    expect(got.COMPLETE_CANADIAN).toBe("No");
    expect(got.BOOTCAMP_CAREER_GAP).toBe("Yes");
  });
});

describe("a start year and quarter from the stated start date (Galatea Associates)", () => {
  // The earliest start, or today once it has passed (BOOTCAMP_CAREER_GAP
  // states 2026-10-19, UK_SENIOR 2027-01-04; SPARSE_CANADIAN states none).
  const startOf = (iso: string): Date => new Date(Math.max(Date.parse(`${iso}T00:00:00Z`), Date.now()));
  const Q = ["January - March (Q1)", "April - June (Q2)", "July - September (Q3)", "October - December (Q4)"];
  it("the year", () => {
    const got = scanned(["BOOTCAMP_CAREER_GAP", "UK_SENIOR", "SPARSE_CANADIAN"], "In which year would you be available to start at Galatea?", ["2026", "2027", "2028", "2029", "2030"], "St. Petersburg, FL");
    expect(got).toEqual({ BOOTCAMP_CAREER_GAP: String(startOf("2026-10-19").getUTCFullYear()), UK_SENIOR: String(startOf("2027-01-04").getUTCFullYear()), SPARSE_CANADIAN: null });
  });
  it("the quarter", () => {
    const got = scanned(["BOOTCAMP_CAREER_GAP", "UK_SENIOR", "SPARSE_CANADIAN"], "In which quarter would you be available to start?", Q, "St. Petersburg, FL");
    expect(got).toEqual({ BOOTCAMP_CAREER_GAP: Q[Math.floor(startOf("2026-10-19").getUTCMonth() / 3)], UK_SENIOR: Q[Math.floor(startOf("2027-01-04").getUTCMonth() / 3)], SPARSE_CANADIAN: null });
  });
});

describe("eligibility read as asked (SoloPulse)", () => {
  it("a university student in a named area: No for a graduate; a student's school is theirs to place (decision 48)", () => {
    const got = scanned(["NEW_GRAD_DECLINER", "COMPLETE_CANADIAN"], "Are you currently a University student in the Atlanta area pursuing an Engineering degree?", YN, "Peachtree Corners, GA");
    expect(got).toEqual({ NEW_GRAD_DECLINER: "No", COMPLETE_CANADIAN: null });
  });
  it("able to hold a U.S. security clearance: No for someone who is no US citizen", () => {
    // A US citizen who holds none answers it themselves (the standing rule).
    const got = scanned(["NEW_GRAD_DECLINER", "BOOTCAMP_CAREER_GAP"], "Are you able to hold a U.S. Security Clearance?", YN, "Peachtree Corners, GA");
    expect(got).toEqual({ NEW_GRAD_DECLINER: "No", BOOTCAMP_CAREER_GAP: null });
  });
});

describe("working proficiency in each language stated at that level (Behaviour Interactive)", () => {
  it("native and fluent are professional; basic and conversational are not", () => {
    const got = scanned(["MONTREAL_CHANGER", "UK_SENIOR", "BOOTCAMP_CAREER_GAP"], "Do you have professional working proficiency in:", ["English", "French"], "Montreal, QC", "checks");
    expect(got).toEqual({ MONTREAL_CHANGER: "French, English", UK_SENIOR: "English", BOOTCAMP_CAREER_GAP: "English" });
  });
});

describe("Lever's race radios: each option is its own label (Azul)", () => {
  // Lever wraps the whole Race question in a <label>, so the first radio's
  // labels are that outer label and its own: read first, its text was the
  // whole question, "Decline to self-identify" included. Two options then
  // held a decline, and someone who states no race got nothing (decision 4
  // of the day session gives the decline). Markup verbatim from the page.
  const RACE = readFileSync("test/fixtures/lever/eeo-race-nested-label.html", "utf8");
  const race = (who: string) => {
    document.body.innerHTML = RACE;
    return scanPage(persona(who), true).fields.find((f) => f.label === "Race");
  };
  it("the first option reads as itself", () => {
    expect(race("US_F1_STUDENT")?.options?.[0]).toBe("Hispanic or LatinoA person of Cuban, Mexican, Puerto Rican, South or Central American, or other Spanish culture or origin regardless of race.");
  });
  it("someone who states no race declines", () => {
    expect(race("US_F1_STUDENT")?.proposedValue).toBe("Decline to self-identify");
  });
  it("a stated race still takes its own option", () => {
    expect(race("US_VETERAN")?.proposedValue).toMatch(/^Black or African American \(Not Hispanic or Latino\)/);
  });
});

describe("a schedule the program sets is accepted by applying (Viget, decision 49)", () => {
  it("a standard 40-hour, Monday-Friday work week", () => {
    const got = scanned(["UK_SENIOR", "BOOTCAMP_CAREER_GAP"], "Are you able to commit to working a standard 40-hour, Monday-Friday work week during the program?", YN, "Boulder, CO");
    expect(got).toEqual({ UK_SENIOR: "Yes", BOOTCAMP_CAREER_GAP: "Yes" });
  });
});
