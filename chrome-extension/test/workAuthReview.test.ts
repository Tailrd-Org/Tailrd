/**
 * Decide B review round 1 (decide-b-review.md): each reviewer probe is a test.
 * The backend port mirrors these in backend/tests/test_profile_facts.py.
 */
import { describe, expect, it } from "vitest";
import { authorizedIn, needsSponsorshipIn, polarityOf, workAuthFacts } from "../src/content/profileFacts";
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

const auth = (text: string, extra: Partial<UserApplicationProfile> = {}) => workAuthFacts(profile({ workAuthorization: text, ...extra }));
const usOf = (text: string) => authorizedIn(auth(text), "US", "CA");
const caOf = (text: string) => authorizedIn(auth(text), "CA", "CA");

describe("C-1: dotted and spaced US spellings are negated like 'US'", () => {
  for (const us of ["U.S.", "U.S", "U.S.A.", "u.s.", "U. S.", "USA", "United States", "America", "US"]) {
    it(`"Authorized to work in Canada but not in the ${us}"`, () => {
      const text = `Authorized to work in Canada but not in the ${us}`;
      expect(usOf(text)).toMatchObject({ value: false, confidence: "high" });
      expect(caOf(text)?.value).toBe(true);
      expect(needsSponsorshipIn(auth(text, { requiresSponsorship: "No" }), "US", null)?.value).toBe(true);
    });
  }
  it("except the U.S.", () => {
    expect(usOf("Authorized to work in Canada, except the U.S.")?.value).toBe(false);
  });
  it("a citizen 'but not the U.S.' is not a US citizen", () => {
    const a = auth("Canadian citizen, but not the U.S. (would need TN)");
    expect(a.byCountry.get("US")?.authorized).not.toBe(true);
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
  });
  it("a dotted denial does not deny Canada", () => {
    expect(caOf("Not authorized in the U.S. but authorized in Canada")?.value).toBe(true);
    expect(usOf("Not authorized in the U.S. but authorized in Canada")?.value).toBe(false);
    const a = auth("not authorized to work in the U.S., Canadian citizen");
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
    expect(a.byCountry.get("US")?.authorized).toBe(false);
  });
  it("a sentence after 'U.S.' still ends the clause", () => {
    const a = auth("Not authorized in the U.S. I am a Canadian citizen.");
    expect(a.byCountry.get("US")?.authorized).toBe(false);
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
  });
});

describe("I-1: 'not sure' about a country is unknown, never a No", () => {
  for (const text of ["Canadian citizen. Not sure about the US.", "Not sure about the US; Canadian citizen"]) {
    it(text, () => {
      expect(usOf(text)).toBeNull();
      expect(caOf(text)?.value).toBe(true);
    });
  }
});

describe("I-2: uncertain or future sponsorship answers are not a No", () => {
  const uncertain = [
    "Not sure", "Unsure", "Not certain", "Unknown", "TBD", "Don't know",
    "Not currently, but I will in the future", "Not now, but will need H-1B later", "Not yet",
    "Not required now but will be after OPT",
  ];
  for (const v of uncertain) {
    it(`polarityOf("${v}") is null`, () => {
      expect(polarityOf(v)).toBeNull();
      expect(needsSponsorshipIn(auth("", { requiresSponsorship: v }), null, null)).toBeNull();
    });
  }
  it("a plain future No stays No", () => {
    expect(polarityOf("No, I will not need sponsorship in the future")).toBe(false);
    expect(polarityOf("Not required")).toBe(false);
  });
});

describe("I-3: 'not a US citizen' does not erase a green card", () => {
  for (const text of ["Not a US citizen but have a green card", "Green card holder (not a US citizen)", "Not a US citizen; I hold a green card"]) {
    it(text, () => {
      expect(auth(text).byCountry.get("US")).toMatchObject({ authorized: true, basis: "permanent_resident" });
    });
  }
});

describe("I-3 follow-up: a permanent resident who is 'not a citizen' is still authorized", () => {
  for (const text of ["I am a US permanent resident, not a citizen", "Not a citizen, US green card holder"]) {
    it(text, () => {
      expect(auth(text).byCountry.get("US")).toMatchObject({ authorized: true, basis: "permanent_resident" });
    });
  }
  it("'not a citizen' with no other status is still a denial", () => {
    expect(auth("Not a citizen of the US").byCountry.get("US")?.authorized).not.toBe(true);
  });
});

