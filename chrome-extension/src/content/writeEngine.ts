/**
 * Layer B: Write Correctness.
 *
 * Splits a fill into two primitives the reconciler drives independently:
 *   - writeControl(): one attempt to push a value into a live control, using
 *     the native value setter and the exact event order focus → input →
 *     change → blur so React/Vue/Angular register it as real user input.
 *   - verifyControl(): does the live DOM now reflect the intended value?
 *
 * Neither schedules timers or retries, that is Layer C's job. Keeping write
 * and verify separate is what lets the reconciler retry, detect drift and stay
 * idempotent (verify-before-write means an already-correct field is untouched).
 */
import {
  cleanText,
  dispatchCommitKeys,
  dispatchInputEvents,
  isPlaceholderFiller,
  ownLabel,
  setNativeValue,
  slottedQueryAll,
} from "./domUtils";
import type { RuntimeControl } from "./formScanner";
import { matchOption } from "./optionMatch";

export { matchOption };

export interface WriteResult {
  /** True when an attempt was actually made (control is writable and live). */
  written: boolean;
  /** Why no attempt was made, unfillable control type, stale node, no match. */
  reason?: string;
}

const UNFILLABLE = "Control cannot be scripted. Handle manually";
const STALE = "Field was removed. Rescan the page";

function isStale(el: HTMLElement | undefined): boolean {
  return !el || !el.isConnected;
}

// ---------------------------------------------------------------------------
// Write
// ---------------------------------------------------------------------------

export function writeControl(control: RuntimeControl, value: string): WriteResult {
  switch (control.controlType) {
    case "password":
      return writeTextLike(control.el as HTMLInputElement, value);
    case "text":
    case "textarea":
      return writeTextLike(control.el as HTMLInputElement | HTMLTextAreaElement, value);
    case "select":
      return writeSelect(control.el as HTMLSelectElement, value);
    case "checkbox":
      return writeCheckbox(control.el as HTMLInputElement, value);
    case "radioGroup":
      return writeRadioGroup(control.radios ?? [], value);
    case "checkboxGroup":
      return writeCheckboxGroup(control.checkboxes ?? [], value);
    case "contenteditable":
      return writeContentEditable(control.el as HTMLElement, value);
    case "ariaRadioGroup":
      return writeAriaRadioGroup(control.el as HTMLElement, value);
    case "file":
    case "customDropdown":
    case "combobox": // driven asynchronously by comboboxEngine, never here
      return { written: false, reason: UNFILLABLE };
  }
}

/**
 * Fire the lifecycle a framework expects, in the exact order the spec mandates:
 * focus → input → change → blur. The value is set through the native prototype
 * setter between focus and input so handlers reading `el.value` see the new
 * value. .focus()/.blur() are used so document.activeElement is correct too.
 */
const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
const MONTHS_FULL = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Parse a flexible date ("2020-01", "Jan 2020", "01/2020", "1/15/2020", "2020")
 *  into ISO parts, or null. */
function parseFlexibleDate(v: string): { y: string; m: string; d: string } | null {
  const s = v.trim();
  const pad = (n: string): string => n.padStart(2, "0");
  let m: RegExpMatchArray | null;
  if ((m = s.match(/^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?/))) return { y: m[1], m: pad(m[2]), d: pad(m[3] ?? "1") };
  if ((m = s.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/))) return { y: m[3], m: pad(m[1]), d: pad(m[2]) };
  if ((m = s.match(/^(\d{1,2})\/(\d{4})$/))) return { y: m[2], m: pad(m[1]), d: "01" };
  if ((m = s.match(/^([A-Za-z]{3,})\.?\s+(\d{4})/))) {
    const mi = MONTHS.indexOf(m[1].slice(0, 3).toLowerCase());
    if (mi >= 0) return { y: m[2], m: pad(String(mi + 1)), d: "01" };
  }
  if ((m = s.match(/^(\d{4})$/))) return { y: m[1], m: "01", d: "01" };
  return null;
}

/** Native date/month inputs only accept ISO (YYYY-MM-DD / YYYY-MM). Reshape a
 *  flexible profile date to fit; anything else passes through unchanged. */
export function formatForDateInput(el: HTMLElement, value: string): string {
  if (!(el instanceof HTMLInputElement) || (el.type !== "date" && el.type !== "month")) return value;
  const p = parseFlexibleDate(value);
  if (!p) return value;
  return el.type === "month" ? `${p.y}-${p.m}` : `${p.y}-${p.m}-${p.d}`;
}

