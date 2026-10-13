/**
 * The deterministic fit engine's result, as the web app shows it.
 *
 * Display rules (match-engine/docs/compliance.md section 2, DECISIONS D-52 / D-85 / D-86):
 * - SCORED: the number only where the full result is known. A displayed interval replaces the
 *   number (D-54) and the jobs list carries no interval (it is not banked), so a card shows the
 *   band the API sent and only the detail view's breakdown shows a number or a range.
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
  | { kind: "band"; band: string }
  | { kind: "not-eligible"; label: string }
  | { kind: "unscored"; label: string };

/** What a job card shows, or null when the job has not been scored for this user yet.
 *  Never a number: the list cannot tell whether the engine would display an interval. */
export function cardFit(job: FitFields): CardFit | null {
  const outcome = job.match_outcome;
  if (outcome === "GATE_FAIL") return { kind: "not-eligible", label: NOT_ELIGIBLE };
  if (outcome === "NEEDS_REVIEW") return { kind: "unscored", label: UNSCORED_TEXT };
  if (outcome === "SCORED" || outcome === "SCORED_LOW_CONFIDENCE") {
    const band = job.match_band || (outcome === "SCORED" && job.match_score > 0 ? bandFor(job.match_score) : "");
    return band ? { kind: "band", band } : null;
  }
  return null;
}

/** The one-line summary of a card result: its band or its outcome words. */
export function fitSummary(fit: CardFit): string {
  return fit.kind === "band" ? fit.band : fit.label;
}

export interface ImpactView {
  word: string;
  /** Bar width, percent of the largest |impact| shown. */
  width: number;
  raw: string;
}

/**
 * Qualitative words for the engine's signed impacts ("+0.31", raw scale), sized against the
 * largest |impact| shown (DECISIONS D-54 addendum): at least 2/3 of it is "Big", at least 1/3
 * plain, below that "Small"; "plus" for a positive impact, "gap" for a negative one. The raw
 * value is kept for the title and aria-label. Null for an empty or unparseable impact.
 */
export function impactViews(raws: ReadonlyArray<string | undefined>): Array<ImpactView | null> {
  const values = raws.map((raw) => {
    const n = raw ? Number.parseFloat(raw) : Number.NaN;
    return Number.isFinite(n) && n !== 0 ? n : null;
  });
  const max = Math.max(0, ...values.map((v) => (v === null ? 0 : Math.abs(v))));
  return values.map((v, i) => {
    if (v === null || max === 0) return null;
    const ratio = Math.abs(v) / max;
    const noun = v > 0 ? "plus" : "gap";
    const word = ratio >= 2 / 3 ? `Big ${noun}` : ratio >= 1 / 3 ? noun[0].toUpperCase() + noun.slice(1) : `Small ${noun}`;
    return { word, width: Math.round(ratio * 100), raw: String(raws[i]) };
  });
}
