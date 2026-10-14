// @vitest-environment-options {"url": "https://jobs.lever.co/qbank/00000000-0000-0000-0000-000000000000/apply"}
/**
 * The Lever question bank (round 5, 2026-10-08): 55 postings from the 52
 * Lever boards in prod scraped_jobs, read from each posting's own /apply page
 * (GET only) and answered for six personas. Each test is an answer the bank
 * got wrong, or a blank a stated fact answers; labels and options verbatim.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import { detectJobPlace } from "../src/content/jobLocation";
import { resolveQuestion, type QuestionInput } from "../src/content/questionResolver";
import { profileFacts } from "../src/content/profileFacts";
import * as P from "./e2e/profiles.mjs";
import type { UserApplicationProfile } from "../src/shared/types";
import { TEST_TODAY } from "./fixtures/profiles";
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

/** One question as the bank renders it, on a page placed in `location`; the
 *  scanner's proposed answer for each persona (null when blank). */
function scanned(label: string, options: string[], location: string, type: "select" | "text" | "textarea" | "checks" = options.length ? "select" : "text", company = "Acme"): Record<Who, string | null> {
  const control =
    type === "select"
      ? `<select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select>`
      : type === "checks"
        ? options.map((o, i) => `<label><input type="checkbox" name="q0[]" value="${i}"> ${esc(o)}</label>`).join("")
        : type === "textarea"
          ? `<textarea id="q0" name="q0"></textarea>`
          : `<input type="text" id="q0" name="q0">`;
  const field = type === "checks" ? `<fieldset class="field" id="q0"><legend>${esc(label)}</legend>${control}</fieldset>` : `<div class="field"><label for="q0">${esc(label)}</label>${control}</div>`;
  const out = {} as Record<Who, string | null>;
  for (const who of ALL) {
    document.body.innerHTML = `<h1 class="job__title">Role</h1><div class="job__location">${esc(location)}</div><form id="application-form">${field}<div class="field"><label for="em">Email</label><input type="text" id="em" name="email"></div><div class="field"><label for="nm">Full name</label><input type="text" id="nm" name="name"></div></form>`;
    const place = detectJobPlace(document);
    setResolveContext({ jobCountry: place.country, jobCity: place.city, jobPlaces: place.places ?? null, company });
    const { fields } = scanPage(persona(who), true);
    const f = fields.find((x) => x.label.startsWith(label.slice(0, 40)));
    out[who] = f?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}

/** The question resolver alone (with its defaults), as a select or a text box. */
function ask(who: Who, label: string, options: string[], jobCountry: string | null = "US", jobCity: string | null = null): string | null {
  const q: QuestionInput = { label, controlType: options.length ? "select" : "text", options, category: "unknown", kind: options.length === 2 && /^yes$/i.test(options[0]) ? "boolean" : options.length ? "choice" : "text" };
  const r = resolveQuestion(q, profileFacts(persona(who), TEST_TODAY), persona(who), { jobCountry, jobCity, company: "Acme" });
  return r && r.status === "answer" ? r.value : null;
}

describe("accommodations at work are not in-office questions (Wattpad)", () => {
  // "(Whether work from home or in-office)" read as an office requirement:
  // everyone willing to move said they need accommodations for a mental
  // health condition; the disability answer stood in for "require".
  const YNP = ["Yes", "No", "Prefer not to say"];
  it("mental health: No for those who state no disability, never Yes", () => {
    const a = scanned("Do you require workplace accommodations due to a mental health condition? (Whether work from home or in-office)", YNP, "Toronto, Ontario");
    expect(a.COMPLETE_CANADIAN).toBe("No");
    expect(a.US_H1B_SENIOR).toBe("No");
    expect(a.US_OPT_ANALYST).toBe("No");
    for (const who of ALL) expect(a[who]).not.toBe("Yes");
  });
  it("a physical disability is not a need for accommodation: theirs to say", () => {
    const a = scanned("Do you require workplace accommodations due to a physical disability or medical condition? (Whether work from home or in-office)", YNP, "Toronto, Ontario");
    expect(a.BOOTCAMP_CAREER_GAP).toBeNull();
    expect(a.COMPLETE_CANADIAN).toBe("No");
  });
});

describe("being local is where one lives, not a requirement to accept (Data Lab)", () => {
  it("'Are you local to the Germantown, MD office (within 25 miles)': No for everyone far from it", () => {
    for (const who of ALL) expect(ask(who, "Are you local to the Germantown, MD office (within 25 miles)", ["Yes", "No"], "US", "Germantown")).toBe("No");
  });
});

describe("CPT and OPT are an F-1 student's (SEP)", () => {
  it("'Does your work authorization now, or will it in the future, involve CPT or OPT?'", () => {
    const q = "Does your work authorization now, or will it in the future, involve CPT (Curricular Practical Training) or OPT (Optional Practical Training)?";
    expect(ask("US_OPT_ANALYST", q, ["Yes", "No"])).toBe("Yes");
    expect(ask("US_H1B_SENIOR", q, ["Yes", "No"])).toBe("No");
    expect(ask("BOOTCAMP_CAREER_GAP", q, ["Yes", "No"])).toBe("No");
    expect(ask("COMPLETE_CANADIAN", q, ["Yes", "No"])).toBe("No");
  });
});

describe("experience questions are not consents or degrees", () => {
  it("'Do you have experience with Direct Marketing Campaigns?' is no marketing opt-out", () => {
    for (const who of ALL) expect(ask(who, "Do you have experience with Direct Marketing Campaigns?", ["Yes", "No"])).toBeNull();
  });
  it("'…implementing RBAC and MFA…' names no Master of Fine Arts", () => {
    const q = "Do you have experience working with authentication protocols (e.g., SAML, OAuth), as well as implementing RBAC and MFA in a production environment?";
    for (const who of ALL) expect(ask(who, q, ["Yes", "No"], "CA")).toBeNull();
  });
  it("years in desktop or IT support roles are not a software career's", () => {
    const opts = ["Less than 1 year", "1-2 years", "2-4 years", "5+ years"];
    const q = "How many years of experience do you have in desktop or IT support roles?";
    expect(ask("COMPLETE_CANADIAN", q, opts, null)).toBeNull();
    expect(ask("BERLIN_STAFF", q, opts, null)).toBeNull();
  });
});

describe("the city you will work from is not where you live now (SEP)", () => {
  it("'If you are accepted for this in-person position, what city will you be working from?'", () => {
    const a = scanned("If you are accepted for this in-person position, what city will you be working from?", [], "Westfield, IN");
    for (const who of ALL) expect(a[who]).toBeNull();
  });
});

describe("notice periods in other units (FiscalNote)", () => {
  const OPTS = ["None/Immediate Availability", "1-2 weeks", "3-4 weeks", "4+ weeks"];
  const Q = "If you receive an offer, how much notice would you need to provide to your current employer?";
  it("3 months is more than four weeks; 4 weeks is 3-4 weeks; immediately is none", () => {
    expect(ask("BERLIN_STAFF", Q, OPTS)).toBe("4+ weeks");
    expect(ask("US_H1B_SENIOR", Q, OPTS)).toBe("3-4 weeks");
    expect(ask("BOOTCAMP_CAREER_GAP", Q, OPTS)).toBe("None/Immediate Availability");
    expect(ask("COMPLETE_CANADIAN", Q, OPTS)).toBe("1-2 weeks");
  });
});

describe("a state list in the label (Kobie)", () => {
  it("'Are you currently located in one of these states? Colorado, …' reads the states", () => {
    const q = "Kobie operates in the following states. Are you currently located in one of these states? Colorado, Connecticut, District of Columbia, Florida, Georgia, Illinois, Indiana, Louisiana, Maryland, Massachusetts, Michigan, Minnesota, Mississippi, Missouri, Nevada, New Jersey, New Mexico, New York, North Carolina, Ohio, Oklahoma, Oregon, Pennsylvania, Rhode Island, South Carolina, Tennessee, Texas, Vermont, Virginia, Wisconsin";
    const OPTS = ["Yes, I'm currently authorized to work and located in one of Kobie's operational states", "No, I'm located outside Kobie's operational states"];
    expect(ask("BOOTCAMP_CAREER_GAP", q, OPTS, null)).toBe(OPTS[0]);
    expect(ask("US_OPT_ANALYST", q, OPTS, null)).toBe(OPTS[0]);
    expect(ask("US_H1B_SENIOR", q, OPTS, null)).toBe(OPTS[1]);
    expect(ask("COMPLETE_CANADIAN", q, OPTS, null)).toBe(OPTS[1]);
  });
});

describe("who referred you is never the applicant (Artera)", () => {
  it("'Were you referred to Artera? If so, please provide their first and last name.'", () => {
    const a = scanned("Were you referred to Artera? If so, please provide their first and last name.", [], "Seattle, Washington");
    expect(a.BERLIN_STAFF).toBeNull();
    expect(a.COMPLETE_CANADIAN).toBe("No");
  });
});

describe("statements of awareness are acknowledged (Zoox)", () => {
  it("'I am aware that this is a hybrid role… based out of Foster City, CA.' is Yes for everyone", () => {
    const a = scanned("I am aware that this is a hybrid role, with 3 days in office expectation, based out of Foster City, CA.", ["Yes", "No"], "Foster City, CA");
    for (const who of ALL) expect(a[who]).toBe("Yes");
  });
});

describe("a preferred name only when it differs (Zoox)", () => {
  it("'If your legal first and last name is your preferred name, you do not need to respond.'", () => {
    const a = scanned("What is your preferred first name and last name? If your legal first and last name is your preferred name, you do not need to respond.", [], "Hayward, CA");
    for (const who of ALL) expect(a[who]).toBeNull();
  });
});

describe("Canada's employment-equity questions (eqbank, Wattpad)", () => {
  it("'Do you self-identify as a racialized person?' is about race, not Hispanic origin", () => {
    const q = "Do you self-identify as a racialized person?Racialized persons - For the purposes of employment equity, members of such groups in Canada are persons other than Aboriginal peoples, who are non-Caucasian in race or non-white in colour. Examples: Black, South Asian, Chinese, Filipino, Latin American, Arab, Korean, Japanese, Southeast Asian, West Asian.";
    const a = scanned(q, ["Yes", "No", "Prefer not to say"], "Toronto");
    expect(a.COMPLETE_CANADIAN).toBe("No");
    expect(a.BERLIN_STAFF).toBe("No");
    expect(a.US_H1B_SENIOR).toBe("Yes");
    expect(a.BOOTCAMP_CAREER_GAP).toBe("Yes");
    expect(a.US_OPT_ANALYST).toBe("Yes");
  });
});

describe("a UK ethnicity list: the stated race's 'any other' background (Spotify)", () => {
  const UK = ["Any other ethnic group", "Arab", "Asian/Asian British: Any other Asian background", "Asian/Asian British: Bangladeshi", "Asian/Asian British: Chinese", "Asian/Asian British: Indian", "Asian/Asian British: Pakistani", "Black/Black British: African", "Black/Black British: Any other Black/ African / Caribbean background", "Black/Black British: Caribbean", "Mixed: Any other mixed background", "Mixed: White and Asian", "Mixed: White and Black African", "Mixed: White and Black Caribbean", "Prefer not to disclose", "White: Any other White background", "White: English/Welsh/Scottish/Northern Irish/British", "White: Irish", "White: Irish Traveller / Minkiers / Pavees (Previously known as Gypsy) 3"];
  it("never a heritage nobody stated (Irish for a German)", () => {
    const a = scanned("What best describes your ethnicity?", UK, "Los Angeles, CA");
    expect(a.COMPLETE_CANADIAN).toBe("White: Any other White background");
    expect(a.BERLIN_STAFF).toBe("White: Any other White background");
    expect(a.BOOTCAMP_CAREER_GAP).toBe("Black/Black British: Any other Black/ African / Caribbean background");
    expect(a.US_H1B_SENIOR).toBe("Asian/Asian British: Any other Asian background");
  });
});

describe("sponsorship lists read by when (SEP)", () => {
  const OPTS = ["I require sponsorship to work in the US at this time", "I do not and will not require sponsorship at any point in the future in order to work in the US", "I am in the process of obtaining permanent work authorization but the process is not complete", "I do not require sponsorship right now, but will at some point in the future"];
  it("an OPT holder needs it later, a citizen never", () => {
    const q = "Do you now or will you ever require sponsorship for a work visa?";
    expect(ask("US_OPT_ANALYST", q, OPTS)).toBe(OPTS[3]);
    expect(ask("BOOTCAMP_CAREER_GAP", q, OPTS)).toBe(OPTS[1]);
    expect(ask("US_H1B_SENIOR", q, OPTS)).toBe(OPTS[0]);
  });
});

describe("a school list takes the school, by its own words (Palantir, National Journal)", () => {
  const PAL = ["Central Washington University", "George Washington University - Main Campus", "Hanoi University of Science and Technology", "Mount Washington College", "Northeastern University", "University of Washington - Bothell", "University of Washington - Seattle", "University of Washington - Tacoma", "University of Waterloo", "Washington College", "Washington State University - Pullman", "Western Washington University", "Other (School Not Listed)"];
  it("'Which university … did you last attend?' never Washington College for the University of Washington", () => {
    const a = scanned('Which university are you currently attending or did you last attend? Please select "Other (School Not Listed)" if your school is not listed.', PAL, "New York, NY");
    expect(a.US_H1B_SENIOR).not.toBe("Washington College");
    expect(a.US_OPT_ANALYST).toBe("Northeastern University");
  });
  const NJ = ["McMaster University", "George Washington University", "Mount Washington College", "Northeastern University", "The Master's College", "University of Washington", "University of Washington - Bothell", "University of Waterloo", "Washington College", "Washington State University"];
  it("'At which institution did you earn your highest degree?' is a school, never 'The Master's College'", () => {
    const a = scanned("At which institution did you earn your highest degree?", NJ, "Washington DC");
    expect(a.US_H1B_SENIOR).toBe("University of Washington");
    expect(a.US_OPT_ANALYST).toBe("Northeastern University");
  });
});

describe("a follow-up for those who answered Yes (Immuta)", () => {
  it("'Optional: If you answered Yes or Unsure, can you provide further details…' is blank for a citizen", () => {
    const a = scanned("Optional: If you answered Yes or Unsure, can you provide further details about the sponsorship or work authorization support you may need in order to work for Immuta?", [], "Washington, DC", "textarea");
    expect(a.BOOTCAMP_CAREER_GAP).toBeNull();
  });
});

describe("a stated referral is a referral option, not a presentation (Match Group)", () => {
  it("'Employee Presentation' is no referral", () => {
    const OPTS = ["Award Annoucement", "LinkedIn - Job Posting", "LinkedIn - Company Post", "LinkedIn - Employee Post", "Glassdoor", "Indeed", "Google", "BuiltIn", "Tinder Tech Blog", "Women Impact Tech", "Spencer Rascoff, CEO", "Employee Presentation", "Other"];
    expect(ask("BERLIN_STAFF", "Where did you first hear about this job?", OPTS)).not.toBe("Employee Presentation");
  });
});

describe("a whole address asked whole (Crest, FiscalNote)", () => {
  it("'What is your street address? Please also include your city, state, and zip code.'", () => {
    const a = scanned("What is your street address? Please also include your city, state, and zip code.", [], "Hartford, CT");
    expect(a.US_H1B_SENIOR).toMatch(/^1200 Westlake Ave N, Unit 805, Seattle, WA 98109/);
  });
  it("'Please provide your complete current residential address:'", () => {
    const a = scanned("Please provide your complete current residential address:", [], "Washington, DC");
    expect(a.US_H1B_SENIOR).toMatch(/^1200 Westlake Ave N/);
  });
});

describe("on site at a place nobody named (Larian)", () => {
  it("someone who will not move cannot say Yes to an unknown studio", () => {
    expect(ask("US_H1B_SENIOR", "This position is onsite in the studio location listed in the posting. Are you willing to work onsite in that location?", ["Yes", "No"], null)).toBeNull();
  });
});

describe("the fixes' edges, from re-running every bank", () => {
  it("an awareness statement that also promises is the promise (Samsara)", () => {
    const q = "I understand that this role is in either Phoenix, AZ or Atlanta, GA and I am willing to participate in a hybrid work model.";
    expect(ask("US_H1B_SENIOR", q, ["Yes", "No"])).toBe("No");
  });
  it("able to be located somewhere is moving there for someone who would (RF Smart)", () => {
    const q = "Are you able to be located in Jacksonville, FL in summer 2026?";
    expect(ask("COMPLETE_CANADIAN", q, ["Yes", "No"])).toBe("Yes");
    expect(ask("US_H1B_SENIOR", q, ["Yes", "No"])).toBe("No");
  });
  it("a school listed per campus is listed: which campus stays theirs (Palantir)", () => {
    const opts = ["University of Washington - Bothell", "University of Washington - Seattle", "University of Washington - Tacoma", "Washington College", "Other - School Not Listed"];
    const a = scanned("Which university are you currently attending or did you last attend?", opts, "New York, NY");
    expect(a.US_H1B_SENIOR).toBeNull();
  });
  it("a nonvisible minority beside 'not a minority' is not decided for someone LGBTQ+ (Wattpad)", () => {
    const opts = ["Nonvisible minority", "Visible minority", "I don't identify as a minority (Visible or Nonvisible)", "Prefer not to say"];
    const a = scanned("Please select an identity that best represents you:", opts, "Toronto, Ontario");
    expect(a.BERLIN_STAFF).toBeNull();
    expect(a.COMPLETE_CANADIAN).toBe("I don't identify as a minority (Visible or Nonvisible)");
    expect(a.US_OPT_ANALYST).toBe("Visible minority");
  });
  it("sponsorship details for someone who needs it, nothing for a citizen (Immuta)", () => {
    const a = scanned("Optional: If you answered Yes or Unsure, can you provide further details about the sponsorship or work authorization support you may need in order to work for Immuta?", [], "Washington, DC", "textarea");
    expect(a.US_H1B_SENIOR).toBe("H-1B visa (transfer required)");
    expect(a.BOOTCAMP_CAREER_GAP).toBeNull();
  });
  it("an address abroad carries its country", () => {
    const a = scanned("Please provide your complete current residential address:", [], "Washington, DC");
    expect(a.BERLIN_STAFF).toMatch(/, Germany$/);
  });
  it("terms that mention requesting an accommodation ask nothing about one (National Journal)", () => {
    const q = "Terms and Conditions: I understand it is this company's policy not to refuse to consider or hire a qualified individual with a disability because of that person's need for a reasonable accommodation as required by the ADA. I agree to request a reasonable accommodation for the interview process if one is necessary.";
    const r = resolveQuestion({ label: q, controlType: "select", options: ["Yes"], category: "unknown", kind: "choice" }, profileFacts(persona("BOOTCAMP_CAREER_GAP"), TEST_TODAY), persona("BOOTCAMP_CAREER_GAP"), { jobCountry: "US", company: "Acme" });
    expect(r?.rule).not.toBe("default:accommodation-theirs");
  });
});

describe("Lever's long questions keep their label (eqbank, Kobie, live 2026-10-08)", () => {
  // <div class="application-label"><div class="text">Question?</div><p
  // class="description">…</p></div>: the question and its definition ran past
  // 300 characters, and so did Kobie's list of 30 states with no definition.
  // Both were labelled with the input's name, and no rule saw them.
  const RADIOS = (name: string, opts: string[]) =>
    `<div class="application-field full-width required-field"><ul data-qa="multiple-choice">${opts.map((o) => `<li><label><input type="radio" name="${name}" value="${esc(o)}" required="required" /><span class="application-answer-alternative">${esc(o)}</span></label></li>`).join("")}</ul></div>`;
  const RACIALIZED = `<li class="application-question"><div class="application-label full-width multiple-choice"><div class="text">Do you self-identify as a racialized person?</div><p class="description">Racialized persons - For the purposes of employment equity, members of such groups in Canada are persons other than Indigenous or Aboriginal People, who are non-Caucasian in race or non-white in colour, regardless of place of birth or citizenship. This includes (but is not limited to): • Black (including African, Caribbean, North American etc.) • Latin American/Hispanic (including Indigenous persons from Central and South America) • East Asian (e.g., Chinese, Japanese, Korean) • South Asian (e.g., Indian, Pakistani, Sri Lankan, etc.) • Southeast Asian (e.g.Filipino, Laotian, Vietnamese, etc.) • Middle Eastern (e.g., Syrian, Afghani, Iranian, etc.) • Persons of Mixed Origin</p></div>${RADIOS("surveysResponses[54d5][responses][field3]", ["Yes", "No", "Prefer not to say"])}</li>`;
  const STATES_Q = "Kobie operates in the following states. Are you currently located in one of these states? Colorado, Connecticut, District of Columbia, Florida, Georgia, Illinois, Indiana, Louisiana, Maryland, Massachusetts, Michigan, Minnesota, Mississippi, Missouri, Nevada, New Jersey, New Mexico, New York, North Carolina, Ohio, Oklahoma, Oregon, Pennsylvania, Rhode Island, South Carolina, Tennessee, Texas, Vermont, Virginia, Wisconsin";
  const STATES = `<li class="application-question custom-question"><div><div class="application-label full-width multiple-choice"><div class="text">${STATES_Q}<span class="required">\u2731</span></div></div>${RADIOS("cards[6617][field10]", ["Yes, I'm currently authorized to work and located in one of Kobie's operational states", "No, I'm located outside Kobie's operational states"])}</div></li>`;
  const page = (inner: string) => `<form id="application-form"><div class="section application-form"><ul>
    <li class="application-question"><label><div class="application-label">Full name<span class="required">\u2731</span></div><div class="application-field"><input type="text" name="name"></div></label></li>
    <li class="application-question"><label><div class="application-label">Email<span class="required">\u2731</span></div><div class="application-field"><input type="email" name="email"></div></label></li>
    ${inner}</ul></div></form>`;

  it("a question with its description is labelled by the question", () => {
    document.body.innerHTML = page(RACIALIZED);
    const { fields } = scanPage(persona("US_H1B_SENIOR"), true, null);
    const f = fields.find((x) => x.controlType === "radioGroup");
    expect(f?.label).toBe("Do you self-identify as a racialized person?");
    expect(f?.proposedValue).toBe("Yes");
  });
  it("a question a paragraph long is labelled whole", () => {
    document.body.innerHTML = page(STATES);
    const { fields } = scanPage(persona("BOOTCAMP_CAREER_GAP"), true, null);
    const f = fields.find((x) => x.controlType === "radioGroup");
    expect(f?.label.startsWith("Kobie operates in the following states.")).toBe(true);
    expect(f?.proposedValue).toBe("Yes, I'm currently authorized to work and located in one of Kobie's operational states");
  });
});

describe("the company named by the header logo (Palantir on Lever, live 2026-10-08)", () => {
  // Lever pages carry no og:site_name, no JSON-LD and no "at <Company>"
  // title; the company is the logo's alt. Unknown, "Palantir Website" was
  // not the company's own site, and "Other" was chosen for a stated
  // "Company website".
  it("reads 'Palantir Technologies' from the logo's alt", async () => {
    const { extractJobIdentity } = await import("../src/content/jobContext");
    document.title = "Palantir Technologies - Data Engineer - Talent Acquisition Operations";
    document.body.innerHTML = `<div class="main-header page-full-width section-wrapper"><div class="main-header-content page-centered narrow-section page-full-width"><a class="main-header-logo" href="https://jobs.lever.co/palantir"><img alt="Palantir Technologies logo" src="https://example.com/logo.png"></a></div></div><div class="posting-headline"><h2>Data Engineer</h2></div>`;
    expect(extractJobIdentity(document).company).toBe("Palantir Technologies");
  });
});

describe("live-only gaps on the Lever pages (2026-10-08)", () => {
  it("'From which job site did you see this posting?' is the how-did-you-hear question (Data Lab)", () => {
    const opts = ["Indeed", "LinkedIN", "University", "Glassdoor", "Zip Recruiter", "Referral", "Search Engine", "Other"];
    expect(ask("COMPLETE_CANADIAN", "From which job site did you see this posting?", opts)).toBe("LinkedIN");
    expect(ask("BERLIN_STAFF", "From which job site did you see this posting?", opts)).toBe("Referral");
  });
  it("a headquarters is an office to move to (SEP)", () => {
    const q = "SEP headquarters is in Westfield, IN. We believe we do our best work together - so, this is not a remote role. Are you able to work from our Westfield, IN headquarters 5 days a week?";
    expect(ask("US_OPT_ANALYST", q, ["Yes", "No"], "US", "Westfield")).toBe("Yes");
    expect(ask("US_H1B_SENIOR", q, ["Yes", "No"], "US", "Westfield")).toBe("No");
  });
  it("'Are you a previous employee of any of the Crest family of companies?' is No for no history there", () => {
    expect(ask("US_H1B_SENIOR", "Are you a previous employee of any of the Crest family of companies?", ["Yes", "No"])).toBe("No");
  });
  it("a pledge about who completed the application is never answered, nor sent to the AI (Kobie)", () => {
    const q = "We include this question to ensure each application is completed by a person. Please select \"I confirm I am completing this application myself\"";
    const opts = ["I used an AI tool or automated service to complete this application", "I confirm I am completing this application myself", "I had someone else complete this application"];
    const r = resolveQuestion({ label: q, controlType: "select", options: opts, category: "unknown", kind: "choice" }, profileFacts(persona("BOOTCAMP_CAREER_GAP"), TEST_TODAY), persona("BOOTCAMP_CAREER_GAP"), { jobCountry: "US", company: "Kobie" });
    expect(r?.status).toBe("abstain");
    expect(r && "blockBackend" in r ? r.blockBackend : false).toBe(true);
  });
  it("Artera's four relocation options: on site where one lives, moving, or neither", () => {
    const q = "Are you currently based in, or willing to relocate to, Santa Barbara, Kansas City, or the Seattle metropolitan area for an onsite role?";
    const opts = ["I currently live in one of those cities AND I am willing to work on-site.", "I currently live in one of those cities, but I am only open to remote opportunities.", "I am willing to relocate to one of those cities AND I am willing to work on-site", "I am not located in one of those cities and I am not willing to relocate"];
    expect(ask("US_H1B_SENIOR", q, opts)).toBe(opts[0]);
    expect(ask("BERLIN_STAFF", q, opts)).toBe(opts[2]);
    expect(ask("BOOTCAMP_CAREER_GAP", q, opts)).toBe(opts[3]);
  });
  it("an attention check by its place: 'select the SECOND option below' (Kobie)", () => {
    expect(ask("COMPLETE_CANADIAN", "To confirm you carefully read instructions, please select the SECOND option below", ["OPTION 1", "OPTION 2", "OPTION 3", "OPTION 4"])).toBe("OPTION 2");
  });
});

describe("a referral by an employee of a family of companies is still a referral (Crest)", () => {
  it("'Were you referred by a current employee of the Crest Family of Companies?' (stated Referral)", () => {
    expect(ask("BERLIN_STAFF", "Were you referred by a current employee of the Crest Family of Companies?", ["Yes", "No"])).toBe("Yes");
    expect(ask("US_H1B_SENIOR", "Were you referred by a current employee of the Crest Family of Companies?", ["Yes", "No"])).toBe("No");
  });
});

describe("Lever bank blanks a stated fact answers", () => {
  it("'I am not a U.S. veteran' is the never-served option (Gopuff)", () => {
    const a = scanned("Are you a veteran?", ["I identify as a U.S. veteran", "I am not a U.S. veteran", "Prefer not to answer"], "New York, NY");
    expect(a.COMPLETE_CANADIAN).toBe("I am not a U.S. veteran");
    expect(a.US_H1B_SENIOR).toBe("I am not a U.S. veteran");
  });
  it("'Caucasian' is White (AltaML)", () => {
    const a = scanned("I identify my ethnicity as", ["Caucasian", "Hispanic, Latino, or Spanish origin", "African American", "Asian", "Indigenous Peoples, First Nations, Inuit, Metis", "Middle Eastern or North African", "Prefer not to disclose"], "Toronto, ON", "checks");
    expect(a.COMPLETE_CANADIAN).toBe("Caucasian");
  });
  it("gender options qualified by cis / trans take both stated facts (AltaML)", () => {
    const opts = ["Cisgender Man", "Cisgender Woman", "Transgender Man", "Transgender Woman", "Non-Binary", "Two-Spirit"];
    const a = scanned("What gender do you identify as?", opts, "Toronto, ON");
    expect(a.COMPLETE_CANADIAN).toBe("Cisgender Woman");
    expect(a.BERLIN_STAFF).toBe("Transgender Man");
    expect(a.US_OPT_ANALYST).toBe("Cisgender Man");
  });
  it("'Legal Full Name (First Name, Middle Name & Last Name)' is the full name (ERG)", () => {
    const a = scanned("Legal Full Name (First Name, Middle Name & Last Name)", [], "Durham, NC");
    expect(a.COMPLETE_CANADIAN).toBe("Maya Tremblay");
  });
  it("'Do you currently work for Gopuff?' is No for someone who works elsewhere", () => {
    expect(ask("COMPLETE_CANADIAN", "Do you currently work for Gopuff?", ["Yes", "No"])).toBe("No");
  });
  it("a bulleted list of U.S.-person statuses over Yes / No (Woven by Toyota)", () => {
    const q = "Are you one of the following: \u2022 U.S. Citizen or National. \u2022 U.S. Lawful Permanent Resident. \u2022 Person granted Refugee status in the United States. \u2022 Person granted Asylum in the United States.";
    expect(ask("BOOTCAMP_CAREER_GAP", q, ["Yes", "No"])).toBe("Yes");
    // The allow-list layer (D-61 amendment): this persona's answer holds words outside the closed vocabulary, so its status is unknown and the device abstains.
    expect(ask("US_H1B_SENIOR", q, ["Yes", "No"])).toBe(null);
    expect(ask("COMPLETE_CANADIAN", q, ["Yes", "No"])).toBe("No");
  });
  it("a U.S.-person list as options (Shield AI)", () => {
    const q = "This position may require access to information and technology that is subject to export controls, such as EAR. Are you any of the following?";
    const opts = ["A United States Citizen", "A lawful permanent resident of the United States (i.e. Greencard holder)", "A protected individual", "Other"];
    expect(ask("BOOTCAMP_CAREER_GAP", q, opts)).toBe("A United States Citizen");
    expect(ask("US_H1B_SENIOR", q, opts)).toBe("Other");
  });
});

describe("more of what the Lever and Ashby pages showed", () => {
  it("periodic travel is no office requirement for someone who prefers remote work (Kobie)", () => {
    const q = "As a remote-first company, Kobie believes flexibility and meaningful in-person connection can go hand in hand. While travel requirements vary by role, employees may periodically travel for team collaboration, customer meetings, training, and company events, including visits to our headquarters in St. Petersburg, Florida. Are you able and willing to travel as needed to support these activities?";
    expect(ask("BOOTCAMP_CAREER_GAP", q, ["Yes", "No"], null)).toBe("Yes");
  });
  it("a list of races is the race question whatever its label says (Wattpad)", () => {
    const opts = ["Black", "Indigenous/First Nations/Metis", "Hispanic or Latine", "White", "Pacific Islander", "East Asian", "South Asian", "Southeast Asian", "Middle Eastern", "Prefer not to say"];
    const a = scanned("Please select an identity (or multiple) that best represents you:", opts, "Toronto, Ontario", "checks");
    expect(a.COMPLETE_CANADIAN).toBe("White");
    expect(a.BOOTCAMP_CAREER_GAP).toBe("Black");
    expect(a.US_H1B_SENIOR).toBeNull();
  });
  it("consent to being recorded, named in the options only, is the applicant's and never the AI's (TensorWave)", () => {
    const opts = ["I agree and consent to the recording and AI summarization of my interview.", "I would like to opt out of interview recording and AI summarization."];
    const r = resolveQuestion({ label: "Candidate Consent Acknowledgment", controlType: "radioGroup", options: opts, category: "unknown", kind: "choice" }, profileFacts(persona("BOOTCAMP_CAREER_GAP"), TEST_TODAY), persona("BOOTCAMP_CAREER_GAP"), { jobCountry: "US", company: "TensorWave" });
    expect(r?.status).toBe("abstain");
    expect(r && "blockBackend" in r ? r.blockBackend : false).toBe(true);
  });
});

describe("the bank re-run's three slips (2026-10-08)", () => {
  it("a race follow-up asks only those who chose 'Two or more races' (SoFi)", () => {
    const opts = ["American Indian or Alaska Native", "Asian", "Black or African American", "Native Hawaiian or Other Pacific Islander", "Hispanic or LatinX", "White"];
    const a = scanned("If you selected \"Two or more races\", please check all racial categories with which you identify with.", opts, "San Francisco, CA", "checks");
    for (const who of ALL) expect(a[who]).toBeNull();
  });
  it("a trans man's gender is the plain 'Male' when it is offered (Tanium, Hootsuite)", () => {
    const a = scanned("Gender: What gender do you most closely identify with?", ["Female", "Male", "Non-binary or third gender", "Transgender Female", "Transgender Male", "Not Listed / Other", "Prefer not to answer"], "Seattle, WA");
    expect(a.BERLIN_STAFF).toBe("Male");
  });
  it("working for a partner or reseller is not working for the company (Geotab)", () => {
    expect(ask("COMPLETE_CANADIAN", "Do you currently work for a partner or reseller of Geotab?", ["Yes", "No"], "CA")).toBeNull();
  });
});
