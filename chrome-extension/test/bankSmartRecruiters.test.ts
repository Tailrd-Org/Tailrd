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
import { resolveWithOptions, scanPage } from "../src/content/formScanner";
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
  type?: "select" | "text" | "textarea" | "checkbox" | "multi";
  description?: string;
}

/** A posting's questions as the bank renders them (one form, its place
 *  outside it); each persona's proposed answer to the question `ask`. */
function scanAll(questions: Q[], ask: string, location = "San Francisco, CA, United States", company = "Acme"): Record<Who, { value: string | null; kept: boolean }> {
  const rows = questions.map((q, i) => {
    const id = `q${i}`;
    const type = q.type ?? (q.options?.length ? "select" : "text");
    const desc = q.description ? `<div id="${id}-d" class="description">${esc(q.description)}</div>` : "";
    const aria = q.description ? ` aria-describedby="${id}-d"` : "";
    if (type === "checkbox") return `<div class="field">${desc}<input type="checkbox" id="${id}" name="sr_${i}" value="1"${aria}><label for="${id}">${esc(q.label)}</label></div>`;
    if (type === "multi") {
      const boxes = (q.options ?? []).map((o, j) => `<label><input type="checkbox" name="sr_${i}[]" value="${j}"> ${esc(o)}</label>`).join("");
      return `<fieldset class="field" id="${id}"><legend>${esc(q.label)}</legend>${desc}${boxes}</fieldset>`;
    }
    const control =
      type === "select"
        ? `<select id="${id}" name="sr_${i}"${aria}><option value="">Select...</option>${(q.options ?? []).map((o, j) => `<option value="${j}">${esc(o)}</option>`).join("")}</select>`
        : type === "textarea"
          ? `<textarea id="${id}" name="sr_${i}"${aria}></textarea>`
          : `<input type="text" id="${id}" name="sr_${i}"${aria}>`;
    return `<div class="field"><label for="${id}">${esc(q.label)}</label>${desc}${control}</div>`;
  });
  const out = {} as Record<Who, { value: string | null; kept: boolean }>;
  for (const who of ALL) {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">${esc(location)}</div><form id="application-form">${rows.join("")}</form>`;
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company });
    const { fields } = scanPage(persona(who), true);
    const holder = document.getElementById(`q${questions.findIndex((q) => q.label === ask)}`)?.closest(".field");
    const f = fields.find((x) => document.querySelector(`[data-ap-field="${x.id}"]`)?.closest(".field") === holder);
    out[who] = { value: f?.proposedValue ?? null, kept: Boolean(f?.deviceAbstained) };
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}

/** Each persona's proposed answer to the question `ask`. */
function answers(questions: Q[], ask: string, location?: string, company?: string): Record<Who, string | null> {
  const all = scanAll(questions, ask, location, company);
  return Object.fromEntries(ALL.map((w) => [w, all[w].value])) as Record<Who, string | null>;
}

