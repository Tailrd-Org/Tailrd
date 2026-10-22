// @vitest-environment-options {"url": "https://job-boards.greenhouse.io/qbank/jobs/1"}
/**
 * Round 8's Greenhouse findings (2026-10-22): live boards and the debug batch
 * of P1b. Labels and options verbatim from each board (GET only). Tested
 * through the scan path.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { scanPage } from "../src/content/formScanner";
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
