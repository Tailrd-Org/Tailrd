import { describe, it, expect } from "vitest";
import { bandFor, bandTone, cardFit, NOT_ELIGIBLE, UNSCORED_TEXT } from "./fit";

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
