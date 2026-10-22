/**
 * Autofill telemetry: build a per-fill summary from the combined pass
 * reports/outcomes so the backend can see which sites and fields the filler
 * struggles with. This is the signal that tells us where a server-side override
 * rule is worth authoring (see api/autofill overrides).
 *
 * It now records SUCCESSES as well as failures, and it diffs what was written
 * against what the page holds afterwards.
 *
 * Why: recording only failures made one whole bug class unobservable by
 * construction, a field answered wrongly but written successfully produced a
 * `filled` count and nothing else, so the record of the page said everything
 * went well. That is exactly the bug we keep hitting ("reported filled,
 * actually wasn't" / "filled with the wrong value"), and it was the one thing
 * the data could never show.
 *
 * The re-scan diff is the second half. Per-write verification (writeEngine)
 * proves the commit stuck AT WRITE TIME. A framework can still revert a value
 * on blur, on validation, or on a re-render that a LATER field triggered, none
 * of which a per-write check can see, because by then it has moved on.
 *
 * Privacy: the DEFAULT record emits field LABELS, categories, provenance and
 * booleans only, never the user's answer values. `observedValuePresent` is a
 * boolean for that reason, "this control holds something" is the observation,
 * not what it holds. That is what every account sends.
 *
 * Diagnostic capture (`TelemetryInputs.capture`) is the deliberate exception: an
 * account that turns it ON also sends answers and sanitised employer markup, so
 * that a form which failed can be rebuilt as a fixture without visiting the live
 * site. It is opt-in, per account, and off unless the backend says otherwise,
 * so the paragraph above stays true for everybody who did not ask for it.
 *
 * Pure: host/url/observations are passed in, so it unit-tests without a
 * document.
 */
import { redactCaptureValue } from "./domCapture";
import { placeOf } from "./placeMatch";
import { optionPolarity } from "./answerKind";
import { countryFromName, DIAL_CODES, regionFromText } from "./geo";
import { matchOption } from "./optionMatch";
import { closestDemographicOption } from "./demographicMatch";
import { parseDateSpan } from "./profileFacts";
import type {
  AutofillTelemetry, DetectedField, FieldCaptureRecord, FieldCategory, FieldOutcomeRecord, FillDurations,
} from "../shared/types";
import type { FieldReport } from "./reconciler";

export interface PassOutcomeLike {
  fieldId: string;
  ok: boolean;
  reason?: string;
}

/** Where a field's value came from, for one fill pass. */
export interface FieldProvenance {
  /** "profile" (on-device fast path) | "backend" | "device" (EEO matching) | "user". */
  tier: string;
  /** Backend pass, when the backend produced it: "derived" | "rule" | "memory" | "ai". */
  pass?: string;
}

/** A field the backend gate refused, with its reason. Never carries a value. */
export interface DroppedAnswerLike {
  fieldId: string;
  reason: string;
  /** Which pass proposed the value that was dropped. */
  source?: string;
}

/**
 * Collapse every pass's reports/outcomes into one verdict per attempted field:
 * ok in ANY pass wins (a field the AI pass filled after the local pass missed
 * counts as filled). The first failure reason seen is kept for the ones that
 * never succeeded.
 */
export function finalOutcomes(
  reports: FieldReport[],
  outcomes: PassOutcomeLike[]
): { ok: Map<string, boolean>; reason: Map<string, string> } {
  const ok = new Map<string, boolean>();
  const reason = new Map<string, string>();
  const mark = (id: string, passed: boolean, why?: string): void => {
    ok.set(id, (ok.get(id) ?? false) || passed);
    if (!passed && why && !reason.has(id)) reason.set(id, why);
  };
  for (const r of reports) mark(r.fieldId, r.ok, r.reason);
  for (const o of outcomes) mark(o.fieldId, o.ok, o.reason);
  return { ok, reason };
}

/**
 * What the page holds for one field after the fill, read from a fresh scan.
 *
 * `value` is compared, never transmitted, the record carries only whether it
 * was present and whether it still matches what was written.
 */
