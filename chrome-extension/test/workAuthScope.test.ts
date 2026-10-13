/**
 * Owner decisions batch B (2026-10-13): three autofill bugs in the profile
 * facts layer.
 *   1. A negated country ("Canada but not in the US") never counts as authorized.
 *   2. "N/A" is unanswered, never "No".
 *   3. A city named like an Object.prototype key ("constructor") must not crash geo.ts.
 */
import { describe, expect, it } from "vitest";
import { authorizedIn, locationFacts, needsSponsorshipIn, polarityOf, workAuthFacts } from "../src/content/profileFacts";
import { countryHintForCity, regionHintForCity, sameMetro } from "../src/content/geo";
import type { UserApplicationProfile } from "../src/shared/types";

function profile(over: Partial<UserApplicationProfile> = {}): UserApplicationProfile {
  return {
    firstName: "", lastName: "", email: "", phone: "", location: "",
    addressStreet: "", addressCity: "", addressState: "", postalCode: "", country: "",
    linkedin: "", github: "", portfolio: "", currentCompany: "", currentTitle: "",
    workAuthorization: "", requiresSponsorship: "", dateOfBirth: "",
    education: [], experience: [], skills: [], coverLetter: "",
    ...over,
  };
}

describe("a negated country is never authorized", () => {
  const cases: Array<[string, boolean | null]> = [
    // [statement, what the US must read as]
    ["Authorized to work in Canada but not in the US", false],
    ["Canada but not in the US", false],
    ["Authorized in Canada, not the US", false],
    ["Authorized to work in Canada, except the US", false],
    ["Yes, in Canada but not the United States", false],
    ["Authorized to work only in Canada", null],
    ["Only in Canada", null],
  ];
  for (const [text, us] of cases) {
    it(`"${text}": Canada yes, the US ${us === false ? "no" : "unknown"}`, () => {
      const a = workAuthFacts(profile({ workAuthorization: text }));
      expect(authorizedIn(a, "CA", "CA")?.value).toBe(true);
      const usAuth = authorizedIn(a, "US", "CA");
      if (us === null) expect(usAuth).toBeNull();
      else expect(usAuth).toMatchObject({ value: false, confidence: "high" });
      expect(a.byCountry.get("US")?.authorized).not.toBe(true);
    });
  }

  it("a denial scoped to one country does not deny the other", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Not authorized to work in the US, but authorized to work in Canada" }));
    expect(authorizedIn(a, "US", "CA")?.value).toBe(false);
    expect(authorizedIn(a, "CA", "CA")?.value).toBe(true);
  });

  it("a negation over two countries joined by 'and' negates both", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Authorized to work in Canada, not in the US and the UK" }));
    expect(authorizedIn(a, "CA", "CA")?.value).toBe(true);
    expect(authorizedIn(a, "US", "CA")?.value).toBe(false);
    expect(authorizedIn(a, "GB", "CA")?.value).toBe(false);
  });

  it("'and' starting a new statement ends the negation", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Not authorized in the US and authorized to work in Canada" }));
    expect(authorizedIn(a, "US", "CA")?.value).toBe(false);
    expect(authorizedIn(a, "CA", "CA")?.value).toBe(true);
  });

  it("a negated sponsorship clause says nothing against the country", () => {
    const a = workAuthFacts(profile({ workAuthorization: "US citizen, not requiring sponsorship in the US" }));
    expect(authorizedIn(a, "US", "US")?.value).toBe(true);
  });

  it("not authorized in the US means sponsorship is needed there", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Canadian citizen, not authorized in the US" }));
    expect(authorizedIn(a, "CA", "CA")).toMatchObject({ value: true });
    expect(a.byCountry.get("CA")?.basis).toBe("citizen");
    expect(needsSponsorshipIn(a, "US", "CA")?.value).toBe(true);
  });

  it("'not a US citizen' alone leaves US authorization unknown, never authorized", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Canadian citizen, not a US citizen" }));
    expect(authorizedIn(a, "CA", "CA")?.value).toBe(true);
    expect(a.byCountry.get("US")?.authorized).not.toBe(true);
  });

  it("is linear on a long run of negations (no clause runs past the next negation)", () => {
    const text = "Not authorized in the US ".repeat(2000);
    const t0 = performance.now();
    const a = workAuthFacts(profile({ workAuthorization: text }));
    expect(performance.now() - t0).toBeLessThan(1000);
    expect(a.byCountry.get("US")?.authorized).toBe(false);
  });

  it("existing readings are unchanged", () => {
    expect(authorizedIn(workAuthFacts(profile({ workAuthorization: "Not authorized to work in the US" })), "US", "CA")?.value).toBe(false);
    const both = workAuthFacts(profile({ workAuthorization: "Authorized to work in Canada and the U.S." }));
    expect(authorizedIn(both, "US", "CA")?.value).toBe(true);
    expect(authorizedIn(both, "CA", "CA")?.value).toBe(true);
  });
});

describe("N/A is unanswered, never No", () => {
  for (const v of ["N/A", "n/a", "NA", "na", "N.A.", "not applicable", "Not Applicable", "N / A"]) {
    it(`polarityOf("${v}") is null`, () => {
      expect(polarityOf(v)).toBeNull();
    });
  }

  it("a sponsorship answer of N/A states nothing", () => {
    expect(workAuthFacts(profile({ requiresSponsorship: "N/A" })).statedSponsorship).toBeNull();
  });

  it("an explicit per-country N/A does not mark the country unauthorized", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Authorized to work in the US", authorizedUS: "N/A", authorizedCanada: "n/a" } as Partial<UserApplicationProfile>));
    expect(authorizedIn(a, "US", "US")?.value).toBe(true);
    expect(a.byCountry.has("CA")).toBe(false);
  });

  it("a work authorization of N/A is unknown", () => {
    expect(workAuthFacts(profile({ workAuthorization: "N/A" })).unscopedAuthorized).toBeNull();
    expect(workAuthFacts(profile({ workAuthorization: "Not applicable" })).unscopedAuthorized).toBeNull();
  });

  it("No and None still read as No", () => {
    expect(polarityOf("No")).toBe(false);
    expect(polarityOf("n")).toBe(false);
    expect(polarityOf("None required")).toBe(false);
  });
});

describe("a city named like an Object.prototype key", () => {
  for (const city of ["constructor", "Constructor", "__proto__", "toString", "hasOwnProperty", "valueOf"]) {
    it(`"${city}" is an unknown city`, () => {
      expect(countryHintForCity(city)).toBeNull();
      expect(regionHintForCity(city)).toBeNull();
      expect(sameMetro(city, "Toronto")).toBeNull();
      expect(() => locationFacts(profile({ addressCity: city }))).not.toThrow();
      expect(() => locationFacts(profile({ location: city }))).not.toThrow();
    });
  }
});
