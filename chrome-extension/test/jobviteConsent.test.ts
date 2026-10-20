// @vitest-environment-options {"url": "https://jobs.jobvite.com/nutanix/job/oi8PAfwz/apply"}
/**
 * Jobvite's "Data Consent" gate (Nutanix, live 2026-10-17 and 2026-10-19),
 * the page's own markup: before the application, a "Location of Residence and
 * Language" select whose options Angular adds from the page's consent
 * policies; once one is chosen, the policy text ("By clicking Submit, I
 * acknowledge that I have read Nutanix's Job Applicant Privacy Notice") with
 * Submit (the form's POST, which sends the consent and opens the application)
 * and I Decline.
 *
 * The extension chose nothing there, found no button it knew, and the flow
 * said "Done … Review and submit" on a gate it never filled.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { scanPage } from "../src/content/formScanner";
import { findAdvanceButton } from "../src/content/advance";
import { getAdapter } from "../src/content/adapters";
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

const GATE = readFileSync("test/fixtures/jobvite/nutanix-consent.html", "utf8");
const GATE_ACCEPT = readFileSync("test/fixtures/jobvite/nutanix-consent-accept.html", "utf8");
const LABEL = "Location of Residence and Language";
const persona = (who: string) => (P as Record<string, unknown>)[who] as UserApplicationProfile;

/** The gate with another company's policy list in place of Nutanix's one. */
function gateWith(policies: string[]): string {
  const options = policies.map((name, i) => `<option ng-repeat="policy in consentPolicies" value="p${i}" class="ng-binding ng-scope">${name}</option>`).join("");
  return GATE.replace(/<!-- ngRepeat: policy in consentPolicies -->[\s\S]*?<!-- end ngRepeat: policy in consentPolicies -->/, options);
}

function propose(html: string, who: string): string | null {
  document.body.innerHTML = html;
  const { fields } = scanPage(persona(who), true);
  const f = fields.find((x) => x.label.startsWith(LABEL));
  return f?.proposedValue ?? null;
}

describe("Jobvite's data-consent gate: the residence and language", () => {
  it("takes Nutanix's one policy for every applicant (All Locations)", () => {
    for (const who of ["US_H1B_SENIOR", "BOOTCAMP_CAREER_GAP", "COMPLETE_CANADIAN", "BERLIN_STAFF", "INDIA_NEW_GRAD", "SPARSE_CANADIAN"]) {
      expect(propose(GATE, who), who).toBe("All Locations (English)");
    }
  });

  const MANY = ["United States (English)", "Canada (English)", "Canada (French)", "Germany (German)", "Germany (English)", "United Kingdom (English)", "All Other Locations (English)"];

  it("takes the applicant's own country of residence, in English", () => {
    expect(propose(gateWith(MANY), "US_H1B_SENIOR")).toBe("United States (English)");
    expect(propose(gateWith(MANY), "BOOTCAMP_CAREER_GAP")).toBe("United States (English)");
    expect(propose(gateWith(MANY), "US_OPT_ANALYST")).toBe("United States (English)");
    expect(propose(gateWith(MANY), "COMPLETE_CANADIAN")).toBe("Canada (English)");
    expect(propose(gateWith(MANY), "SPARSE_CANADIAN")).toBe("Canada (English)");
    expect(propose(gateWith(MANY), "BERLIN_STAFF")).toBe("Germany (English)");
    expect(propose(gateWith(MANY), "UK_SENIOR")).toBe("United Kingdom (English)");
  });

  it("takes the list's other-locations policy for a country it does not name", () => {
    expect(propose(gateWith(MANY), "INDIA_NEW_GRAD")).toBe("All Other Locations (English)");
  });

  it("takes the country's only policy when it is not in English", () => {
    expect(propose(gateWith(["United States (English)", "Germany (German)", "Rest of World (English)"]), "BERLIN_STAFF")).toBe("Germany (German)");
  });

  it("leaves it to the applicant when the list names neither the country nor other places", () => {
    expect(propose(gateWith(["United States (English)", "Canada (English)"]), "INDIA_NEW_GRAD")).toBeNull();
  });
});

describe("Jobvite's data-consent gate: its Submit is the step's advance", () => {
  it("turns the gate with the consent form's own button, whatever it says", () => {
    document.body.innerHTML = GATE_ACCEPT;
    const adapter = getAdapter(location.hostname, location.href);
    expect(adapter?.id).toBe("jobvite");
    const form = document.querySelector<HTMLElement>('form[name="consentForm"]')!;
    const adv = findAdvanceButton(form, adapter);
    expect(adv?.el.textContent?.trim()).toBe("Submit");
    expect(adv?.kind).toBe("advance");
  });

  it("never treats the application's own Submit as a gate's", () => {
    document.body.innerHTML = `<form name="applyForm" method="POST" action="/nutanix/job/oi8PAfwz/apply/submit"><label for="fn">First Name</label><input id="fn"><button type="submit" class="jv-button jv-button-primary">Submit</button></form>`;
    const adapter = getAdapter(location.hostname, location.href);
    const adv = findAdvanceButton(document.querySelector<HTMLElement>("form")!, adapter);
    expect(adv?.kind).toBe("terminal");
  });

  it("never takes a consent form that leads to job alerts", () => {
    document.body.innerHTML = GATE_ACCEPT.replace('action="/nutanix/job/oi8PAfwz/apply"', 'action="/nutanix/jobAlerts"');
    const adapter = getAdapter(location.hostname, location.href);
    const adv = findAdvanceButton(document.querySelector<HTMLElement>('form[name="consentForm"]')!, adapter);
    expect(adv?.kind).not.toBe("advance");
  });
});
