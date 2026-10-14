/**
 * Question bank 2 (79 more Greenhouse postings, 2026-10-05): answers read by
 * hand and found wrong for a persona. Each test fails on the code before its fix.
 */
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { stubLayout } from "./helpers/layout";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import type { UserApplicationProfile } from "../src/shared/types";
import * as P from "./e2e/profiles.mjs";

const YES_NO = ["Yes", "No"];
type Persona = keyof typeof P;
type Ctx = { jobCountry: string | null; company: string; jobCity?: string | null; jobPlaces?: string[] | null };

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-05T12:00:00Z"));
});
afterAll(() => {
  restore();
  vi.useRealTimers();
  document.body.innerHTML = "";
});

const esc = (t: string): string => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** One question as the bank renders it (a select for a list, a text box
 *  otherwise), scanned for a persona: the proposed answer, "abstain" or "none". */
function ask(who: Persona, label: string, options?: string[], ctx: Ctx = { jobCountry: "US", company: "" }): string {
  const control = options
    ? `<select id="q0"><option value="">Select...</option>${options.map((o, j) => `<option value="${j}">${esc(o)}</option>`).join("")}</select>`
    : `<input type="text" id="q0">`;
  document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">${esc(label)}</label>${control}</div></form>`;
  setResolveContext({ jobCountry: ctx.jobCountry, company: ctx.company, jobCity: ctx.jobCity ?? null, jobPlaces: ctx.jobPlaces ?? null });
  const { fields } = scanPage(P[who] as unknown as UserApplicationProfile, true);
  const f = fields[0];
  return f?.proposedValue ?? (f?.deviceAbstained ? "abstain" : "none");
}

describe("the place the applicant lives, in other words (Datacor)", () => {
  const AUTH = "Are you legally authorized to work in the location you currently reside?";
  const SPONSOR = "Will you now, or in the future, require sponsorship for a work permit for the location you currently reside?";
  it("citizens at home are authorized there and need no sponsorship", () => {
    for (const who of ["COMPLETE_CANADIAN", "INDIA_NEW_GRAD", "BOOTCAMP_CAREER_GAP"] as Persona[]) {
      expect(ask(who, AUTH, YES_NO), who).toBe("Yes");
      expect(ask(who, SPONSOR, YES_NO), who).toBe("No");
    }
    // The allow-list layer (D-61 amendment): "German citizen (EU)" holds "EU", outside the closed
    // vocabulary, so the citizenship is unknown and the device abstains.
    expect(ask("BERLIN_STAFF", AUTH, YES_NO)).toBe("abstain");
    expect(ask("BERLIN_STAFF", SPONSOR, YES_NO)).toBe("abstain");
  });
  it("visa holders in the US keep their answers", () => {
    expect(ask("US_H1B_SENIOR", AUTH, YES_NO)).toBe("Yes");
    expect(ask("US_H1B_SENIOR", SPONSOR, YES_NO)).toBe("Yes");
  });
});

describe("where you are enrolled now, for someone who graduated (Datacor, DV Trading)", () => {
  const SCHOOL = "Please include the university you are currently enrolled in.";
  const DEGREE = "Please include what degree you are currently pursuing in addition to major(s) and any minor(s).";
  const DV = "Please re-confirm the university you currently attend";
  const DV_LIST = ["Massachusetts Institute of Technology", "University of Waterloo", "University of Washington", "Northeastern University", "Indian Institute of Technology Madras", "Other"];
  it("a graduate is enrolled nowhere: no school, no degree in progress", () => {
    for (const who of ["US_H1B_SENIOR", "US_OPT_ANALYST", "BERLIN_STAFF", "INDIA_NEW_GRAD", "BOOTCAMP_CAREER_GAP"] as Persona[]) {
      expect(ask(who, SCHOOL), who).not.toMatch(/University|Institute|School/);
      expect(ask(who, DEGREE), who).toBe("abstain");
      expect(ask(who, DV, DV_LIST), who).not.toMatch(/University|Institute/);
    }
  });
  it("a student gets their school, and the degree with its major", () => {
    expect(ask("COMPLETE_CANADIAN", SCHOOL)).toBe("University of Waterloo");
    expect(ask("COMPLETE_CANADIAN", DV, DV_LIST)).toBe("University of Waterloo");
    expect(ask("COMPLETE_CANADIAN", DEGREE)).toBe("Bachelor of Applied Science in Mechatronics Engineering");
  });
});

