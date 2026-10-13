/**
 * The deterministic fit engine's result, as the web app shows it.
 *
 * Display rules (match-engine/docs/compliance.md section 2, DECISIONS D-52 / D-85 / D-86):
 * - SCORED: "Fit 76" with the band the API sent. Never a percent sign after the score.
 * - SCORED_LOW_CONFIDENCE: the band only, no number.
 * - GATE_FAIL: "Not eligible"; the reason is in the detail view's headline.
 * - NEEDS_REVIEW: "We couldn't score this"; the detail view lists the plain sentence per reason.
 * Bands come from the API wherever it sends one. bandFor() mirrors the engine's band config
 * (match-engine/config/bands.yaml, 85 / 70 / 55) for the few places that only get a number.
 */

export type FitOutcome = "SCORED" | "SCORED_LOW_CONFIDENCE" | "NEEDS_REVIEW" | "GATE_FAIL";
export type BandTone = "strong" | "good" | "partial" | "limited" | "none";

export const NOT_ELIGIBLE = "Not eligible";
export const UNSCORED_TEXT = "We couldn't score this";

/** match-engine/config/bands.yaml, high to low. */
export const FIT_BANDS: ReadonlyArray<{ min: number; band: string; tone: BandTone }> = [
  { min: 85, band: "Strong match", tone: "strong" },
  { min: 70, band: "Good match", tone: "good" },
  { min: 55, band: "Partial match", tone: "partial" },
  { min: 0, band: "Limited match", tone: "limited" },
];

export const STRONG_MIN = FIT_BANDS[0].min;

/** The band for a displayed integer fit score. */
export function bandFor(score: number): string {
  for (const b of FIT_BANDS) if (score >= b.min) return b.band;
  return FIT_BANDS[FIT_BANDS.length - 1].band;
}

export function bandTone(band: string | null | undefined): BandTone {
  return FIT_BANDS.find((b) => b.band === band)?.tone ?? "none";
}

export function isBand(label: string | null | undefined): boolean {
  return bandTone(label) !== "none";
}

export interface FitFields {
  match_score: number;
  match_band?: string | null;
  match_outcome?: string | null;
}

export type CardFit =
  | { kind: "scored"; score: number; band: string }
  | { kind: "band"; band: string }
  | { kind: "not-eligible"; label: string }
  | { kind: "unscored"; label: string };

/** What a job card shows, or null when the job has not been scored for this user yet. */
export function cardFit(job: FitFields): CardFit | null {
  const outcome = job.match_outcome;
  if (outcome === "GATE_FAIL") return { kind: "not-eligible", label: NOT_ELIGIBLE };
  if (outcome === "NEEDS_REVIEW") return { kind: "unscored", label: UNSCORED_TEXT };
  if (outcome === "SCORED" || outcome === "SCORED_LOW_CONFIDENCE") {
    const band = job.match_band || (job.match_score > 0 ? bandFor(job.match_score) : "");
    if (outcome === "SCORED" && job.match_score > 0) return { kind: "scored", score: job.match_score, band };
    return band ? { kind: "band", band } : null;
  }
  return null;
}

/** The one-line summary of a card result ("Fit score 76 · Good match"). */
export function fitSummary(fit: CardFit): string {
  if (fit.kind === "scored") return `Fit score ${fit.score} · ${fit.band}`;
  if (fit.kind === "band") return fit.band;
  return fit.label;
}
