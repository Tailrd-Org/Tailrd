/**
 * LIVE real application pages, taken from jobs in the Tailrd app (prod
 * scraped_jobs, listing_status=active, 2026-10-03). The real ATS JavaScript
 * runs; only reads leave the browser (harness.mjs blocks every submit, upload,
 * mutation, autosave and beacon).
 *
 * Profile: SPARSE_CANADIAN, shaped like real production profiles (one location
 * string, free-text work authorization "Canadian citizen", no stated years,
 * no stated current company, a co-op in progress). Expectations encode what a
 * CORRECT deterministic filler does with it:
 *
 *   - fill what the profile states or implies with high confidence
 *     (Kinaxis is the current employer: its row ends "Present"; Toronto/ON/
 *     Canada come from the location string; a Canadian citizen IS entitled to
 *     work in Canada; Toronto is NOT in the United States)
 *   - leave everything else untouched: preferences nobody stated, US work
 *     authorization for a Canadian citizen (unknown, not "no"), salary, EEO
 *     (this profile has none), essays.
 *
 * A field missing from `expect` must stay unchanged: any write there is
 * reported as UNEXPECTED, the wrong-kind / invented-answer failure.
 */
import { SPARSE_CANADIAN } from "../profiles.mjs";

const P = SPARSE_CANADIAN;
const NAME = "Maya Tremblay";
const PHONE = { re: "416\\D*555\\D*0142" };
const TORONTO = { re: "toronto" };
const LINKEDIN = P.linkedin;
// Greenhouse's phone dial-code picker: "Canada" selects "Canada +1", and the
// widget then displays only "+1".
const DIAL_CODE_CANADA = { re: String.raw`canada|^\+\s*1$` };

const live = (c) => ({ mode: "live", profile: P, trigger: { fillTimeoutMs: 180000 }, ...c });

const NEXTHOP_EXPECT = {
  "#nickname_hpcsaf": null,
  "#firstName": "Maya",
  "#lastName": "Tremblay",
  "#email": P.email,
  "#phone": PHONE,
  "label:Address": null,
  "label:City": "Toronto",
  "label:Postal Code": null,
  "label:Date Available": null,
  "#websiteUrl": null,
  "#linkedinUrl": LINKEDIN,
  "#educationInstitutionName": "University of Waterloo",
  // BambooHR fills this itself with the posting URL; we must not touch it.
  "label:Link to This Job": { re: "bamboohr\.com|^$" },
};

/**
 * The user clicks "Apply for This Job" (BambooHR then holds its form hidden for
 * 2.5-5 s, live 2026-10-03). Clicked programmatically: at 1366x900 the auto-
 * mounted panel covers BambooHR's right column, Apply button included, so a
 * pointer click lands on the panel (NOTES.md, needs-you).
 */
async function userOpensBambooForm(page) {
  const apply = page.locator('button:has-text("Apply for This Job"), a:has-text("Apply for This Job")').first();
  await apply.waitFor({ state: "attached", timeout: 15000 });
  await apply.evaluate((el) => el.click());
}

