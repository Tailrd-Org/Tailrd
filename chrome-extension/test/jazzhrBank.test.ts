// @vitest-environment-options {"url": "https://qbank.applytojob.com/apply/QBANK0001/qbank"}
/**
 * The JazzHR question bank (round 8: prod's 31 active postings and up to three
 * more of each company's, 86 postings, read blind). Labels, options and markup
 * verbatim from each apply page (GET only): JazzHR's form-group, its control
 * label and its resumator names. Tested through the scan path.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import { detectJobPlace } from "../src/content/jobLocation";
import { extractJobIdentity } from "../src/content/jobContext";
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

/** A JazzHR questionnaire question: a select (options), a checkbox group
 *  (`boxes`), or a text box (neither). */
function question(label: string, opts: { options?: string[]; boxes?: string[]; textarea?: boolean; required?: boolean } = {}): string {
  const id = "2900001";
  const mark = opts.required === false ? "" : `<i class="asterisk">*</i>`;
  const head = `<label id="resumator-questionnaire-q${id}-label" for="resumator-questionnaire-q${id}" class="control-label">${esc(label)}${mark}</label>`;
  if (opts.boxes) {
    const boxes = opts.boxes.map((b, i) => `<label class="checkbox" for="resumator-checkbox-${id}-${i + 1}"><input type="checkbox" id="resumator-checkbox-${id}-${i + 1}" name="resumator-checkbox-${id}-${i + 1}" class="resumator-questionnaire-checkbox" value="${esc(b)}">${esc(b)}</label>`).join("");
    return `<div class="form-group">${head}<input type="hidden" name="resumator-questionnaire[${id}]" id="resumator-questionnaire-q${id}" class="resumator-questionnaire-checkbox-answer" value="">${boxes}</div>`;
  }
  if (opts.options) {
    const options = [`<option value="">-- No answer --</option>`, ...opts.options.map((o) => `<option value="${esc(o)}">${esc(o)}</option>`)].join("");
    return `<div class="form-group">${head}<select name="resumator-questionnaire[${id}]" id="resumator-questionnaire-q${id}" class="form-control">${options}</select></div>`;
  }
  if (opts.textarea) return `<div class="form-group">${head}<textarea name="resumator-questionnaire[${id}]" id="resumator-questionnaire-q${id}" class="form-control resumator-field"></textarea></div>`;
  return `<div class="form-group">${head}<input type="text" name="resumator-questionnaire[${id}]" id="resumator-questionnaire-q${id}" maxlength="255" class="form-control resumator-field" value=""></div>`;
}

/** JazzHR's apply page around a form: its logo (alt "brand logo"), the job's
 *  attributes and the title that alone names the company. */
function page(form: string, location: string, company: string): void {
  document.title = `Role - ${company} - Career Page`;
  document.body.innerHTML = `<div class="brand-logo"><img src="logo.png" alt="brand logo"></div><h2>Role</h2><div class="job-attributes-container"><div title="Location"> <i class="fa fa-map-marker"></i>${esc(location)} </div><div id="resumator-job-employment" title="Type"> <i class="fa fa-clock-o"></i>Full Time </div></div><form id="form_submit_new_resume" action="https://qbank.applytojob.com/apply/QBANK0001/qbank" method="POST"><div class="form-group"><label for="resumator-email-value" class="control-label">Email Address<i class="asterisk">*</i></label><input type="email" name="resumator-email-value" id="resumator-email-value"></div>${form}</form>`;
}

/** The field whose label starts with `label`, on a JazzHR page for `company`
 *  in `location`, the page's own place and company read as the extension
 *  reads them: each persona's proposed answer (null when blank). */
function scanned(who: string[], html: string, label: string, location: string, company = "Acme"): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const w of who) {
    page(html, location, company);
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company: extractJobIdentity(document).company });
    const squash = (s: string): string => s.replace(/\s+/g, " ").trim();
    const f = scanPage(persona(w), true).fields.find((x) => squash(x.label).startsWith(squash(label).slice(0, 40)));
    out[w] = f?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}
const each = (values: (string | null)[]): Record<string, string | null> => Object.fromEntries(ALL.map((w, i) => [w, values[i]]));

