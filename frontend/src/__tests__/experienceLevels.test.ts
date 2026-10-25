import { describe, it, expect } from "vitest";
import * as fc from "fast-check";
import {
  DEFAULT_EXPERIENCE_LEVELS,
  EXPERIENCE_OPTIONS,
  experienceLevelLabel,
  experienceLevelsOrDefault,
  normalizeExperienceLevels,
} from "../components/JobFilterBar";

const OPTIONS = EXPERIENCE_OPTIONS.map((o) => o.value);

describe("EXPERIENCE_OPTIONS", () => {
  it("are Jobright's six levels, in order, with their exact labels", () => {
    expect(EXPERIENCE_OPTIONS).toEqual([
      { value: "intern_new_grad", label: "Intern/New Grad" },
      { value: "entry", label: "Entry Level" },
      { value: "mid", label: "Mid Level" },
      { value: "senior", label: "Senior Level" },
      { value: "lead", label: "Lead/Staff" },
      { value: "director", label: "Director/Executive" },
    ]);
  });

  it("default to Intern/New Grad plus Entry Level", () => {
    expect(DEFAULT_EXPERIENCE_LEVELS).toEqual(["intern_new_grad", "entry"]);
  });
});

/**
 * Saved filters outlive the options that produced them. A value that survived
 * rehydration without a checkbox would be an invisible filter: it cannot be cleared,
 * and it silently returns zero jobs.
 */
describe("normalizeExperienceLevels", () => {
  it("keeps every current option", () => {
    expect(normalizeExperienceLevels(OPTIONS)).toEqual(OPTIONS);
  });

  it("migrates the two-option values onto the six", () => {
    expect(normalizeExperienceLevels(["internship"])).toEqual(["intern_new_grad"]);
    // "New Grad / Entry Level" keeps the entry level it always included.
    expect(normalizeExperienceLevels(["new_grad"])).toEqual(["intern_new_grad", "entry"]);
    expect(normalizeExperienceLevels(["internship", "new_grad"])).toEqual(["intern_new_grad", "entry"]);
  });

  it("keeps the older intern_new_grad and entry keys, which are options again", () => {
    expect(normalizeExperienceLevels(["intern_new_grad"])).toEqual(["intern_new_grad"]);
    expect(normalizeExperienceLevels(["entry"])).toEqual(["entry"]);
  });

  it("drops anything else", () => {
    expect(normalizeExperienceLevels(["executive", "Senior Level", "staff", "expert"])).toEqual([]);
    expect(normalizeExperienceLevels(["staff", "senior", "expert"])).toEqual(["senior"]);
  });

  it("is case- and space-tolerant and dedupes overlapping values", () => {
    expect(normalizeExperienceLevels([" Senior ", "INTERNSHIP", "new_grad", "entry"])).toEqual([
      "senior",
      "intern_new_grad",
      "entry",
    ]);
  });

  it("survives malformed persisted state", () => {
    expect(normalizeExperienceLevels(undefined)).toEqual([]);
    expect(normalizeExperienceLevels(null)).toEqual([]);
    expect(normalizeExperienceLevels("mid")).toEqual(["mid"]);
    expect(normalizeExperienceLevels([42, null, {}, "", "  "])).toEqual([]);
  });

  it("only ever yields options, and is idempotent", () => {
    const anyValue = fc.oneof(
      fc.constantFrom(...OPTIONS, "internship", "new_grad", "executive", "Senior Level"),
      fc.string(),
      fc.constant(null),
      fc.integer(),
    );
    fc.assert(
      fc.property(fc.oneof(fc.array(anyValue), anyValue), (input) => {
        const once = normalizeExperienceLevels(input);
        expect(once.every((v) => OPTIONS.includes(v))).toBe(true);
        expect(new Set(once).size).toBe(once.length);
        // Re-running on already-normalized state must not drift.
        expect(normalizeExperienceLevels(once)).toEqual(once);
      }),
      { numRuns: 200 },
    );
  });
});

describe("experienceLevelsOrDefault", () => {
  it("gives the default when no choice survives", () => {
    expect(experienceLevelsOrDefault([])).toEqual(["intern_new_grad", "entry"]);
    expect(experienceLevelsOrDefault(undefined)).toEqual(["intern_new_grad", "entry"]);
    expect(experienceLevelsOrDefault(["expert"])).toEqual(["intern_new_grad", "entry"]);
  });

  it("keeps a real choice, never adding the default to it", () => {
    expect(experienceLevelsOrDefault(["director"])).toEqual(["director"]);
    expect(experienceLevelsOrDefault(["internship"])).toEqual(["intern_new_grad"]);
  });

  it("hands out a fresh array each time", () => {
    const first = experienceLevelsOrDefault([]);
    first.push("mid");
    expect(experienceLevelsOrDefault([])).toEqual(["intern_new_grad", "entry"]);
    expect(DEFAULT_EXPERIENCE_LEVELS).toEqual(["intern_new_grad", "entry"]);
  });
});

describe("experienceLevelLabel", () => {
  it("names every stored level", () => {
    expect(
      ["internship", "new_grad", "entry", "mid", "senior", "lead", "director", "executive"].map(
        experienceLevelLabel,
      ),
    ).toEqual([
      "Internship", "New Grad", "Entry Level", "Mid Level", "Senior Level", "Lead/Staff",
      "Director", "Executive",
    ]);
  });

  it("is blank for anything else", () => {
    expect(experienceLevelLabel("")).toBe("");
    expect(experienceLevelLabel(null)).toBe("");
    expect(experienceLevelLabel("intern_new_grad")).toBe("");
  });
});