describe("I-4: 'not yet' is a negation", () => {
  it("'not yet authorized' with no country is an unscoped No", () => {
    expect(auth("Not yet authorized").unscopedAuthorized).toBe(false);
  });
  it("Canadian citizen; not yet authorized in the US", () => {
    const a = auth("Canadian citizen; not yet authorized in the US");
    expect(a.byCountry.get("US")?.authorized).toBe(false);
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
  });
  it("Not yet authorized in the US; OPT pending", () => {
    expect(auth("Not yet authorized in the US; OPT pending").byCountry.get("US")?.authorized).toBe(false);
  });
});

describe("I-5: other plain-English negations never leave the US authorized", () => {
  const outright = [
    "Canadian citizen, no US work authorization",
    "Canadian citizen without US work authorization",
    "Canadian citizen. Cannot work in the US.",
    "Canadian citizen, can't work in the US",
    "Canadian citizen, can’t work in the US",
    "Canadian citizen, unable to work in the US",
    "Canadian citizen, ineligible for US employment",
    "Unauthorized in the US, Canadian citizen",
    "Canadian citizen (I don't have US status)",
    "Canadian citizen, US work authorization: no",
    "Citizen of Canada, no authorization for the US",
  ];
  for (const text of outright) {
    it(text, () => {
      expect(usOf(text)).toMatchObject({ value: false, confidence: "high" });
      // "without" in the citizen's own segment vetoes it (decide-b round 5): unknown, never a No.
      if (/without/.test(text)) expect(caOf(text)).toBeNull();
      else expect(caOf(text)?.value).toBe(true);
    });
  }
  it("a clause that needs sponsorship for the US is not a US citizen", () => {
    const a = auth("Canadian citizen; I would require sponsorship to work in the US");
    expect(a.byCountry.get("US")?.authorized).not.toBe(true);
    expect(needsSponsorshipIn(a, "US", "CA")?.value).toBe(true);
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
  });
  it("not needing sponsorship is not a denial", () => {
    expect(auth("I am a Canadian citizen and do not require sponsorship").byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
    expect(auth("Authorized to work in the US without sponsorship").byCountry.get("US")?.authorized).toBe(true);
    expect(auth("Canadian citizen, no sponsorship needed for the US").byCountry.get("US")?.authorized).not.toBe(false);
  });
});

describe("probe follow-ups: sponsorship before the country, pending status", () => {
  it("Authorized in Canada; US requires sponsorship", () => {
    const a = auth("Authorized in Canada; US requires sponsorship");
    expect(a.byCountry.get("US")?.authorized).not.toBe(true);
    expect(needsSponsorshipIn(a, "US", "CA")?.value).toBe(true);
    expect(a.byCountry.get("CA")?.authorized).toBe(true);
  });
  it("Canadian citizen, US authorization pending: the US is unknown", () => {
    const a = auth("Canadian citizen, US authorization pending");
    expect(a.byCountry.has("US")).toBe(false);
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
  });
});

describe("I-6: a denial after the country stays in its clause", () => {
  it("Canadian PR; US not authorized", () => {
    const a = auth("Canadian PR; US not authorized");
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "permanent_resident" });
    expect(a.byCountry.get("US")?.authorized).toBe(false);
  });
});

describe("M-1 / M-2: demonyms and 'not only'", () => {
  it("'not Canadian' is about citizenship, not a Canadian work denial", () => {
    const a = auth("Yes - US citizen (not Canadian)");
    expect(a.byCountry.get("CA")?.authorized).not.toBe(false);
    expect(a.byCountry.get("US")).toMatchObject({ authorized: true, basis: "citizen" });
  });
  it("'Not only Canada, also the US' denies nothing", () => {
    const a = auth("Not only Canada, also the US");
    expect(a.byCountry.get("CA")?.authorized).not.toBe(false);
    expect(a.byCountry.get("US")?.authorized).not.toBe(false);
  });
});

