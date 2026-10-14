/**
 * The shared work-authorization fixture (test/fixtures/work-auth-cases.json):
 * every phrasing from the three decide-b review probe sets with its expected
 * reading. backend/tests/test_profile_facts.py grades the Python port against
 * the SAME file with the same rules.
 *
 * Per country (or "null" for a question that names none):
 *   a    authorizedIn:      true / false exact, "nt" never true, "nf" never false, "unk" must be unknown
 *   cit  true: the basis is never a non-citizen one; false: the basis is never "citizen"
 *   pr   true: the basis is never a work permit, student or denied; false: never "permanent_resident"
 *   sp   needsSponsorshipIn, same codes as a
 * Unknown (null) never counts as wrong, except for "unk". A wrong-YES is an
 * answer more favourable to the applicant than the truth (authorized, citizen,
 * no sponsorship needed); a wrong-NO is the reverse.
 *
 * Every case must be graded clean (no wrong-YES, no wrong-NO). "L" marks lost
 * value: a definite expectation (true/false, cit or pr true) read as unknown.
 * It is counted, never failed, and the total may only go down.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { authorizedIn, needsSponsorshipIn, workAuthFacts } from "../src/content/profileFacts";
import type { UserApplicationProfile } from "../src/shared/types";

type Exp = "nt" | "nf" | "unk" | boolean;
interface Case {
  source: string;
  wa: string;
  rs?: string;
  res?: string;
  id?: number;
  exp: Record<string, { a?: Exp; cit?: boolean; pr?: boolean; sp?: Exp }>;
}

const NON_CITIZEN = ["permanent_resident", "work_permit", "student", "denied"];
const NON_PR = ["work_permit", "student", "denied"];
/** Lost value on the whole fixture (467 phrasings) after fix round 5: the veto
 *  lexicon trades value for zero wrong yeses (24 before the round on the same
 *  467; round 4 left 1 on the first 348). */
const LOST_VALUE_CEILING = 49;

const CASES: Case[] = JSON.parse(readFileSync(join(__dirname, "fixtures", "work-auth-cases.json"), "utf8"));

function judge(exp: Exp | undefined, val: boolean | null, kind: "a" | "sp"): "Y" | "N" | "L" | null {
  if (exp === undefined) return null;
  if (val === null) return exp === true || exp === false ? "L" : null;
  const favourable = kind === "a";
  if (exp === "nf") return val === false ? (kind === "a" ? "N" : "Y") : null;
  if (exp === "nt") return val === true ? (kind === "a" ? "Y" : "N") : null;
  if (exp === "unk") return val === favourable ? "Y" : "N";
  if (val === exp) return null;
  return val === favourable ? "Y" : "N";
}

export function gradeCase(c: Case): string[] {
  const profile = { workAuthorization: c.wa, requiresSponsorship: c.rs ?? "" } as UserApplicationProfile;
  const a = workAuthFacts(profile);
  const errs: string[] = [];
  for (const [x, e] of Object.entries(c.exp)) {
    const code = x === "null" ? null : x;
    const av = authorizedIn(a, code, c.res ?? null)?.value ?? null;
    const basis = code ? a.byCountry.get(code)?.basis ?? null : null;
    const sp = needsSponsorshipIn(a, code, c.res ?? null)?.value ?? null;
    const ja = judge(e.a, av, "a");
    if (ja) errs.push(`${ja} ${x} authorized=${av}`);
    if (e.cit === true && basis !== "citizen") errs.push(`${basis && NON_CITIZEN.includes(basis) ? "N" : "L"} ${x} basis ${basis} (citizen expected)`);
    if (e.cit === false && basis === "citizen") errs.push(`Y ${x} basis citizen`);
    if (e.pr === false && basis === "permanent_resident") errs.push(`Y ${x} basis permanent_resident`);
    if (e.pr === true && basis !== "permanent_resident" && basis !== "citizen") errs.push(`${basis && NON_PR.includes(basis) ? "N" : "L"} ${x} basis ${basis} (permanent resident expected)`);
    const js = judge(e.sp, sp, "sp");
    if (js) errs.push(`${js} ${x} needsSponsorship=${sp}`);
  }
  return errs;
}

describe("shared work-authorization fixture", () => {
  it("has the 467 reviewer phrasings", () => {
    expect(CASES.length).toBe(467);
  });
  for (const [i, c] of CASES.entries()) {
    it(`#${i} [${c.source}] ${JSON.stringify(c.wa)}${c.rs ? ` rs=${JSON.stringify(c.rs)}` : ""}${c.res ? ` res=${c.res}` : ""}`, () => {
      expect(gradeCase(c).filter((e) => !e.startsWith("L"))).toEqual([]);
    });
  }
  it("totals: zero wrong-yes, zero wrong-no, lost value within the ceiling", () => {
    let wrongYes = 0;
    let wrongNo = 0;
    let lost = 0;
    for (const c of CASES) {
      const errs = gradeCase(c);
      const y = errs.some((e) => e.startsWith("Y"));
      const n = errs.some((e) => e.startsWith("N"));
      if (y) wrongYes++;
      if (n) wrongNo++;
      if (!y && !n && errs.some((e) => e.startsWith("L"))) lost++;
    }
    expect(wrongYes).toBe(0);
    expect(wrongNo).toBe(0);
    expect(lost).toBeLessThanOrEqual(LOST_VALUE_CEILING);
  });
});
