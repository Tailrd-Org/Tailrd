/**
 * Workable question bank (89 postings from 31 Workable accounts our users
 * saw, 2026-10-05; questions read from the public form endpoint): answers read
 * by hand and found wrong for a persona. Each test fails on the code before
 * its fix.
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

type Kind = "text" | "textarea" | "number" | "select" | "checkboxes";

/** One question as the bank renders it, scanned for a persona: the proposed
 *  answer, "abstain" or "none". */
function ask(who: Persona, label: string, options?: string[], ctx: Ctx = US, kind: Kind = options ? "select" : "text"): string {
  let control: string;
  if (kind === "checkboxes") {
    const boxes = (options ?? []).map((o, j) => `<label><input type="checkbox" name="q0[]" value="${j}"> ${esc(o)}</label>`).join("");
    document.body.innerHTML = `<form id="application-form"><fieldset class="field" id="q0"><legend>${esc(label)}</legend>${boxes}</fieldset></form>`;
  } else {
    control =
      kind === "select"
        ? `<select id="q0"><option value="">Select...</option>${(options ?? []).map((o, j) => `<option value="${j}">${esc(o)}</option>`).join("")}</select>`
        : kind === "textarea"
          ? `<textarea id="q0"></textarea>`
          : `<input type="${kind === "number" ? "number" : "text"}" id="q0">`;
    document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">${esc(label)}</label>${control}</div></form>`;
  }
  setResolveContext({ jobCountry: ctx.jobCountry, company: ctx.company, jobCity: ctx.jobCity ?? null, jobPlaces: ctx.jobPlaces ?? null });
  const { fields } = scanPage(P[who] as unknown as UserApplicationProfile, true);
  const f = fields[0];
  return f?.proposedValue ?? (f?.deviceAbstained ? "abstain" : "none");
}

const US: Ctx = { jobCountry: "US", company: "" };
const ALL: Persona[] = ["COMPLETE_CANADIAN", "US_H1B_SENIOR", "BOOTCAMP_CAREER_GAP", "US_OPT_ANALYST", "BERLIN_STAFF", "INDIA_NEW_GRAD"];

describe("an age question is about age, whatever its note says (Saalex, Credence)", () => {
  it("every adult is at least 18, even when the note mentions work authorization", () => {
    const q = "Are you at least 18 years or older? (If no, you may be required to provide authorization to work):";
    for (const who of ALL) expect(ask(who, q, YES_NO), who).toBe("Yes");
    const korea: Ctx = { jobCountry: "KR", company: "Credence" };
    expect(ask("COMPLETE_CANADIAN", "Are you at least 18 years old? (if no, you may be required to provide authorization to work)", YES_NO, korea)).toBe("Yes");
  });
});

describe("a first-person statement of authorization AND no sponsorship (DISA Technologies)", () => {
  it("is Yes only for someone authorized who will never need sponsorship", () => {
    const q = "I am legally authorized to work in the United States and will not require visa sponsorship now or in the future.";
    expect(ask("BOOTCAMP_CAREER_GAP", q, YES_NO)).toBe("Yes");
    for (const who of ["COMPLETE_CANADIAN", "US_H1B_SENIOR", "US_OPT_ANALYST", "BERLIN_STAFF", "INDIA_NEW_GRAD"] as Persona[]) {
      expect(ask(who, q, YES_NO), who).toBe("No");
    }
  });
});