function writeTextLike(
  el: HTMLInputElement | HTMLTextAreaElement,
  value: string
): WriteResult {
  if (isStale(el)) return { written: false, reason: STALE };
  const v = formatForDateInput(el, value);
  el.focus({ preventScroll: true });
  setNativeValue(el, v);
  dispatchInputEvents(el, v);
  // Enter commits typeaheads / applies input masks / wakes keyup validators.
  // Only for single-line inputs: Enter is a newline in a textarea, and a
  // password field gains nothing (and stays safest untouched by key events).
  if (el instanceof HTMLInputElement && el.type !== "password") dispatchCommitKeys(el);
  el.blur(); // many ATS validate on blur
  return { written: true };
}

function writeSelect(el: HTMLSelectElement, value: string): WriteResult {
  if (isStale(el)) return { written: false, reason: STALE };
  const match = matchSelectOption(el, value);
  if (!match) return { written: false, reason: `No option matches "${truncate(value)}"` };
  el.focus({ preventScroll: true });
  setNativeValue(el, match.value);
  dispatchInputEvents(el);
  el.blur();
  return { written: true };
}

function writeCheckbox(el: HTMLInputElement, value: string): WriteResult {
  if (isStale(el)) return { written: false, reason: STALE };
  const desired = parseDesiredBool(value);
  if (desired === null) return { written: false, reason: "Ambiguous checkbox value" };
  // click() drives the framework's own handlers, safer than setting .checked.
  if (el.checked !== desired) el.click();
  return { written: true };
}

/**
 * The group's radios as the page has them now. A page may re-render the
 * option a click chose: Paylocity replaces the chosen radio's element (live
 * 2026-10-08), so the scanned group lacked the answer it held and the retry
 * reported "No option matches". A named group is read again from the page
 * when any of its radios has left it.
 */
function liveRadios(radios: HTMLInputElement[]): HTMLInputElement[] {
  const live = radios.filter((r) => r.isConnected);
  if (live.length === radios.length) return live;
  const name = radios.find((r) => r.name)?.name;
  if (!name) return live;
  const anchor = live[0];
  const root: ParentNode = anchor?.form ?? (anchor?.getRootNode() as ParentNode | undefined) ?? radios[0].ownerDocument;
  const fresh = Array.from(root.querySelectorAll<HTMLInputElement>('input[type="radio"]')).filter((r) => r.name === name);
  return fresh.length > 0 ? fresh : live;
}

function writeRadioGroup(radios: HTMLInputElement[], value: string): WriteResult {
  const live = liveRadios(radios);
  if (live.length === 0) return { written: false, reason: STALE };
  const match = matchRadio(live, value);
  if (!match) return { written: false, reason: `No option matches "${truncate(value)}"` };
  if (!match.checked) match.click();
  return { written: true };
}

function writeContentEditable(el: HTMLElement, value: string): WriteResult {
  if (isStale(el)) return { written: false, reason: STALE };
  el.focus({ preventScroll: true });
  const doc = el.ownerDocument;
  const selection = doc.getSelection();
  if (selection) {
    selection.selectAllChildren(el);
    const inserted = doc.execCommand("insertText", false, value);
    if (!inserted) {
      el.textContent = value;
      dispatchInputEvents(el, value);
    }
  } else {
    el.textContent = value;
    dispatchInputEvents(el, value);
  }
  el.blur();
  return { written: true };
}

// ---------------------------------------------------------------------------
// Verify: does the live DOM reflect the intended value?
// ---------------------------------------------------------------------------

export function verifyControl(control: RuntimeControl, value: string): boolean {
  switch (control.controlType) {
    case "password": {
      const el = control.el as HTMLInputElement | undefined;
      if (isStale(el)) return false;
      return el!.value === value; // exact, never fuzzy-match a password
    }
    case "text":
    case "textarea": {
      const el = control.el as HTMLInputElement | HTMLTextAreaElement | undefined;
      if (isStale(el)) return false;
      return valueReflects(formatForDateInput(el!, value), el!.value);
    }
    case "select": {
      const el = control.el as HTMLSelectElement | undefined;
      if (isStale(el)) return false;
      const match = matchSelectOption(el!, value);
      return Boolean(match) && el!.value === match!.value;
    }
    case "checkbox": {
      const el = control.el as HTMLInputElement | undefined;
      if (isStale(el)) return false;
      const desired = parseDesiredBool(value);
      return desired !== null && el!.checked === desired;
    }
    case "radioGroup": {
      const live = liveRadios(control.radios ?? []);
      if (live.length === 0) return false;
      const match = matchRadio(live, value);
      return Boolean(match) && match!.checked;
    }
    case "checkboxGroup": {
      const live = (control.checkboxes ?? []).filter((c) => c.isConnected);
      if (live.length === 0) return false;
      const matched = boxesFor(live, value);
      return matched.length > 0 && matched.every((c) => c.checked);
    }
    case "contenteditable": {
      const el = control.el;
      if (isStale(el)) return false;
      return valueReflects(value, cleanText(el!.textContent));
    }
    case "ariaRadioGroup": {
      const group = control.el;
      if (isStale(group)) return false;
      const match = findAriaRadio(group!, value);
      return Boolean(match) && isChosen(match!);
    }
    case "file":
    case "customDropdown":
    case "combobox":
      return false;
  }
}

