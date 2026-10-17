// @vitest-environment-options {"url": "https://jobs.jobvite.com/qbank/job/oQBANK01/apply"}
/**
 * The Jobvite question bank (round 6, 2026-10-17): 43 postings of the six
 * Jobvite companies in prod scraped_jobs, each form read from its own apply
 * page (GET only) and answered for six personas. Each test is an answer the
 * bank got wrong, or a blank a stated fact answers; labels and options verbatim.
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

const ALL = ["COMPLETE_CANADIAN", "US_H1B_SENIOR", "BOOTCAMP_CAREER_GAP", "US_OPT_ANALYST", "BERLIN_STAFF", "INDIA_NEW_GRAD"] as const;
type Who = (typeof ALL)[number];
const persona = (who: Who) => (P as Record<string, unknown>)[who] as UserApplicationProfile;
const esc = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

interface Q {
  label: string;
  options?: string[];
  type?: "select" | "text" | "textarea";
}

/** A posting's questions as the bank renders them (one form, its place
 *  outside it); each persona's proposed answer to the question `ask`. */
function answers(questions: Q[], ask: string, location: string, company = "Acme"): Record<Who, string | null> {
  const rows = questions.map((q, i) => {
    const id = `q${i}`;
    const type = q.type ?? (q.options?.length ? "select" : "text");
    const control =
      type === "select"
        ? `<select id="${id}" name="jv_${i}"><option value="">Select...</option>${(q.options ?? []).map((o, j) => `<option value="${j}">${esc(o)}</option>`).join("")}</select>`
        : type === "textarea"
          ? `<textarea id="${id}" name="jv_${i}"></textarea>`
          : `<input type="text" id="${id}" name="jv_${i}">`;
    return `<div class="field"><label for="${id}">${esc(q.label)}</label>${control}</div>`;
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

const YN = ["Yes", "No"];

describe("Address 2 is the address's second line (Ross Video, GEI, SRC)", () => {
  it("never takes the city line: the street's unit, or blank", () => {
    const form: Q[] = [{ label: "Address" }, { label: "Address 2" }, { label: "City" }, { label: "Zip/Postal Code" }];
    const a = answers(form, "Address 2", "Hybrid Remote, Nepean, Ontario", "Ross Video");
    expect(a.US_OPT_ANALYST).toBe("Apt 12C");
    expect(a.US_H1B_SENIOR).toBe("Unit 805");
    for (const who of ["COMPLETE_CANADIAN", "BOOTCAMP_CAREER_GAP", "BERLIN_STAFF", "INDIA_NEW_GRAD"] as const) expect(a[who], who).toBeNull();
  });
});

describe("an export-control confirmation is a Yes for anyone it does not exclude (Ross Video)", () => {
  // It names the embargoed places, then asks the applicant to CONFIRM they
  // meet the requirement: the sanctions rule answered "No" (not from those
  // places) to a question whose Yes means exactly that.
  const EXPORT =
    "Employment at Ross Video is subject to United States Export Administration Regulations, which restrict access to certain technologies and information based on country of origin, citizenship, nationality, or permanent residency. As a result, these regulations include restrictions for individuals from countries currently under U.S. embargo, including Cuba, Iran, Syria, or North Korea. Can you confirm that you meet these requirements and are eligible to comply?";
  it("Yes when the stated citizenship and home are not embargoed; never No", () => {
    const a = answers([{ label: EXPORT, options: YN }], EXPORT, "Hybrid Remote, Nepean, Ontario", "Ross Video");
    expect(a.BOOTCAMP_CAREER_GAP).toBe("Yes");
    expect(a.INDIA_NEW_GRAD).toBe("Yes");
    expect(a.COMPLETE_CANADIAN).toBe("Yes");
    for (const who of ALL) expect(a[who], who).not.toBe("No");
  });
});

describe("Current Company / School (Nutanix)", () => {
  it("is the employer for someone who works, the school for a student", () => {
    const a = answers([{ label: "Current Company / School" }], "Current Company / School", "San Jose, California", "Nutanix");
    expect(a.US_H1B_SENIOR).toBe("Expedia Group");
    expect(a.US_OPT_ANALYST).toBe("Wayfair");
    expect(a.BERLIN_STAFF).toBe("Zalando SE");
  });
});

describe("blanks a stated fact answers (Jobvite bank)", () => {
  it("a former employee or contractor of a company named after a comma, or one of 'our' companies", () => {
    const a = answers([{ label: "Have you previously been employed by, or contracted to, Ross Video or any of its entities?", options: YN }], "Have you previously been employed by, or contracted to, Ross Video or any of its entities?", "Hybrid Remote, Nepean, Ontario", "Ross Video");
    for (const who of ALL) expect(a[who], who).toBe("No");
    const RANGE = "Have you previously been employed by any of our Range Group companies such as: Kensington Tours, Butterfield & Robinson, Abercrombie & Kent?";
    const b = answers([{ label: RANGE, options: YN }], RANGE, "Toronto, Ontario", "Kensington Tours");
    expect(b.COMPLETE_CANADIAN).toBe("No");
  });
  it("Immediately, or With Reasonable Notice, by the notice", () => {
    const L = "When would you be available to start a new opportunity?";
    const a = answers([{ label: L, options: ["Immediately", "With Reasonable Notice", "Other"] }], L, "Hybrid Remote, Nepean, Ontario", "Ross Video");
    expect(a.BOOTCAMP_CAREER_GAP).toBe("Immediately");
    expect(a.US_OPT_ANALYST).toBe("With Reasonable Notice");
    expect(a.US_H1B_SENIOR).toBe("With Reasonable Notice");
  });
  it("no application filled out before", () => {
    const L = "Have you ever filled out an application with us before?";
    const a = answers([{ label: L, options: YN }], L, "Marquette, Michigan", "GEI Consultants");
    for (const who of ALL) expect(a[who], who).toBe("No");
  });
  it("no other employer kept", () => {
    const L = "If selected for this position will you continue to work for another employer?";
    const a = answers([{ label: L, options: YN }], L, "Marquette, Michigan", "GEI Consultants");
    for (const who of ALL) expect(a[who], who).toBe("No");
  });
  it("a VEVRAA paragraph over its two veteran options is the veteran question", () => {
    const L =
      "As a Government contractor we are subjected to the Vietnam Era Veterans’ Readjustment Assistance Act of 1974, as amended by the Jobs for Veterans Act of 2002, 38 U.S.C. 4212 (VEVRAA), which requires Government contractors to take affirmative action to employ and advance in employment: (1) disabled veterans; (2) recently separated veterans; (3) active duty wartime or campaign badge veterans; and (4) Armed Forces service medal veterans. If you believe you belong to any of the categories of protected veterans listed above, please indicate by making the appropriate selection.";
    const a = answers([{ label: L, options: ["I AM NOT A PROTECTED VETERAN", "I IDENTIFY AS ONE OR MORE OF THE CLASSIFICATIONS OF PROTECTED VETERAN LISTED ABOVE"] }], L, "Syracuse, New York", "SRC");
    expect(a.COMPLETE_CANADIAN).toBe("I AM NOT A PROTECTED VETERAN");
    expect(a.US_OPT_ANALYST).toBe("I AM NOT A PROTECTED VETERAN");
  });
  it("a G.P.A. list by the stated GPA, on its own scale only", () => {
    const L = "What is your overall G.P.A.?";
    const OPTS = ["Below 2.5", "2.6", "2.7", "2.8", "2.9", "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8", "3.9", "4.0"];
    const a = answers([{ label: L, options: OPTS }], L, "Syracuse, New York", "SRC");
    expect(a.COMPLETE_CANADIAN).toBe("3.7");
    expect(a.INDIA_NEW_GRAD).toBeNull();
  });
  it("a highest qualification list like a highest education list", () => {
    const L = "Highest Level of Qualification";
    const a = answers([{ label: L, options: ["High School Diploma", "Associate Degree", "Bachelor's Degree", "Master's Degree", "Doctoral Degree (PhD)"] }], L, "San Jose, California", "Nutanix");
    expect(a.BOOTCAMP_CAREER_GAP).toBe("High School Diploma");
    expect(a.US_H1B_SENIOR).toBe("Master's Degree");
  });
  it("full-time work the posting asks for is accepted by applying", () => {
    const L = "This position is Full-Time with the required hours of work Monday to Friday 8:30am-4:30pm. Are you available for Full Time work?";
    const a = answers([{ label: L, options: YN }], L, "Ottawa, Ontario", "CMPA");
    expect(a.COMPLETE_CANADIAN).toBe("Yes");
    expect(a.BERLIN_STAFF).toBe("Yes");
    const M = "Are you available to work in a full-time position?";
    expect(answers([{ label: M, options: YN }], M, "Ottawa, Ontario", "CMPA").US_OPT_ANALYST).toBe("Yes");
  });
});

describe("the job-board channel, not one board, across the banks", () => {
  // Re-running the older banks after the named-board rule: the channel with
  // brands as examples must stay the channel, and no other channel (a job
  // bank, a forum, an ad) may stand in for it (question banks 2026-10-17).
  const src = (who: Who, label: string, options: string[], company = "Acme"): string | null => {
    const r = answers([{ label, options }], label, "New York, New York", company)[who];
    return r;
  };
  it("a job board stated takes the general option that names brands as examples", () => {
    const FAIRE = ["Answered “unaware” to previous question", "Online advertisement (social media, display ads, etc.)", "Billboard or outdoor advertising", "Faire's website", "Faire’s technical blogs (Faire.Tech, The Craft)", "Someone I know personally (friend, family, former colleague)", "Job posting on LinkedIn, Indeed, or other job board", "LinkedIn post or content (not a job posting)", "News article or media coverage", "Industry event or conference", "Other"];
    expect(src("US_OPT_ANALYST", "How did you hear about Faire? (Select all that apply)", FAIRE, "Faire")).toBe("Job posting on LinkedIn, Indeed, or other job board");
    const KOBIE = ["LinkedIn Jobs Posting", "Best Workplaces News", "Kobie Employee Referral", "Kobie Career Page", "Indeed or other job board", "Recruiter outreach", "I don't recall/ I applied through a job aggregator"];
    expect(src("US_OPT_ANALYST", "How did you learn about this specific role at Kobie? (Select the most accurate answer)", KOBIE, "Kobie")).toBe("Indeed or other job board");
    const BIGID = ["Marketing Event/Conference", "Social Media", "Friend/Colleague", "White Paper/Press Release", "Job Board/Recruitment Platform", "BigID Website", "Other"];
    expect(src("US_OPT_ANALYST", "How did you hear about BigID?", BIGID, "BigID")).toBe("Job Board/Recruitment Platform");
    expect(src("COMPLETE_CANADIAN", "How did you hear about BigID?", BIGID, "BigID")).toBe("Job Board/Recruitment Platform");
  });
  it("never a state's job bank or an online forum", () => {
    const BANDWIDTH = ["LinkedIn", "Referral", "Indeed", "Include.io", "NSBE Careers- National Society of Black Engineers Career Site", "Google Search", "Facebook", "Twitter", "Handshake", "InHerSight", "Rewriting The Code", "VA VR&E", "Job Fair/Event", "Local Meetup/Conference", "Campus Recruiting", "NCWorks", "NCWorks (Veterans)", "Connecting Colorado", "New York State Job Bank", "Other"];
    expect(src("US_OPT_ANALYST", "How did you hear about this position?", BANDWIDTH, "Bandwidth")).not.toBe("New York State Job Bank");
    const COVEO = ["Career Fair / Salon de l'emploi", "Coveo Careers Page / Page Carrières de Coveo", "Glassdoor", "Google Jobs", "Indeed", "LinkedIn", "Monster", "Online Forum or Community / Forum ou communauté en ligne (Reddit, Blind, Discord, etc.)", "Other"];
    expect(String(src("US_OPT_ANALYST", "Where did you hear about Coveo?", COVEO, "Coveo"))).not.toMatch(/^Online Forum/);
  });
});

describe("a stated source never becomes a board nobody named (Nutanix, ActioNet)", () => {
  it("LinkedIn or a job board is not Girls in Tech's job board", () => {
    const NUTANIX = [
      "Recruiter - please specify name",
      "University On-Campus Info Session",
      "DBD",
      "INSPIRE",
      "Ripplematch",
      "Handshake",
      "Girls in Tech Job Board",
      "Internal Employee - please specify name ",
      "Other - please list source below",
    ];
    const a = answers([{ label: "How did you hear about us?", options: NUTANIX }], "How did you hear about us?", "San Jose, California", "Nutanix");
    for (const who of ALL) expect(a[who], who).not.toBe("Girls in Tech Job Board");
  });
  it("a job board is not Indeed", () => {
    const ACTIONET = ["ActioNet Career Page", "CareerBuilder", "ClearanceJobs", "Current Employee", "Dice", "Indeed", "LinkedIn", "Referred by ActioNet Employee", "Job Fair", "Other"];
    const a = answers([{ label: "How did you hear about the position?", options: ACTIONET }], "How did you hear about the position?", "Vienna, Virginia", "ActioNet");
    expect(a.US_OPT_ANALYST).not.toBe("Indeed");
    expect(a.COMPLETE_CANADIAN).toBe("LinkedIn");
    expect(a.US_H1B_SENIOR).toBe("ActioNet Career Page");
  });
  // SRC's list is the standard applicant-source one: "Internet Career Site"
  // is the job site channel. LinkedIn got "Other" (round 6, bank re-run), and
  // so did the company's own website, which that option may well be.
  it("a job site the list does not name is its internet career site, never Other", () => {
    const SRC = ["Internet Career Site", "Job Fair/Career Event", "Social Media", "Employee Referral", "Internet Search Engine", "Advertisement", "Other"];
    const a = answers([{ label: "Applicant Source", options: SRC }], "Applicant Source", "North Syracuse, New York", "SRC, Inc.");
    expect(a.COMPLETE_CANADIAN).toBe("Internet Career Site");
    expect(a.INDIA_NEW_GRAD).toBe("Internet Career Site");
    expect(a.US_OPT_ANALYST).toBe("Internet Career Site");
    expect(a.US_H1B_SENIOR).not.toBe("Other");
    expect(a.BOOTCAMP_CAREER_GAP).toBe("Social Media");
    expect(a.BERLIN_STAFF).toBe("Employee Referral");
  });
  it("a named career site is no company's own site: Other stands (Enova, Greenhouse bank)", () => {
    const ENOVA = ["Agency", "Built in Chicago", "Campus Career Site", "Campus Event", "Campus Career Fair", "CareerBuilder", "Conference", "Facebook", "Glassdoor", "Indeed", "LinkedIn", "Meetup", "Grace Hopper", "Other", "Twitter"];
    const a = answers([{ label: "How did you hear about this job?", options: ENOVA }], "How did you hear about this job?", "Chicago, IL", "Enova International");
    expect(a.US_H1B_SENIOR).toBe("Other");
  });
});
