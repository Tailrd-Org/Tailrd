import { describe, it, expect } from "vitest";
import * as fc from "fast-check";
import {
  DEFAULT_EXPERIENCE_LEVELS,
  EXPERIENCE_OPTIONS,
  availableExperienceOptions,
  experienceLevelLabel,
  experienceLevelsOnOffer,
  experienceLevelsOrDefault,
  normalizeExperienceLevels,
  parseAvailableLevels,
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

/**
 * Until every level is ingested, production stores only internships and new grads, so
 * the pages offer the options that have live jobs behind them (GET /jobs/levels) and
 * nothing else: an option with no jobs is a silent dead end.
 */
describe("parseAvailableLevels", () => {
  it("reads the option keys the endpoint answers with", () => {
    expect(parseAvailableLevels({ levels: ["intern_new_grad", "senior"] })).toEqual(["intern_new_grad", "senior"]);
  });

  it("keeps only keys that are options", () => {
    expect(parseAvailableLevels({ levels: ["internship", "senior", 4, null, "Senior"] })).toEqual(["senior"]);
  });

  it("is null for anything the pages cannot use, which means 'not known'", () => {
    for (const bad of [undefined, null, [], "<html>proxy error</html>", 7, {}, { levels: "senior" },
      { levels: [] }, { levels: ["astronaut"] }]) {
      expect(parseAvailableLevels(bad)).toBeNull();
    }
  });
});

describe("availableExperienceOptions", () => {
  it("offers all six while the list is not known, so a failed lookup never removes the filter", () => {
    expect(availableExperienceOptions(null)).toEqual(EXPERIENCE_OPTIONS);
    expect(availableExperienceOptions(undefined)).toEqual(EXPERIENCE_OPTIONS);
  });

  it("offers only the options that have jobs, with their labels", () => {
    expect(availableExperienceOptions(["intern_new_grad"])).toEqual([
      { value: "intern_new_grad", label: "Intern/New Grad" },
    ]);
  });

  it("keeps the options' own order, whatever order the list comes in", () => {
    expect(availableExperienceOptions(["director", "entry", "intern_new_grad"])).toEqual([
      { value: "intern_new_grad", label: "Intern/New Grad" },
      { value: "entry", label: "Entry Level" },
      { value: "director", label: "Director/Executive" },
    ]);
  });

  it("never leaves nothing to choose", () => {
    expect(availableExperienceOptions([])).toEqual(EXPERIENCE_OPTIONS);
    expect(availableExperienceOptions(["astronaut"])).toEqual(EXPERIENCE_OPTIONS);
  });
});

describe("experienceLevelsOnOffer", () => {
  const STUDENT_ONLY = ["intern_new_grad"];

  it("is experienceLevelsOrDefault while the options are not known", () => {
    for (const saved of [undefined, [], ["director"], ["internship"], ["new_grad"], ["mid", "senior"]]) {
      expect(experienceLevelsOnOffer(saved, null)).toEqual(experienceLevelsOrDefault(saved));
    }
  });

  it("drops a saved level whose option is hidden, so it is never an invisible filter", () => {
    expect(experienceLevelsOnOffer(["intern_new_grad", "entry"], STUDENT_ONLY)).toEqual(["intern_new_grad"]);
    expect(experienceLevelsOnOffer(["entry", "mid", "senior"], ["intern_new_grad", "mid"])).toEqual(["mid"]);
  });

  it("holds migrated old values to the offer too", () => {
    // The old "New Grad / Entry Level" becomes Intern/New Grad plus Entry Level.
    expect(experienceLevelsOnOffer(["new_grad"], STUDENT_ONLY)).toEqual(["intern_new_grad"]);
    expect(experienceLevelsOnOffer(["new_grad"], ["intern_new_grad", "entry"])).toEqual(["intern_new_grad", "entry"]);
  });

  it("falls back to the default levels that are on offer", () => {
    expect(experienceLevelsOnOffer(["director"], STUDENT_ONLY)).toEqual(["intern_new_grad"]);
    expect(experienceLevelsOnOffer([], STUDENT_ONLY)).toEqual(["intern_new_grad"]);
    expect(experienceLevelsOnOffer(undefined, ["entry", "senior"])).toEqual(["entry"]);
  });

  it("and to every option on offer when no default is", () => {
    expect(experienceLevelsOnOffer(["director"], ["mid", "senior"])).toEqual(["mid", "senior"]);
  });

  it("is never empty, only yields offered levels, and is idempotent", () => {
    const anyValue = fc.oneof(
      fc.constantFrom(...OPTIONS, "internship", "new_grad", "executive"),
      fc.string(),
      fc.constant(null),
    );
    const anyAvailable = fc.oneof(fc.constant(null), fc.constant([]), fc.subarray(OPTIONS, { minLength: 1 }));
    fc.assert(
      fc.property(fc.array(anyValue), anyAvailable, (saved, available) => {
        const offered = availableExperienceOptions(available).map((o) => o.value);
        const once = experienceLevelsOnOffer(saved, available);
        expect(once.length).toBeGreaterThan(0);
        expect(once.every((v) => offered.includes(v))).toBe(true);
        expect(new Set(once).size).toBe(once.length);
        expect(experienceLevelsOnOffer(once, available)).toEqual(once);
      }),
      { numRuns: 200 },
    );
  });
});