describe("the job's place and company on JazzHR's apply page (every one of the bank's 86 pages read neither)", () => {
  it("the place is the job attribute titled Location", () => {
    page("", "Ottawa, ON, Canada", "TechInsights");
    expect(detectJobPlace(document)).toMatchObject({ country: "CA", city: "Ottawa" });
    page("", "New York City, NY", "Healthpeak Properties, Inc.");
    expect(detectJobPlace(document)).toMatchObject({ country: "US" });
  });
  it("the company is the title's, never the logo's 'brand'", () => {
    page("", "New York City, NY", "Healthpeak Properties, Inc.");
    expect(extractJobIdentity(document).company).toBe("Healthpeak Properties, Inc.");
  });
  it("so an unscoped sponsorship question is asked of the job's country: none for a Canadian in Ottawa", () => {
    const L = "Do you require sponsorship now or any time in the future?";
    expect(scanned(["COMPLETE_CANADIAN", "INDIA_NEW_GRAD"], question(L, { options: ["Yes", "No"] }), L, "Ottawa, ON, Canada", "TechInsights")).toEqual({ COMPLETE_CANADIAN: "No", INDIA_NEW_GRAD: "Yes" });
  });
});

describe("JazzHR's own citizenship / employment eligibility list", () => {
  const STATUS = `<div class="form-group"><label id="resumator-citizen-label" for="resumator-citizen-value" class="control-label">What's your citizenship / employment eligibility?<i class="asterisk">*</i></label><select id="resumator-citizen-value" name="resumator-citizen-value" class="form-control"><option value="0" selected="">No answer</option><optgroup label="United States"><option value="10">I am a U.S. Citizen/Permanent Resident</option><option value="20">Non-citizen allowed to work for any employer</option><option value="30">Non-citizen allowed to work for current employer</option><option value="40">Non-citizen seeking work authorization</option></optgroup><optgroup label="Canada"><option value="60">I am a Canadian Citizen/Permanent Resident</option></optgroup><option value="99">Other</option></select></div>`;
  it("is the applicant's own status, never Other: a citizen's country, else a non-citizen with no right to work here seeking one (H-1B and OPT stay blank, D-61)", () => {
    expect(scanned(ALL, STATUS, "What's your citizenship", "Claremore, OK")).toEqual(
      each(["I am a Canadian Citizen/Permanent Resident", null, "I am a U.S. Citizen/Permanent Resident", null, "Non-citizen seeking work authorization", "Non-citizen seeking work authorization"])
    );
  });
});

describe("work authorization and sponsorship (Infinitive, Foxconn)", () => {
  it("hired without the need for visa sponsorship at any time is the right without sponsorship, not the need for it", () => {
    const L = "Are you eligible to be hired by Infinitive as a full-time employee without the need for Infinitive visa sponsorship at any time during your employment?";
    expect(scanned(ALL, question(L, { options: YN }), L, "McLean, VA", "Infinitive")).toEqual(each(["No", "No", "Yes", "No", "No", "No"]));
  });
  it("a work visa currently sponsored by an employer asks what the visa is: No for no right there or a citizen's; the H-1B and OPT statements are withheld (D-61)", () => {
    const L = "Is your work visa currently being sponsored by an employer?";
    expect(scanned(ALL, question(L, { options: YN }), L, "McLean, VA", "Infinitive")).toEqual(each(["No", null, "No", null, "No", "No"]));
  });
  it("authorized to work for any employer is a citizen's or resident's right; a right of a kind not known stays blank", () => {
    const L = "Are you currently legally authorized to work in the US for any employer?";
    expect(scanned(ALL, question(L, { options: YN }), L, "McLean, VA", "Infinitive")).toEqual(each(["No", null, "Yes", null, "No", "No"]));
  });
  it("a visa an employer sponsors, stated plainly, is Yes to 'currently sponsored' and No to 'any employer'", () => {
    const h1b = { ...persona("US_H1B_SENIOR"), workAuthorization: "H-1B visa" } as UserApplicationProfile;
    const run = (label: string): string | null => {
      page(question(label, { options: YN }), "McLean, VA", "Infinitive");
      setResolveContext({ jobCountry: "US", jobCity: "McLean", jobPlaces: null, company: "Infinitive" });
      return scanPage(h1b, true).fields.find((x) => x.label.startsWith(label.slice(0, 40)))?.proposedValue ?? null;
    };
    expect(run("Is your work visa currently being sponsored by an employer?")).toBe("Yes");
    expect(run("Are you currently legally authorized to work in the US for any employer?")).toBe("No");
  });
  it("a yes/no asked in a text box is answered Yes or No, not with the status statement", () => {
    const L = "If hired, can you show proof that you are eligible to work in the U.S.?";
    expect(scanned(ALL, question(L), L, "San Jose, CA", "Foxconn")).toEqual(each(["No", "Yes", "Yes", "Yes", "No", "No"]));
  });
  it("whether sponsorship is required, asked after another question in a text box, is Yes or No", () => {
    const L = "If you answered “Yes” above, do you currently require or will you require in the future, company sponsorship (e.g., work visa sponsorship) to work legally in the U.S.?";
    expect(scanned(ALL, question(L), L, "San Jose, CA", "Foxconn")).toEqual(each(["Yes", "Yes", "No", "Yes", "Yes", "Yes"]));
  });
});