export interface ObservedField {
  fieldId: string;
  value: string;
  /** A single checkbox: unticked reads as "". */
  checkbox?: boolean;
  /** A control that only takes one of its options (select, dropdown, radios). */
  choice?: boolean;
  /** A self-identification field's category: its answer reads back in the
   *  list's own words ("Prefer not to say" as "I don't wish to answer"). */
  demographic?: FieldCategory;
}

/** What one field was asked to hold. */
export interface IntendedField {
  fieldId: string;
  value: string;
}

/** Loose equality between a written value and what the control reads back.
 *
 *  Mirrors writeEngine's own read-back comparison: a control may normalize
 *  case, punctuation or surrounding space without having changed the answer,
 *  and a choice widget may display more than was written ("Yes" → "Yes, I am").
 *
 *  An EMPTY control is never equal to a non-empty write. That has to be stated
 *  outright: every string contains "", so a substring test alone reports a
 *  cleared field as unchanged, which would silently drop the most common
 *  revert there is.
 *
 *  Lenience is deliberately one-directional: a missed revert costs a record, a
 *  spurious one costs the user a question they already answered. */
function sameValue(written: string, observed: string): boolean {
  const core = (s: string): string =>
    s
      .normalize("NFD")
      .replace(/[̀-ͯ]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]/g, "");
  const w = core(written);
  const o = core(observed);
  if (!w) return !o;
  if (!o) return false;
  if (w === o || o.includes(w) || w.includes(o)) return true;
  // An option saying the same yes or no in other words: "Yes" picked "Consent",
  // "None" picked "Never held a clearance" (Brex, SpaceX, live 2026-10-03).
  const polarity = optionPolarity(written);
  if (polarity !== null && polarity === optionPolarity(observed)) return true;
  // A phone picker shows the dial code of the country picked: "Canada" read
  // back as "+1" (Greenhouse, live 2026-10-05; prod telemetry logged it as
  // changed on 2026-09-28 and 2026-10-03).
  // A picker that shows the chosen country's flag before its code: "Canada"
  // reads back "🇨🇦 +1" (Dayforce, live 2026-10-05). The flag names the
  // country; +1 alone would also be the United States'.
  const flag = /^\s*([\u{1F1E6}-\u{1F1FF}])([\u{1F1E6}-\u{1F1FF}])\s*(?:\+\s?\d{1,4})?\s*$/u.exec(observed);
  if (flag) {
    const iso = String.fromCharCode((flag[1].codePointAt(0) ?? 0) - 0x1f1e6 + 65, (flag[2].codePointAt(0) ?? 0) - 0x1f1e6 + 65);
    const named = countryFromName(written);
    if (named) return named.code === iso;
  }
  const dial = /^\+\s?(\d{1,4})$/.exec(observed.trim());
  if (dial) {
    const country = countryFromName(written);
    if (country && DIAL_CODES[country.code] === dial[1]) return true;
  }
  // A date's part holds its part of the date it was given: Workday's Month box
  // reads "6" for "Jun 2012", its Year box "2012" (replica, 2026-10-05).
  if (/^\d{1,4}$/.test(observed.trim())) {
    const span = parseDateSpan(written);
    if (span && span.precision !== "year") {
      const n = Number(observed.trim());
      const d = span.earliest;
      const hit = observed.trim().length === 4 ? n === d.getUTCFullYear() : n === d.getUTCMonth() + 1 || (span.precision === "day" && n === d.getUTCDate());
      if (hit) return true;
    }
  }
  // A native date input reads back the day typed as "01/04/2027" in ISO,
  // "2027-01-04" (Paylocity, live 2026-10-03).
  const day = isoDay(written);
  if (day && day === isoDay(observed)) return true;
  // A state chosen by name shows its code: "Texas" reads back "TX"
  // (Paylocity's State list, live 2026-10-08).
  const regionW = regionFromText(written);
  const regionO = regionW ? regionFromText(observed, regionW.country) : null;
  if (regionW && regionO && regionW.code === regionO.code && regionW.country === regionO.country) return true;
  // A place typeahead keeps its own spelling of the place typed: "Montréal,
  // QC" became "Montreal, Quebec, Canada" (Superhuman on Ashby, live 2026-10-03).
  if (written.includes(",") && observed.includes(",")) {
    const a = placeOf(written);
    const b = placeOf(observed);
    return (
      Boolean(a.city) &&
      a.city === b.city &&
      (!a.region || !b.region || a.region === b.region) &&
      (!a.country || !b.country || a.country === b.country)
    );
  }
  return false;
}