describe("an internship OR full-time experience (DoorDash Canada)", () => {
  const Q = "Have you previously completed at least 1 internship or have relevant full-time experience?";
  it("years of full-time engineering is Yes", () => {
    expect(ask("US_H1B_SENIOR", Q, YES_NO)).toBe("Yes");
    expect(ask("BERLIN_STAFF", Q, YES_NO)).toBe("Yes");
    expect(ask("BOOTCAMP_CAREER_GAP", Q, YES_NO)).toBe("Yes");
  });
  it("an internship is still Yes", () => {
    expect(ask("INDIA_NEW_GRAD", Q, YES_NO)).toBe("Yes");
    expect(ask("COMPLETE_CANADIAN", Q, YES_NO)).toBe("Yes");
  });
});

describe("a GPA on another scale is not placed in 4.0 buckets (DoorDash, Klaviyo)", () => {
  it("8.6/10 picks no 4.0 bucket", () => {
    expect(ask("INDIA_NEW_GRAD", "Please indicate your most recent GPA", ["3.75+", "3.41 - 3.74", "Below 3.4"])).not.toBe("3.75+");
    expect(ask("INDIA_NEW_GRAD", "What is your GPA?", ["Below 2", "2 - 2.24", "3 - 3.24", "3.5 - 3.74", "3.75 - 4", "4+"])).not.toBe("4+");
  });
  it("a 4.0-scale GPA still does", () => {
    expect(ask("COMPLETE_CANADIAN", "Please indicate your most recent GPA", ["3.75+", "3.41 - 3.74", "Below 3.4"])).toBe("3.41 - 3.74");
  });
});

describe("the undergraduate discipline of someone with a master's (DV Trading)", () => {
  const Q = "Undergrad Discipline(s)";
  const LIST = ["Mathematics", "Statistics", "Computer Science", "Information Systems", "Data Science", "Business", "Engineering", "Other"];
  it("is the bachelor's major, not the master's", () => {
    expect(ask("US_H1B_SENIOR", Q, LIST)).toBe("Computer Science");
    expect(ask("US_OPT_ANALYST", Q, LIST)).not.toBe("Data Science");
  });
  it("someone with no bachelor's has no undergraduate discipline", () => {
    expect(ask("BOOTCAMP_CAREER_GAP", Q, LIST)).not.toBe("Other");
  });
});

describe("an F-1 student planning CPT/OPT (Cloudflare)", () => {
  const Q = "Are you a student in F-1 status who plans to work pursuant to curricular practical training (CPT), or optional practical training (OPT)?";
  it("a Canadian citizen is not in F-1 status, enrolled or not", () => {
    expect(ask("COMPLETE_CANADIAN", Q, YES_NO)).toBe("No");
  });
});

describe("in our <city> office, for someone who will not move (Cloudflare, Giftogram)", () => {
  const Q = "Are you able to work in our Austin office 3–5 days a week?";
  it("a Seattle or Denver applicant who will not relocate: No", () => {
    expect(ask("US_H1B_SENIOR", Q, YES_NO)).toBe("No");
    expect(ask("BOOTCAMP_CAREER_GAP", Q, YES_NO)).toBe("No");
  });
  it("someone who will relocate: Yes", () => {
    expect(ask("US_OPT_ANALYST", Q, YES_NO)).toBe("Yes");
  });
  it("a citizen who cannot be onsite in New Jersey is still No", () => {
    expect(ask("BOOTCAMP_CAREER_GAP", "Are you a US Citizen or Green Card Holder that can work onsite in Whippany NJ ~3 days per week", YES_NO)).toBe("No");
  });
});

describe("US citizenship for someone on a student visa (Covar)", () => {
  it("F-1 OPT is not citizenship", () => {
    // The allow-list layer (D-61 amendment): "F-1 STEM OPT (EAD valid through June 2028)" holds
    // words outside the closed vocabulary, so nothing is known and the device abstains.
    expect(ask("US_OPT_ANALYST", "Are you a US citizen? Eligibility for a US security clearance is required for this role, and that requires US citizenship.", YES_NO)).toBe("abstain");
  });
});

