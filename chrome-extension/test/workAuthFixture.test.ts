/**
 * The shared work-authorization fixture (test/fixtures/work-auth-cases.json):
 * every phrasing from the three decide-b review probe sets with its expected
 * reading. backend/tests/test_profile_facts.py grades the Python port against
 * the SAME file with the same rules.
 *
 * Per country (or "null" for a question that names none):
 *   a    authorizedIn:      true / false exact, "nt" never true, "nf" never false, "unk" must be unknown
 *   cit  true: the basis is never a non-citizen one; false: the basis is never "citizen"
 *   sp   needsSponsorshipIn, same codes as a
 * Unknown (null) never counts as wrong, except for "unk". A wrong-YES is an
 * answer more favourable to the applicant than the truth (authorized, citizen,
 * no sponsorship needed); a wrong-NO is the reverse.
 *
 * Every case must be graded clean, except a case marked "gap": "wrong-no",
 * which may still be a wrong-NO (never a wrong-YES).
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
  exp: Record<string, { a?: Exp; cit?: boolean; sp?: Exp }>;
  gap?: "wrong-no";
}

const CASES: Case[] = JSON.parse(readFileSync(join(__dirname, "fixtures", "work-auth-cases.json"), "utf8"));

function judge(exp: Exp | undefined, val: boolean | null, kind: "a" | "sp"): "Y" | "N" | null {
  if (exp === undefined || val === null) return null;
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
    if (e.cit === true && basis && ["permanent_resident", "work_permit", "student", "denied"].includes(basis)) errs.push(`N ${x} basis ${basis}`);
    if (e.cit === false && basis === "citizen") errs.push(`Y ${x} basis citizen`);
    const js = judge(e.sp, sp, "sp");
    if (js) errs.push(`${js} ${x} needsSponsorship=${sp}`);
  }
  return errs;
}

describe("shared work-authorization fixture", () => {
  it("has the 210 reviewer phrasings", () => {
    expect(CASES.length).toBe(210);
  });
  for (const [i, c] of CASES.entries()) {
    it(`#${i} [${c.source}] ${JSON.stringify(c.wa)}${c.rs ? ` rs=${JSON.stringify(c.rs)}` : ""}${c.res ? ` res=${c.res}` : ""}`, () => {
      const errs = gradeCase(c);
      if (c.gap === "wrong-no") expect(errs.filter((e) => e.startsWith("Y"))).toEqual([]);
      else expect(errs).toEqual([]);
    });
  }
  it("totals: zero wrong-yes, wrong-no within the base 642818a count (34)", () => {
    let wrongYes = 0;
    let wrongNo = 0;
    for (const c of CASES) {
      const errs = gradeCase(c);
      if (errs.some((e) => e.startsWith("Y"))) wrongYes++;
      if (errs.some((e) => e.startsWith("N"))) wrongNo++;
    }
    expect(wrongYes).toBe(0);
    expect(wrongNo).toBeLessThanOrEqual(34);
  });
});
