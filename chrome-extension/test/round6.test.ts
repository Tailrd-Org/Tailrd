// @vitest-environment-options {"url": "https://job-boards.greenhouse.io/justworks/jobs/8236815"}
/**
 * Round 6 (2026-10-17): Greenhouse boards in no bank and no case, live. Each
 * test is a write a live page got wrong, or a blank a stated fact answers;
 * labels verbatim from the live pages.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import { sentencesOf } from "../src/content/statementText";
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

/** One question on a Greenhouse-like form, the job in `country`; the
 *  proposed answer for each named persona. */
function propose(label: string, options: string[], whos: string[], country = "US", control?: string): Record<string, string | null> {
  const c =
    control ??
    (options.length
      ? `<select id="q0" name="q0"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select>`
      : `<input type="text" id="q0" name="q0">`);
  const out: Record<string, string | null> = {};
  for (const who of whos) {
    document.body.innerHTML = `<form><div class="field"><label for="q0">${esc(label)}</label>${c}</div><div class="field"><label for="em">Email</label><input id="em" name="email"></div></form>`;
    setResolveContext({ jobCountry: country, jobCity: null, jobPlaces: null, company: "Acme" });
    const { fields } = scanPage(persona(who), true);
    out[who] = fields.find((f) => f.label.startsWith(label.slice(0, 30)))?.proposedValue ?? null;
  }
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return out;
}

describe("a sentence goes on past an abbreviation", () => {
  it("never ends at e.g., i.e., etc. or U.S. before a capital", () => {
    expect(sentencesOf("Will you require sponsorship (e.g. H-1B status, etc.) to work here? This includes transfers.")).toEqual([
      "Will you require sponsorship (e.g. H-1B status, etc.) to work here?",
      "This includes transfers.",
    ]);
    expect(sentencesOf("Are you a U.S. Citizen? Please answer.")).toEqual(["Are you a U.S. Citizen?", "Please answer."]);
    expect(sentencesOf("List your skills, i.e. Python. Then sign.")).toEqual(["List your skills, i.e. Python.", "Then sign."]);
    expect(sentencesOf("Sponsorship (E.g. H-1B) needed? Yes or no.")).toEqual(["Sponsorship (E.g. H-1B) needed?", "Yes or no."]);
  });
  it("ends only before a capital or a digit", () => {
    expect(sentencesOf("Did anyone refer you to this position? if so, who?")).toEqual(["Did anyone refer you to this position? if so, who?"]);
  });
  it("so a referral asked yes or no takes no job title (Syska Hennessy)", () => {
    // The applicant's title was written into it (question bank 2026-10-17).
    const a = propose("Did anyone refer you to this position? if so, who?", [], ["US_OPT_ANALYST", "US_H1B_SENIOR"]);
    expect(a.US_OPT_ANALYST).toBeNull();
    expect(a.US_H1B_SENIOR).toBeNull();
  });
});

describe("a visa status asked in words is the status (Sigma)", () => {
  // A text box: "What is your current visa status?*" got "No", the
  // sponsorship answer (live 2026-10-17).
  it("is the applicant's own statement of it, never a yes or no", () => {
    const a = propose("What is your current visa status?*", [], ["BOOTCAMP_CAREER_GAP", "US_OPT_ANALYST", "US_GREENCARD_STUDENT"]);
    expect(a.BOOTCAMP_CAREER_GAP).toBe("US Citizen");
    expect(a.US_OPT_ANALYST).toBe("F-1 STEM OPT (EAD valid through June 2028)");
    expect(a.US_GREENCARD_STUDENT).toBe("U.S. permanent resident (green card)");
  });
});

describe("a project to describe is no list of skills (PDT)", () => {
  // Two such boxes got "SQL, Python, Tableau" (live 2026-10-17).
  const PROJECT =
    "Please provide a brief description (a few sentences or bullets) of a project or two you have worked on that demonstrate(s) your data analysis experience/skills (optional for those from more theoretical fields): *";
  it("leaves the essay to the applicant", () => {
    const a = propose(PROJECT, [], ["US_OPT_ANALYST"], "US", `<textarea id="q0" name="q0"></textarea>`);
    expect(a.US_OPT_ANALYST).toBeNull();
  });
  it("still lists skills where skills are asked", () => {
    const a = propose("Please list your technical skills", [], ["US_OPT_ANALYST"], "US", `<textarea id="q0" name="q0"></textarea>`);
    expect(a.US_OPT_ANALYST).toBe("SQL, Python, Tableau");
  });
});