describe("timing", () => {
  const units = ["Not sure about the U.S. ", "Canadian PR; US not authorized; ", "no U.S. status, ", "I would require sponsorship in the U. S.; ", "u.s.a.u.s.a."];
  for (const unit of units) {
    it(`is linear on a long run of "${unit.trim()}"`, () => {
      const text = unit.repeat(Math.ceil(50000 / unit.length));
      const t0 = performance.now();
      workAuthFacts(profile({ workAuthorization: text }));
      polarityOf(text);
      expect(performance.now() - t0).toBeLessThan(1000);
    });
  }
});

describe("a positive status belongs to the countries of its own clause", () => {
  it("Canadian citizen; TN eligible for US: never a US citizen or authorized", () => {
    const a = auth("Canadian citizen; TN eligible for US");
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
    expect(a.byCountry.get("US")?.basis).not.toBe("citizen");
    expect(a.byCountry.get("US")?.authorized).not.toBe(true);
    expect(authorizedIn(a, "US", "CA")?.value).not.toBe(true);
  });
  it("US citizen and Canadian permanent resident", () => {
    const a = auth("US citizen and Canadian permanent resident");
    expect(a.byCountry.get("US")).toMatchObject({ authorized: true, needsSponsorship: false, basis: "citizen" });
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, needsSponsorship: false, basis: "permanent_resident" });
  });
  it("Citizen of Canada, authorized to work in the US via H-1B", () => {
    const a = auth("Citizen of Canada, authorized to work in the US via H-1B");
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
    expect(a.byCountry.get("US")).toMatchObject({ authorized: true, needsSponsorship: true, basis: "work_permit" });
  });
  it("Canadian and American citizen", () => {
    const a = auth("Canadian and American citizen");
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
    expect(a.byCountry.get("US")).toMatchObject({ authorized: true, basis: "citizen" });
  });
  it("Canadian citizen with H-1B in the US (was the 'one status for every country' pin)", () => {
    const a = auth("Canadian citizen with H-1B in the US");
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
    expect(a.byCountry.get("US")).toMatchObject({ authorized: true, needsSponsorship: true, basis: "work_permit" });
    expect(needsSponsorshipIn(a, "US", "CA")?.value).toBe(true);
  });
  it("Canadian citizen not requiring sponsorship in the US: the US is not a citizenship", () => {
    expect(auth("Canadian citizen not requiring sponsorship in the US").byCountry.get("US")?.basis).not.toBe("citizen");
  });
  it("Canadian citizen and US resident: a bare 'resident' is no US status", () => {
    const a = auth("Canadian citizen and US resident");
    expect(a.byCountry.get("CA")).toMatchObject({ authorized: true, basis: "citizen" });
    expect(a.byCountry.get("US")?.basis).not.toBe("citizen");
  });
  it("shared statements still reach every country listed with them", () => {
    for (const text of ["Authorized to work in Canada and the U.S.", "Authorized to work in the US and Canada", "I am a citizen of Canada and the United States"]) {
      const a = auth(text);
      expect(a.byCountry.get("CA")?.authorized).toBe(true);
      expect(a.byCountry.get("US")?.authorized).toBe(true);
    }
    expect(auth("I am a citizen of Canada and the United States").byCountry.get("US")?.basis).toBe("citizen");
  });
  it("'U.S. citizen' and 'U.S.A. Citizen' keep the status with the country", () => {
    for (const text of ["U.S. citizen", "U.S. Citizen", "U.S.A. citizen", "U. S. citizen"]) {
      expect(auth(text).byCountry.get("US")).toMatchObject({ authorized: true, basis: "citizen" });
    }
    expect(auth("U.S. permanent resident (green card)").byCountry.get("US")).toMatchObject({ basis: "permanent_resident" });
  });
  it("an F-1 student with OPT is still unknown in the US", () => {
    expect(auth("F-1 student visa (OPT eligible)").byCountry.get("US")).toMatchObject({ authorized: null, basis: "student" });
  });
  it("is linear on a long run of statuses and countries", () => {
    const text = "Canadian citizen and US green card holder with H-1B via TN / ".repeat(1000);
    const t0 = performance.now();
    auth(text);
    expect(performance.now() - t0).toBeLessThan(1000);
  });
});
