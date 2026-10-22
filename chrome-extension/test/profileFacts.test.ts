/**
 * The deterministic inference layer (profileFacts.ts): facts derived from the
 * profile, each with the confidence that decides whether it may fill a field.
 */
import { describe, expect, it } from "vitest";
import {
  authorizedIn,
  availabilityFacts,
  educationFacts,
  employmentFacts,
  locationFacts,
  needsSponsorshipIn,
  noticeToDays,
  parseAddress,
  parseDateSpan,
  workAuthFacts,
} from "../src/content/profileFacts";
import type { UserApplicationProfile } from "../src/shared/types";

const TODAY = new Date(Date.UTC(2026, 9, 3, 12)); // 2026-10-03, noon UTC: that day in every timezone

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

describe("parseAddress", () => {
  it("splits a full Canadian address", () => {
    const a = parseAddress("1055 W Georgia St, Vancouver, BC V6E 3P3, Canada");
    expect(a.street).toBe("1055 W Georgia St");
    expect(a.city).toBe("Vancouver");
    expect(a.region?.code).toBe("BC");
    expect(a.postal?.code).toBe("V6E 3P3");
    expect(a.country?.code).toBe("CA");
  });

  it("splits a US address with an apartment and a ZIP after the state", () => {
    const a = parseAddress("77 Massachusetts Ave, Apt 4B, Cambridge, MA 02139");
    expect(a.street).toBe("77 Massachusetts Ave, Apt 4B");
    expect(a.city).toBe("Cambridge");
    expect(a.region?.name).toBe("Massachusetts");
    expect(a.postal?.code).toBe("02139");
  });

  it("does not take a five-digit street number for the ZIP", () => {
    expect(parseAddress("12345 Main St, Springfield, IL 62701").postal?.code).toBe("62701");
  });

  it("reads a city, province, country location string", () => {
    const a = parseAddress("Toronto, ON, Canada");
    expect(a.city).toBe("Toronto");
    expect(a.region?.name).toBe("Ontario");
    expect(a.country?.name).toBe("Canada");
  });
});

describe("locationFacts", () => {
  it("derives city/province/country from the location string alone, at high confidence", () => {
    const f = locationFacts(profile({ location: "Toronto, ON, Canada" }));
    expect(f.city).toMatchObject({ value: "Toronto", confidence: "high" });
    expect(f.region?.value.code).toBe("ON");
    expect(f.country).toMatchObject({ confidence: "high" });
    expect(f.country?.value.name).toBe("Canada");
  });

  it("derives the country from a province code ('Ottawa, ON')", () => {
    const f = locationFacts(profile({ location: "Ottawa, ON" }));
    expect(f.country?.value.code).toBe("CA");
    expect(f.country?.confidence).toBe("high");
  });

  it("a bare city is only a MEDIUM hint for its country, and a medium city", () => {
    const f = locationFacts(profile({ location: "Toronto" }));
    expect(f.country?.confidence).toBe("medium");
    expect(f.city?.confidence).toBe("medium");
  });

  it("a street with no civic number up front stays whole: its locality is no city (Bengaluru, Profound Medical on BambooHR, live, round 8)", () => {
    // "14, 5th Cross, Indiranagar": the house number apart, then the cross
    // road and the neighbourhood. Read as a city, "Indiranagar" took the
    // street with it, and "Address *" was left blank.
    const f = locationFacts(profile({ addressStreet: "14, 5th Cross, Indiranagar", addressCity: "Bengaluru", addressState: "Karnataka", postalCode: "560038", country: "India" }));
    expect(f.street?.value).toBe("14, 5th Cross, Indiranagar");
    expect(f.city?.value).toBe("Bengaluru");
  });

  it("splits a one-line street address into its parts", () => {
    const f = locationFacts(profile({ addressStreet: "1055 W Georgia St, Vancouver, BC V6E 3P3, Canada" }));
    expect(f.street?.value).toBe("1055 W Georgia St");
    expect(f.city?.value).toBe("Vancouver");
    expect(f.region?.value.code).toBe("BC");
    expect(f.postalCode?.value).toBe("V6E 3P3");
    expect(f.country?.value.code).toBe("CA");
  });

  it("stated structured fields win over parsed ones", () => {
    const f = locationFacts(profile({ location: "Toronto, ON, Canada", addressCity: "Mississauga", country: "Canada" }));
    expect(f.city?.value).toBe("Mississauga");
    expect(f.country?.source).toBe("profile:country");
  });

  it("never reads 'Remote' as a city", () => {
    expect(locationFacts(profile({ location: "Remote" })).city).toBeNull();
  });
});