describe("an expected graduation date that has passed (DV Trading)", () => {
  const Q = "Please re-confirm your expected graduation date";
  const LIST = ["I've already graduated", "August 2026 - December 2026", "January 2027 - July 2027", "August 2027 - December 2027", "January 2028 - June 2028"];
  it("June 2026 is already graduated, never the next term", () => {
    expect(ask("INDIA_NEW_GRAD", Q, LIST)).toBe("I've already graduated");
  });
  it("a student still gets the term holding their date", () => {
    expect(ask("COMPLETE_CANADIAN", Q, LIST)).toBe("January 2027 - July 2027");
  });
});

describe("the school someone attended, from a long list (SharkNinja)", () => {
  const Q = "Which college did you attend?";
  const LIST = [
    "Rhode Island School of Design (RI)", "Indiana Institute of Technology (IN)", "University of Maryland--University College (MD)",
    "University of Washington (WA)", "University of Washington - Bothell (WA)", "Washington College (MD)", "Northeastern University (MA)", "Other",
  ];
  it("past tense is the main school, matched by its own words", () => {
    expect(ask("US_H1B_SENIOR", Q, LIST)).toBe("University of Washington (WA)");
    expect(ask("US_OPT_ANALYST", Q, LIST)).toBe("Northeastern University (MA)");
  });
  it("never a school that only shares the common words", () => {
    expect(ask("BOOTCAMP_CAREER_GAP", Q, LIST)).not.toBe("Rhode Island School of Design (RI)");
    expect(ask("INDIA_NEW_GRAD", Q, LIST)).not.toBe("Indiana Institute of Technology (IN)");
    expect(ask("BERLIN_STAFF", Q, LIST)).not.toBe("University of Maryland--University College (MD)");
  });
});

describe("worked WITH us before (Paylocity, live 2026-10-05)", () => {
  it("is the hiring company's history: No for someone who never worked there", () => {
    expect(ask("US_VETERAN" as Persona, "Have you worked with us before?", ["--", "Yes", "No"], { jobCountry: "US", company: "Choice Solutions LLC" })).toBe("No");
  });
  it("a skill worked with is no employer", () => {
    expect(ask("US_VETERAN" as Persona, "Have you worked with Kubernetes before?", ["Yes", "No"], { jobCountry: "US", company: "Choice Solutions LLC" })).not.toBe("No");
  });
});

describe("batch 3 live pages (2026-10-05)", () => {
  const BREEZY = "The summer internship is a hybrid schedule based out of our downtown Chicago HQ (in-office 2x/week). Candidates must be local or able to relocate independently - relocation assistance is not provided. Can you meet this requirement?";
  it("a requirement that mentions no assistance is given still asks the requirement (Breezy)", () => {
    expect(ask("US_OPT_ANALYST", BREEZY, YES_NO)).toBe("Yes");
    expect(ask("US_H1B_SENIOR", BREEZY, YES_NO)).toBe("No");
  });
  it("asking for assistance stays the applicant's", () => {
    expect(ask("US_OPT_ANALYST", "Will you require relocation assistance?", YES_NO)).not.toBe("Yes");
  });
  it("graduating in a term that is not theirs: No (CareerPuck)", () => {
    expect(ask("INDIA_NEW_GRAD", "Are you graduating in Spring 2027?", YES_NO)).toBe("No");
    expect(ask("BERLIN_STAFF", "Are you graduating in Spring 2027?", YES_NO)).toBe("No");
  });
});

describe("pay alignment stays the applicant's (Nuro, question bank 2026-10-05)", () => {
  it("a compensation package the question describes is never accepted for them", () => {
    const q = "Please confirm that you have read and are aligned with the compensation package above (hourly rate based on shift and experience, discretionary bonus, full benefits, daily meal reimbursement, PTO + company holidays). Does this align with your pay expectations?";
    expect(ask("US_H1B_SENIOR", q, YES_NO)).not.toBe("Yes");
  });
});