/** "2027-01-04" for "2027-01-04" or the US "01/04/2027"; null otherwise. */
function isoDay(text: string): string | null {
  const t = text.trim();
  const iso = /^(\d{4})-(\d{2})-(\d{2})$/.exec(t);
  if (iso) return t;
  const us = /^(\d{1,2})\/(\d{1,2})\/(\d{4})$/.exec(t);
  return us ? `${us[3]}-${us[1].padStart(2, "0")}-${us[2].padStart(2, "0")}` : null;
}

/** One field whose committed value disagrees with what was written. */
export interface RevertedField {
  fieldId: string;
  /** True when the control is now empty; false when it holds something else. */
  cleared: boolean;
}

/**
 * Fields that were written successfully and no longer hold what was written.
 *
 * Only fields we actually wrote AND believed we wrote are considered: a field
 * whose write already failed is a failure, not a revert, and conflating the two
 * would hide the interesting case behind the ordinary one.
 *
 * A field missing from `observed` is NOT reported. It left the DOM between the
 * fill and the re-scan (a step change, a collapsed section), which says nothing
 * about whether the value stuck.
 */
export function revertedFields(
  intended: IntendedField[],
  observed: ObservedField[],
  filledOk: ReadonlySet<string>
): RevertedField[] {
  const byId = new Map(observed.map((o) => [o.fieldId, o]));
  const out: RevertedField[] = [];
  for (const { fieldId, value } of intended) {
    if (!filledOk.has(fieldId)) continue;
    const seen = byId.get(fieldId);
    if (!seen) continue;
    const now = seen.value.trim();
    // A box is ticked or not: it reads "checked" or "" whatever was written
    // ("yes" read back as "checked" was logged as changed on Ramp, and an
    // unticked "no" as cleared on Superhuman, live 2026-10-03).
    const want = /^(yes|checked|true|on|1)$/i.test(value.trim()) ? true : /^(no|unchecked|false|off|0)$/i.test(value.trim()) ? false : null;
    if (seen.checkbox && want !== null) {
      const ticked = now !== "" && !/^(unchecked|false|off|no)$/i.test(now);
      if (want !== ticked) out.push({ fieldId, cleared: !ticked });
      continue;
    }
    if (sameValue(value, now)) continue;
    // An option the shared matcher picks for the answer is the answer, in the
    // option's words: "6" in "5-7 years", "…classifications of a protected
    // veteran" as the list's "…of protected veteran" (Workday replica,
    // 2026-10-05). Options only: in a text box a near-match is a change.
    if (seen.choice && now && matchOption([now], (o) => o, (o) => o, value) !== null) continue;
    // So is the option the self-ID rules pick for it: the widget now takes a
    // self-ID answer by them, and "Prefer not to say" committed as "I don't
    // wish to answer" was reported as reverted (Gusto, live, round 8).
    if (seen.choice && now && seen.demographic && closestDemographicOption(seen.demographic, value, [now]) !== null) continue;
    out.push({ fieldId, cleared: now === "" });
  }
  return out;
}

/**
 * Interesting first, so a size cap never silently drops the failures.
 *
 * A `filled` field is captured too (that is how a silently-wrong-but-written
 * answer becomes visible at all), but if something has to go, it goes first.
 */