describe("workAuthFacts / authorizedIn / needsSponsorshipIn", () => {
  it("a Canadian citizen is authorized in Canada with no sponsorship, and UNKNOWN for the US", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Canadian citizen" }));
    expect(authorizedIn(a, "CA", "CA")).toMatchObject({ value: true, confidence: "high" });
    expect(needsSponsorshipIn(a, "CA", "CA")).toMatchObject({ value: false, confidence: "high" });
    expect(authorizedIn(a, "US", "CA")).toBeNull();
    expect(needsSponsorshipIn(a, "US", "CA")).toBeNull();
  });

  it("'Authorized to work in Canada' says nothing about the United States", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Authorized to work in Canada", requiresSponsorship: "No" }));
    expect(authorizedIn(a, "US", "CA")).toBeNull();
    expect(authorizedIn(a, "CA", "CA")?.value).toBe(true);
    // The applicant's own "No" covers the country their status names, and an
    // unscoped question, but not a country they never mentioned.
    expect(needsSponsorshipIn(a, "CA", "CA")?.value).toBe(false);
    expect(needsSponsorshipIn(a, null, "CA")).toMatchObject({ value: false, confidence: "high" });
    expect(needsSponsorshipIn(a, "US", "CA")).toBeNull();
  });

  it("reads a two-country statement", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Authorized to work in Canada and the U.S." }));
    expect(authorizedIn(a, "US", "CA")?.value).toBe(true);
    expect(authorizedIn(a, "CA", "CA")?.value).toBe(true);
  });

  it("a green card holder needs no US sponsorship", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Green card holder" }));
    expect(authorizedIn(a, "US", "US")?.value).toBe(true);
    expect(needsSponsorshipIn(a, "US", "US")?.value).toBe(false);
  });

  it("an F-1 student's authorization is unknown, but their stated sponsorship answer stands", () => {
    const a = workAuthFacts(profile({ workAuthorization: "F-1 student visa (OPT eligible)", requiresSponsorship: "Yes" }));
    expect(authorizedIn(a, "US", "US")).toBeNull();
    expect(needsSponsorshipIn(a, "US", "US")).toMatchObject({ value: true, confidence: "high" });
  });

  it("an H-1B holder is authorized and needs sponsorship", () => {
    const a = workAuthFacts(profile({ workAuthorization: "H-1B" }));
    expect(authorizedIn(a, "US", "US")?.value).toBe(true);
    expect(needsSponsorshipIn(a, "US", "US")?.value).toBe(true);
  });

  it("a bare 'yes' applies to the applicant's own country and to unscoped questions only", () => {
    const a = workAuthFacts(profile({ workAuthorization: "yes" }));
    expect(authorizedIn(a, null, "CA")?.value).toBe(true);
    expect(authorizedIn(a, "CA", "CA")?.value).toBe(true);
    expect(authorizedIn(a, "US", "CA")).toBeNull();
  });

  it("a negative statement is a No", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Not authorized to work in the US" }));
    expect(authorizedIn(a, "US", "CA")?.value).toBe(false);
  });

  it("'North America' is not the United States", () => {
    const a = workAuthFacts(profile({ workAuthorization: "Authorized to work in North America" }));
    expect(a.byCountry.has("US")).toBe(false);
  });
});

