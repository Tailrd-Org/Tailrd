/**
 * Decide B fix round 4 (decide-b-round4-brief.md, after decide-b-review3.md).
 * The backend port mirrors every test here in backend/tests/test_profile_facts_round4.py.
 *
 * - A pending, applied-for, lottery, waiting or eligible status is never held.
 * - On a labelled multi-line answer, one country's line never changes another's.
 * - Every pattern on user text is linear: whitespace runs are collapsed first, and
 *   100,000 spaces, 100,000 punctuation characters and a 50,000-character answer
 *   each read in under 200 ms.
 */
import { beforeAll, describe, expect, it } from "vitest";
import { authorizedIn, needsSponsorshipIn, polarityOf, workAuthFacts } from "../src/content/profileFacts";
import type { UserApplicationProfile } from "../src/shared/types";

const auth = (wa: string, rs = "") => workAuthFacts({ workAuthorization: wa, requiresSponsorship: rs } as UserApplicationProfile);

describe("a pending, applied-for, lottery, waiting or eligible status is never held", () => {
  const PENDING = [
    "I-485 pending",
    "I485 pending",
    "green card pending",
    "Green card - pending",
    "green card (pending)",
    "Green card in process",
    "green card application in progress",
    "Waiting on my green card",
    "GC pending",
    "H-1B lottery pending",
    "H-1B lottery",
    "In the H1B lottery",
    "Entered the H-1B lottery this year",
    "Applying for PR",
    "Applied for PR",
    "PR - pending",
    "PR (pending)",
    "PR application submitted",
    "Eligible for PR next year",
    "Canadian citizen, TN-eligible",
    "Canadian citizen, can get a TN",
    "Canadian citizen; would relocate to the US on a TN",
    "Canadian citizen. Plan to get TN for US jobs.",
    "Canadian citizen; open to relocating to the US with TN",
    "Planning to apply for a TN",
    "Could get an H-1B",
    "Can obtain TN status",
  ];
  for (const text of PENDING) {
    it(JSON.stringify(text), () => {
      const a = auth(text);
      expect(authorizedIn(a, "US", null)?.value).not.toBe(true);
      expect(a.byCountry.get("US")?.basis).not.toBe("permanent_resident");
      expect(a.byCountry.get("CA")?.basis ?? null).not.toBe("permanent_resident");
      expect(authorizedIn(a, null, null)?.value ?? null).not.toBe(true);
      if (!/canadian citizen/i.test(text)) expect(authorizedIn(a, "CA", "CA")?.value ?? null).not.toBe(true);
    });
  }
  it("a held status next to a pending one is still held", () => {
    expect(auth("On H-1B, green card pending").byCountry.get("US")).toMatchObject({ authorized: true, needsSponsorship: true, basis: "work_permit" });
    expect(auth("I-485 pending, EAD in hand").byCountry.get("US")).toMatchObject({ authorized: true, basis: "work_permit" });
  });
});

describe("on a labelled multi-line answer, one country's line never changes another's", () => {
  const LINES: Array<[string, string, string]> = [
    ["Canada: citizen\nUSA: needs sponsorship", "citizen", "statement"],
    ["Canada: Citizen\nUS: Requires sponsorship", "citizen", "statement"],
    ["Canada: PR\nUS: need visa", "permanent_resident", "statement"],
    ["Canada: permanent resident\nUS: would need a visa", "permanent_resident", "statement"],
  ];
  for (const [text, caBasis] of LINES) {
    it(JSON.stringify(text), () => {
      const a = auth(text);
      expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, needsSponsorship: false, basis: caBasis });
      expect(needsSponsorshipIn(a, "CA", null)?.value).toBe(false);
      expect(needsSponsorshipIn(a, "US", null)?.value).toBe(true);
      expect(authorizedIn(a, "US", null)?.value ?? null).not.toBe(true);
    });
  }
  it("a US line's pending status never reaches the Canada line", () => {
    const a = auth("Canada: open work permit\nUS: green card pending");
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "work_permit" });
    expect(authorizedIn(a, "US", null)?.value ?? null).not.toBe(true);
  });
});

describe("answers the review read as unknown", () => {
  it("informal yes and no, and a question echoed before the answer", () => {
    expect(polarityOf("Nope")).toBe(false);
    expect(polarityOf("yep")).toBe(true);
    expect(polarityOf("Do you require sponsorship? No I do not")).toBe(false);
    expect(polarityOf("Do you now or in the future require sponsorship? No.")).toBe(false);
  });
  it("a leading No that a sponsorship need confirms is a No; one moved to later is not", () => {
    expect(authorizedIn(auth("No, I need sponsorship"), null, null)?.value).toBe(false);
    expect(authorizedIn(auth("No - I will require sponsorship"), "CA", "CA")?.value).toBe(false);
    expect(authorizedIn(auth("No, but I will need sponsorship later"), null, null)).toBeNull();
  });
  it("'N/A - citizen' does not block the single-country inference", () => {
    expect(needsSponsorshipIn(auth("US citizen", "N/A - citizen"), null, null)?.value).toBe(false);
  });
});

// --- timing --------------------------------------------------------------------------------
const PUNCT = ",.;:-/()&+!?|[]'\"*#@%=_~<>";
const punctuation = (n: number): string => PUNCT.repeat(Math.ceil(n / PUNCT.length)).slice(0, n);
const MIXED = "US: - / ( ) , ; . Canada ? | & + ";
const REALISTIC =
  "Hi! I'm a 4th year CS student at UofT.\nCitizenship: Canadian\nCanada: citizen\nUSA: needs sponsorship\n" +
  "I am a Canadian citizen, not authorized in the U.S. (would need TN). Green card - pending. " +
  "H-1B lottery pending; open to relocating to the US with TN. Canada - yes / US - no.\n";
const TIMING_INPUTS: Record<string, string> = {
  "100,000 spaces": " ".repeat(100_000),
  "100,000 spaces inside a status": "Canadian" + " ".repeat(100_000) + "citizen",
  "100,000 spaces after a country": "US" + " ".repeat(100_000) + "x",
  "100,000 mixed whitespace": "Canada \t\n ".repeat(10_000).slice(0, 100_000),
  "100,000 punctuation characters": punctuation(100_000),
  "100,000 punctuation characters between countries": "US" + punctuation(100_000) + "Canada",
  "100,000 punctuation and countries": MIXED.repeat(Math.ceil(100_000 / MIXED.length)).slice(0, 100_000),
  "a 50,000-character realistic answer": REALISTIC.repeat(Math.ceil(50_000 / REALISTIC.length)).slice(0, 50_000),
};

describe("timing: every pattern on user text is linear", () => {
  beforeAll(() => {
    auth("Canadian citizen; US: no", "No");
  });
  for (const [name, text] of Object.entries(TIMING_INPUTS)) {
    it(`${name} reads in under 200 ms`, () => {
      const t0 = performance.now();
      const a = auth(text, text);
      authorizedIn(a, "US", null);
      needsSponsorshipIn(a, "CA", null);
      polarityOf(text);
      expect(performance.now() - t0).toBeLessThan(200);
    });
  }
});