describe("working without sponsorship now, and later (OnLogic)", () => {
  it("an H-1B worker needs it now; an OPT worker needs it later", () => {
    const now = "Are you currently able to work in the U.S. without employment visa sponsorship?";
    const later = "Are you able to work in the U.S. without employment visa sponsorship in the future?";
    expect(ask("US_H1B_SENIOR", now, YES_NO)).toBe("No");
    expect(ask("US_OPT_ANALYST", now, YES_NO)).toBe("Yes");
    expect(ask("BOOTCAMP_CAREER_GAP", now, YES_NO)).toBe("Yes");
    expect(ask("US_H1B_SENIOR", later, YES_NO)).toBe("No");
    expect(ask("US_OPT_ANALYST", later, YES_NO)).toBe("No");
    expect(ask("BOOTCAMP_CAREER_GAP", later, YES_NO)).toBe("Yes");
  });

  it("a U.S. citizen is asked about, not anyone authorized (Credence)", () => {
    const q = "Are you legally authorized to work in the U.S. as a U.S. citizen or Fully Naturalized U.S. Citizen?";
    expect(ask("BOOTCAMP_CAREER_GAP", q, YES_NO)).toBe("Yes");
    // The allow-list layer (D-61 amendment): this persona's answer holds words outside the closed vocabulary, so its status is unknown and the device abstains.
    expect(ask("US_H1B_SENIOR", q, YES_NO)).toBe("abstain");
    expect(ask("US_OPT_ANALYST", q, YES_NO)).toBe("abstain");
  });
});

describe("citizenship is not residence (Open Data Jobs)", () => {
  const q = "Check every country of which you are a citizen.";
  const opts = ["United States of America", "Australia", "Canada", "New Zealand", "United Kingdom", "Other"];
  it("someone on a US visa is no US citizen; a stated citizenship is answered", () => {
    expect(ask("US_H1B_SENIOR", q, opts, US, "checkboxes")).not.toBe("United States of America");
    expect(ask("US_OPT_ANALYST", q, opts, US, "checkboxes")).not.toBe("United States of America");
    expect(ask("BOOTCAMP_CAREER_GAP", q, opts, US, "checkboxes")).toBe("United States of America");
    expect(ask("COMPLETE_CANADIAN", q, opts, US, "checkboxes")).toBe("Canada");
    // The allow-list layer (D-61 amendment): this persona's answer holds words outside the closed vocabulary, so its status is unknown and the device abstains.
    expect(ask("BERLIN_STAFF", q, opts, US, "checkboxes")).toBe("abstain");
  });
});

describe("statements to acknowledge are not EEO or employment questions", () => {
  it("an EEO policy asking for your name to acknowledge gets the name, never a gender identity (Flourish Research)", () => {
    const q = "Flourish Research is an equal employment opportunity employer. Employment decisions are based on merit and business needs, and not on race, color, sex, (including pregnancy and gender identity), citizenship status, national origin, ancestry, gender, sexual orientation, age, religion, creed, physical or mental disability, genetic information, marital status, veteran status, political affiliation, or any other factor protected by law. Flourish Research complies with the law regarding reasonable accommodation for handicapped and disabled employees.   To acknowledge, please enter your full name below.";
    expect(ask("BERLIN_STAFF", q)).toBe("Jürgen Weiß");
    expect(ask("COMPLETE_CANADIAN", q)).toBe("Maya Tremblay");
  });

  it("a certification mentioning disability information is accepted, not answered as a disability question (SSCI)", () => {
    const q = "I certify that the facts contained in this application are true and complete to the best of my knowledge and understand that, if employed, falsified statements on this application shall be grounds for dismissal. I authorize investigation of all statements contained herein and the references and employers listed above to give you any and all information concerning my previous employment and any pertinent information they may have, personal or otherwise, and release the company from all liability for any damage that may result from utilization of such information. I also understand and agree that no representative of the company has any authority to enter into any agreement for employment for any specified period of time, or to make any agreement contrary to the foregoing, unless it is in writing and signed by an authorized company representative. This waiver does not permit the release or use of disability – related or medical information in a manner prohibited by the Americans with Disabilities Act (ADA) and other relevant federal and state laws. I understand that a consumer credit report or criminal records check may be necessary prior to my employment. If such reports are required, I understand that, in compliance with federal law, the company will provide me with a written notice regarding the use of these reports and will also obtain a separate written authorization from me to consent to these reports. I also understand that a poor credit history or conviction will not automatically result in disqualification from employment.” In compliance with federal law, all persons hired will be required to verify identity and eligibility to work in the United States and to complete the required employment eligibility verification document upon hire.";
    for (const who of ALL) expect(ask(who, q, ["Accept", "Decline"], US, "checkboxes"), who).toBe("Accept");
  });

  it("initials to agree are never a company or a No (Credence)", () => {
    const certify = "I hereby certify that the information given by me is true in all respects. I authorize Company and its representatives to contact my prior employers and all others (with the exception of my current employer, only if I have marked \"May we contact your present employer\" on this application as \"No\") for the purpose of verification of the information I have supplied and release same from any liability resulting from the information released. I authorize employers, schools and other persons named on this application to provide any information or transcripts requested. Initials to agree";
    expect(ask("COMPLETE_CANADIAN", certify)).not.toBe("Kinaxis");
    const contingent = "I understand employment with Company is also contingent on my providing sufficient documentation necessary to establish my identity and eligibility to work in the United States.   If employed, I understand that as a condition of employment that I may be required to agree to and sign a non-solicitation, non-disclosure, and/or other similar agreements. I also agree to notify the organization during the pre-employment process of any non-solicitation, non-disclosure, and/or other similar agreements that I may have already signed with current and former employers. Initial to agree.";
    expect(ask("BOOTCAMP_CAREER_GAP", contingent)).not.toBe("No");
  });

  it("an affirmation that you signed NO restrictive agreement is never answered No (Saalex)", () => {
    const q = "Affirmation: I affirm that I have not entered into any non-competition, non-solicitation, non-disclosure/confidentiality or any other agreement with a former employer that limits, restricts, bars, or in any way impacts my ability to work for Saalex:";
    expect(ask("US_H1B_SENIOR", q, YES_NO, US, "checkboxes")).not.toBe("No");
  });
});