describe("education (Axtria, Calyx, West 4th, ASR, Space Dynamics Lab, SimIS)", () => {
  it("the highest degree earned or pursuing counts the one in progress", () => {
    const L = "Highest Degree Earned/Pursuing";
    expect(scanned(ALL, question(L, { options: ["Associate Degree", "Bachelor", "Master", "MBA", "PhD", "Others"] }), L, "Berkeley Heights, NJ")).toEqual(
      each(["Bachelor", "Master", null, "Master", null, "Bachelor"])
    );
  });
  it("the completion date of the highest degree is its month and year, when the profile has the month", () => {
    const L = "Completion date of your highest degree (mm/yyyy)";
    expect(scanned(ALL, question(L), L, "Berkeley Heights, NJ")).toEqual(each(["04/2027", null, null, "05/2025", null, "06/2026"]));
  });
  it("a graduate degree's field is asked of a graduate degree only", () => {
    const L = "Field of Study: Graduate/MBA/PhD Degree";
    const got = scanned(ALL, question(L, { required: false }), L, "Berkeley Heights, NJ");
    expect(got).toMatchObject({ COMPLETE_CANADIAN: null, US_H1B_SENIOR: "Information Systems", BOOTCAMP_CAREER_GAP: null, US_OPT_ANALYST: "Data Science", INDIA_NEW_GRAD: null });
  });
  it("a Bachelor of Business Administration's field is Business Administration", () => {
    const L = "Field of Study / Major: Undergraduate Degree";
    expect(scanned(["US_OPT_ANALYST"], question(L), L, "Berkeley Heights, NJ")).toEqual({ US_OPT_ANALYST: "Business Administration" });
  });
  it("a graduate degree is 'Graduate Degree or higher' (a checkbox group)", () => {
    const L = "What is your highest level of educational attainment?";
    const boxes = ["High school degree or equivalent", "Some college, or Associate's Degree", "Bachelor's Degree", "Graduate Degree or higher", "Prefer not to say"];
    const got = scanned(ALL, question(L, { boxes, required: false }), L, "West Valley City, UT", "Calyx Containers");
    expect(got).toMatchObject({ US_H1B_SENIOR: "Graduate Degree or higher", US_OPT_ANALYST: "Graduate Degree or higher", INDIA_NEW_GRAD: "Bachelor's Degree", BOOTCAMP_CAREER_GAP: "High school degree or equivalent" });
  });
  it("'Bachelor's Degree or Higher' holds a master's", () => {
    const L = "What is your highest level of education?";
    const got = scanned(ALL, question(L, { options: ["High School or GED", "Associate's Degree", "Bachelor's Degree or Higher"] }), L, "Remote");
    expect(got).toMatchObject({ US_H1B_SENIOR: "Bachelor's Degree or Higher", US_OPT_ANALYST: "Bachelor's Degree or Higher", INDIA_NEW_GRAD: "Bachelor's Degree or Higher", BOOTCAMP_CAREER_GAP: "High School or GED" });
  });
  it("a college or university box never takes a bootcamp", () => {
    const L = "College or University";
    expect(scanned(["BOOTCAMP_CAREER_GAP", "US_H1B_SENIOR"], question(L, { required: false }), L, "Claremore, OK")).toEqual({ BOOTCAMP_CAREER_GAP: null, US_H1B_SENIOR: "University of Washington" });
  });
  it("enrolled at a school that is not the student's own is No", () => {
    const L = "Are you currently enrolled at Utah State University?";
    expect(scanned(ALL, question(L, { options: YN }), L, "North Logan, UT", "Space Dynamics Laboratory")).toEqual(each(["No", "No", "No", "No", "No", "No"]));
  });
  it("a degree in named fields: never Yes for a degree in another field, No for a degree not held", () => {
    const MS = "Do you have:  Masters of Science degree in physics, nuclear engineering, or related discipline from an accredited university";
    expect(scanned(ALL, question(MS), MS, "Arlington, VA", "SimIS")).toEqual(each(["No", null, "No", null, null, "No"]));
    const EE = "Do you have a bachelor’s or graduate degree in electrical or computer engineering?";
    expect(scanned(["US_OPT_ANALYST", "COMPLETE_CANADIAN", "BOOTCAMP_CAREER_GAP"], question(EE, { options: YN }), EE, "North Logan, UT")).toEqual({ US_OPT_ANALYST: null, COMPLETE_CANADIAN: "No", BOOTCAMP_CAREER_GAP: "No" });
  });
  it("degrees as alternatives with their years are no one degree required: never No for a master's and fifteen years", () => {
    const L = "Do you have 5+ years of professional engineering experience with a BS, 3+ years with a MS, or 0+ years with a PhD?";
    const got = scanned(ALL, question(L, { options: YN }), L, "Stafford, VA", "Space Dynamics Laboratory");
    expect(got.US_H1B_SENIOR).not.toBe("No");
    expect(got).toMatchObject({ COMPLETE_CANADIAN: "No", US_OPT_ANALYST: "No", INDIA_NEW_GRAD: "No" });
  });
  it("a university with a degree of unknown level is still a university", () => {
    const L = "College or University";
    expect(scanned(["BERLIN_STAFF"], question(L, { required: false }), L, "Claremore, OK")).toEqual({ BERLIN_STAFF: "Technische Universität München" });
  });
  it("a PhD with years of experience is No without the PhD, whatever the years", () => {
    const L = "Do you have the desired • PhD in physics, nuclear engineering, electrical engineering or related discipline with six (6) years of experience?";
    expect(scanned(ALL, question(L, { textarea: true }), L, "Arlington, VA", "SimIS")).toEqual(each(["No", "No", "No", "No", "No", "No"]));
  });
});

