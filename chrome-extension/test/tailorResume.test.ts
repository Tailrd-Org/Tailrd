import { describe, it, expect } from "vitest";
import { buildTailorRequestBody, mapTailorResponse } from "../src/api/tailorResume";
import type { JobContext } from "../src/shared/types";

const ctx: JobContext = { jobDescription: "Need AWS", jobTitle: "Engineer", company: "Acme" };

describe("buildTailorRequestBody", () => {
  it("maps opts + context to the snake_case backend payload", () => {
    expect(buildTailorRequestBody(7, ctx, ["Skills"], ["AWS"])).toEqual({
      resume_id: 7, job_description: "Need AWS", job_title: "Engineer",
      company: "Acme", sections: ["Skills"], add_keywords: ["AWS"],
    });
  });

  it("sends null sections/keywords when omitted (server auto-weaves)", () => {
    expect(buildTailorRequestBody(null, ctx)).toEqual({
      resume_id: null, job_description: "Need AWS", job_title: "Engineer",
      company: "Acme", sections: null, add_keywords: null,
    });
  });
});

describe("mapTailorResponse", () => {
  it("maps snake_case server fields to the camelCase TailorResult", () => {
    const doc = { header: { name: "Jane" }, sections: [], theme: {} };
    expect(
      mapTailorResponse({
        document: doc, original_overall_score: 60, new_overall_score: 82,
        new_ats_score: 78, new_keyword_coverage: 90,
        matched_keywords: ["Python"], missing_keywords: ["AWS"], diff_summary: "d",
      })
    ).toEqual({
      document: doc, originalScore: 60, newScore: 82, atsScore: 78,
      keywordCoverage: 90, matchedKeywords: ["Python"], missingKeywords: ["AWS"],
      diffSummary: "d", matchLabel: "", reasons: [],
    });
  });

  it("keeps the engine's null scores null and carries its label", () => {
    const doc = { header: { name: "Jane" }, sections: [], theme: {} };
    const out = mapTailorResponse({
      document: doc, original_overall_score: null, new_overall_score: null,
      new_ats_score: null, new_keyword_coverage: null, match_label: "We couldn't score this",
      reasons: ["We couldn't find any requirements in this job posting."],
      matched_keywords: [], missing_keywords: [], diff_summary: "",
    });
    expect(out.originalScore).toBeNull();
    expect(out.newScore).toBeNull();
    expect(out.atsScore).toBeNull();
    expect(out.keywordCoverage).toBeNull();
    expect(out.matchLabel).toBe("We couldn't score this");
    expect(out.reasons).toEqual(["We couldn't find any requirements in this job posting."]);
  });
});