describe("a veteran in the family is not the applicant's service (Justworks)", () => {
  // "Are you a spouse or domestic partner to a veteran?" got "No" from the
  // applicant's own "I have never served" (live 2026-10-17).
  it("declines where the list lets it, never answers from the applicant's own status", () => {
    const a = propose("Are you a spouse or domestic partner to a veteran?*", ["Yes", "No", "I don't wish to answer"], ["US_GREENCARD_STUDENT", "US_VETERAN"]);
    expect(a.US_GREENCARD_STUDENT).not.toBe("No");
    expect(a.US_VETERAN).not.toBe("Yes");
  });
});

describe("blanks a stated fact answers (Greenhouse, live 2026-10-17)", () => {
  it("the deemed export rule does not touch a US citizen or permanent resident (Everpure)", () => {
    const a = propose("Does the deemed export rule affect your employment by Everpure?*", ["Yes", "No"], ["US_VETERAN", "US_GREENCARD_STUDENT", "US_H1B_SENIOR"]);
    expect(a.US_VETERAN).toBe("No");
    expect(a.US_GREENCARD_STUDENT).toBe("No");
    expect(a.US_H1B_SENIOR).toBeNull();
  });
  it("documentation of the resume's history, if requested, is provided (Justworks)", () => {
    const a = propose("If requested, would you be able to provide documentation reflecting the education and employment history listed on your resume? *", ["Yes", "No"], ["US_GREENCARD_STUDENT", "US_VETERAN"]);
    expect(a.US_GREENCARD_STUDENT).toBe("Yes");
    expect(a.US_VETERAN).toBe("Yes");
  });
  it("a graduate returns to no program after the internship (Everpure)", () => {
    const a = propose("Will you be returning to an undergraduate or graduate program after the internship if selected?*", ["Yes", "No"], ["US_VETERAN", "INDIA_NEW_GRAD", "COMPLETE_CANADIAN"]);
    expect(a.US_VETERAN).toBe("No");
    expect(a.INDIA_NEW_GRAD).toBe("No");
    expect(a.COMPLETE_CANADIAN).toBeNull();
  });
});

describe("a box that only acknowledges reads its question (Everpure)", () => {
  // Greenhouse's markup, verbatim but for the link: a lone "Acknowledge/Confirm"
  // box under the legend "Personal Information Policy" was left to the AI and
  // dropped (live 2026-10-17).
  const box = (legend: string, description: string): string =>
    `<form><fieldset class="checkbox" id="question_69589930[]" aria-required="true"><legend class="label checkbox__description">${legend}<!-- --> <span class="required">*</span></legend>` +
    `<div class="checkbox__wrapper"><div class="checkbox__input"><input description="${legend}" required="" type="checkbox" id="question_69589930[]_754163668" name="question_69589930[]" aria-describedby="question_69589930[]-description question_69589930[]-error" aria-invalid="false" aria-errormessage="question_69589930[]-error" value="754163668"/></div>` +
    `<label for="question_69589930[]_754163668">Acknowledge/Confirm</label></div></fieldset>` +
    `<div class="body body__secondary"><div id="question_69589930[]-description" class="question-description"><p>${description}</p></div></div>` +
    `<div class="field"><label for="em">Email</label><input id="em" name="email"></div></form>`;
  const proposed = (html: string): string | null => {
    document.body.innerHTML = html;
    setResolveContext({ jobCountry: "US", jobCity: null, jobPlaces: null, company: "Everpure" });
    const { fields } = scanPage(persona("US_VETERAN"), true);
    setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
    return fields.find((f) => /acknowledge/i.test(f.label))?.proposedValue ?? null;
  };
  it("acknowledges a policy", () => {
    const html = box(
      "Personal Information Policy",
      "Wonder how or why Pure collects or uses any information you provide? Check out our <a href=\"https://example.com/notice.pdf\">Applicant &amp; Candidate Personal Information Protection Notice</a>."
    );
    expect(proposed(html)).toMatch(/^(yes|true|checked)$/i);
  });
  it("never agrees to arbitrate for the applicant", () => {
    const html = box("Mutual Arbitration Agreement", "By checking this box you agree to resolve any dispute through binding individual arbitration and waive the right to a jury trial.");
    expect(proposed(html)).toBeNull();
  });
  // Twilio's pledge over one "Acknowledge" box stays the applicant's
  // (decision 6 of round 4). The question bank renders its description
  // between the legend and the box; Greenhouse puts it after the fieldset.
  const PLEDGE =
    "By checking this box, I confirm I have read, reviewed and understood the guidelines outlined in the Candidate AI Responsible Use Policy. I affirm that all the information and materials I submit throughout my application and candidacy will reflect my own work and experience.";
  it("leaves a pledge of the applicant's own work blank, the description inside the fieldset", () => {
    const html =
      `<form><fieldset class="field" id="q0"><legend>${PLEDGE}*</legend><div id="q0-d" class="description">Candidate AI Responsible Use Policy</div>` +
      `<label><input type="checkbox" name="question_68522887[]" value="0"> Acknowledge</label></fieldset><div class="field"><label for="em">Email</label><input id="em" name="email"></div></form>`;
    expect(proposed(html)).toBeNull();
  });
  it("leaves a pledge of the applicant's own work blank, Greenhouse's markup", () => {
    expect(proposed(box(PLEDGE, "Candidate AI Responsible Use Policy").replace(/Acknowledge\/Confirm/g, "Acknowledge"))).toBeNull();
  });
});

