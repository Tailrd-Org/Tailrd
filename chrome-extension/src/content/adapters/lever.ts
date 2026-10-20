// chrome-extension/src/content/adapters/lever.ts
/**
 * Lever (`*.lever.co`). Lever's standard fields (name/email/phone/country/
 * cover-letter) fill cleanly through the generic pipeline, so this adapter only
 * owns the two places the generic path gets Lever wrong:
 *
 *  1. Current-company / org text field, named `org` / `current-company`,
 *     whose visible label the generic classifier reads fine but whose machine
 *     name is the more reliable signal (kept from the old common-table entry).
 *
 *  2. The "Current location" typeahead (`input[data-qa="location-input"]`).
 *     Typing text into the visible input alone does NOT stick: Lever submits a
 *     hidden sibling `input[name="selectedLocation"]` holding a JSON
 *     `{"name": "<text>"}`, and validates on that. The generic text writer sets
 *     only the visible input, so the value silently reverts ("did not stick").
 *     We mirror Jobright's `handleLocationInput`: set BOTH the visible field and
 *     the hidden `selectedLocation` JSON.
 */
import type { FieldCategory } from "../../shared/types";
import { ADAPTERS } from "./registry";
import { setNativeValue } from "./shared";
import type { AdapterFillResult, FieldContext, FillContext, SiteAdapter } from "./types";
import type { Classification } from "../fieldMatcher";
import { pickPlaceOption } from "../placeMatch";

const LEVER_HOST = /(^|\.)lever\.co$/i;
const ORG_RE = /\borg(anization)?\b|current[_\s-]?(company|employer)/i;

function attrBlob(el: HTMLElement): string {
  return [el.getAttribute("name"), el.id].filter(Boolean).join(" ");
}

/** Set an input through the native setter and fire input/change so Lever's
 *  React state registers it. */
function setInput(el: HTMLInputElement, value: string): void {
  setNativeValue(el, value);
}

const sleep = (ms: number): Promise<void> => new Promise((r) => setTimeout(r, ms));
/** How long Lever's location search may take to list suggestions: a city's
 *  first search answered after more than 5 s (round 7, a plain browser), and
 *  Waabi's Toronto went blank once under the 8 s this was. */
const SUGGESTION_WAIT_MS = 12000;
/** Type like a user: key events around a native-setter write + input event,
 *  which is what Lever's typeahead listens to before it searches. */
function typeLikeUser(input: HTMLInputElement, text: string): void {
  const key = text ? text[text.length - 1] : "Backspace";
  const init: KeyboardEventInit = { key, bubbles: true, cancelable: true, composed: true };
  input.dispatchEvent(new KeyboardEvent("keydown", init));
  setInput(input, text);
  input.dispatchEvent(new InputEvent("input", { bubbles: true, composed: true, data: text, inputType: "insertText" }));
  input.dispatchEvent(new KeyboardEvent("keyup", init));
}

/**
 * The suggestion to pick for `value` ("Toronto, ON, Canada"), matched as a
 * PLACE (see placeMatch.ts): Lever writes "Toronto, ON, CAN".
 */
export function pickLocationSuggestion(suggestions: string[], value: string): number {
  return pickPlaceOption(suggestions, value);
}

/**
 * Fill Lever's "Current location" typeahead the way a user does: type the city,
 * wait for Lever's own suggestion list, click the suggestion that is the
 * applicant's city. Lever then writes BOTH the visible input and the hidden
 * `selectedLocation` it validates on.
 *
 * Writing the text (and a hand-built selectedLocation JSON) without picking a
 * suggestion did not stick on live pages (2026-10-03, three Lever forms): the
 * typeahead clears an unpicked entry. That path remains only as the fallback
 * when no suggestion list appears.
 */
async function fillLeverLocation(input: HTMLInputElement, value: string): Promise<AdapterFillResult> {
  const scope =
    input.closest(".application-question, .application-field, form") ?? input.parentElement ?? document;
  const hidden = scope.querySelector<HTMLInputElement>('input[name="selectedLocation"]');
  const results = scope.querySelector<HTMLElement>(".dropdown-results");
  input.focus({ preventScroll: true });
  if (results) {
    const query = value.split(",")[0].trim();
    typeLikeUser(input, query);
    // Lever's search can take several seconds: 4 s cleared the city on SEP
    // and Wattpad once each (live, 2026-10-08), their lists arriving later.
    for (let waited = 0; waited < SUGGESTION_WAIT_MS; waited += 100) {
      await sleep(100);
      const items = Array.from(results.children).filter((c) => (c.textContent || "").trim()) as HTMLElement[];
      if (items.length === 0) continue;
      const idx = pickLocationSuggestion(items.map((c) => (c.textContent || "").trim()), value);
      if (idx < 0) break; // a list with no confident match: leave it to the user
      const item = items[idx];
      item.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      item.dispatchEvent(new MouseEvent("mouseup", { bubbles: true, cancelable: true }));
      item.click();
      await sleep(150);
      input.blur();
      const picked = !hidden || Boolean(hidden.value);
      return picked && input.value.trim()
        ? { filled: true }
        : { filled: false, reason: "Lever did not accept the location suggestion. Pick it manually." };
    }
    // No (confident) suggestion: undo our query text, the user picks.
    typeLikeUser(input, "");
    input.blur();
    return { filled: false, reason: "Pick your location from Lever's suggestion list." };
  }
  // Older markup without a suggestion list: the visible + hidden write.
  setInput(input, value);
  if (!hidden) {
    input.blur();
    return { filled: false, reason: "Pick your location from Lever's suggestion list." };
  }
  setInput(hidden, JSON.stringify({ name: value }));
  input.blur();
  return { filled: true };
}

export const leverAdapter: SiteAdapter = {
  id: "lever",
  label: "Lever",
  match: (host) => LEVER_HOST.test(host),

  classify(ctx: FieldContext): Classification | undefined {
    const category: FieldCategory = "currentCompany";
    if (ORG_RE.test(attrBlob(ctx.el))) return { category, confidence: 0.95, sensitive: false };
    return undefined;
  },

  fillOperation(ctx: FillContext): Promise<AdapterFillResult> | undefined {
    const el = ctx.el;
    if (el instanceof HTMLInputElement && el.matches('input[data-qa="location-input"]')) {
      return fillLeverLocation(el, ctx.value);
    }
    return undefined;
  },
};

ADAPTERS.push(leverAdapter);
