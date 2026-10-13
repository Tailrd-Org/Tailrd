import { describe, it, expect } from "vitest";
import { bandFor, bandTone, cardFit, impactViews, NOT_ELIGIBLE, UNSCORED_TEXT } from "./fit";

describe("fit bands (engine bands.yaml: 85 / 70 / 55)", () => {
  it("maps a displayed score to the engine's band names at the exact edges", () => {
    expect(bandFor(100)).toBe("Strong match");
    expect(bandFor(85)).toBe("Strong match");
    expect(bandFor(84)).toBe("Good match");
    expect(bandFor(70)).toBe("Good match");
    expect(bandFor(69)).toBe("Partial match");
    expect(bandFor(55)).toBe("Partial match");
    expect(bandFor(54)).toBe("Limited match");
    expect(bandFor(0)).toBe("Limited match");
  });

  it("does not use the old 80 / 60 thresholds", () => {
    expect(bandFor(80)).toBe("Good match");
    expect(bandFor(60)).toBe("Partial match");
  });

  it("gives each band a tone and anything else none", () => {
    expect(bandTone("Strong match")).toBe("strong");
    expect(bandTone("Good match")).toBe("good");
    expect(bandTone("Partial match")).toBe("partial");
    expect(bandTone("Limited match")).toBe("limited");
    expect(bandTone("We couldn't score this")).toBe("none");
    expect(bandTone(null)).toBe("none");
  });
});

describe("cardFit", () => {
  it("SCORED shows the band the API sent, never the number (the list has no interval, D-54)", () => {
    // 82 would have been "STRONG" under the old 80 cut; the API's band wins.
    expect(cardFit({ match_score: 82, match_band: "Good match", match_outcome: "SCORED" })).toEqual({
      kind: "band",
      band: "Good match",
    });
  });

  it("SCORED without a band falls back to the engine's bands", () => {
    expect(cardFit({ match_score: 86, match_band: null, match_outcome: "SCORED" })).toEqual({
      kind: "band",
      band: "Strong match",
    });
  });

  it("SCORED_LOW_CONFIDENCE shows the band only", () => {
    expect(
      cardFit({ match_score: 0, match_band: "Partial match", match_outcome: "SCORED_LOW_CONFIDENCE" }),
    ).toEqual({ kind: "band", band: "Partial match" });
  });

  it("GATE_FAIL reads Not eligible and NEEDS_REVIEW We couldn't score this", () => {
    expect(cardFit({ match_score: 0, match_band: null, match_outcome: "GATE_FAIL" })).toEqual({
      kind: "not-eligible",
      label: NOT_ELIGIBLE,
    });
    expect(cardFit({ match_score: 0, match_band: null, match_outcome: "NEEDS_REVIEW" })).toEqual({
      kind: "unscored",
      label: UNSCORED_TEXT,
    });
    expect(NOT_ELIGIBLE).toBe("Not eligible");
    expect(UNSCORED_TEXT).toBe("We couldn't score this");
  });

  it("shows nothing for a job not scored yet", () => {
    expect(cardFit({ match_score: 0, match_band: null, match_outcome: null })).toBeNull();
    expect(cardFit({ match_score: 0 })).toBeNull();
  });

  it("a SCORED row with neither a number nor a band shows nothing", () => {
    expect(cardFit({ match_score: 0, match_band: null, match_outcome: "SCORED" })).toBeNull();
  });
});

describe("impactViews (D-54 addendum: Big at >= 2/3 of the max, plain at >= 1/3, Small below)", () => {
  const words = (raws: string[]) => impactViews(raws).map((v) => v?.word ?? null);

  it("labels exact ties at 2/3 and 1/3, and just below 1/3, for pluses", () => {
    expect(words(["+0.30", "+0.20", "+0.10", "+0.09"])).toEqual(["Big plus", "Big plus", "Plus", "Small plus"]);
    expect(words(["+0.90", "+0.60", "+0.30", "+0.29"])).toEqual(["Big plus", "Big plus", "Plus", "Small plus"]);
  });

  it("labels exact ties at 2/3 and 1/3, and just below 1/3, for gaps", () => {
    expect(words(["-0.30", "-0.20", "-0.10", "-0.09"])).toEqual(["Big gap", "Big gap", "Gap", "Small gap"]);
    expect(words(["-0.06", "-0.04", "-0.02", "-0.01"])).toEqual(["Big gap", "Big gap", "Gap", "Small gap"]);
  });

  it("just below 2/3 is plain, not Big", () => {
    expect(words(["+0.30", "+0.19", "-0.19"])).toEqual(["Big plus", "Plus", "Gap"]);
  });

  it("sizes pluses and gaps against one shared max, by absolute value", () => {
    const views = impactViews(["+0.10", "-0.30"]);
    expect(views.map((v) => v?.word)).toEqual(["Plus", "Big gap"]);
    expect(views.map((v) => v?.width)).toEqual([33, 100]);
    expect(views.map((v) => v?.raw)).toEqual(["+0.10", "-0.30"]);
  });

  it("every exact two-decimal tie at 1/3 and 2/3 of a max lands on the higher label", () => {
    for (let m = 3; m <= 99; m += 3) {
      const max = (m / 100).toFixed(2);
      const third = ((m / 3) / 100).toFixed(2);
      const twoThirds = ((2 * m / 3) / 100).toFixed(2);
      expect(words([`+${max}`, `+${twoThirds}`, `+${third}`])).toEqual(["Big plus", "Big plus", "Plus"]);
      expect(words([`-${max}`, `-${twoThirds}`, `-${third}`])).toEqual(["Big gap", "Big gap", "Gap"]);
    }
  });

  it("an empty, zero or unparseable impact has no view", () => {
    expect(impactViews(["", undefined, "0.00", "n/a", "+0.10"]).map((v) => v?.word ?? null)).toEqual([
      null, null, null, null, "Big plus",
    ]);
  });
});