describe("years in a named role (VivSoft, West 4th)", () => {
  it("total years cap a role's years but never prove them: 2 years as an Appian Developer is No under 2 years in all, else unknown", () => {
    const L = "This position requires at least 2 years of professional experience working as an Appian Developer. Do you meet this requirement?";
    expect(scanned(ALL, question(L, { options: YN }), L, "Remote", "VivSoft Technologies")).toEqual(each(["No", null, null, "No", null, "No"]));
  });
  it("years as a Data Engineer: the lowest range from a short career, the role's own rows when every title names it, never the top range from other roles", () => {
    const L = "How many years of experience do you have as a Data Engineer?";
    const options = ["0-2", "3-5", "6-9", "10+"];
    const got = scanned(ALL, question(L, { options }), L, "Arlington, VA", "West 4th Strategy");
    expect(got).toEqual(each(["0-2", null, null, "0-2", null, "0-2"]));
    // Every row a data engineer's (since 2019, seven years stated): the role
    // is proven, its years the career's.
    const base = persona("US_H1B_SENIOR") as UserApplicationProfile & { experience?: { title?: string; startDate?: string }[] };
    const engineer = { ...base, yearsOfExperience: "7", experience: (base.experience ?? []).filter((x) => /data engineer/i.test(x.title ?? "") && (x.startDate ?? "") >= "2019") } as UserApplicationProfile;
    page(question(L, { options }), "Arlington, VA", "West 4th Strategy");
    setResolveContext({ jobCountry: "US", jobCity: "Arlington", jobPlaces: null, company: "West 4th Strategy" });
    expect(scanPage(engineer, true).fields.find((x) => x.label.startsWith("How many years"))?.proposedValue).toBe("6-9");
  });
});

