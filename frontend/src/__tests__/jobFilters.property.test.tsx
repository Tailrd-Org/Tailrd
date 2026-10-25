import { describe, it, expect } from "vitest";
import * as fc from "fast-check";
import {
  CAREER_FAMILY_OPTIONS,
  EXPERIENCE_OPTIONS,
  defaultFilters,
  restoreFilters,
} from "../components/JobFilterBar";

/**
 * Feature: job-scraper-aggregator, Property 11: Filter Persistence Round-Trip
 * Validates: Requirements 10.7
 *
 * restoreFilters is what the Jobs page runs on localStorage["job-aggregator-filters"].
 */

const COUNTRY_VALUES = ["", "US", "CA"];
const WORK_TYPE_VALUES = ["remote", "hybrid", "onsite"];
const FAMILY_VALUES = CAREER_FAMILY_OPTIONS.map((o) => o.value);
const EXPERIENCE_VALUES = EXPERIENCE_OPTIONS.map((o) => o.value);
const DATE_VALUES = ["", "24h", "3d", "week", "month"];

const jobFiltersArb = fc.record({
  country: fc.constantFrom(...COUNTRY_VALUES),
  location: fc.array(fc.constantFrom("Toronto", "Ottawa, ON", "Remote"), { maxLength: 3 }),
  work_type: fc.subarray(WORK_TYPE_VALUES),
  career_family: fc.subarray(FAMILY_VALUES),
  // The level filter is never empty: nothing chosen means the default levels.
  experience_level: fc.subarray(EXPERIENCE_VALUES, { minLength: 1 }),
  date_posted: fc.constantFrom(...DATE_VALUES),
});

describe("Property 11: Filter Persistence Round-Trip", () => {
  it("saving and restoring filters produces the same state", () => {
    fc.assert(
      fc.property(jobFiltersArb, (filters) => {
        expect(restoreFilters(JSON.stringify(filters))).toEqual(filters);
      }),
      { numRuns: 100 },
    );
  });

  it("nothing saved gives the defaults: every filter open except the levels", () => {
    expect(restoreFilters(null)).toEqual(defaultFilters());
    expect(restoreFilters("")).toEqual(defaultFilters());
    expect(defaultFilters()).toEqual({
      country: "",
      location: [],
      work_type: [],
      career_family: [],
      experience_level: ["intern_new_grad", "entry"],
      date_posted: "",
    });
  });

  it("corrupted storage gives the defaults", () => {
    for (const bad of ["not valid json{{{", "null", "42", "[]", '"text"']) {
      expect(restoreFilters(bad)).toEqual(defaultFilters());
    }
  });

  it("partial data fills in the defaults", () => {
    fc.assert(
      fc.property(fc.constantFrom(...COUNTRY_VALUES), (country) => {
        expect(restoreFilters(JSON.stringify({ country }))).toEqual({ ...defaultFilters(), country });
      }),
      { numRuns: 20 },
    );
  });
});

describe("Saved filters from before the six levels and the career families", () => {
  it("someone who never chose a level gets Intern/New Grad and Entry Level", () => {
    // The old page saved its empty default, which then meant the whole (student) catalogue.
    const old = { country: "CA", location: [], work_type: [], role_category: [], experience_level: [], date_posted: "" };
    expect(restoreFilters(JSON.stringify(old)).experience_level).toEqual(["intern_new_grad", "entry"]);
  });

  it("the old experience values migrate", () => {
    const restore = (experience_level: unknown) =>
      restoreFilters(JSON.stringify({ experience_level })).experience_level;
    expect(restore(["internship"])).toEqual(["intern_new_grad"]);
    expect(restore(["new_grad"])).toEqual(["intern_new_grad", "entry"]);
    expect(restore(["senior", "bogus"])).toEqual(["senior"]);
    expect(restore(["bogus"])).toEqual(["intern_new_grad", "entry"]);
  });

  it("saved job functions become career families, and role_category is gone", () => {
    const restored = restoreFilters(JSON.stringify({
      role_category: ["Software Engineering", "Accounting and Finance", "Arts and Entertainment"],
    }));
    expect(restored.career_family).toEqual([
      "software_engineering",
      "finance_accounting",
      "banking_insurance_investment",
    ]);
    expect(restored).not.toHaveProperty("role_category");
  });

  it("a single legacy location string becomes a one-tag list", () => {
    expect(restoreFilters(JSON.stringify({ location: "Ottawa" })).location).toEqual(["Ottawa"]);
  });
});