/**
 * Whether the live string reflects what we wrote. Tolerant of whitespace and
 * framework reformatting (e.g. a phone field that turns "5551234567" into
 * "(555) 123-4567") so the reconciler does not loop reapplying a value the
 * framework legitimately reshaped, but still catches genuine mismatches.
 */
function valueReflects(written: string, current: string): boolean {
  const w = written.trim();
  const c = current.trim();
  if (w === c) return true;
  if (!c) return false;
  const core = (s: string): string => s.toLowerCase().replace(/[^a-z0-9]/g, "");
  const cw = core(w);
  const cc = core(c);
  if (cw !== "" && cw === cc) return true;
  // An international number whose widget keeps the country code in its own
  // picker: "+44 20 7946 0958" shows as "20 7946 0958" (Workable, live
  // 2026-10-03, reported "did not stick"). The rest of the number, whole.
  if (/^\+/.test(w) && /^\d+$/.test(cw) && /^\d{7,}$/.test(cc) && cw.endsWith(cc) && cw.length - cc.length <= 3) return true;
  // The reverse: a widget that adds the country code to a number written
  // without one, "(416) 555-0142" read back as "+14165550142" (Waymo's
  // embedded Greenhouse, live 2026-10-05, reported "did not stick").
  return /^\+/.test(c) && !/^\+/.test(w) && /^\d{7,}$/.test(cw) && /^\d+$/.test(cc) && cc.endsWith(cw) && cc.length - cw.length <= 3;
}

// ---------------------------------------------------------------------------
// Option matching (shared by select + radio, write + verify)
// ---------------------------------------------------------------------------
// matchOption lives in optionMatch.ts (shared with the MAIN-world driver).

function matchSelectOption(el: HTMLSelectElement, value: string): HTMLOptionElement | null {
  const options = Array.from(el.options).filter((o) => !o.disabled);
  // Split-date pickers render Month / Day / Year as three separate <select>s
  // that often share one visual label ("Date of birth"), so the resolver hands
  // the SAME full date to each. Reduce it to the part this select expects before
  // matching, a no-op for every non-date select (returns the value unchanged).
  const target =
    reduceDateForOptions(value, options.map((o) => cleanText(o.textContent))) ?? value;
  return matchOption(options, (o) => cleanText(o.textContent), (o) => o.value, target);
}

type DatePart = "monthName" | "monthNum" | "day" | "year";

/**
 * When `value` is a full date AND this option set is one part of a split date
 * picker, return just that part (as the form expects it); otherwise null so the
 * caller matches the value unchanged. Gated on BOTH conditions so an ordinary
 * numeric or text select is never reinterpreted as a date.
 */
function reduceDateForOptions(value: string, optionTexts: string[]): string | null {
  const p = parseFlexibleDate(value);
  if (!p) return null; // not a date answer, leave every other select untouched
  switch (classifyDatePartOptions(optionTexts)) {
    case "monthName":
      return MONTHS_FULL[Number(p.m) - 1] ?? null;
    case "monthNum":
      return String(Number(p.m));
    case "day":
      return String(Number(p.d));
    case "year":
      return p.y;
    default:
      return null;
  }
}

/**
 * Classify a select's options as a date part, or null. Requires a strong,
 * near-complete signature (all 12 months, days running to 28–31, a run of
 * 4-digit years) so a short numeric or bucketed-range select never trips it.
 */
