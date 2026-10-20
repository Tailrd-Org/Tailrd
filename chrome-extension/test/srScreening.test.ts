// @vitest-environment-options {"url": "https://jobs.smartrecruiters.com/oneclick-ui/company/WesternDigital/publication/ed8ce28c-9574-492d-819d-caa4e0dbb275/screening?dcr_ci=WesternDigital"}
/**
 * SmartRecruiters' screening step (round 7). Its Next needs no POST, so the
 * one-click flow reaches it live; there every question's text is slotted
 * from the light DOM into a <label> inside the component's shadow root
 * (`<slot name="label-content">`), so a label's own textContent is empty:
 * the scan read 29 fields with no label ("" or "*") and said "No application
 * form found on this page" (Western Digital, live). Markup captured from the
 * page in a plain browser, shadow roots included; five of its questions.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { scanPage } from "../src/content/formScanner";
import { setResolveContext } from "../src/content/fieldResolver";
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

/** Turn each captured `<template shadowrootmode="open">` into the shadow root it was. */
function hydrate(root: ParentNode): void {
  for (const t of Array.from(root.querySelectorAll<HTMLTemplateElement>("template[shadowrootmode]"))) {
    const host = t.parentElement;
    if (!host || host.shadowRoot) continue;
    const shadow = host.attachShadow({ mode: "open" });
    shadow.append(t.content);
    t.remove();
    hydrate(shadow);
  }
}

const PAGE = readFileSync("test/fixtures/smartrecruiters/screening-wd.html", "utf8");
const persona = (who: string) => (P as Record<string, unknown>)[who] as UserApplicationProfile;
const scan = (who: string) => {
  document.body.innerHTML = PAGE;
  hydrate(document.body);
  setResolveContext({ jobCountry: "US", jobCity: "Rochester", jobPlaces: null, company: "Western Digital" });
  const { fields } = scanPage(persona(who), true);
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return fields;
};

describe("SmartRecruiters' screening questions are read by their slotted text", () => {
  it("every question has its label", () => {
    const labels = scan("US_GREENCARD_STUDENT").map((f) => f.label.replace(/\s*\*$/, ""));
    for (const q of [
      "What is your Current University?",
      "What is your highest level of education?",
      "What is your expected (or actual) graduation month?",
      "Are you legally authorized or have the legal right to work in the country you have selected?",
      "Have you ever been employed by WD or any of its subsidiaries?",
    ]) {
      expect(labels.some((l) => l.startsWith(q)), q).toBe(true);
    }
  });

  it("and answered as the bank answers them", () => {
    const fields = scan("US_GREENCARD_STUDENT");
    const of = (q: string) => fields.find((f) => f.label.startsWith(q))?.proposedValue ?? null;
    expect(of("Are you legally authorized or have the legal right")).toBe("Yes");
    expect(of("Have you ever been employed by WD")).toBe("No");
    expect(of("What is your Current University?")).toBe("San José State University");
  });
});
