// @vitest-environment-options {"url": "https://career-schwab.icims.com/jobs/127949/associate-software-engineer/login?in_iframe=1"}
/**
 * iCIMS's candidate login with a phone (Schwab, live 2026-10-17), the page's
 * own form markup: an email, a "Country Code" dropdown (an <a role=combobox>
 * over 245 "(+93) Afghanistan" options) and the number beside it.
 *
 * The scan read the dropdown's first 60 options only, so the only "+1" left
 * was Canada's, and a Boston number got "(+1) Canada"; and since the picker
 * was not seen as one, the number box took "+1 617 555 0148" whole.
 */
import { describe, it, expect, beforeAll, afterAll } from "vitest";
import { readFileSync } from "node:fs";
import { scanPage } from "../src/content/formScanner";
import { looksLikeDialCodes } from "../src/content/phoneNumber";
import { stubLayout } from "./helpers/layout";

let restore: () => void;
beforeAll(() => {
  restore = stubLayout();
});
afterAll(() => restore());

const SCHWAB_LOGIN = readFileSync("test/fixtures/icims/schwab-login-form.html", "utf8");

function scan(profile: Record<string, unknown>) {
  document.body.innerHTML = SCHWAB_LOGIN;
  // The page's stylesheet hides the dropdown's own search box until it opens.
  document.querySelector<HTMLElement>(".dropdown-container")!.style.display = "none";
  const { fields } = scanPage(profile as never, false);
  return (label: string) => fields.find((f) => f.label === label);
}

describe("iCIMS's login with a phone (Schwab)", () => {
  it("knows a dialing code written first in parentheses", () => {
    expect(looksLikeDialCodes("(+93) Afghanistan", undefined)).toBe(true);
    expect(looksLikeDialCodes("— Make a Selection —", ["— Make a Selection —", "(+93) Afghanistan", "(+355) Albania", "(+213) Algeria", "(+1684) American Samoa", "(+376) Andorra"])).toBe(true);
  });

  it("sets the code from the whole list, and the number without it (a Boston number)", () => {
    const field = scan({ firstName: "Ji-woo", lastName: "Park", email: "jiwoo.park@example.com", phone: "+1 617 555 0148", country: "United States", location: "Boston, MA, United States" });
    expect(field("Country Code")?.category).toBe("phoneCountryCode");
    expect(field("Country Code")?.proposedValue).toBe("(+1) United States");
    expect(field("Number")?.proposedValue).toBe("617 555 0148");
    expect(field("Email")?.proposedValue).toBe("jiwoo.park@example.com");
  });

  // Its submit reads "I Acknowledge the Privacy Notice", no advance verb, so
  // the flow ended "Done … Review and submit" on the login (live 2026-10-17).
  it("turns the sign-in step with iCIMS's own button, whatever it says", async () => {
    const { getAdapter } = await import("../src/content/adapters");
    const { findAdvanceButton } = await import("../src/content/advance");
    document.body.innerHTML = SCHWAB_LOGIN;
    const adapter = getAdapter(location.hostname, location.href);
    expect(adapter?.id).toBe("icims");
    const adv = findAdvanceButton(document.getElementById("enterEmailForm")!, adapter);
    expect(adv?.el.id).toBe("enterEmailSubmitButton");
    expect(adv?.kind).toBe("advance");
  });

  it("gives a Canadian number Canada's +1", () => {
    const field = scan({ firstName: "Maya", lastName: "Tremblay", email: "maya.tremblay@example.com", phone: "+1 613 555 0175", country: "Canada", location: "Ottawa, ON, Canada" });
    expect(field("Country Code")?.proposedValue).toBe("(+1) Canada");
    expect(field("Number")?.proposedValue).toBe("613 555 0175");
  });
});