const CAPTURE_RANK: Record<string, number> = {
  failed: 0, reverted: 1, dropped: 2, skipped: 3, filled: 4,
};

/**
 * Strip the user's answer out of a failure reason.
 *
 * The fill engine builds reasons by quoting what it tried to write, e.g.
 * `No option matches "University of Ottawa" (saw: McGill | Queen's)`. That text
 * then went into `field_outcomes`, which is sent for EVERY account, so the
 * record has been carrying answers all along despite this module promising it
 * did not. Found on 2026-08-13 by a test asserting the promise.
 *
 * Only the quoted span is removed. The `(saw: …)` list is the EMPLOYER's option
 * text, not the applicant's answer, and it is the single most useful thing in a
 * dropdown failure, so it stays.
 *
 * Diagnostic captures keep the reason intact: that record stores the answer in
 * its own column by design, so there is nothing left to protect there.
 */
export function scrubAnswerFromReason(reason: string): string {
  return reason.replace(/"[^"]*"/g, '"<value>"');
}

export function rankForCapture(captures: FieldCaptureRecord[]): FieldCaptureRecord[] {
  return [...captures].sort(
    (a, b) => (CAPTURE_RANK[a.outcome] ?? 9) - (CAPTURE_RANK[b.outcome] ?? 9)
  );
}

export interface TelemetryInputs {
  reports: FieldReport[];
  outcomes: PassOutcomeLike[];
  /** fieldId → where its value came from. Missing → tier "" (unknown). */
  provenance?: ReadonlyMap<string, FieldProvenance>;
  /** What each field was asked to hold, for the re-scan diff. */
  intended?: IntendedField[];
  /** Fresh scan of the page after the fill settled. */
  observed?: ObservedField[];
  /** Candidate values the backend gate refused. */
  dropped?: DroppedAnswerLike[];
  /**
   * Diagnostic capture. Absent (the default, and the only behaviour for an
   * account that did not opt in) means no answers and no markup are recorded.
   *
   * `snapshot` is injected rather than imported so this module stays DOM-free
   * and unit-testable: the content script closes over its live registry.
   */
  capture?: {
    snapshot(fieldId: string): { dom: string; selector: string; options: string[] } | null;
  };
  /** Which build produced this report. */
  extensionVersion?: string;
  durations?: FillDurations;
}

