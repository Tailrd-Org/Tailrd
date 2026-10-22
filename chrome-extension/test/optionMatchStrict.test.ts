/**
 * Regression tests for option matching: a choice control gets one of its REAL
 * options, and only when the match is confident. "The closest-looking option"
 * is not an answer: when two options fit equally, or the only fit contradicts
 * the answer's yes/no polarity, the field is left blank.
 *
 * Bugs pinned here (2026-10-03 overnight run):
 *  - token overlap ignored negation: "I am not a protected veteran" scored 100%
 *    on BOTH "I am a protected veteran" and "I am not a veteran", so option
 *    ORDER decided the answer (the in-page harness showed the tie).
 *  - the multi-word substring tier matched characters inside words: "no" is
 *    inside "I do NOt know", so an "I do not know" answer selected "No".
 *  - ties were broken by option order instead of being refused.
 *  - a non-yes/no value could land on a yes/no choice through a shared word
 *    ("Quebec" → "Yes, I live in Quebec").
 */
import { describe, expect, it } from "vitest";
import { matchOption } from "../src/content/writeEngine";

const pick = (options: string[], target: string): string | null =>
  matchOption(options, (o) => o, (o) => o, target);

describe("matchOption: polarity", () => {
  it("never picks the opposite yes/no meaning, whatever the option order", () => {
    const veteran = ["I am a protected veteran", "I am not a veteran", "Decline to self-identify"];
    expect(pick(veteran, "I am not a protected veteran")).toBe("I am not a veteran");
    expect(pick([...veteran].reverse(), "I am not a protected veteran")).toBe("I am not a veteran");
  });

  it("maps a bare Yes/No onto sentence options by polarity", () => {
    const opts = ["Yes, I am legally authorized", "No, I am not legally authorized"];
    expect(pick(opts, "Yes")).toBe("Yes, I am legally authorized");
    expect(pick(opts, "No")).toBe("No, I am not legally authorized");
  });

  it("reads 'I do not require' style options as No", () => {
    const opts = ["I will require sponsorship", "I do not require sponsorship"];
    expect(pick(opts, "No")).toBe("I do not require sponsorship");
    expect(pick(opts, "Yes")).toBe("I will require sponsorship");
  });

  it("does not read 'Prefer not to say' as a No", () => {
    expect(pick(["Yes", "Prefer not to say"], "No")).toBeNull();
  });

  it("does not pick 'Maybe' for a Yes", () => {
    expect(pick(["Maybe So", "Not really"], "Yes")).toBeNull();
  });

  it("a non-yes/no value never lands on a yes/no choice through a shared word", () => {
    expect(pick(["Yes, I live in Quebec", "No, I do not live in Quebec"], "Quebec")).toBeNull();
    expect(pick(["YES", "NO"], "Toronto, ON, Canada")).toBeNull();
  });
});

describe("matchOption: containment is word-based", () => {
  it("'no' inside 'not' is not the option No", () => {
    expect(pick(["Yes", "No"], "I do not know")).toBeNull();
  });

  it("prefers the most specific contained option over the first one", () => {
    expect(pick(["Intern", "Engineer", "Software Engineer"], "Software Engineer Intern")).toBe("Software Engineer");
  });
});

describe("matchOption: ambiguity is refused", () => {
  it("refuses a tie between two equally good options", () => {
    expect(pick(["Toronto, ON", "Toronto, OH"], "Toronto")).toBeNull();
  });

  it("refuses a number sitting on the boundary of two buckets", () => {
    expect(pick(["0-1 year", "1-3 years", "4-6 years", "Over 6 years"], "1")).toBeNull();
  });

  it("places an unambiguous number in its bucket", () => {
    expect(pick(["0-1 year", "1-3 years", "4-6 years", "Over 6 years"], "1.42")).toBe("1-3 years");
    expect(pick(["0-1 year", "1-3 years", "4-6 years", "Over 6 years"], "8")).toBe("Over 6 years");
  });

  it("refuses a bare year inside several options, whatever words they add (Ashby, live 2026-10-03)", () => {
    // Graduation year "2027" picked "December 2027" for having fewer extra
    // words than "January - June 2027": a coin flip on the month.
    const opts = ["2026", "January - June 2027", "December 2027", "May/June 2028", "December 2028", "2029"];
    expect(pick(opts, "2027")).toBeNull();
    expect(pick(opts, "2026")).toBe("2026");
    expect(pick(opts, "2029")).toBe("2029");
    expect(pick(["Class of 2026", "Class of 2027", "Class of 2028"], "2027")).toBe("Class of 2027");
  });

  it("a word an option repeats is shared once (DV Trading on Greenhouse, round 8's final regression)", () => {
    // "June 2026" shared "2026" twice with "August 2026 - December 2026",
    // two words of four: a June graduate took a range that ends without
    // June. Left blank, the list's real options go back to the resolver.
    const opts = ["I've already graduated", "August 2026 - December 2026", "January 2027 - July 2027", "August 2027 - December 2027", "After January 2029"];
    expect(pick(opts, "June 2026")).toBeNull();
    expect(pick(opts, "August 2026 - December 2026")).toBe("August 2026 - December 2026");
  });
});

describe("matchOption: unchanged behaviour", () => {
  it("exact and case-insensitive matches still win", () => {
    expect(pick(["Male", "Female"], "Male")).toBe("Male");
    expect(pick(["YES", "NO"], "Yes")).toBe("YES");
    expect(pick(["Canada", "United States"], "canada")).toBe("Canada");
  });

  it("a single-word answer still finds its option", () => {
    expect(pick(["Bachelor's Degree", "Master's Degree"], "Bachelor")).toBe("Bachelor's Degree");
  });

  it("a morphological variant still matches ('Canada' → 'Canadian')", () => {
    expect(pick(["American", "Canadian", "Mexican"], "Canada")).toBe("Canadian");
  });

  it("two words sharing a stem are not variants (Greenhouse discipline, live 2026-10-03)", () => {
    // "mecha" made "Mechanical Engineering" a PERFECT match for a Mechatronics
    // student, and it was committed.
    const disciplines = ["Chemical Engineering", "Electrical Engineering", "Mechanical Engineering", "Computer Science"];
    expect(pick(disciplines, "Mechatronics Engineering")).toBeNull();
    expect(pick(["Software Engineer", "Engineering Manager"], "Software Engineering")).toBe("Software Engineer");
  });
});