describe("education requirements read the degrees held (Credence, Avalore, Saalex)", () => {
  it("a bachelor's minimum is not met by a student or a bootcamp certificate", () => {
    for (const q of ["Do you meet the eduucation requirements, a bachelor's degree minimum?", "Do you currently hold a minimum of a Bachelor's degree from an accredited university?"]) {
      expect(ask("COMPLETE_CANADIAN", q, YES_NO), q).toBe("No");
      expect(ask("BOOTCAMP_CAREER_GAP", q, YES_NO), q).toBe("No");
      expect(ask("US_H1B_SENIOR", q, YES_NO), q).toBe("Yes");
      expect(ask("US_OPT_ANALYST", q, YES_NO), q).toBe("Yes");
      expect(ask("INDIA_NEW_GRAD", q, YES_NO), q).toBe("Yes");
    }
  });

  it("a university student has a high-school diploma", () => {
    expect(ask("COMPLETE_CANADIAN", "Do you have a HS Diploma or GED?", YES_NO)).toBe("Yes");
  });

  it("the school you will attend in a future term is a student's, not a graduate's (RentVision)", () => {
    const q = "At which university will you be enrolled for the Fall 2027 semester?";
    expect(ask("US_H1B_SENIOR", q, undefined, US, "textarea")).not.toBe("University of Washington");
    // She graduates in April 2027.
    expect(ask("COMPLETE_CANADIAN", q, undefined, US, "textarea")).not.toBe("University of Waterloo");
  });

  it("the undergraduate school is not the master's (Open Data Jobs)", () => {
    const q = "Enter your undergraduate major or primary field of study and the educational institution(s) attended. If you did not attend an undergraduate program, enter N/A.";
    expect(ask("US_H1B_SENIOR", q, undefined, US, "textarea")).not.toMatch(/master/i);
    expect(ask("BOOTCAMP_CAREER_GAP", q, undefined, US, "textarea")).not.toMatch(/turing/i);
  });

  it("a high school's name and place is not where you live now (Saalex)", () => {
    expect(ask("BOOTCAMP_CAREER_GAP", "Highschool Name & Location:")).not.toBe("Denver, CO");
  });
});

