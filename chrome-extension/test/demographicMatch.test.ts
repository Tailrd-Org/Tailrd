import { describe, it, expect } from "vitest";
import { closestDemographicOption } from "../src/content/demographicMatch";

const RACE = ["White", "Black or African American", "Asian", "Hispanic or Latino", "Two or More Races", "Prefer Not to Say"];

describe("closestDemographicOption", () => {
  it("maps Arab to a MENA option when offered", () => {
    const opts = [...RACE, "Middle Eastern or North African"];
    expect(closestDemographicOption("eeoRace", "Arab", opts)).toBe("Middle Eastern or North African");
  });
  it("maps Arab to White when no MENA option exists", () => {
    expect(closestDemographicOption("eeoRace", "Arab", RACE)).toBe("White");
  });
  it("an answer nothing matches is left to the applicant, never declined for them (round 3, 2026-10-03)", () => {
    expect(closestDemographicOption("eeoRace", "Klingon", RACE)).toBeNull();
    expect(closestDemographicOption("eeoRace", "Prefer not to say", RACE)).toBe("Prefer Not to Say");
  });
  it("returns null when there is no match and no decline option", () => {
    expect(closestDemographicOption("eeoRace", "Klingon", ["White", "Asian"])).toBeNull();
  });
  it("maps Woman to Female", () => {
    expect(closestDemographicOption("eeoGender", "Woman", ["Male", "Female", "Non-binary"])).toBe("Female");
  });
});

/**
 * REGRESSION (2026-10-03): the matcher tested SUBSTRINGS, so "male" was found
 * inside "female" and "man" inside "woman", and whichever option came first
 * won. Most forms list Female / Woman first, so a male applicant got Female.
 * Same bug class as the driver's old pickOption (optionMatch.ts).
 */
describe("closestDemographicOption: whole words only", () => {
  it("never reads Male as Female, or Man as Woman, whatever the option order", () => {
    expect(closestDemographicOption("eeoGender", "Male", ["Female", "Male", "Decline to self-identify"])).toBe("Male");
    expect(closestDemographicOption("eeoGender", "Man", ["Woman", "Man", "Non-binary"])).toBe("Man");
    expect(closestDemographicOption("eeoGender", "man", ["Woman", "Male", "I prefer not to say"])).toBe("Male");
  });
});

/**
 * REGRESSION (a real profile on Robinhood, 2026-10-03): "Asian" became "East
 * Asian" because the first option holding the word won. An answer broader
 * than every option it fits names none of them, so the field stays blank and
 * the user picks. Options verbatim from Superhuman's race question (Ashby).
 */
describe("closestDemographicOption: an answer broader than the options", () => {
  const SUPERHUMAN = ["I don't wish to answer", "I prefer to self-describe", "White or European", "Southeast Asian", "South Asian", "Native Hawaiian or Pacific Islander", "Middle Eastern or North African", "Indigenous, American Indian or Alaska Native", "Hispanic, Latinx or of Spanish Origin", "East Asian", "Black or of African descent"];
  it("never picks one subgroup of 'Asian', and never declines for the user either", () => {
    expect(closestDemographicOption("eeoRace", "Asian", SUPERHUMAN)).toBeNull();
  });
  it("an exact option beats a narrower one listed before it", () => {
    expect(closestDemographicOption("eeoRace", "Asian", ["East Asian", "Asian", "White"])).toBe("Asian");
  });
  it("the one option holding the answer is still the answer", () => {
    expect(closestDemographicOption("eeoRace", "Asian", ["White (Not Hispanic or Latino)", "Asian (Not Hispanic or Latino)", "Decline to self-identify"])).toBe("Asian (Not Hispanic or Latino)");
    expect(closestDemographicOption("eeoDisability", "No, I do not have a disability", ["Yes, I have a disability (or previously had a disability)", "No, I do not have a disability and have not had one in the past", "I do not want to answer"])).toBe("No, I do not have a disability and have not had one in the past");
  });
  it("a synonym held by several options is no answer: 'Male' is not 'Cisgender man'", () => {
    expect(closestDemographicOption("eeoGenderIdentity", "Male", ["Cisgender man", "Cisgender woman", "Transgender man", "Transgender woman", "Non-binary"])).toBeNull();
    // The option that IS the synonym still wins over a qualified one.
    expect(closestDemographicOption("eeoGenderIdentity", "Male", ["Transgender man", "Man", "Woman"])).toBe("Man");
  });
});

/**
 * Superhuman on Ashby with a non-binary applicant (round 3, 2026-10-03):
 * "Gay or Lesbian" ticked "Lesbian" (the longer of two options inside it), and
 * a race the list does not offer ("Two or More Races" on a mark-all-that-apply
 * list) was DECLINED on the applicant's behalf. A disjunction names no single
 * option; a stated answer is never turned into a decline.
 */
describe("closestDemographicOption: a stated answer the options do not settle", () => {
  const ORIENTATION = ["I don't wish to answer", "I prefer to self-describe", "Queer", "Lesbian", "Gay", "Bisexual and/or pansexual", "Asexual", "Heterosexual"];
  it("'Gay or Lesbian' picks neither", () => {
    expect(closestDemographicOption("eeoSexualOrientation", "Gay or Lesbian", ORIENTATION)).toBeNull();
  });
  it("a stated race the list lacks is left to the applicant, not declined", () => {
    const SUPERHUMAN = ["I don't wish to answer", "I prefer to self-describe", "White or European", "Southeast Asian", "South Asian", "Native Hawaiian or Pacific Islander", "Middle Eastern or North African", "Indigenous, American Indian or Alaska Native", "Hispanic, Latinx or of Spanish Origin", "East Asian", "Black or of African descent"];
    expect(closestDemographicOption("eeoRace", "Two or More Races", SUPERHUMAN)).toBeNull();
  });
  it("a stated decline still finds the list's decline option", () => {
    expect(closestDemographicOption("eeoRace", "Prefer not to say", ["White", "Asian", "I don't wish to answer"])).toBe("I don't wish to answer");
  });
});

/**
 * Pronouns are a set, however long it is written: "She/Her" is "She/her/hers",
 * never "She/her/hers, they/them/theirs", which adds a set nobody stated.
 * Rippling's list (live, round 8: blank for all three personas, as both
 * options hold "she her"; the widget's own matcher had picked it before the
 * self-ID rules ran there).
 */
describe("closestDemographicOption: pronouns", () => {
  const RIPPLING = ["She/her/hers", "He/him/his", "They/them/theirs", "She/her/hers, they/them/theirs", "He/him/his, they/them/theirs", "Just use my name"];
  it("a stated set is the option of that set alone", () => {
    expect(closestDemographicOption("eeoPronouns", "She/Her", RIPPLING)).toBe("She/her/hers");
    expect(closestDemographicOption("eeoPronouns", "He/Him", RIPPLING)).toBe("He/him/his");
    expect(closestDemographicOption("eeoPronouns", "They/Them", RIPPLING)).toBe("They/them/theirs");
  });
  it("two sets stated are the option of both", () => {
    expect(closestDemographicOption("eeoPronouns", "She/They", RIPPLING)).toBe("She/her/hers, they/them/theirs");
  });
  it("a set no option names is no answer; the option written as stated comes first", () => {
    expect(closestDemographicOption("eeoPronouns", "Xe/Xem", RIPPLING)).toBeNull();
    expect(closestDemographicOption("eeoPronouns", "She/Her", ["She/Her", "She/Her/Hers", "He/Him"])).toBe("She/Her");
  });
});
