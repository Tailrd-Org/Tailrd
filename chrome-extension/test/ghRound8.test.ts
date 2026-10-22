// @vitest-environment-options {"url": "https://job-boards.greenhouse.io/qbank/jobs/1"}
/**
 * Round 8's Greenhouse findings (2026-10-22): live boards and the debug batch
 * of P1b. Labels and options verbatim from each board (GET only). Tested
 * through the scan path.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { resolveWithOptions, scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
import { degreeTitleOption } from "../src/content/fieldMatcher";
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
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
});

const persona = (who: string) => (P as Record<string, unknown>)[who] as UserApplicationProfile;
const esc = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const select = (id: string, label: string, options: string[]): string =>
  `<div class="field"><label for="${id}">${esc(label)}</label><select id="${id}" name="${id}"><option value="">Select...</option>${options.map((o, i) => `<option value="${i}">${esc(o)}</option>`).join("")}</select></div>`;

/**
 * Arcesium's education rows (live, round 8): a list of degree TITLES, where
 * the level the extension proposes ("Master") names several options, so the
 * H-1B persona's Master of Science in Information Systems and Bachelor of
 * Engineering in Computer Science were both left blank.
 */
describe("a list of degree titles takes the row's own title", () => {
  const TITLES = ["Advanced Diploma", "Associate", "Bachelor of Arts", "Bachelor of Business Administration", "Bachelor of Engineering", "Bachelor of Science", "Doctor of Philosophy", "Master of Arts", "Master of Business Administration", "Master of Engineering", "Master of Science"];
  it("per education row, on the scan path", () => {
    document.body.innerHTML = `<form id="application-form">${select("school--0", "School", ["University of Washington", "Other"])}${select("degree--0", "Degree", TITLES)}${select("school--1", "School", ["Hanoi University of Science and Technology", "Other"])}${select("degree--1", "Degree", TITLES)}</form>`;
    const { fields } = scanPage(persona("US_H1B_SENIOR"), true);
    const value = (id: string) => fields.find((f) => document.getElementById(id)?.getAttribute("data-ap-field") === f.id)?.proposedValue ?? null;
    expect(value("degree--0")).toBe("Master of Science");
    expect(value("degree--1")).toBe("Bachelor of Engineering");
  });
  it("a list of levels keeps the level", () => {
    document.body.innerHTML = `<form id="application-form">${select("degree--0", "Degree", ["Associate's Degree", "Bachelor's Degree", "Master's Degree", "Doctor of Philosophy (Ph.D.)", "High School"])}</form>`;
    const { fields } = scanPage(persona("US_H1B_SENIOR"), true);
    expect(fields.find((f) => f.category === "degree")?.proposedValue).toBe("Master's Degree");
  });
  it("the title names one option or none", () => {
    expect(degreeTitleOption(TITLES, "Master of Science in Information Systems")).toBe("Master of Science");
    expect(degreeTitleOption(TITLES, "Bachelor of Applied Science in Mechatronics Engineering")).toBeNull();
    expect(degreeTitleOption(["Master of Science (M.S.)", "Master of Arts (M.A.)"], "Master of Science in Data Science")).toBe("Master of Science (M.S.)");
    expect(degreeTitleOption(["Master of Science", "Master of Science in Engineering"], "Master of Science in Engineering")).toBe("Master of Science in Engineering");
  });
});

/**
 * Legend Biotech's "Are you open to relocation?*" (live, round 8) was Yes for
 * an applicant who never said: the requirement default (decision 49) took the
 * open-ended question for a posting's requirement. Decision 49 keeps that one
 * blank without a stated answer; a move the posting names stays accepted.
 */
describe("an open-ended relocation question is the applicant's (decision 49)", () => {
  const ask = (who: string, label: string): string | null => {
    document.body.innerHTML = `<form id="application-form">${select("q0", label, ["Yes", "No"])}</form>`;
    setResolveContext({ jobCountry: "US", jobCity: "Somerset", jobPlaces: null, company: "Legend Biotech" });
    return scanPage(persona(who), true).fields.find((f) => f.label.startsWith(label.slice(0, 20)))?.proposedValue ?? null;
  };
  it("blank without a stated answer, the stated one otherwise", () => {
    expect(ask("US_F1_STUDENT", "Are you open to relocation?*")).toBeNull();
    expect(ask("US_F1_STUDENT", "Would you be willing to relocate?")).toBeNull();
    expect(ask("INDIA_NEW_GRAD", "Are you open to relocation?*")).toBe("Yes");
    expect(ask("US_H1B_SENIOR", "Are you open to relocation?*")).toBe("No");
  });
  it("a move the posting names is accepted by applying", () => {
    expect(ask("US_F1_STUDENT", "Are you willing to relocate to Somerset, NJ for this internship?")).toBe("Yes");
  });
});

/**
 * Hear.com's and EviSmart's education rows (live, round 8), Greenhouse's
 * standard degree list: a Quebec "Baccalauréat en psychologie" named no level
 * the extension knew, and a certificate (Concordia's, a bootcamp's) has no
 * degree of its own on the list. Both rows were left blank.
 */
