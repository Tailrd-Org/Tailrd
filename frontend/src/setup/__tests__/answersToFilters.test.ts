import { describe, it, expect } from "vitest";
import { answersToFilters } from "../answersToFilters";
import { emptyAnswers } from "../types";
import { EXPERIENCE_OPTIONS, restoreFilters } from "../../components/JobFilterBar";

describe("answersToFilters", () => {
  it("maps an empty answer set to the default filters", () => {
    expect(answersToFilters(emptyAnswers)).toEqual({
      country: "", location: [], work_type: [], career_family: [],
      experience_level: ["intern_new_grad", "entry"], date_posted: "",
    });
  });

  it("maps country, city, remote, career families, and experience", () => {
    const r = answersToFilters({
      ...emptyAnswers,
      country: "CA",
      city: "Ottawa",
      open_to_remote: true,
      job_functions: ["software_engineering", "data_ai_analytics"],
      experience_level: "intern_new_grad",
    });
    expect(r.country).toBe("CA");
    expect(r.location).toEqual(["Ottawa"]);
    expect(r.work_type).toEqual(["remote"]);
    expect(r.career_family).toEqual(["software_engineering", "data_ai_analytics"]);
    expect(r.experience_level).toEqual(["intern_new_grad"]);
  });

  it("maps each of the six experience answers to that one level", () => {
    for (const { value } of EXPERIENCE_OPTIONS) {
      expect(answersToFilters({ ...emptyAnswers, experience_level: value }).experience_level).toEqual([value]);
    }
  });

  it("omits city from location when blank and remote when false", () => {
    const r = answersToFilters({ ...emptyAnswers, country: "US" });
    expect(r.location).toEqual([]);
    expect(r.work_type).toEqual([]);
  });

  it("writes what the Jobs page restores unchanged", () => {
    const r = answersToFilters({
      ...emptyAnswers,
      country: "US",
      city: "Austin",
      job_functions: ["skilled_trades"],
      experience_level: "senior",
    });
    expect(restoreFilters(JSON.stringify(r))).toEqual(r);
  });
});
