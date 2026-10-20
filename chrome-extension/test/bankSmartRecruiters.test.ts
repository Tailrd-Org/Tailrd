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
import { veteranOption } from "../src/content/demographicMatch";

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

describe("self-identification lists are read by their options (Veolia)", () => {
  const INTRO =
    "\"If you are applying for a job outside of U.S. and do not wish to participate, please select Decline to Self-Identify. You are invited to provide certain information in order to assist us, with meeting federal and state record-keeping requirements. Providing this information is voluntary. Any information provided will be kept confidential and will not adversely affect your employment or consideration for employment. We, are an Equal Opportunity Employer and do not discriminate in hiring or employment against any individual on the basis of race, religion, color, creed, gender, gender identity or expression, national origin, ancestry, age, marital status, citizenship, physical or mental disability, veteran status, sexual orientation, or status within any other group that is protected by anti-discrimination laws.\"";
  const RACES = ["American Indian or Alaska Native", "Asian", "Black or African American", "Hispanic or Latino", "I do not wish to self identify", "Native Hawaiian or Other Pacific Islander", "Two or More Races", "White"];

  it("a race list under an EEO paragraph that names gender identity is the race question", () => {
    // Read as gender identity by the paragraph's words: blank for everyone
    // who stated a race.
    expect(one({ label: INTRO, options: RACES }, "Nanticoke, PA, United States")).toEqual({
      COMPLETE_CANADIAN: "White",
      US_H1B_SENIOR: "Asian",
      BOOTCAMP_CAREER_GAP: "Black or African American",
      US_OPT_ANALYST: "Asian",
      BERLIN_STAFF: "White",
      INDIA_NEW_GRAD: "I do not wish to self identify",
    });
  });

  it("genders under 'Please select' are the gender question", () => {
    // Unread, the list took its decline for applicants who stated a gender.
    expect(one({ label: "Please select", options: ["Male", "Female", "Non-binary", "I do not wish to identify"] }, "Nanticoke, PA, United States")).toEqual({
      COMPLETE_CANADIAN: "Female",
      US_H1B_SENIOR: "Female",
      BOOTCAMP_CAREER_GAP: "Male",
      US_OPT_ANALYST: "Male",
      BERLIN_STAFF: "Male",
      INDIA_NEW_GRAD: "I do not wish to identify",
    });
  });

  it("'a veteran but not a protected veteran' served: a never-served applicant is not a protected veteran", () => {
    const VEVRAA =
      "We are a Government contractor subject to the Vietnam Era Veterans Readjustment Assistance Act of 1974, as amended by the Jobs for Veterans Act of 2002, 38 U.S.C. 4212 VEVRAA, which requires Government contractors to take affirmative action to employ and advance in employment: 1. disabled veterans 2. recently separated veterans 3. active duty wartime or campaign badge veterans and 4. Armed Forces service medal veterans. For definitions click the help icon below. If you believe you belong to any of the categories of protected veterans listed, please indicate by checking the appropriate box below. As a Government contractor subject to VEVRAA, we request this information in order to measure the effectiveness of the outreach and positive recruitment efforts we undertake pursuant to VEVRAA.";
    const OPTS = ["I am a protected veteran", "I am a veteran but not a protected veteran", "I am not a protected veteran", "I choose not to self-identify"];
    expect(one({ label: VEVRAA, options: OPTS }, "Nanticoke, PA, United States")).toEqual({
      ...every("I am not a protected veteran"),
      INDIA_NEW_GRAD: "I choose not to self-identify",
    });
    expect(veteranOption("I identify as one or more of the classifications of a protected veteran", "Veteran Status", OPTS)).toBe("I am a protected veteran");
  });
});