describe("employmentFacts", () => {
  const rows = [
    { company: "Shopify", title: "Software Developer Intern", startDate: "2025-01", endDate: "2025-04", description: "" },
    { company: "Kinaxis", title: "Software Engineer Co-op", startDate: "2025-09", endDate: "Present", description: "" },
  ];

  it("the row ending 'Present' is the current employer and title", () => {
    const e = employmentFacts(profile({ experience: rows }), TODAY);
    expect(e.currentCompany).toMatchObject({ value: "Kinaxis", confidence: "high" });
    expect(e.currentTitle?.value).toBe("Software Engineer Co-op");
    expect(e.currentlyEmployed?.value).toBe(true);
  });

  it("totals experience across rows, as fractional years", () => {
    const e = employmentFacts(profile({ experience: rows }), TODAY);
    expect(e.totalYears?.confidence).toBe("high");
    expect(e.totalYears!.value).toBeGreaterThan(1.3);
    expect(e.totalYears!.value).toBeLessThan(1.5);
  });

  it("counts overlapping roles once", () => {
    const e = employmentFacts(
      profile({
        experience: [
          { company: "A", title: "x", startDate: "2020-01", endDate: "2022-12", description: "" },
          { company: "B", title: "y", startDate: "2021-01", endDate: "2022-12", description: "" },
        ],
      }),
      TODAY
    );
    expect(Math.round(e.totalYears!.value)).toBe(3);
  });

  it("an undatable row makes the total only MEDIUM (it is a lower bound)", () => {
    const e = employmentFacts(profile({ experience: [...rows, { company: "C", title: "z", startDate: "", endDate: "", description: "" }] }), TODAY);
    expect(e.totalYears?.confidence).toBe("medium");
  });

  it("a stated years-of-experience wins", () => {
    expect(employmentFacts(profile({ yearsOfExperience: "5", experience: rows }), TODAY).totalYears?.value).toBe(5);
  });

  it("no current row: no current company inferred", () => {
    const e = employmentFacts(profile({ experience: [rows[0]] }), TODAY);
    expect(e.currentCompany).toBeNull();
    expect(e.mostRecentCompany?.value).toBe("Shopify");
  });
});

describe("educationFacts", () => {
  it("a 2027 graduation is in progress in October 2026", () => {
    const e = educationFacts(
      profile({ education: [{ school: "University of Waterloo", degree: "Bachelor of Applied Science in Mechatronics Engineering", graduationYear: "2027" }] }),
      TODAY
    );
    expect(e.currentlyEnrolled?.value).toBe(true);
    expect(e.highestRank?.value).toBe(4);
    // Nothing finished yet: a known "none", which answers "Do you have a degree?" No.
    expect(e.highestCompletedRank).toMatchObject({ value: 0, confidence: "high" });
    expect(e.primary?.school).toBe("University of Waterloo");
  });

  it("a 2026 graduation is complete by October 2026", () => {
    const e = educationFacts(profile({ education: [{ school: "uOttawa", degree: "BSc Computer Science", graduationYear: "2026" }] }), TODAY);
    expect(e.highestCompletedRank?.value).toBe(4);
  });

  it("a master's in progress over a finished bachelor's", () => {
    const e = educationFacts(
      profile({
        education: [
          { school: "MIT", degree: "Master of Science in Computer Science", graduationYear: "2027" },
          { school: "UofT", degree: "BASc", graduationYear: "2024" },
        ],
      }),
      TODAY
    );
    expect(e.highestRank?.value).toBe(5);
    expect(e.highestCompletedRank?.value).toBe(4);
    expect(e.primary?.school).toBe("MIT");
  });
});

describe("availability", () => {
  it("notice period → earliest start date", () => {
    const a = availabilityFacts(profile({ noticePeriod: "2 weeks" }), TODAY);
    expect(a.noticeDays?.value).toBe(14);
    expect(a.earliestStart?.value.toISOString().slice(0, 10)).toBe("2026-10-17");
    expect(a.earliestStart?.confidence).toBe("high");
  });

  it("noticeToDays reads common phrasings", () => {
    expect(noticeToDays("Immediately")).toBe(0);
    expect(noticeToDays("1 month")).toBe(30);
    expect(noticeToDays("3 weeks")).toBe(21);
    expect(noticeToDays("whenever")).toBeNull();
  });

  it("a stated start date in the past means now", () => {
    const a = availabilityFacts(profile({ earliestStartDate: "2026-09-01" }), TODAY);
    expect(a.earliestStart?.value.toISOString().slice(0, 10)).toBe("2026-10-03");
  });
});

describe("parseDateSpan", () => {
  it("refuses an ambiguous day/month order", () => {
    expect(parseDateSpan("03/04/1998")).toBeNull();
  });
  it("reads month names", () => {
    expect(parseDateSpan("June 2024")?.precision).toBe("month");
  });
});