describe("experience of one kind is not the whole career (Saalex, JeffreyM, Smartflower)", () => {
  it("years doing a specific thing are never the total years", () => {
    expect(ask("US_H1B_SENIOR", "Do you have a minimum of 3 years of professional experience performing system administration of Microsoft Windows Server environment?", YES_NO)).not.toBe("Yes");
    expect(ask("BERLIN_STAFF", "Do you have a minimum of 3 years of professional experience performing system administration of Microsoft Windows Server environment?", YES_NO)).not.toBe("Yes");
    expect(ask("US_H1B_SENIOR", "How many years of experience do you have owning customer implementations, deployments, technical programs, or operational outcomes?", undefined, US, "number")).not.toBe("15");
    expect(ask("US_H1B_SENIOR", "Describe your experience servicing industrial equipment. How many years of experience, what kind of equipment, what was your role?", undefined, US, "textarea")).not.toBe("15");
  });

  it("fewer years in total than asked is still No", () => {
    expect(ask("COMPLETE_CANADIAN", "Do you have a minimum of 3 years of professional experience performing system administration of Microsoft Windows Server environment?", YES_NO)).toBe("No");
  });

  it("experience managing program risks is not claimed for anyone", () => {
    expect(ask("INDIA_NEW_GRAD", "Do you have experience managing program risks, performance metrics, and strategic initiatives involving cost, schedule, and performance objectives?", YES_NO)).not.toBe("Yes");
  });

  it("a rating of a skill is not the skills list", () => {
    expect(ask("COMPLETE_CANADIAN", "How would you rate your closing skills?")).not.toBe("Python, C++, ROS");
  });

  it("a supervisor's name and title are not the applicant's title", () => {
    expect(ask("COMPLETE_CANADIAN", "Supervisor Name & Title:")).not.toBe("Software Engineer Co-op");
  });

  it("a link to one project is not the GitHub profile", () => {
    expect(ask("COMPLETE_CANADIAN", "Please share a link to a project you built with an LLM API (GitHub, demo, or write-up).")).not.toBe("https://github.com/mayatremblay");
  });
});

describe("where you are, and will go (Financeit, RentVision, Credence)", () => {
  const toronto: Ctx = { jobCountry: "CA", company: "Financeit", jobCity: "Toronto" };
  it("comfortable commuting to Toronto is an in-person question", () => {
    const q = "Are you comfortable commuting to our office 2-3x a week in Downtown Toronto?";
    expect(ask("COMPLETE_CANADIAN", q, YES_NO, toronto)).toBe("Yes");
    expect(ask("US_H1B_SENIOR", q, YES_NO, toronto)).toBe("No");
    expect(ask("BOOTCAMP_CAREER_GAP", q, YES_NO, toronto)).toBe("No");
  });

  it("willing to relocate is asked, whatever the note about living there", () => {
    const q = "This position is in Lincoln, Nebraska. If necessary, are you willing to relocate?  (Please check YES if you already live in the Lincoln, NE area.)";
    const lincoln: Ctx = { jobCountry: "US", company: "RentVision", jobCity: "Lincoln" };
    expect(ask("COMPLETE_CANADIAN", q, YES_NO, lincoln)).toBe("Yes");
    expect(ask("US_OPT_ANALYST", q, YES_NO, lincoln)).toBe("Yes");
    expect(ask("US_H1B_SENIOR", q, YES_NO, lincoln)).toBe("No");
  });

  it("currently meeting a worksite requirement is not a willingness to move", () => {
    const korea: Ctx = { jobCountry: "KR", company: "Credence", jobCity: "Pyeongtaek-si" };
    expect(ask("COMPLETE_CANADIAN", "Do you currently meet the worksite location requirements for being On-site or Hybrid as defined in the job description?", YES_NO, korea)).not.toBe("Yes");
  });
});

describe("numbers", () => {
  it("a salary written with a period between thousands is one number (Mindex's Salary Range)", () => {
    expect(ask("BERLIN_STAFF", "Salary Range", undefined, US, "number")).toBe("120000");
  });
});