describe("a row's degree in French, and a certificate (decision 96)", () => {
  const STANDARD = ["Associate's Degree", "Bachelor's Degree", "Doctor of Medicine (M.D.)", "Doctor of Philosophy (Ph.D.)", "Engineer's Degree", "High School", "Juris Doctor (J.D.)", "Master of Business Administration (M.B.A.)", "Master's Degree", "Other"];
  const rows = (who: string, n: number): (string | null)[] => {
    const html = Array.from({ length: n }, (_, i) => select(`degree--${i}`, "Degree", STANDARD)).join("");
    document.body.innerHTML = `<form id="application-form">${html}</form>`;
    const { fields } = scanPage(persona(who), true);
    return Array.from({ length: n }, (_, i) => fields.find((f) => document.getElementById(`degree--${i}`)?.getAttribute("data-ap-field") === f.id)?.proposedValue ?? null);
  };
  it("Baccalauréat en … is a bachelor's; a certificate is the list's Other", () => {
    expect(rows("MONTREAL_CHANGER", 2)).toEqual(["Bachelor's Degree", "Other"]);
    expect(rows("BOOTCAMP_CAREER_GAP", 2)).toEqual(["Other", "High School"]);
  });
  it("a list without Other leaves the certificate blank", () => {
    document.body.innerHTML = `<form id="application-form">${select("degree--0", "Degree", STANDARD.filter((o) => o !== "Other"))}</form>`;
    expect(scanPage(persona("BOOTCAMP_CAREER_GAP"), true).fields.find((f) => f.category === "degree")?.proposedValue ?? null).toBeNull();
  });
});

/**
 * Bracebridge Capital (live, round 8): "Are you in pursuit of a MS degree in
 * Computer Science, Engineering or Information Systems at Northeastern
 * University?" was blank for a San José State student (its "Are you
 * currently attending Northestern University?" beside it was No).
 */
describe("a degree pursued at a named school", () => {
  const ask = (who: string, label: string): string | null => {
    document.body.innerHTML = `<form id="application-form">${select("q0", label, ["Yes", "No"])}</form>`;
    return scanPage(persona(who), true).fields.find((f) => f.label.startsWith(label.slice(0, 20)))?.proposedValue ?? null;
  };
  const PURSUIT = "Are you in pursuit of a MS degree in Computer Science, Engineering or Information Systems at Northeastern University? *";
  it("No for someone studying elsewhere, or whose degree there is finished", () => {
    expect(ask("US_GREENCARD_STUDENT", PURSUIT)).toBe("No");
    // Northeastern's MS in Data Science, finished May 2025.
    expect(ask("US_OPT_ANALYST", PURSUIT)).toBe("No");
  });
  it("currently attending: a finished degree there is not attendance now", () => {
    expect(ask("US_OPT_ANALYST", "Are you currently attending Northeastern University?*")).toBe("No");
    expect(ask("US_GREENCARD_STUDENT", "Are you currently attending Northestern University?*")).toBe("No");
  });
  it("attending or a recent graduate: a graduate there is one", () => {
    expect(ask("US_OPT_ANALYST", "Are you attending or a recent graduate of Northeastern University?")).toBe("Yes");
    expect(ask("US_GREENCARD_STUDENT", "Are you attending or a recent graduate of Northeastern University?")).toBe("No");
  });
});

/**
 * Trace3 (live, round 8): "What are your current salary requirements?" went
 * to the AI for a veteran who states $135,000: "current salary" read as the
 * pay now, which the extension never gives.
 */
describe("salary requirements asked as current", () => {
  const ask = (who: string, label: string): string | null => {
    document.body.innerHTML = `<form id="application-form"><div class="field"><label for="q0">${esc(label)}</label><input type="text" id="q0" name="q0"></div></form>`;
    return scanPage(persona(who), true).fields.find((f) => f.label.startsWith(label.slice(0, 20)))?.proposedValue ?? null;
  };
  it("a requirement is the expectation; the pay now stays unanswered", () => {
    expect(ask("US_VETERAN", "What are your current salary requirements? *")).toBe("$135,000");
    expect(ask("US_VETERAN", "What is your current salary?")).toBeNull();
  });
});

describe("Bracebridge's self-ID list under a questionnaire heading (live, round 8)", () => {
  it("gender options under the heading are the gender question (decision 90)", () => {
    // Its label is the section heading; the stated gender was passed over for
    // the decline.
    document.body.innerHTML = `<form id="application-form">${select("q1", "Voluntary Equal Employment Opportunity Questionnaire:", ["Male", "Female", "I choose not to disclose"])}</form>`;
    const f = scanPage(persona("US_GREENCARD_STUDENT"), true).fields.find((x) => x.label.startsWith("Voluntary"));
    expect(f?.proposedValue ?? null).toBe("Female");
  });
  it("the same list read once its lazy options load (the re-ask)", () => {
    // Live, the list was a react-select: no options at scan, so the heading
    // read as another self-ID topic and its re-ask took the decline.
    document.body.innerHTML = `<form id="application-form"><div class="field"><label id="q2-label" for="q2">Voluntary Equal Employment Opportunity Questionnaire:</label><div class="select__control"><input id="q2" role="combobox" aria-haspopup="true" aria-expanded="false" aria-autocomplete="list" aria-labelledby="q2-label"></div></div></form>`;
    const p = persona("US_GREENCARD_STUDENT");
    const { fields, registry } = scanPage(p, true);
    const f = fields.find((x) => x.label.startsWith("Voluntary"))!;
    expect(f.sensitive).toBe(true);
    expect(resolveWithOptions(f, registry, p, null, true, ["Male", "Female", "I choose not to disclose"])).toBe("Female");
  });
});