describe("some high school is no diploma (Lucid Motors)", () => {
  // A bootcamp graduate with a high school diploma got "Some High School"
  // (question bank 2026-10-17): the GED in the diploma's option ruled it out.
  it("takes the diploma", () => {
    const a = propose(
      "What is your highest level of education completed?",
      ["Some High School", "High School Diploma/GED", "Bachelors Degree", "Masters Degree", "PHD"],
      ["BOOTCAMP_CAREER_GAP", "US_VETERAN"]
    );
    expect(a.BOOTCAMP_CAREER_GAP).toBe("High School Diploma/GED");
    expect(a.US_VETERAN).toBe("Bachelors Degree");
  });
  it("never takes it for a high school graduate either", () => {
    const a = propose("Highest level of education", ["Some High School", "High School Diploma/GED", "Some College", "Associate's Degree"], ["BOOTCAMP_CAREER_GAP"]);
    expect(a.BOOTCAMP_CAREER_GAP).not.toBe("Some High School");
  });
  it("nor for a student who has completed nothing yet (Lucid Motors, Wattpad)", () => {
    const a = propose(
      "What is your highest level of education completed?",
      ["Some High School", "High School Diploma/GED", "Bachelors Degree", "Masters Degree", "PHD"],
      ["COMPLETE_CANADIAN"]
    );
    expect(a.COMPLETE_CANADIAN).not.toBe("Some High School");
    const b = propose(
      "What is the highest degree that you have attained?",
      ["Less than high school or equivalent", "High school or equivalent", "College diploma", "University degree", "Advanced degree (MSc, MA, PhD)", "Prefer not to say"],
      ["COMPLETE_CANADIAN"],
      "CA"
    );
    expect(b.COMPLETE_CANADIAN).not.toBe("Less than high school or equivalent");
  });
});

describe("Justworks' sponsorship question, read whole", () => {
  // "(e.g. H-1B" ended a sentence, so the question read was "H-1B status,
  // etc.) to work legally … in the United States?", a work-right question:
  // Yes for everyone, a citizen and a green-card holder included (live).
  const L =
    "Will you now or in the future require sponsorship for employment visa status (e.g. H-1B status, etc.) to work legally for our Company in the United States? This includes requests to transfer an existing visa sponsorship from one employer to another.";
  it("No for citizens and permanent residents, Yes for a visa holder", () => {
    const a = propose(L, ["Yes", "No"], ["US_GREENCARD_STUDENT", "BOOTCAMP_CAREER_GAP", "US_VETERAN", "US_H1B_SENIOR"]);
    expect(a.US_GREENCARD_STUDENT).toBe("No");
    expect(a.BOOTCAMP_CAREER_GAP).toBe("No");
    expect(a.US_VETERAN).toBe("No");
    expect(a.US_H1B_SENIOR).toBe("Yes");
  });
});