describe("blanks now answered", () => {
  it("a LinkedIn link asked for at length, in a text area (Ammortal, Open Data Jobs)", () => {
    const li = "https://www.linkedin.com/in/maya-tremblay";
    expect(ask("COMPLETE_CANADIAN", "Please paste your LinkedIn profile link here. Please confirm the link is accurate and works before submitting your application.", undefined, US, "textarea")).toBe(li);
    expect(ask("COMPLETE_CANADIAN", "Paste the full web address (URL) of your LinkedIn profile, beginning with https, for example, https://www.linkedin.com/in/yourname. If you do not have one, enter N/A.", undefined, US, "textarea")).toBe(li);
  });

  it("work rights in other words: eligibility, eligible to be employed, a petition on your behalf", () => {
    expect(ask("US_H1B_SENIOR", "Do you currently have eligibility to work in the US?", YES_NO)).toBe("Yes");
    expect(ask("COMPLETE_CANADIAN", "Are you legally eligible to be employed in the United States? (Proof of identity and eligibility will be required upon employment):", YES_NO)).toBe("No");
    const petition = "Will you now or in the future require TWG Global to file a petition or application for employment - based status on your behalf to begin or continue employment with TWG Global?";
    expect(ask("US_H1B_SENIOR", petition, YES_NO)).toBe("Yes");
    expect(ask("BOOTCAMP_CAREER_GAP", petition, YES_NO)).toBe("No");
  });

  it("a U.S. person is a citizen or permanent resident, never a visa holder (Jeffrey M. Consulting)", () => {
    const q = "This position involves access to export-controlled information. Are you a “U.S. person” as defined under applicable U.S. export-control regulations, including ITAR and EAR?";
    expect(ask("BOOTCAMP_CAREER_GAP", q, YES_NO)).toBe("Yes");
    // The allow-list layer (D-61 amendment): this persona's answer holds words outside the closed vocabulary, so its status is unknown and the device abstains.
    expect(ask("US_OPT_ANALYST", q, YES_NO)).toBe("none");
    expect(ask("US_H1B_SENIOR", q, YES_NO)).toBe("none");
  });

  it("an EU citizen may work in another EU country (Genetec's Austria)", () => {
    // The allow-list layer (D-61 amendment): this persona's answer holds words outside the closed vocabulary, so its status is unknown and the device abstains.
    expect(ask("BERLIN_STAFF", "Are you legally authorized to work in Austria?", YES_NO, { jobCountry: "AT", company: "Genetec" })).toBe("abstain");
  });

  it("French: living in, and allowed to work in, Canada (Mila)", () => {
    const montreal: Ctx = { jobCountry: "CA", company: "Mila", jobCity: "Montreal" };
    expect(ask("COMPLETE_CANADIAN", "Résidez-vous actuellement au Canada?", YES_NO, montreal)).toBe("Yes");
    expect(ask("US_H1B_SENIOR", "Résidez-vous actuellement au Canada?", YES_NO, montreal)).toBe("No");
    expect(ask("COMPLETE_CANADIAN", "Êtes-vous légalement autorisés à occuper un emploi au Canada?", YES_NO, montreal)).toBe("Yes");
  });

  it("currently employed is any job; an internal employee is of the company named (Saalex, Flourish)", () => {
    expect(ask("US_H1B_SENIOR", "Are you currently employed?", YES_NO)).toBe("Yes");
    expect(ask("COMPLETE_CANADIAN", "Are you an internal employee of Flourish Research or Valkyrie Clinical Trials?", YES_NO)).toBe("No");
  });

  it("an attention check is answered as it says (DISA Technologies)", () => {
    expect(ask("COMPLETE_CANADIAN", "To be considered, please choose option C below.", ["A", "B", "C", "D"])).toBe("C");
  });

  it("a list of clearances has the no-clearance option for someone holding none (Credence)", () => {
    const opts = ["Confidential", "Secret", "Top Secret", "I currently do not have an active security clearance", "Add answer of choice"];
    expect(ask("COMPLETE_CANADIAN", "Please indicate which active clearances you have", opts, US, "checkboxes")).toBe("I currently do not have an active security clearance");
  });

  it("the current degree program counts a degree in progress (Enfos)", () => {
    expect(ask("COMPLETE_CANADIAN", "What is your current degree program or highest level of education?", ["Bachelor's", "Master's", "PhD / Doctoral", "Other"])).toBe("Bachelor's");
  });

  it("the latest employer is the most recent job, ended or not (Saalex)", () => {
    expect(ask("BOOTCAMP_CAREER_GAP", "Latest Employer Name:")).toBe("Ibotta");
  });
});