export function buildAutofillTelemetry(
  fields: DetectedField[],
  ctx: { host: string; url: string; atsType: string },
  inputs: TelemetryInputs
): AutofillTelemetry {
  const { ok: finalOk, reason: reasonById } = finalOutcomes(inputs.reports, inputs.outcomes);

  const byId = new Map(fields.map((f) => [f.id, f]));
  const intended = inputs.intended ?? [];
  const observed = inputs.observed ?? [];
  const okIds = new Set([...finalOk].filter(([, ok]) => ok).map(([id]) => id));
  const reverted = observed.length > 0 ? revertedFields(intended, observed, okIds) : [];
  const revertedById = new Map(reverted.map((r) => [r.fieldId, r]));
  const droppedById = new Map((inputs.dropped ?? []).map((d) => [d.fieldId, d]));
  const observedById = new Map(observed.map((o) => [o.fieldId, o.value]));
  const intendedIds = new Set(intended.map((i) => i.fieldId));

  const failedFields: { label: string; category: string; reason: string }[] = [];
  const fieldOutcomes: FieldOutcomeRecord[] = [];
  const fieldCaptures: FieldCaptureRecord[] = [];
  const intendedById = new Map(intended.map((i) => [i.fieldId, i.value]));
  let filled = 0;
  let failed = 0;

  // Every field with a verdict, plus every field the gate dropped, a dropped
  // field was never written, so it has no report of its own and would
  // otherwise vanish from the record entirely.
  const ids = new Set<string>([...finalOk.keys(), ...droppedById.keys()]);

  for (const id of ids) {
    const f = byId.get(id);
    const label = (f?.label ?? "").slice(0, 200);
    const category = f?.category ?? "unknown";
    const prov = inputs.provenance?.get(id);
    const drop = droppedById.get(id);
    const wroteOk = finalOk.get(id) ?? false;
    const revert = revertedById.get(id);

    let outcome: FieldOutcomeRecord["outcome"];
    let reason = "";
    if (drop && !wroteOk) {
      outcome = "dropped";
      reason = drop.reason;
    } else if (revert) {
      outcome = "reverted";
      reason = revert.cleared ? "value_cleared_after_write" : "value_changed_after_write";
    } else if (wroteOk) {
      outcome = "filled";
    } else if (!intendedIds.has(id) && !finalOk.has(id)) {
      outcome = "skipped";
    } else {
      outcome = "failed";
      reason = reasonById.get(id) ?? "";
    }

    // A revert is a failure of the fill even though the write reported success:
    // the page does not hold the answer. Counting it as filled is precisely the
    // lie this record exists to stop telling.
    if (outcome === "filled") filled++;
    else failed++;
    // Answers are stripped from every reason that leaves in the DEFAULT record
    // (see scrubAnswerFromReason); the diagnostic capture below keeps the
    // original, because it stores the answer in its own column anyway.
    const safeReason = scrubAnswerFromReason(reason);
    if (outcome !== "filled" && outcome !== "skipped") {
      failedFields.push({ label, category, reason: safeReason.slice(0, 200) });
    }

    fieldOutcomes.push({
      label,
      category,
      tier: prov?.tier ?? "",
      pass: prov?.pass ?? "",
      expectedValuePresent: intendedIds.has(id),
      observedValuePresent: (observedById.get(id) ?? "").trim() !== "",
      outcome,
      ...(safeReason ? { reason: safeReason.slice(0, 200) } : {}),
    });

    if (inputs.capture) {
      const snap = inputs.capture.snapshot(id);
      const [value, redacted] = redactCaptureValue(
        category,
        intendedById.get(id) ?? f?.proposedValue ?? "",
        f?.sensitive ?? false
      );
      fieldCaptures.push({
        fieldId: id,
        label: f?.label ?? label, // full, not the 200-char outcome label
        category,
        confidence: f?.confidence ?? 0,
        controlType: f?.controlType ?? "",
        inputType: f?.inputType ?? "",
        helpText: f?.helpText ?? "",
        required: f?.required ?? false,
        groupIndex: f?.groupIndex ?? null,
        // The live list beats the scan-time one: a react-select scans with no
        // options at all, which is precisely the case worth capturing.
        options: snap?.options?.length ? snap.options : f?.options ?? [],
        proposedValue: value,
        // Redacted on the same terms: what the page HOLDS for a demographic
        // field is the same answer as what we proposed, so withholding one and
        // sending the other would protect nothing.
        observedValue: redactCaptureValue(
          category, observedById.get(id) ?? "", f?.sensitive ?? false
        )[0],
        redacted,
        tier: prov?.tier ?? "",
        pass: prov?.pass ?? "",
        outcome,
        reason,
        dom: snap?.dom ?? "",
        selector: snap?.selector ?? "",
      });
    }
  }

  return {
    host: ctx.host,
    atsType: ctx.atsType,
    url: ctx.url,
    totalFields: ids.size,
    filled,
    failed,
    skipped: 0,
    failedFields: failedFields.slice(0, 50),
    fieldOutcomes: fieldOutcomes.slice(0, 100),
    reverted: reverted.length,
    ...(inputs.extensionVersion ? { extensionVersion: inputs.extensionVersion } : {}),
    ...(inputs.durations ? { durations: inputs.durations } : {}),
    // Capped: a form with hundreds of controls should not post a megabyte.
    // Sorted so the fields that FAILED survive the cap, they are the ones worth
    // turning into a fixture.
    ...(inputs.capture ? { fieldCaptures: rankForCapture(fieldCaptures).slice(0, 120) } : {}),
  };
}