export default [
  // ---------------------------------------------------------------- Greenhouse
  // Retired 2026-10-08 (Greenhouse's job API answered 404), restored 2026-10-17:
  // the posting is open again. real-live-embedded.mjs frames the same posting.
  live({
    id: "live-gh-oneimaging",
    ats: "greenhouse",
    url: "https://job-boards.greenhouse.io/oneimaging/jobs/4403125009",
    expect: {
      "#first_name": "Maya",
      "#last_name": "Tremblay",
      "#preferred_name": { oneOf: ["Maya", ""] },
      "#email": P.email,
      // The phone dial-code picker (inside fieldset.phone-input): "Canada"
      // selects "Canada +1", and the widget then displays only "+1".
      "#country": DIAL_CODE_CANADA,
      "#phone": PHONE,
      "label:University of": { re: "^no" },
      "label:Miami HQ": null,
      "label:career fair": null,
      "label:LinkedIn Profile": LINKEDIN,
      "label:Desired Salary": null,
      "label:authorized to work in the United States": null,
      "label:require sponsorship": null,
      // The page renamed its declines on 2026-10-03 ("Decline to self-identify",
      // veteran status included): either wording is the decline.
      "#gender": { oneOf: ["Decline To Self Identify", "Decline to self-identify"] },
      "#hispanic_ethnicity": { oneOf: ["Decline To Self Identify", "Decline to self-identify"] },
      "#veteran_status": { oneOf: ["I don't wish to answer", "Decline to self-identify"] },
    },
  }),
  live({
    id: "live-gh-garner",
    ats: "greenhouse",
    url: "https://job-boards.greenhouse.io/garnerhealth/jobs/6174213004",
    expect: {
      "#first_name": "Maya",
      "#last_name": "Tremblay",
      "#preferred_name": { oneOf: ["Maya", ""] },
      "#email": P.email,
      // The phone dial-code picker (inside fieldset.phone-input): "Canada"
      // selects "Canada +1", and the widget then displays only "+1".
      "#country": DIAL_CODE_CANADA,
      "#phone": PHONE,
      "#candidate-location": TORONTO,
      "#school--0": { re: "waterloo" },
      "#degree--0": { re: "bachelor" },
      "label:expected graduation date": { re: "2027|^$" },
      "label:LinkedIn Profile": LINKEDIN,
      "label:If 'Other' selected": null,
      "label:desired salary": null,
      "label:legally authorized to work": null,
      "label:require sponsorship": null,
      "label:relocate to the NYC": null,
      "label:Which state do you currently reside": { re: "^$|outside|other|not|canada|international|n/?a" },
      "label:gender identity": "I don't wish to answer",
      "label:racial/ethnic": "I don't wish to answer",
      "label:sexual orientation": "I don't wish to answer",
      "label:transgender": "I don't wish to answer",
      "label:disability": "I don't wish to answer",
      "label:veteran or active": "I don't wish to answer",
    },
  }),
  live({
    id: "live-gh-bertram",
    ats: "greenhouse",
    url: "https://job-boards.greenhouse.io/bertramcapitalmanagement/jobs/8789855002",
    expect: {
      "#first_name": "Maya",
      "#last_name": "Tremblay",
      "#email": P.email,
      // The phone dial-code picker (inside fieldset.phone-input): "Canada"
      // selects "Canada +1", and the widget then displays only "+1".
      "#country": DIAL_CODE_CANADA,
      "#phone": PHONE,
      "#candidate-location": TORONTO,
      "label:LinkedIn Profile": LINKEDIN,
      "label:require employer sponsorship": null,
      "label:undergraduate degree": { oneOf: ["University of Waterloo", ""] },
      "label:standardized tests": null,
      "#gender": "Decline To Self Identify",
      "#hispanic_ethnicity": "Decline To Self Identify",
      "#veteran_status": "I don't wish to answer",
    },
  }),
  // --------------------------------------------------------------------- Lever
  live({
    id: "live-lever-rover",
    ats: "lever",
    url: "https://jobs.lever.co/rover/303b3b2f-9679-4a77-8908-456e29240486/apply",
    expect: {
      "name=name": NAME,
      "name=email": P.email,
      "name=phone": PHONE,
      "#location-input": TORONTO,
      "name=org": "Kinaxis",
      "name=urls[LinkedIn]": LINKEDIN,
      "name=urls[Twitter]": null,
      "name=urls[GitHub]": null,
      "name=urls[Portfolio]": null,
      "name=urls[Other]": null,
      "label:require sponsorship in the future": null,
      "label:live in or near Seattle": null,
      "label:good fit for this role": null,
      "label:years of professional software development": "1-3 years",
      "name=eeo[gender]": "Decline to self-identify",
      "name=eeo[race]": "Decline to self-identify",
      "name=eeo[veteran]": "Decline to self-identify",
      "name=cards[fb5c103c-4111-4530-94b9-07965c4a027e][field3]": "No",
    },
  }),
  live({
    id: "live-lever-eqbank",
    ats: "lever",
    url: "https://jobs.lever.co/eqbank/eefb3fa3-a55b-4bfd-b1b2-5419f35c7703/apply",
    expect: {
      // "…a racialized person?" and "…a person with a disability?", labelled
      // since round 5 (a question with its description): no race or disability
      // stated, so the page's decline, as for Gender Identity.
      "radio=surveysResponses[54d5f494-8f40-45ec-a0d4-736d5c1eac87][responses][field2]": "Prefer not to say",
      "radio=surveysResponses[54d5f494-8f40-45ec-a0d4-736d5c1eac87][responses][field3]": "Prefer not to say",
      "name=name": NAME,
      "name=email": P.email,
      "name=phone": PHONE,
      "#location-input": TORONTO,
      "name=org": "Kinaxis",
      "label:background check": null,
      "label:school you are currently attending": "University of Waterloo",
      "label:legally entitled to work in Canada": "Yes",
      "label:year of studies": null,
      "label:Gender Identity": "Prefer not to say",
      "label:Indigenous": null,
    },
  }),
  live({
    id: "live-lever-kepler",
    ats: "lever",
    url: "https://jobs.lever.co/kepler/2ad02ce3-1d56-4aee-9f1d-5199c780c0c1/apply",
    expect: {
      "name=name": NAME,
      "name=email": P.email,
      "name=phone": PHONE,
      "#location-input": TORONTO,
      "name=org": "Kinaxis",
      "name=urls[LinkedIn]": LINKEDIN,
      "name=urls[Twitter]": null,
      "name=urls[GitHub]": null,
      "name=urls[Portfolio]": null,
      "name=urls[Other]": null,
      "name=urls[Website/Blog]": null,
      "label:enrolled as a student": null,
      "label:returning to your studies": null,
      "label:legally able to work in Canada": "Yes",
      "label:Toronto Office": "Yes",
      "label:What year of study": null,
      "label:final year of study": null,
      // select2: the hidden native select is filled, labelled by its question.
      "select~Post-Secondary institution": "University of Waterloo",
      // The picker's display box, which the dumper reads since round 3: present
      // once a school is chosen, and showing that school.
      "label=University of Waterloo": { ifPresent: "University of Waterloo" },
      "label:availability for a 4-month": null,
      "label:age range": null,
      "label:ethnicity": "Prefer not to answer",
      "label:gender do you identify": "Prefer not to answer",
      "label:sexual orientation": "Prefer not to answer",
      "label:disability": "Prefer not to answer",
      "label:veteran": "Prefer not to answer",
    },
  }),
  // --------------------------------------------------------------------- Ashby
  live({
    id: "live-ashby-pangram",
    ats: "ashby",
    url: "https://jobs.ashbyhq.com/pangramlabs/0512183c-2373-499c-8459-91d0fd2d72f3/application",
    expect: {
      "#_systemfield_name": NAME,
      "#_systemfield_email": P.email,
      "label:authorized to work in the United States": null,
      "label:right use of AI tools": null,
      "label:Why Pangram": null,
      "label:Anything else": null,
    },
  }),
  live({
    id: "live-ashby-interplay",
    ats: "ashby",
    url: "https://jobs.ashbyhq.com/interplay/bdf67758-1f20-4a01-8bb3-ccebfa79e9ac/application",
    expect: {
      "#_systemfield_name": NAME,
      "#_systemfield_email": P.email,
      "label:LinkedIn Profile": LINKEDIN,
      "label:Where are you located": "North America",
      "label:Introvert": null,
      "label:Sensing": null,
      "label:Feeling": null,
      "label:Perceiving": null,
      "label:AI will take over": null,
      "label=Start typing...": "LinkedIn",
    },
  }),
  // ------------------------------------------------------------------ Workable
  live({
    id: "live-workable-mindex",
    ats: "workable",
    url: "https://apply.workable.com/mindex/j/84B10DB922/apply/",
    expect: {
      // Round 3: the row holds her running co-op (Kinaxis): "I currently work here".
      "name=current": "checked",
      "#firstname": "Maya",
      "#lastname": "Tremblay",
      "#email": P.email,
      "#headline": null,
      "#input_phone": PHONE,
      // Workable guesses Address from the visitor's IP; the guess is replaced
      // with the profile's location (round 5).
      "#address": "Toronto, ON, Canada",
      "#cover_letter": null,
      "label:Salary Range": null,
      "radio=QA_12563770": null,
      "radio=QA_12563778": "YES",
      "radio=QA_12457572": null,
      "radio=QA_12457573": "NO",
      "label:Do you live in the United States": "NO",
      "radio=QA_12457574": null,
      "label:referred to this job": "No",
      "label:Where did you see this job": "Online job board",
      // Workable's optional Education / Experience sections, when rendered.
      "#school": { ifPresent: "University of Waterloo" },
      "#field_of_study": { ifPresent: "Mechatronics Engineering" },
      "#degree": { ifPresent: { re: "bachelor" } },
      "#title": { ifPresent: "Software Engineer Co-op" },
      "#company": { ifPresent: "Kinaxis" },
      "ariaradio=Are you able to work onsite at Mindex’s Rochester office at ": "YES",
    },
  }),
  live({
    id: "live-workable-financeit",
    ats: "workable",
    url: "https://apply.workable.com/financeit/j/D3CF97088A/apply/",
    expect: {
      "#firstname": "Maya",
      "#lastname": "Tremblay",
      "#email": P.email,
      "#input_phone": PHONE,
      // Workable guesses Address from the visitor's IP; the guess is replaced
      // with the profile's location (round 5).
      "#address": "Toronto, ON, Canada",
      "#summary": null,
      "#cover_letter": null,
      "label:salary expectations": null,
      "radio=QA_12558420": "YES",
      "radio=QA_12558421": "YES",
      "ariaradio=Are you eligible to work in Canada without sponsorship?": "YES",
      "ariaradio=Are you comfortable commuting to our office 2-3x a week in D": "YES",
    },
  }),
  // ----------------------------------------------------------- SmartRecruiters
  live({
    id: "live-sr-servicenow",
    ats: "smartrecruiters",
    url: "https://jobs.smartrecruiters.com/oneclick-ui/company/ServiceNow/publication/c6a55189-e797-4e24-93c8-7bd5cef0e2ef?dcr_ci=ServiceNow",
    expect: {
      "label:First name": "Maya",
      "label:Last name": "Tremblay",
      "label:Email*": P.email,
      "label:Confirm your email": P.email,
      "label:City": TORONTO,
      // The phone dial-code picker (a slotted web-component button).
      "label:Country code": DIAL_CODE_CANADA,
      "label:Phone number": PHONE,
      "label:LinkedIn": LINKEDIN,
      "label:Facebook": null,
      "label:X (fka Twitter)": null,
      "label:Website": null,
      "label:Let the company know": null,
    },
  }),
  // live-sr-bosch retired 2026-10-08. The posting is still open, but from this
  // machine SmartRecruiters puts a bot challenge (it needs a POST, which live
  // runs block) in front of every page after the first for at least several
  // minutes: in round 4's two regressions ServiceNow and Bosch each passed once
  // and failed once, whichever came second. One SmartRecruiters page stays.
  // ------------------------------------------------------------------ BambooHR
  // The posting cases start on the job page and let the extension open the form
  // (a harness click on top of the flow's own toggled the form shut again);
  // -user-opened clicks "Apply" itself and presses Autofill at once.
  live({
    id: "live-bamboo-nexthop",
    ats: "bamboohr",
    url: "https://nexthopai.bamboohr.com/careers/64",
    expect: NEXTHOP_EXPECT,
  }),
  live({
    // The user opens the form and presses Autofill a moment later, while
    // BambooHR still holds it hidden: the fill must wait for it, not click
    // "Apply" again (which toggles the form shut).
    id: "live-bamboo-nexthop-user-opened",
    ats: "bamboohr",
    url: "https://nexthopai.bamboohr.com/careers/64",
    beforeFill: userOpensBambooForm,
    settleMs: 0,
    trigger: { fillTimeoutMs: 180000, clickImmediately: true },
    expect: NEXTHOP_EXPECT,
  }),
  live({
    id: "live-bamboo-armstrong",
    ats: "bamboohr",
    url: "https://armstrongfluidtechnology.bamboohr.com/careers/1001",
    expect: {
      "#nickname_hpcsaf": null,
      "#firstName": "Maya",
      "#lastName": "Tremblay",
      "#email": P.email,
      "#phone": PHONE,
      "label:Address": null,
      "label:City": "Toronto",
      "label:Postal Code": null,
      "label:Date Available": null,
      "#desiredPay": null,
      "#websiteUrl": null,
      "#linkedinUrl": LINKEDIN,
      // BambooHR renders a native radio and an ARIA twin per question.
      "radio~legally entitled to work in Canada": "Yes",
      "label:background check": null,
      // BambooHR fills this itself with the posting URL; we must not touch it.
      "label:Link to This Job": { re: "bamboohr\.com|^$" },
      "radio=customQuestionAnswers.yes_no_2378": "Yes",
    },
  }),
  // ------------------------------------------------------------------- Jobvite
  live({
    id: "live-jobvite-actionet",
    ats: "jobvite",
    url: "https://jobs.jobvite.com/actionet/job/ofYQzfwm/apply",
    expect: {
      "label:First Name": "Maya",
      "label:Last Name": "Tremblay",
      "label:Address*": null,
      "label:Address2": null,
      "label:City": "Toronto",
      "label:State": { oneOf: ["Not Applicable", ""] },
      "label:Zip/Postal Code": null,
      "label:Email": P.email,
      "label:Phone": PHONE,
      "label:Desired Salary": null,
      "label:Gender": "Decline To Self Identify",
      "label:Race/Ethnicity": "Decline to Self Identify",
      "label:Disability Status": "I do not wish to answer",
      "label:Veteran Status": "Decline To Self Identify",
      "label:age 18 or older": "Yes",
      "label:Active Clearance": null,
      "label:Clearance Type": null,
      // A degree in progress: "Some College", never "Bachelors" (was blank).
      "label:highest education": "Some College",
      "label:certifications": null,
      "label:total number of years of relevant experience": { oneOf: ["1", ""] },
      "label:current or past employee of ActioNet": "Neither",
      "label:family member": "No",
      "label:How did you hear": "ActioNet Career Page",
      "label:If referred by": null,
      "label:Job Fair": null,
      "label:conflicts of interest": "No",
      "label:non-competes": "No",
      "label:current or former government employee?": "No",
      // "If you are a current or former government employee, …?": the condition
      // is false for this applicant, and the form says so in an option.
      "label:are you currently": "I am not a current or former government employee",
      "label:recused": "I am not a current or former government employee",
      "label:procurement official": "No",
    },
  }),
];