function classifyDatePartOptions(texts: string[]): DatePart | null {
  const vals = texts.map((t) => t.trim()).filter((t) => t && !isPlaceholderFiller(t));
  if (vals.length < 4) return null;
  const monthHits = vals.filter((t) => MONTHS.includes(t.slice(0, 3).toLowerCase())).length;
  if (monthHits >= 12) return "monthName";
  // Every remaining candidate must be a bare integer, or this isn't a date part.
  if (!vals.every((t) => /^\d{1,4}$/.test(t))) return null;
  const nums = vals.map(Number);
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  if (vals.every((t) => /^\d{4}$/.test(t)) && min >= 1900 && max <= 2100) return "year";
  if (min === 1 && max >= 28 && max <= 31) return "day";
  if (min === 1 && max === 12 && vals.length >= 12) return "monthNum";
  return null;
}

function matchRadio(radios: HTMLInputElement[], value: string): HTMLInputElement | null {
  const labelOf = (r: HTMLInputElement): string =>
    cleanText(ownLabel(r)?.textContent) || r.value;
  return matchOption(radios, labelOf, (r) => r.value, value);
}

// Native checkbox groups ("select all that apply"), a multi-select answer may
// name one or more options; check each matching box (additive, never unchecks).
function answerParts(value: string): string[] {
  const parts = value.split(/[,;\n]+/).map((s) => s.trim()).filter(Boolean);
  return parts.length > 0 ? parts : [value.trim()].filter(Boolean);
}

function matchCheckbox(boxes: HTMLInputElement[], value: string): HTMLInputElement | null {
  const labelOf = (c: HTMLInputElement): string => cleanText(ownLabel(c)?.textContent) || c.value;
  return matchOption(boxes, labelOf, (c) => c.value, value);
}

/**
 * The boxes an answer names. An answer that IS one box's label is that box:
 * its commas are the label's own ("No, I do not have a disability and have
 * not had one in the past"). Only an answer no label carries whole is a list.
 */
function boxesFor(live: HTMLInputElement[], value: string): HTMLInputElement[] {
  const key = (s: string): string => s.toLowerCase().replace(/\s+/g, " ").trim();
  const whole = live.find((c) => key(cleanText(ownLabel(c)?.textContent) || c.value) === key(value));
  if (whole) return [whole];
  const out: HTMLInputElement[] = [];
  for (const part of answerParts(value)) {
    const match = matchCheckbox(live, part);
    if (match && !out.includes(match)) out.push(match);
  }
  return out;
}

function writeCheckboxGroup(checkboxes: HTMLInputElement[], value: string): WriteResult {
  const live = checkboxes.filter((c) => c.isConnected);
  if (live.length === 0) return { written: false, reason: STALE };
  const boxes = boxesFor(live, value);
  if (boxes.length === 0) return { written: false, reason: `No option matches "${truncate(value)}"` };
  for (const box of boxes) if (!box.checked) box.click();
  return { written: true };
}

// ARIA radio groups (role=radiogroup with role=radio divs), selected by clicking
// the matching radio; the framework flips its aria-checked. Ashby's Yes/No is
// the same group made of buttons that flip aria-pressed instead.
function ariaRadiosOf(group: HTMLElement): HTMLElement[] {
  // Slotted radios too (SmartRecruiters' screening step, round 7).
  const radios = slottedQueryAll(group, '[role="radio"]');
  return (radios.length ? radios : Array.from(group.querySelectorAll<HTMLElement>("button[aria-pressed]"))).filter(
    (r) => r.getAttribute("aria-disabled") !== "true" && !(r as HTMLButtonElement).disabled
  );
}

function isChosen(option: HTMLElement): boolean {
  return option.getAttribute("aria-checked") === "true" || option.getAttribute("aria-pressed") === "true";
}

function findAriaRadio(group: HTMLElement, value: string): HTMLElement | null {
  return matchOption(
    ariaRadiosOf(group),
    (r) => cleanText(r.getAttribute("aria-label")) || cleanText(r.getAttribute("label")) || cleanText(r.textContent),
    (r) => r.getAttribute("data-value") ?? r.getAttribute("value") ?? "",
    value
  );
}

function writeAriaRadioGroup(group: HTMLElement, value: string): WriteResult {
  if (isStale(group)) return { written: false, reason: STALE };
  const match = findAriaRadio(group, value);
  if (!match) return { written: false, reason: `No option matches "${truncate(value)}"` };
  if (!isChosen(match)) match.click();
  return { written: true };
}

function parseDesiredBool(value: string): boolean | null {
  if (/^(yes|y|true|1|agree|checked)$/i.test(value.trim())) return true;
  if (/^(no|n|false|0|unchecked)$/i.test(value.trim())) return false;
  return null;
}

function truncate(s: string, max = 40): string {
  return s.length > max ? `${s.slice(0, max)}…` : s;
}