describe("statements and boxes that are no self-ID, language or ID question (Space Dynamics Lab, Axtria, Productive Playhouse, Strategic Workforce Development, Laansu)", () => {
  it("an attestation ending on an employer's disability policy is accepted, never withdrawn by a self-ID pick", () => {
    const L =
      "I understand and agree that any misrepresentation on this application will be sufficient cause for cancellation of this application and/or separation from the employer's service if I have been employed. I give the employer the right to investigate all references and to secure additional information about me, if job-related. I hereby release from liability the employer and its representatives for seeking such information and all other persons, corporations or organizations for furnishing such information. The employer is an Equal Opportunity Employer. The employer does not discriminate in employment and no question on this application is used for the purpose of limiting or excusing any applicant's consideration for employment on a basis prohibited by local, state, or federal law. I understand that just as I am free to resign at any time, the employer reserves the right to terminate my employment at any time, with or without cause and without prior notice. I understand that no representative of the employer has the authority to make any assurances to the contrary. I understand that it is this company's policy not to refuse to hire a qualified individual with a disability because of this person's need for an accommodation that would be required by the ADA.";
    const options = ["I understand and accept these terms", "I don't accept these terms and wish to withdraw my application"];
    const got = scanned(ALL, question(L, { options }), L, "North Logan, UT", "Space Dynamics Laboratory");
    for (const w of ALL) expect(got[w], w).not.toBe("I don't accept these terms and wish to withdraw my application");
    expect(got.COMPLETE_CANADIAN).toBe("I understand and accept these terms");
  });
  it("a level asked for a language the profile does not list is its 'none' option, never another language's level (decision 105)", () => {
    const L = "Please select your Japanese language proficiency level:";
    expect(scanned(ALL, question(L, { options: ["Native", "JLPT N1", "JLPT N2", "JLPT N3", "JLPT N4", "No Proficiency"] }), L, "Tokyo, Japan")).toEqual(
      each(["No Proficiency", "No Proficiency", "No Proficiency", "No Proficiency", "No Proficiency", "No Proficiency"])
    );
  });
  it("which language the applicant applies for is theirs to name", () => {
    const L = "Which language are you applying for?";
    expect(scanned(["COMPLETE_CANADIAN", "INDIA_NEW_GRAD"], question(L), L, "Remote")).toEqual({ COMPLETE_CANADIAN: null, INDIA_NEW_GRAD: null });
  });
  it("two forms of valid ID are no degree held", () => {
    const L = "Do you have two forms of valid ID (for example, a state ID or driver's license plus a Social Security card or birth certificate)?";
    expect(scanned(ALL, question(L, { options: YN }), L, "Kansas City, MO")).toEqual(each([null, null, null, null, null, null]));
  });
  it("being clearable is being able to obtain one: No for anyone not a US citizen, a citizen's own to answer (decision 81)", () => {
    const L = "Are you clearable for a Public Trust Clearance? (See minimum clearance requirements in job description)";
    expect(scanned(ALL, question(L, { options: YN }), L, "Sterling, VA", "Laansu")).toEqual(each(["No", "No", null, "No", "No", "No"]));
  });
});

describe("age, relocation and the job's own site (Space Dynamics Lab, Axtria)", () => {
  it("18 years or older, without 'of age'", () => {
    const L = "Are you 18 years or older?";
    expect(scanned(ALL, question(L, { options: YN }), L, "North Logan, UT")).toEqual(each(["Yes", "Yes", "Yes", "Yes", "Yes", "Yes"]));
  });
  it("willing to move to an area is the willingness to relocate", () => {
    const L = "Would you be willing to move to New Jersey area if you currently reside elsewhere?";
    expect(scanned(ALL, question(L, { options: YN }), L, "Berkeley Heights, NJ", "Axtria")).toEqual(each(["Yes", "No", "No", "Yes", "Yes", "Yes"]));
  });
  it("relocation options written as sentences", () => {
    const L = "This position requires onsite support at the location listed in the job posting. Are you willing to work onsite and relocate if you do not currently reside within commuting distance?";
    const options = ["Yes, I am willing to relocate.", "Yes, I live within commuting distance.", "No, I do not wish to relocate or support onsite."];
    const yes = options[0];
    const no = options[2];
    expect(scanned(ALL, question(L, { options }), L, "Stafford, VA", "Space Dynamics Laboratory")).toEqual(each([yes, no, no, yes, yes, yes]));
  });
  it("the company's own website, its name shortened ('Space Dynamics Lab Website')", () => {
    const L = "Where did you hear about this job?";
    const options = ["Space Dynamics Lab Website", "From a Space Dynamics Lab Employee", "LinkedIn", "Indeed.com", "GlassDoor", "Clearancejobs.com", "Other"];
    expect(scanned(["US_H1B_SENIOR"], question(L, { options }), L, "North Logan, UT", "Space Dynamics Laboratory")).toEqual({ US_H1B_SENIOR: "Space Dynamics Lab Website" });
  });
});
