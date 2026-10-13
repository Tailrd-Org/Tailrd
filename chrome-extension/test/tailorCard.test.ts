import { describe, it, expect } from "vitest";
import { scoreJumpText, buildTailorCardHtml } from "../src/content/tailorCard";
import type { TailorResult } from "../src/shared/types";

const result: TailorResult = {
  document: {}, originalScore: 72, newScore: 85, atsScore: 88,
  keywordCoverage: 92, matchedKeywords: ["react"], missingKeywords: ["aws", "docker"],
  diffSummary: "", matchLabel: "Good match", reasons: [],
};

describe("scoreJumpText", () => {
  it("shows a jump when the score improves", () => {
    expect(scoreJumpText(72, 85)).toBe("Match 7.2 → 8.5");
  });
  it("shows 'held' when unchanged", () => {
    expect(scoreJumpText(80, 80)).toBe("Match held at 8.0");
  });
  it("shows only the new score when there is no before score", () => {
    expect(scoreJumpText(null, 85)).toBe("Match 8.5");
  });
  it("keeps the before score and names the band when the new score is withheld", () => {
    expect(scoreJumpText(72, null, "Good match")).toBe("Match 7.2 → Good match");
  });
  it("shows the engine's label when there is no score at all", () => {
    expect(scoreJumpText(null, null, "We couldn't score this")).toBe("We couldn't score this");
    expect(scoreJumpText(null, null)).toBe("");
  });
});

describe("buildTailorCardHtml", () => {
  it("renders score, stats, keyword chips and the preview action", () => {
    const html = buildTailorCardHtml(result, new Set(["aws"]));
    expect(html).toContain("Match 7.2 → 8.5");
    expect(html).toContain("ATS 88 · 92% coverage");
    expect(html).toContain('data-kw="aws"');
    expect(html).toContain('data-kw="docker"');
    expect(html).toContain("ap-kw on"); // aws is selected
    // Single primary action: open the PDF preview (regen/download/attach live inside it).
    expect(html).toContain('id="ap-tailor-preview"');
  });
  it("never renders null or NaN when the engine gives no number", () => {
    const html = buildTailorCardHtml(
      { ...result, originalScore: null, newScore: null, atsScore: null, keywordCoverage: null,
        matchLabel: "We couldn't score this" },
      new Set()
    );
    expect(html).not.toMatch(/null|NaN|undefined/);
    expect(html).toContain("We couldn't score this");
    expect(html).not.toContain("ATS");
    expect(html).not.toContain("coverage");
  });
  it("shows coverage alone when only the score is withheld (band only)", () => {
    const html = buildTailorCardHtml({ ...result, newScore: null, atsScore: null, keywordCoverage: 40 }, new Set());
    expect(html).toContain("Good match");
    expect(html).toContain("40% coverage");
    expect(html).not.toContain("ATS");
    expect(html).not.toMatch(/null|NaN/);
  });
  it("shows why the score went away, escaped", () => {
    const html = buildTailorCardHtml(
      { ...result, newScore: null, atsScore: null, reasons: ["Most matching skills are only <listed>, not shown in your experience."] },
      new Set()
    );
    expect(html).toContain('class="ap-tailor-reasons"');
    expect(html).toContain("Most matching skills are only &lt;listed&gt;, not shown in your experience.");
  });
  it("has no reasons block when there are none", () => {
    expect(buildTailorCardHtml(result, new Set())).not.toContain("ap-tailor-reasons");
  });
  it("omits the keyword row when there are no missing keywords", () => {
    const html = buildTailorCardHtml({ ...result, missingKeywords: [] }, new Set());
    expect(html).not.toContain("ap-kw-row");
  });
  it("escapes HTML in keyword chips", () => {
    const evil: TailorResult = { ...result, missingKeywords: ['"><img src=x onerror=alert(1)>'] };
    const html = buildTailorCardHtml(evil, new Set());
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });
});
