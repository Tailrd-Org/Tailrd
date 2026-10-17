/**
 * Shared contract between the isolated-world client (mainWorldClient.ts) and the
 * MAIN-world driver (mainWorld.ts). Kept dependency-free so both worlds bundle it
 * without pulling in isolated-only code, and so the two sides can never drift.
 */
export type FillDriver = "react-select" | "workday";

/** Isolated → MAIN: please fill this field. */
export const MW_FILL_EVENT = "tailrd:mw:fill";
/** MAIN → isolated: here is the outcome. */
export const MW_RESULT_EVENT = "tailrd:mw:result";
/** Isolated → MAIN: toggle blocking-dialog suppression during an active fill/flow. */
export const MW_SUPPRESS_EVENT = "tailrd:mw:suppress";

export interface MwSuppressDetail {
  on: boolean;
}

export interface MwFillDetail {
  id: number;
  /** Value of FIELD_ID_ATTR on the target node (locates it in the MAIN world). */
  fieldId: string;
  value: string;
  kind: FillDriver;
  /** A city/location field's full place ("San Jose, CA, United States"):
   *  place suggestions are chosen by it. */
  placeHint?: string;
  /** A school field: an option is chosen by the school's own words only. */
  school?: boolean;
}

export interface MwResultDetail {
  id: number;
  ok: boolean;
  /** The widget's committed/displayed value after the fill, if readable. */
  committed?: string;
  reason?: string;
  /** On a miss, the option labels the widget offered, for the caller to
   *  choose among (the re-ask) rather than guess again. */
  options?: string[];
}
