// @vitest-environment-options {"url": "https://whiteswandata.pinpointhq.com/en/postings/2d85b27e-e368-4b75-91d0-302e2904d31c/applications/new"}
/**
 * Round 8's smaller ATSs (Pinpoint, Dayforce, Gusto, Comeet, ADP, Recruitee),
 * read blind live, 2026-10-22. Labels and options verbatim (GET only).
 * Tested through the scan path.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
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

const persona = (who: string) => (P as Record<string, unknown>)[who] as UserApplicationProfile;
const esc = (s: string): string => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** One Yes/No question on a posting in `city`: the scanner's proposed answer. */
function askYesNo(who: string, label: string, ctx: { jobCountry: string; jobCity: string | null; company: string }): string | null {
  document.body.innerHTML = `<form><fieldset><legend>${esc(label)}</legend><label><input type="radio" name="q0" value="Yes">Yes</label><label><input type="radio" name="q0" value="No">No</label></fieldset></form>`;
  setResolveContext({ jobCountry: ctx.jobCountry, jobCity: ctx.jobCity, jobPlaces: null, company: ctx.company });
  const f = scanPage(persona(who), true).fields.find((x) => x.label.startsWith(label.slice(0, 25)));
  setResolveContext({ jobCountry: null, jobCity: null, jobPlaces: null, company: "" });
  return f?.proposedValue ?? null;
}

describe("White Swan's office question (Pinpoint, live, round 8)", () => {
  const Q = "This role is a full time, office based role. Are you happy to work from our New York office 5 days a week?";
  const NY = { jobCountry: "US", jobCity: "New York", company: "White Swan" };
  it("being happy to is being willing: the office the posting sets is accepted by applying", () => {
    // UK_SENIOR (London, willing to relocate) was sent to the AI.
    expect(askYesNo("UK_SENIOR", Q, NY)).toBe("Yes");
  });
  it("someone who will not move answers from where they live; a remote-only preference stays theirs", () => {
    // MONTREAL_CHANGER: Montreal, will not relocate.
    expect(askYesNo("MONTREAL_CHANGER", Q, NY)).toBe("No");
  });
});
