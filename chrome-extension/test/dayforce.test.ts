/**
 * Dayforce's application form (jobs.dayforcehcm.com, live 2026-10-05): Ant
 * Design form items, a select without a search box (its input read-only),
 * and work-history rows numbered in their ids.
 */
import { describe, it, expect, beforeAll, afterAll, beforeEach } from "vitest";
import { stubLayout } from "./helpers/layout";
import { scanPage } from "../src/content/formScanner";
import type { UserApplicationProfile } from "../src/shared/types";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());
beforeEach(() => {
  document.body.innerHTML = "";
});

const item = (id: string, label: string, control: string): string =>
  `<div class="ant-form-item"><div class="ant-row ant-form-item-row"><div class="ant-col ant-form-item-label"><label for="${id}" title="${label}">${label}</label></div>
   <div class="ant-col ant-form-item-control"><div class="ant-form-item-control-input"><div class="ant-form-item-control-input-content">${control}</div></div></div></div></div>`;

/** Ant Design's select without a search box: its input is read-only and only takes focus. */
const fixedSelect = (id: string): string =>
  `<div class="ant-select ant-select-in-form-item w-full ant-select-single ant-select-show-arrow"><div class="ant-select-selector"><span class="ant-select-selection-search"><input id="${id}" readonly unselectable="on" style="opacity:0" autocomplete="off" class="ant-select-selection-search-input" role="combobox" aria-expanded="false" aria-haspopup="listbox" aria-owns="${id}_list" aria-autocomplete="list" aria-controls="${id}_list" aria-required="true" value=""></span><span class="ant-select-selection-placeholder"></span></div><span class="ant-select-arrow" aria-hidden="true"></span></div>`;

const workRow = (n: number): string => {
  const id = (f: string): string => `jobPostingApplication_workHistory_${n}_${f}`;
  return `<div>
    ${item(id("title"), "Position Title", `<input id="${id("title")}" class="ant-input" type="text" value="">`)}
    ${item(id("isCurrent"), "Current Job", `<label class="ant-checkbox-wrapper"><span class="ant-checkbox"><input id="${id("isCurrent")}" class="ant-checkbox-input" type="checkbox"><span class="ant-checkbox-inner"></span></span></label>`)}
    ${item(id("companyName"), "Employer Name", `<input id="${id("companyName")}" class="ant-input" type="text" value="">`)}
    ${item(id("effectiveStart"), "Start Date", `<input id="${id("effectiveStart")}" class="ant-input" type="date" value="">`)}
  </div>`;
};

const person = {
  firstName: "Maya", lastName: "Tremblay", email: "maya.tremblay@example.com", phone: "(416) 555-0142",
  location: "Toronto, ON, Canada", country: "Canada", howDidYouHear: "LinkedIn",
  skills: [], education: [],
  experience: [
    { company: "Shopify", title: "Software Developer Intern", startDate: "2025-01", endDate: "2025-04", description: "" },
    { company: "Kinaxis", title: "Software Engineer Co-op", startDate: "2025-09", endDate: "Present", description: "" },
  ],
} as unknown as UserApplicationProfile;

describe("Dayforce", () => {
  it("scans a select without a search box, whose input is read-only", () => {
    document.body.innerHTML = `<form>
      ${item("jobPostingApplication_personalInfo_preferredContactMethod", "Preferred Contact Method", fixedSelect("jobPostingApplication_personalInfo_preferredContactMethod"))}
      ${item("jobPostingApplication_personalInfo_candidateSource", "How did you hear about this job?", fixedSelect("jobPostingApplication_personalInfo_candidateSource"))}
    </form>`;
    const fields = scanPage(person, false, null).fields;
    const heard = fields.find((f) => f.label === "How did you hear about this job?");
    expect(heard?.controlType).toBe("combobox");
    expect(fields.find((f) => f.label === "Preferred Contact Method")?.controlType).toBe("combobox");
  });

  it("ticks Current Job for the job still running, and only for it", () => {
    // The page lists row 1 (Kinaxis, "Present") above row 0 (Shopify, ended).
    document.body.innerHTML = `<form>${workRow(1)}${workRow(0)}</form>`;
    const boxes = scanPage(person, false, null).fields.filter((f) => f.label === "Current Job");
    expect(boxes.map((f) => [f.groupIndex, f.proposedValue])).toEqual([
      [1, "yes"],
      [0, "no"],
    ]);
  });

  it("leaves a day-precise Start Date to you when the profile knows only the month", () => {
    document.body.innerHTML = `<form>${workRow(0)}</form>`;
    const start = scanPage(person, false, null).fields.find((f) => f.label === "Start Date");
    expect(start?.proposedValue ?? null).toBeNull();
  });
});

/** An education row: its "Not Completed" box is numbered like the work rows
 *  (Woodmen of the World, live, round 8). */
const educationRow = (n: number): string => {
  const id = (f: string): string => `jobPostingApplication_educationHistory_${n}_${f}`;
  return `<div>
    ${item(id("degree"), "Degree", `<input id="${id("degree")}" class="ant-input" type="text" value="">`)}
    ${item(id("notCompleted"), "Not Completed", `<label class="ant-checkbox-wrapper"><span class="ant-checkbox"><input id="${id("notCompleted")}" class="ant-checkbox-input" type="checkbox"><span class="ant-checkbox-inner"></span></span></label>`)}
  </div>`;
};

describe("Dayforce's education rows", () => {
  it("ticks Not Completed for a degree still in progress, and only for it", () => {
    // Sent to the AI live for a Waterloo student graduating in April 2027.
    const student = { ...person, education: [{ school: "University of Waterloo", degree: "Bachelor of Applied Science in Mechatronics Engineering", graduationYear: "2027" }, { school: "Toronto Metropolitan University", degree: "Certificate in Data Analytics", graduationYear: "2020" }] } as unknown as UserApplicationProfile;
    document.body.innerHTML = `<form>${educationRow(0)}${educationRow(1)}</form>`;
    const boxes = scanPage(student, false, null).fields.filter((f) => f.label === "Not Completed");
    expect(boxes.map((f) => [f.groupIndex, f.proposedValue])).toEqual([
      [0, "yes"],
      [1, "no"],
    ]);
  });
});