/** Whether the device kept one question from the AI, per persona. */
const keptBy = (q: Q, location?: string, company?: string): Record<Who, boolean> => {
  const all = scanAll([q], q.label, location, company);
  return Object.fromEntries(ALL.map((w) => [w, all[w].kept])) as Record<Who, boolean>;
};

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

  it("only a status held right there is OPT: never an example, an 'ongoing basis' or an SMS opt-out (bank re-runs)", () => {
    // Caught by the bank re-runs while the OPT rule above was widened.
    expect(one({ label: "Are you currently on OPT or CPT?", options: YN }).US_OPT_ANALYST).toBe("Yes");
    expect(one({ label: "Are you currently on OPT or CPT?", options: YN }).US_H1B_SENIOR).toBe("No");
    const salesloft =
      "Do you currently hold temporary work authorization in the United States (e.g., OPT, CPT, etc.) and/or will you require visa sponsorship (e.g., H-1B, TN, O-1, etc.) to continue working in the U.S. in the future?";
    const s = one({ label: salesloft, options: YN });
    expect([s.COMPLETE_CANADIAN, s.US_H1B_SENIOR, s.BERLIN_STAFF, s.INDIA_NEW_GRAD]).toEqual(["Yes", "Yes", "Yes", "Yes"]);
    const rave =
      "Are you authorized to work in the United States on an ongoing basis without the need for employer sponsorship now or in the future (including after the expiration of any temporary work authorization such as OPT or CPT)?";
    const r = one({ label: rave, options: YN });
    expect([r.BOOTCAMP_CAREER_GAP, r.US_OPT_ANALYST]).toEqual(["Yes", "No"]);
    const lucid =
      "Would you like to receive communications via SMS and/or WhatsApp to the number provided on your application? Message & data rates may apply and message frequency may vary. If you select no, we will only communicate with you via email and/or telephone calls. If you select yes and change your mind, you can always reply STOP to opt out.";
    expect(one({ label: lucid, options: YN })).toEqual(every("No"));
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

describe("education questions read as asked", () => {
  it("the area of study of the highest level is the field, never the level (City and County of San Francisco)", () => {
    // Got "Master's Degree", "Diploma", "Bachelor's Degree".
    const got = one({ label: "In which area of study was the major coursework of your highest level of education?" }, "San Francisco, CA, United States");
    for (const who of ALL) expect(got[who] ?? "", who).not.toMatch(/\b(degree|diploma|bachelor|master|high school|doctorate)\b/i);
    expect(got.US_H1B_SENIOR).toBe("Information Systems");
    expect(got.US_OPT_ANALYST).toBe("Data Science");
  });

  it("a student's highest level completed is the high school diploma a degree program implies (EVERSANA, M3USA)", () => {
    const EVERSANA = ["None completed", "High School Diploma or GED", "Associate Degree (e.g. AA, AAS)", "Bachelor's Degree (e.g. BA, BS)", "Master's Degree (e.g. MA, MS, MBA)", "Professional Degree (e.g. MD, JD)", "Doctorate Degree (e.g. PhD, EdD)"];
    expect(one({ label: "Please confirm your highest level of education completed.", options: EVERSANA }).COMPLETE_CANADIAN).toBe("High School Diploma or GED");
    const M3 = ["High School Diploma", "Associate Degree", "Bachelor's Degree", "Masters Degree", "Doctorate or higher", "None of the Above"];
    expect(one({ label: "What is the highest level of education you have obtained to date?", options: M3 }).COMPLETE_CANADIAN).toBe("High School Diploma");
  });

  it("a master's of science is the list's Master of Science, not its Master of Arts (Western Digital)", () => {
    const WD = ["High School Diploma or Equivalent", "Associates Degree", "Bachelors of Science", "Bachelor of Arts", "Master of Science", "Master of Arts", "PhD", "MBA", "Other"];
    const got = one({ label: "What is your highest level of education?", options: WD });
    expect(got.US_H1B_SENIOR).toBe("Master of Science");
    expect(got.US_OPT_ANALYST).toBe("Master of Science");
  });

  it("the current or most recently completed degree is the one in progress for a student (Resultant)", () => {
    const OPTS = ["Vocational Certification", "Associate's Degree", "Bachelor's Degree", "MBA", "Master's Degree", "PhD", "Other"];
    expect(one({ label: "What is your current or most recently completed degree type?", options: OPTS }, "Indianapolis, IN, United States").COMPLETE_CANADIAN).toBe("Bachelor's Degree");
  });

  it("a final-year student is a senior, in their 4th year or above; a graduate is none of them", () => {
    const BOSCH = ["Freshman", "Sophomore", "Junior", "Senior", "Other"];
    expect(one({ label: "What is your current class standing?", options: BOSCH }, "Charleston, SC, United States").COMPLETE_CANADIAN).toBe("Senior");
    const GDMSI = ["1st Year", "2nd Year", "3rd Year", "4th Year or Above"];
    expect(one({ label: "What year of your program are you currently completing?", options: GDMSI }, "Cole Harbour, NS, Canada").COMPLETE_CANADIAN).toBe("4th Year or Above");
    // Gallup on Greenhouse (live): a graduate enrolled nowhere in the fall.
    const GALLUP = ["1st year associates degree", "2nd year associates degree", "Freshman", "Sophomore", "Junior", "Senior", "Graduate-level student", "None of the above"];
    const fall = one({ label: "Please indicate your level of education during the Fall 2026 Semester:", options: GALLUP }, "San Francisco, CA, United States");
    expect(fall.COMPLETE_CANADIAN).toBe("Senior");
    expect(fall.US_OPT_ANALYST).toBe("None of the above");
    expect(fall.US_H1B_SENIOR).toBe("None of the above");
  });

  it("a graduation month list takes the month; an actual one a graduate too (Resultant, Western Digital)", () => {
    const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    expect(one({ label: "What is your expected graduation month?", options: MONTHS }).COMPLETE_CANADIAN).toBe("April");
    const wd = one({ label: "What is your expected (or actual) graduation month?", options: MONTHS });
    expect(wd.COMPLETE_CANADIAN).toBe("April");
    expect(wd.US_OPT_ANALYST).toBe("May");
    expect(wd.US_H1B_SENIOR).toBeNull();
  });

  it("a graduation month no profile holds stays the applicant's, kept from the AI (CTC, IMC on Greenhouse; Resultant, Western Digital)", () => {
    // The bank's r7d run sent these to the AI: a graduate's year-only date
    // has no month for anyone to read.
    const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    for (const label of ["What is your expected graduation month?", "What is your expected (or actual) graduation month?"]) {
      const kept = keptBy({ label, options: MONTHS }, "Chicago, IL");
      expect(kept.US_H1B_SENIOR, label).toBe(true);
      expect(kept.BOOTCAMP_CAREER_GAP, label).toBe(true);
      expect(kept.BERLIN_STAFF, label).toBe(true);
    }
  });

  it("a term after a student's graduation finds them graduated (HAI Group on Greenhouse)", () => {
    // "Senior" for a student who graduates in April 2027.
    const HAI = ["Sophomore", "Junior", "Senior", "Graduate Student", "Other Advanced Degree Program", "I will have graduated college/university"];
    const got = one({ label: "In summer 2027, what year will you be rising into?", options: HAI }, "Cheshire, CT");
    expect(got.COMPLETE_CANADIAN).toBe("I will have graduated college/university");
    // Graduates with a degree have graduated; a bootcamp and high school are no college.
    expect(got.US_H1B_SENIOR).toBe("I will have graduated college/university");
    expect(got.INDIA_NEW_GRAD).toBe("I will have graduated college/university");
    expect(got.BOOTCAMP_CAREER_GAP).toBeNull();
    // The summer before the final year: rising into it.
    expect(one({ label: "In summer 2026, what year will you be rising into?", options: HAI }, "Cheshire, CT").COMPLETE_CANADIAN).toBe("Senior");
  });

  it("an open range before a term takes an earlier graduation (Duolingo on Greenhouse, live)", () => {
    const DUO = ["Anything before Fall/Winter 2026", "Fall/Winter 2026", "Spring/Summer 2027", "Fall/Winter 2027", "Spring/Summer 2028", "Fall/Winter 2028", "Spring/Summer 2029", "Anything AFTER Spring/Summer 2029"];
    expect(one({ label: "What is your expected graduation timeline?", options: DUO }, "Pittsburgh, PA, United States").INDIA_NEW_GRAD).toBe("Anything before Fall/Winter 2026");
  });

  it("an older graduation year is the list's 'Prior to 2023' (Resultant)", () => {
    const OPTS = ["Prior to 2023", "2023", "2024", "2025", "2026", "2027", "2028", "2029", "2030"];
    const got = one({ label: "What is your expected graduation year?", options: OPTS }, "Indianapolis, IN, United States");
    expect(got.COMPLETE_CANADIAN).toBe("2027");
    expect(got.US_H1B_SENIOR).toBe("Prior to 2023");
    expect(got.BERLIN_STAFF).toBe("Prior to 2023");
  });
});

describe("a bot check, and who completed the form, stay the applicant's (Wellmark)", () => {
  /** The scanned field of a one-question form: its answer and whether the
   *  device kept it from the AI. */
  function scanned(q: Q, who: Who = "COMPLETE_CANADIAN"): { value: string | null; kept: boolean } {
    document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">${esc(q.label)}</label><select id="q0" name="sr_0"><option value="">Select...</option>${(q.options ?? []).map((o, j) => `<option value="${j}">${esc(o)}</option>`).join("")}</select></div></form>`;
    setResolveContext({ jobCountry: "US", jobCity: "Des Moines", jobPlaces: null, company: "Wellmark, Inc." });
    const f = scanPage(persona(who), true).fields.find((x) => x.label.startsWith(q.label.slice(0, 30)));
    setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
    return { value: f?.proposedValue ?? null, kept: Boolean(f?.deviceAbstained) };
  }

  it("a question that tells people from bots is never answered, nor sent to the AI", () => {
    const third = scanned({ label: "What is the third word in this sentence? *(used to monitor web traffic authenticity)*", options: ["yes", "the", "no", "sentence", "agile"] });
    expect(third).toEqual({ value: null, kept: true });
    const founded = scanned({ label: "What year was Wellmark founded? *(used to monitor web traffic authenticity)*", options: ["2032", "October", "Independent", "1939", "Candy"] });
    expect(founded).toEqual({ value: null, kept: true });
  });

  it("whether an AI completed the form is never ours, nor the AI's", () => {
    const opts = ["No - I am a human, and I am completing this form", "Yes - I am a human, and I used AI to help me complete this form", "Yes - AI automated the completion of this form on behalf of the applicant"];
    expect(scanned({ label: "We support the responsible use of AI. Is this application form being completed by an AI tool?", options: opts })).toEqual({ value: null, kept: true });
  });
});

describe("an address asked whole", () => {
  it("a mailing address is the whole address, where the profile has the street (Arcesium on Greenhouse, live)", () => {
    // Got "Seattle, WA".
    const got = one({ label: "Current Mailing Address" }, "New York", "Arcesium");
    expect(got.US_H1B_SENIOR).toBe("1200 Westlake Ave N, Unit 805, Seattle, WA 98109");
    // No street stated: the place, as before.
    expect(got.COMPLETE_CANADIAN).toBe("Toronto, ON, Canada");
  });
});

describe("presence and relocation", () => {
  it("attending an onsite interview is travel, not a move (Versant)", () => {
    // No for the two applicants who will not relocate.
    const label = "This role requires a final-stage onsite interview at one of our office locations (Orlando, FL or Los Angeles, CA). Are you able and willing to attend an onsite interview at one of these locations?";
    expect(one({ label, options: YN }, "Orlando, FL, United States", "Versant")).toEqual(every("Yes"));
  });

  it("'If applicable,' asks the question after it (CRB)", () => {
    const got = one({ label: "If applicable, are you willing to relocate?", options: YN }, "Kansas City, MO, United States", "CRB");
    expect(got.COMPLETE_CANADIAN).toBe("Yes");
    expect(got.US_H1B_SENIOR).toBe("No");
    expect(got.BOOTCAMP_CAREER_GAP).toBe("No");
  });

  it("living in or around a metro area elsewhere is No (Cricut)", () => {
    const got = one({ label: "Are you CURRENTLY living in/around the Salt Lake City Metro area?", options: YN }, "South Jordan, UT, United States", "Cricut");
    expect(got).toEqual(every("No"));
  });

  it("an onsite requirement asked in the description is answered like one asked in the label (AMCA on Greenhouse, live)", () => {
    const got = one({ label: "Onsite Availability", options: YN, description: "This job is 5 days a week onsite in El Segundo, CA. Please confirm that you are willing and able to be onsite 5 days a week." }, "El Segundo, CA", "AMCA");
    expect(got.BERLIN_STAFF).toBe("Yes");
    expect(got.US_H1B_SENIOR).toBe("No");
  });
});

describe("citizenship, clearance and export", () => {
  it("a dual citizen holds two citizenships: a US citizen alone is not one (LLNL)", () => {
    const got = one({ label: "Are you a dual citizen (including U.S. citizenship) or a legal permanent resident of the United States?", options: YN }, "Livermore, CA, United States");
    expect(got.BOOTCAMP_CAREER_GAP).toBe("No");
    expect(got.COMPLETE_CANADIAN).toBe("No");
    // Not allowed to work in the US: neither a US citizen nor a US permanent
    // resident (blank in r7d; No before round 7).
    expect(got.BERLIN_STAFF).toBe("No");
  });

  it("whether a clearance can be achieved is eligibility, not a clearance held (Boston Engineering)", () => {
    // Read as "do you hold one?": No for everyone. A US clearance goes to US
    // citizens; a citizen who holds none answers the rest themselves (the
    // standing rule for "able to obtain").
    const got = one({ label: "This position may require security clearance. Can this be achieved?", options: YN }, "Waltham, MA, United States");
    expect(got.BOOTCAMP_CAREER_GAP).toBeNull();
    expect(got.COMPLETE_CANADIAN).toBe("No");
    expect(got.INDIA_NEW_GRAD).toBe("No");
    expect(got.US_H1B_SENIOR).toBe("No");
  });

  it("a citizen of the country of employment (Western Digital)", () => {
    const got = one({ label: "Are you a citizen of the country you’ll be employed in?", options: YN }, "Rochester, MN, United States");
    expect(got.BOOTCAMP_CAREER_GAP).toBe("Yes");
    expect(got.COMPLETE_CANADIAN).toBe("No");
    expect(got.INDIA_NEW_GRAD).toBe("No");
  });

  it("the country of birth is never in the profile (GDMSI)", () => {
    const opts = ["Canada", "Prefer not to disclose", "Other", "Afghanistan", "Germany", "India", "United States"];
    const description =
      "Some programs may require personal information about your nationality, place of birth or other related information for security and trade related restrictions. Disclosure of this information is strictly voluntary.";
    expect(one({ label: "Please state the country you were born in:", options: opts, description }, "Cole Harbour, NS, Canada")).toEqual(every(null));
  });

  it("an ITAR status list: a US citizen's, and Not Applicable for someone who is no US person (AMCA on Greenhouse, live)", () => {
    const opts = ["U.S. Citizen", "U.S. Permanent Residence (“Green Card” holder)", "Status as a “protected person” under 8 U.S.C 1324b(a)(3)", "Not Applicable"];
    const description =
      "This position requires use of information which is subject to the International Traffic in Arms Regulations (ITAR). All applicants must be U.S. persons within the meaning of ITAR. ITAR defines a US person as a US citizen, permanent resident, or protected person under 8 U.S.C 1324b(a)(3).";
    const got = one({ label: "ITAR Status", options: opts, description }, "El Segundo, CA");
    expect(got.BOOTCAMP_CAREER_GAP).toBe("U.S. Citizen");
    expect(got.COMPLETE_CANADIAN).toBe("Not Applicable");
    expect(got.INDIA_NEW_GRAD).toBe("Not Applicable");
  });

  it("a US person meets a deemed export license's requirements (Axon on Greenhouse, live)", () => {
    const got = one({ label: "Based on the below information, would you meet the requirements for a deemed export license in order to access EAR - controlled technology?", options: YN }, "Boston, Massachusetts, United States", "Axon");
    expect(got.BOOTCAMP_CAREER_GAP).toBe("Yes");
  });
});

describe("dates in the format the question states", () => {
  it("DD/MM/YYYY when the label says so (RodeoFX)", () => {
    const got = one({ label: "À quelle date seriez-vous disponible pour commencer ? (JJ/MM/AAAA) What is the earliest date you would be available to start? (DD/MM/YYYY)" }, "Montreal, QC, Canada");
    expect(got.US_H1B_SENIOR).toBe("30/11/2026");
    expect(got.BOOTCAMP_CAREER_GAP).toMatch(/^\d{2}\/1[01]\/2026$/);
  });

  it("MM/YY when the label says so, nothing from a bare year (Bosch)", () => {
    const got = one({ label: "When is your expected graduation date? (If you have already graduated, please provide that date.) (MM/YY)" }, "Charleston, SC, United States");
    expect(got.COMPLETE_CANADIAN).toBe("04/27");
    expect(got.US_OPT_ANALYST).toBe("05/25");
    expect(got.US_H1B_SENIOR).toBeNull();
  });
});

describe("defaults read the question asked", () => {
  it("a co-op term's length is no start date (GDMSI)", () => {
    expect(one({ label: "What is the available term length for your upcoming co-op work term? (Multi Select)", options: ["4 months", "8 months", "12 months", "18 months"] }, "Cole Harbour, NS, Canada")).toEqual(every(null));
  });

  it("not using tobacco is the applicant's own to state (Wellmark)", () => {
    const label =
      "I acknowledge and understand that Wellmark, Inc. is a tobacco-free employer and that I do not use tobacco or tobacco-like products (examples include but are not limited to cigarettes, e-cigarettes, marijuana (in any smokable form), cigars, chewing tobacco, snuff, pipes, vaporizers, etc.) as written in its Tobacco-Free Workplace Policy. I further understand that Wellmark, Inc. is relying on my acknowledgement that I do not use tobacco or prohibited products as a condition of its consideration of my employment. More info about this policy in the FAQ's on our careers page I understand that false or misleading information given in my application, interview(s), or any supplemental information in the employment process may result in termination. I understand, also, that I am required to abide by all rules and policies of Wellmark, Inc. I have read and understand the statements above about Wellmark's policies";
    expect(one({ label, options: YN }, "Des Moines, IA, United States", "Wellmark, Inc.")).toEqual(every(null));
  });

  it("a referral asked as 'refer you to this position' (Cricut, CRB)", () => {
    const got = one({ label: "Did a Cricut employee refer you to this position?", options: YN }, "South Jordan, UT, United States", "Cricut");
    expect(got.COMPLETE_CANADIAN).toBe("No");
    expect(got.BERLIN_STAFF).toBe("Yes");
    const crb = one({ label: "Have you been referred to this position by an existing CRB employee?", options: YN }, "Kansas City, MO, United States", "CRB");
    expect(crb.COMPLETE_CANADIAN).toBe("No");
  });

  it("a referral by a government official is the government question, not a referral (Robinhood on Greenhouse)", () => {
    // Yes for the applicant an employee referred (bank r7d).
    const label =
      "Robinhood adheres to applicable laws and regulations in relation to government officials given inherent bribery and/or corruption risk. A government official is any person that performs a public function on any level or acts in any official capacity on behalf of a government or government owned entity. \na)  Do you currently hold or have you held, within the last 5 years, a position as a government official?\nb)  Have you been referred or recommended for this position by a government official?\nc)  Are you related to or have a close personal relationship with a government official?";
    expect(one({ label, options: YN }, "New York, NY", "Robinhood")).toEqual(every("No"));
  });

  it("requiring a visa to work is the sponsorship question (Circle Logistics)", () => {
    expect(one({ label: "Will you now or in the future require a visa to work in the United States?", options: YN }, "Fort Wayne, IN, United States")).toEqual({
      COMPLETE_CANADIAN: "Yes",
      US_H1B_SENIOR: "Yes",
      BOOTCAMP_CAREER_GAP: "No",
      US_OPT_ANALYST: "Yes",
      BERLIN_STAFF: "Yes",
      INDIA_NEW_GRAD: "Yes",
    });
  });

  it("'if not, type N/A' takes N/A from an applicant it does not apply to (M3USA, National Life on Greenhouse)", () => {
    expect(one({ label: "Did someone from our company suggest you apply? If so, please provide their name – if not type N/A." }, "Fort Washington, PA, United States", "M3USA").COMPLETE_CANADIAN).toBe("N/A");
    const nl =
      "Are you involved in any business activity that you would expect to continue during your employment with National Life Group? If yes, please explain in the space provided here. If no or not applicable, type N/A.";
    expect(one({ label: nl, type: "textarea" }, "Addison, TX", "National Life Group")).toEqual(every("N/A"));
  });

  it("a status question for nonimmigrants is Not Applicable to a citizen (National Life on Greenhouse, live)", () => {
    const label =
      "If your current employment eligibility is based on F-1 student visa optional practical training (OPT) or another nonimmigrant status (e.g., TN, E-3, etc.), will you be seeking Company support for either STEM OPT eligibility or maintenance (once your current OPT expires) or H-1B lottery registration?";
    expect(one({ label, options: ["Yes", "No", "Not Applicable"] }, "Addison, TX", "National Life Group").BOOTCAMP_CAREER_GAP).toBe("Not Applicable");
  });

  it("one option per language spoken, never one the question leaves out (Gallup on Greenhouse, live)", () => {
    // Every option holding the word was ticked: fourteen "English (…)",
    // "Creoles and pidgins, English based", "German, Old High".
    const LANGS = [
      "Creoles and pidgins, English based", "Creoles and pidgins, French-based", "English", "English (Australia)", "English (Canada)", "English (United States)",
      "French", "French (Canada)", "French (France)", "German", "German (Germany)", "German (Switzerland)", "German, Middle High", "German, Old High", "Low German",
      "Haitian", "Hindi", "Indian (Hindi)", "Indian (Kannada)", "Kannada", "Korean", "Spanish", "Spanish (Mexico)", "Tamil (Ceylon)", "Tamil (India)", "Vietnamese",
    ];
    const got = one({ label: "Please indicate which languages you speak in addition to English:", options: LANGS, type: "multi" }, "San Francisco, CA", "Gallup");
    expect(got.COMPLETE_CANADIAN).toBe("French");
    expect(got.US_H1B_SENIOR).toBe("Vietnamese");
    expect(got.US_OPT_ANALYST).toBe("Korean");
    expect(got.BERLIN_STAFF).toBe("German");
    expect(got.INDIA_NEW_GRAD).toBe("Tamil (India), Kannada, Hindi");
  });

  it("Greenhouse's multi-select is a list of answers too, once its options are read (Gallup, live)", () => {
    // The same question on the live page is a react-select multi
    // (value-container--is-multi), read as a single choice: the language rule
    // never applied and it went to the AI (blank, both live runs). Markup
    // from the page's field capture.
    const LANGS = ["English", "French", "German", "Hindi", "Kannada", "Korean", "Spanish", "Tamil (India)", "Vietnamese"];
    document.body.innerHTML = `<form id="application-form"><div class="field"><label id="question_7988717009[]-label" for="question_7988717009[]">Please indicate which languages you speak in addition to English:</label><div class="select-shell remix-css-b62m3t-container"><div><div class="select__control--outside-label select__control remix-css-13cymwt-control"><div class="select__value-container select__value-container--is-multi remix-css-hlgwow"><div class="select__placeholder remix-css-1jqq78o-placeholder" id="react-select-question_7988717009[]-placeholder">Select...</div><div class="select__input-container remix-css-19bb58m" data-value=""><input class="select__input" autocomplete="off" id="question_7988717009[]" tabindex="0" type="text" aria-autocomplete="list" aria-expanded="false" aria-haspopup="true" aria-labelledby="question_7988717009[]-label" aria-required="false" role="combobox" aria-activedescendant="" value=""></div></div><div class="select__indicators--outside-label select__indicators remix-css-1wy0on6"><button type="button" class="icon-button icon-button--sm" aria-label="Toggle flyout" tabindex="-1"></button></div></div></div></div></div></form>`;
    setResolveContext({ jobCountry: "US", jobCity: "San Francisco", jobPlaces: null, company: "Gallup" });
    const { fields, registry } = scanPage(persona("US_OPT_ANALYST"), true);
    const f = fields.find((x) => x.label.startsWith("Please indicate which languages"))!;
    expect(resolveWithOptions(f, registry, persona("US_OPT_ANALYST"), null, true, LANGS)).toBe("Korean");
    expect(resolveWithOptions(f, registry, persona("INDIA_NEW_GRAD"), null, true, LANGS)).toBe("Tamil (India), Kannada, Hindi");
    setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  });

  it("a box for one language takes one, without its level (SBT, City and County of San Francisco)", () => {
    // Got "English (Native), French (Professional)": a row of SmartRecruiters'
    // language list asks one language, its level in the next box.
    const got = one({ label: "Please type in a language that you speak" }, "Plano, TX, United States");
    expect(got.COMPLETE_CANADIAN).toBe("English");
    expect(got.US_H1B_SENIOR).toBe("Vietnamese");
  });

  it("someone enrolled nowhere attends no college nearby (Gallup on Greenhouse, live)", () => {
    const got = one({ label: "Do you currently attend a college or university within an 80-mile radius of San Francisco, CA?", options: YN }, "San Francisco, CA", "Gallup");
    expect(got.US_OPT_ANALYST).toBe("No");
    expect(got.US_H1B_SENIOR).toBe("No");
  });
});
